"""
Toil pipeline to align all transcripts in a source genePred to a target genePred. For non-CGP transcripts,
this can be determined by using tools.nameConversions.strip_alignment_numbers() on the name field. For CGP transcripts,
which have new IDs, we use the name2 field which will have assigned a gene ID to try and align to all protein coding
transcripts associated with that gene ID.

Alignment is only performed on protein coding transcripts. For CGP, the in-frame CDS will be aligned using BLAT to the
in-frame CDS of each protein coding transcript of the assigned parental gene. For transMap and AugustusTM(R)
transcripts, two alignments we be performed - full mRNA and in-frame CDS.
"""
import argparse
import collections
import itertools
import logging

from toil.common import Toil
from toil.job import Job

import tools.bio
import tools.dataOps
import tools.fileOps
import tools.nameConversions
import tools.pipeline
import tools.procOps
import tools.psl
import tools.sqlInterface
import tools.toilInterface
import tools.transcripts


def align_transcripts(args, toil_options):
    """
    Main entry function for transcript alignment toil pipeline
    :param args: dictionary of arguments from CAT
    :param toil_options: toil options Namespace object
    """
    with Toil(toil_options) as toil:
        if not toil.options.restart:
            input_file_ids = argparse.Namespace()
            input_file_ids.ref_genome_fasta = tools.toilInterface.write_fasta_to_filestore(toil, args.ref_genome_fasta)
            input_file_ids.genome_fasta = tools.toilInterface.write_fasta_to_filestore(toil, args.genome_fasta)
            input_file_ids.annotation_gp = toil.importFile('file://' + args.annotation_gp)
            input_file_ids.ref_db = toil.importFile('file://' + args.ref_db_path)
            input_file_ids.modes = {}
            for mode in args.transcript_modes:
                input_file_ids.modes[mode] = toil.importFile('file://' + args.transcript_modes[mode]['gp'])
            job = Job.wrapJobFn(setup, args, input_file_ids, memory='16G')
            results_file_ids = toil.start(job)
        else:
            results_file_ids = toil.restart()
        for file_path, file_id in results_file_ids.iteritems():
            tools.fileOps.ensure_file_dir(file_path)
            toil.exportFile(file_id, 'file://' + file_path)


def setup(job, args, input_file_ids):
    """
    First function for align_transcripts pipeline. Splits up the genePred entries into chunks that will be aligned
    with BLAT.
    :param args: dictionary of arguments from CAT
    :param input_file_ids: dictionary of fileStore file IDs for the inputs to this pipeline
    """
    job.fileStore.logToMaster('Beginning Align Transcripts run on {}'.format(args.genome), level=logging.INFO)
    # load all fileStore files necessary
    annotation_gp = job.fileStore.readGlobalFile(input_file_ids.annotation_gp)
    ref_genome_db = job.fileStore.readGlobalFile(input_file_ids.ref_db)
    genome_fasta = tools.toilInterface.load_fasta_from_filestore(job, input_file_ids.genome_fasta,
                                                                 prefix='genome', upper=False)
    ref_genome_fasta = tools.toilInterface.load_fasta_from_filestore(job, input_file_ids.ref_genome_fasta,
                                                                     prefix='ref_genome', upper=False)
    # load required reference data into memory
    tx_biotype_map = tools.sqlInterface.get_transcript_biotype_map(ref_genome_db)
    ref_transcript_dict = tools.transcripts.get_gene_pred_dict(annotation_gp)
    # will hold a mapping of output file paths to lists of Promise objects containing output
    results = collections.defaultdict(list)
    # start generating chunks of the transMap/Augustus genePreds which we know the 1-1 alignment for
    for tx_mode in ['transMap', 'augTM', 'augTMR']:
        if tx_mode not in args.transcript_modes:
            continue
        # output file paths
        mrna_path = args.transcript_modes[tx_mode]['mRNA']
        cds_path = args.transcript_modes[tx_mode]['CDS']
        # begin loading transcripts and sequences
        gp_path = job.fileStore.readGlobalFile(input_file_ids.modes[tx_mode])
        transcript_dict = tools.transcripts.get_gene_pred_dict(gp_path)
        transcript_dict = {aln_id: tx for aln_id, tx in transcript_dict.iteritems() if
                           tx_biotype_map[tools.nameConversions.strip_alignment_numbers(aln_id)] == 'protein_coding'}
        for aln_mode, out_path in zip(*[['mRNA', 'CDS'], [mrna_path, cds_path]]):
            seq_iter = get_alignment_sequences(transcript_dict, ref_transcript_dict, genome_fasta,
                                               ref_genome_fasta, aln_mode)
            for chunk in group_transcripts(seq_iter):
                j = job.addChildJobFn(run_blat_chunk, chunk, aln_mode)
                results[out_path].append(j.rv())

    # if we ran AugustusCGP, align those CDS sequences
    if 'augCGP' in args.transcript_modes:
        cgp_cds_path = args.transcript_modes['augCGP']['CDS']
        # CGP transcripts have multiple assignments based on the name2 identifier, which contains a gene ID
        gene_tx_map = tools.sqlInterface.get_gene_transcript_map(ref_genome_db)
        tx_biotype_map = tools.sqlInterface.get_transcript_biotype_map(ref_genome_db)
        augustus_cgp_gp = job.fileStore.readGlobalFile(input_file_ids.modes['augCGP'])
        cgp_transcript_dict = tools.transcripts.get_gene_pred_dict(augustus_cgp_gp)
        cgp_transcript_seq_iter = get_cgp_sequences(cgp_transcript_dict, ref_transcript_dict, genome_fasta,
                                                    ref_genome_fasta, gene_tx_map, tx_biotype_map)
        for chunk in group_transcripts(cgp_transcript_seq_iter):
            j = job.addChildJobFn(run_blat_chunk, chunk, 'CDS')
            results[cgp_cds_path].append(j.rv())
    if len(results) == 0:
        err_msg = 'Align Transcripts pipeline did not detect any input genePreds for {}'.format(args.genome)
        raise RuntimeError(err_msg)
    # convert the results Promises into resolved values
    return job.addFollowOnJobFn(merge, results, args).rv()


def get_alignment_sequences(transcript_dict, ref_transcript_dict, genome_fasta, ref_genome_fasta, mode):
    """Generator that yields a tuple of (tx_id, tx_seq, ref_tx_id, ref_tx_seq)"""
    assert mode in ['mRNA', 'CDS']
    for tx_id, tx in transcript_dict.iteritems():
        ref_tx_id = tools.nameConversions.strip_alignment_numbers(tx_id)
        ref_tx = ref_transcript_dict[ref_tx_id]
        tx_seq = tx.get_mrna(genome_fasta) if mode == 'mRNA' else tx.get_cds(genome_fasta, in_frame=True)
        ref_tx_seq = ref_tx.get_mrna(ref_genome_fasta) if mode == 'mRNA' else ref_tx.get_cds(ref_genome_fasta,
                                                                                             in_frame=True)
        if len(ref_tx_seq) > 50 and len(tx_seq) > 50:
            yield tx_id, tx_seq, ref_tx_id, ref_tx_seq


def get_cgp_sequences(transcript_dict, ref_transcript_dict, genome_fasta, ref_genome_fasta, gene_tx_map,
                      tx_biotype_map):
    """
    Generator for CGP transcripts. Same as get_alignment_sequences, but will resolve name2 field into all target
    transcripts
    """
    for cgp_id, tx in transcript_dict.iteritems():
        if 'jg' in tx.name2:
            continue  # this transcript was not assigned any parents
        ref_tx_ids = gene_tx_map[tx.name2]
        tx_seq = tx.get_cds(genome_fasta, in_frame=True)
        assert len(tx_seq) % 3 == 0, cgp_id
        for ref_tx_id in ref_tx_ids:
            biotype = tx_biotype_map[ref_tx_id]
            if biotype != 'protein_coding':
                continue
            ref_tx = ref_transcript_dict[ref_tx_id]
            ref_tx_seq = ref_tx.get_cds(ref_genome_fasta, in_frame=True)
            assert len(ref_tx_seq) % 3 == 0, ref_tx_id
            if len(ref_tx_seq) > 50 and len(tx_seq) > 50:
                yield cgp_id, tx_seq, ref_tx_id, ref_tx_seq


def run_blat_chunk(job, chunk, mode):
    """
    Runs an alignment chunk through BLAT for either coding or non-coding transcripts
    :param chunk: List of (tx_id, tx_seq, ref_tx_id, ref_tx_seq) tuples
    :param mode: One of ['mRNA', 'CDS']. Determines what mode of alignment we will perform.
    :return: List of PSL output
    """
    def parse_blat(tmp_psl):
        # filter for only + alignments, as we are expecting to be on the same strand
        # translation alignments have explicit strand, and we only want ++
        filter_strand = '+' if mode == 'mRNA' else '++'
        psls = [psl for psl in tools.psl.psl_iterator(tmp_psl) if psl.strand == filter_strand]
        if len(psls) == 0:
            return None
        longest = sorted(psls, key=lambda p: -p.coverage)[0]
        return '\t'.join(longest.psl_string())

    assert mode in ['mRNA', 'CDS']
    tmp_ref = tools.fileOps.get_tmp_toil_file()
    tmp_tgt = tools.fileOps.get_tmp_toil_file()
    tmp_psl = tools.fileOps.get_tmp_toil_file()
    tmp_filtered_psl = tools.fileOps.get_tmp_toil_file()
    results = []
    if mode == 'mRNA':
        cmd = ['blat', '-noHead', '-minIdentity=0', tmp_ref, tmp_tgt, tmp_psl]
    else:  # mode == CDS. Filter these for problematic alignments that happen in edge cases
        cmd = ['blat', '-t=dnax', '-q=rnax', '-noHead', '-minIdentity=0', tmp_ref, tmp_tgt, tmp_psl]
    for tx_id, tx_seq, ref_tx_id, ref_tx_seq in chunk:
        with open(tmp_ref, 'w') as tmp_ref_h:
            tools.bio.write_fasta(tmp_ref_h, ref_tx_id, ref_tx_seq)
        with open(tmp_tgt, 'w') as tmp_tgt_h:
            tools.bio.write_fasta(tmp_tgt_h, tx_id, tx_seq)
        tools.procOps.run_proc(cmd)
        try:
            tools.procOps.run_proc(['pslCheck', '-quiet', tmp_psl, '-pass={}'.format(tmp_filtered_psl)])
        except tools.pipeline.ProcException:
            pass
        results.append(parse_blat(tmp_filtered_psl))
    return results


def merge(job, results, args):
    """
    Merge together chain files.
    :param results: dict of list of promises from each alignment chunk for each category
    :param args: arguments to the pipeline
    :return:
    """
    job.fileStore.logToMaster('Merging Alignment output for {}'.format(args.genome), level=logging.INFO)
    results_file_ids = {}
    for gp_category, result_list in results.iteritems():
        tmp_results_file = tools.fileOps.get_tmp_toil_file()
        with tools.fileOps.opengz(tmp_results_file, 'w') as outf:
            for line in itertools.chain.from_iterable(result_list):  # results is list of lists
                if line is not None:
                    outf.write(line + '\n')
        results_file_ids[gp_category] = job.fileStore.writeGlobalFile(tmp_results_file)
    return results_file_ids


###
# Helper functions
###


def group_transcripts(tx_iter, num_bases=10 ** 6, max_seqs=1000):
    """
    Group up transcripts by num_bases, unless that exceeds max_seqs. A greedy implementation of the bin packing problem.
    Helps speed up the execution of BLAT when faced with very large genes
    """
    tx_id, tx_seq, ref_tx_id, ref_tx_seq = tx_iter.next()
    this_bin = [(tx_id, tx_seq, ref_tx_id, ref_tx_seq)]
    bin_base_count = len(tx_seq)
    num_seqs = 1
    for tx_id, tx_seq, ref_tx_id, ref_tx_seq in tx_iter:
        bin_base_count += len(tx_seq)
        num_seqs += 1
        if bin_base_count >= num_bases or num_seqs >= max_seqs:
            yield this_bin
            this_bin = [(tx_id, tx_seq, ref_tx_id, ref_tx_seq)]
            bin_base_count = len(tx_seq)
            num_seqs = 1
        else:
            this_bin.append((tx_id, tx_seq, ref_tx_id, ref_tx_seq))
    yield this_bin
