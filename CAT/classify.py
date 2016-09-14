"""
A series of classifiers that evaluate transMap, AugustusTMR and AugustusCGP output.

These classifiers are broken down into 3 groups, which will each end up as a table in the database:

Alignment:

These classifiers apply only to the transMap alignments, and measure how well we mapped over this region:
1. Paralogy: The # of times this transcript was aligned
2. AlnExtendsOffConfig: Does this alignment run off the end of a contig?
3. AlignmentPartialMap: Did this transcript not map completely?
4. AlnAbutsUnknownBases: Does this alignment have Ns immediately touching any exons?
5. AlnContainsUnknownBases: Are there any Ns within the transcript alignment?
6. LongAlignment: Did this transcript align in a insanely long fashion? Indicative of paralogy problems.
7. Synteny: If this transcript aligned more than once, assign a boolean based on synteny to whether this is the
    most probable transcript. This is used to filter for pseudogenes.

<alnMode>_<txMode>_Metrics:

These classifiers are per-transcript evaluations based on both the transcript alignment and the genome context.
1. PercentUnknownBases: % of mRNA bases that are Ns.
2. AlnCoverage: Alignment coverage in transcript space.
3. AlnIdentity: Alignment identity in transcript space.
4. Badness: A measure of how bad the alignment is related to Jim Kent's badness score.
5. NumMissingIntrons: Number of original introns not within a wiggle distance of any introns in the target.
6. NumMissingExons: Do we lose any exons? Defined based on parent sequence, with wiggle room.
7. CdsStartStat: Is the CDS likely to be a complete start? Simply extracted from the genePred
8. CdsEndStat: Is the CDS likely to be a complete stop? Simply extracted from the genePred

<alnMode>_<txMode>_Evaluation:

These classifiers are per-transcript evaluations based on the transcript alignment.
Unlike the other two tables, this table stores the actual location of the problems (in genome coordinates) as a
BED-like format. In cases where there are multiple problems, they will be additional rows.
1. CodingInsertion: Do we have any frame-shifting coding insertions?
2. CodingDeletion: Do we have any frame-shifting coding deletions?
3. CodingMult3Insertion: Do we have any mod3 coding insertions?
4. CodingMult3Deletion: Do we have any mod3 coding deletions?
5. NonCodingInsertion: Do we have indels in UTR sequence?
6. NonCodingDeletion: Do we have any indels in UTR sequence?
7. ExonGain: Do we gain any exons? Defined as having a continuous block of sequence with no alignment that is spliced.
8. InFrameStop: Are there any in-frame stop codons?


The Metrics and Evaluation groups will have multiple tables for each of the input methods used:
txMode:
1) transMap
2) augTM
3) augTMR
4) augCGP

alnMode:
1) CDS
2) mRNA

"""
import argparse
import operator
import logging
import bisect
import itertools
import collections
import pandas as pd
import tools.transcripts
import tools.intervals
import tools.psl
import tools.fileOps
import tools.dataOps
import tools.bio
import tools.sqlInterface
import tools.nameConversions
import tools.mathOps
import tools.toilInterface

from toil.job import Job
from toil.common import Toil


# hard coded long transMap size. Bigger than 3 megabases is probably a spurious alignment.
long_tx_size = 3 * 10 ** 6


def classify(args, toil_options):
    """
    Entry point for Transcript Classification Toil pipeline.
    """
    with Toil(toil_options) as toil:
        if not toil.options.restart:
            fasta_file_ids = tools.toilInterface.write_fasta_to_filestore(toil, args.genome_fasta)
            input_file_ids = argparse.Namespace()
            input_file_ids.fasta = fasta_file_ids
            input_file_ids.tm_psl = toil.importFile('file://' + args.tm_psl)
            input_file_ids.tm_gp = toil.importFile('file://' + args.tm_gp)
            input_file_ids.annotation_gp = toil.importFile('file://' + args.annotation_gp)
            input_file_ids.annotation_db = toil.importFile('file://' + args.annotation_db)
            input_file_ids.modes = {}  # modes will hold the file IDs broken down by mode
            input_file_ids.gps = {}  # gps will hold input genePred file IDs
            for tx_mode, path_dict in args.alignment_modes.iteritems():
                aln_file_ids = {}
                gp_file_id = toil.importFile('file://' + path_dict['gp'])
                for aln_mode in ['mRNA', 'CDS']:
                    if aln_mode in path_dict:
                        aln_file_ids[aln_mode] = toil.importFile('file://' + path_dict[aln_mode])
                input_file_ids.modes[tx_mode] = aln_file_ids
                input_file_ids.gps[tx_mode] = gp_file_id
            job = Job.wrapJobFn(setup_classify, args, input_file_ids)
            results = toil.start(job)
        else:
            results = toil.restart()
    return dict(itertools.chain.from_iterable(results))


def setup_classify(job, args, input_file_ids):
    """
    Splits the pipeline into three sections - aln_classify, metrics_classify, and evaluation_classify
    :param args: Configuration dictionary passed in by Luigi.
    :param input_file_ids: Dictionary of fileStore IDs
    :return: tuples of (tablename: pandas DataFrame)
    """
    job.fileStore.logToMaster('Beginning Transcript Evaluation run on {}'.format(args.genome), level=logging.INFO)
    aln_df = job.addChildJobFn(aln_classify, args, input_file_ids, memory='8G').rv()
    # nested inner list to deal with paired tables coming out of metrics_evaluation_classify
    dfs = [[('Alignment', aln_df)]]
    for tx_mode, aln_file_ids in input_file_ids.modes.iteritems():
        for aln_mode, tx_aln_psl_file_id in aln_file_ids.iteritems():
            gp_file_id = input_file_ids.gps[tx_mode]
            mc_job = job.addChildJobFn(metrics_evaluation_classify, tx_mode, aln_mode, gp_file_id, tx_aln_psl_file_id,
                                       input_file_ids, args)
            dfs.append(mc_job.rv())
    return dfs


def aln_classify(job, args, input_file_ids):
    """
    Runs alignment classification based on transMap PSLs, genePreds and the genome FASTA.
    :param args: Configuration dictionary passed in by Luigi.
    :param input_file_ids: Dictionary of fileStore IDs
    :return: DataFrame
    """
    job.fileStore.logToMaster('Beginning Alignment Evaluation run on {}'.format(args.genome), level=logging.INFO)
    psl_dict = tools.psl.get_alignment_dict(job.fileStore.readGlobalFile(input_file_ids.tm_psl))
    gp_dict = tools.transcripts.get_gene_pred_dict(job.fileStore.readGlobalFile(input_file_ids.tm_gp))
    ref_gp_dict = tools.transcripts.get_gene_pred_dict(job.fileStore.readGlobalFile(input_file_ids.annotation_gp))
    fasta = tools.toilInterface.load_fasta_from_filestore(job, input_file_ids.fasta)
    r = []
    paralog_count = paralogy(psl_dict)  # we have to count paralogs globally
    synteny_scores = synteny(ref_gp_dict, gp_dict)  # we also have to count synteny globally
    for aln_id, tx in gp_dict.iteritems():
        aln = psl_dict[aln_id]
        r.append([aln_id, 'Paralogy', paralog_count[aln_id]])
        r.append([aln_id, 'Synteny', synteny_scores[aln_id]])
        r.append([aln_id, 'AlnExtendsoffContig', aln_extends_off_contig(aln)])
        r.append([aln_id, 'AlnPartialMap', alignment_partial_map(aln)])
        r.append([aln_id, 'AlnAbutsUnknownBases', aln_abuts_unknown_bases(tx, fasta)])
        r.append([aln_id, 'AlnContainsUnknownBases', aln_contains_unknown_bases(tx, fasta)])
        r.append([aln_id, 'LongTranscript', long_transcript(tx)])
    df = pd.DataFrame(r, columns=['TransMapId', 'Classifier', 'Value'])
    df.set_index(['TransMapId', 'Classifier'], inplace=True)
    return df


def metrics_evaluation_classify(job, tx_mode, aln_mode, gp_file_id, tx_aln_psl_file_id, input_file_ids, args,
                                chunk_size=500):
    """
    Entry point for both metrics and alignment evaluation processes.

    This job parses:
    1) The alignment record PSL into PslRow objects,
    2) The reference genePred into GenePredTranscript objects,
    3) The target genePred into GenePredTranscript objects, and
    4) The annotation SQLite database to determine biotypes.

    These are then grouped into chunks and new jobs are created to calculate the classifications.

    A final combination job combines the results into the output dataframe.

    :param tx_mode: Transcript type we are evaluating. Used to build the table name
    :param aln_mode: Alignment mode. One of mRNA, CDS. Used to build the table name and determine if we are going to
    work on CDS or mRNA sequences.
    :param gp_file_id: genePred file ID for this mode
    :param tx_aln_psl_file_id: Alignment PSL file id for this mode
    :param input_file_ids: Dictionary of fileStore IDs
    :param args: Configuration dictionary passed in by Luigi.
    :param chunk_size: The number of transcripts each job will process.
    :return: two tuples of (tablename, dataframe)
    """
    def tx_iter():
        """
        yields tuples of (GenePredTranscript <reference> , GenePredTranscript <target>, PslRow, biotype
        """
        for (ref_name, target_name), psl in pslord_dict.iteritems():
            ref_tx = ref_tx_dict[ref_name]
            tx = tx_dict[target_name]
            biotype = tx_biotype_map[ref_name]
            yield ref_tx, tx, psl, biotype, aln_mode

    # load files
    tx_aln_psl = job.fileStore.readGlobalFile(tx_aln_psl_file_id)
    ref_gp = job.fileStore.readGlobalFile(input_file_ids.annotation_gp)
    annotation_db = job.fileStore.readGlobalFile(input_file_ids.annotation_db)
    gp = job.fileStore.readGlobalFile(gp_file_id)

    # parse files
    pslord_dict = tools.psl.get_alignment_dict(tx_aln_psl)
    tx_dict = tools.transcripts.get_gene_pred_dict(gp)
    ref_tx_dict = tools.transcripts.get_gene_pred_dict(ref_gp)

    # load transcript biotype map
    tx_biotype_map = tools.sqlInterface.get_transcript_biotype_map(annotation_db, args.ref_genome)

    # start the classification process
    # mc_r/ec_r will contain uncollapsed result Promises that will be resolved before merge_metrics_results
    mc_r = []
    ec_r = []
    for transcript_chunk in tools.dataOps.grouper(tx_iter(), chunk_size):
        mc_j = job.addChildJobFn(calculate_metrics, transcript_chunk)
        mc_r.append(mc_j.rv())
        ec_j = job.addChildJobFn(calculate_evaluations, transcript_chunk)
        ec_r.append(ec_j.rv())

    # start merging the results into dataframes
    # these are the DataFrame column names for the respective tables
    mc_columns = ['TranscriptId', 'classifier', 'value']
    ec_columns = ['TranscriptId', 'classifier', 'chromosome', 'start', 'stop', 'strand']
    # these are the sqlite table names
    mc_table_name = '_'.join([aln_mode, tx_mode, 'Metrics'])
    ec_table_name = '_'.join([aln_mode, tx_mode, 'Evaluation'])

    ec_df = job.addFollowOnJobFn(merge_results, ec_r, ec_columns).rv()
    mc_df = job.addFollowOnJobFn(merge_results, mc_r, mc_columns).rv()

    return (mc_table_name, mc_df), (ec_table_name, ec_df)


def calculate_metrics(job, transcript_chunk):
    """
    Calculates the alignment metrics and the number of missing original introns on this transcript_chunk
    :param transcript_chunk: tuples of ref_tx, tx, psl, biotype, aln_mode
    :return: list of (aln_id, classifier, result) tuples
    """
    r = []
    for ref_tx, tx, psl, biotype, aln_mode in transcript_chunk:
        if biotype == 'protein_coding':
            r.append([tx.name, 'CdsStartStat', tx.cds_start_stat])
            r.append([tx.name, 'CdsEndStat', tx.cds_end_stat])
        num_missing_introns = calculate_num_missing_introns(ref_tx, tx, psl, aln_mode)
        num_missing_exons = calculate_num_missing_exons(ref_tx, psl, aln_mode)
        r.append([tx.name, 'AlnCoverage', psl.coverage])
        r.append([tx.name, 'AlnIdentity', psl.identity])
        r.append([tx.name, 'Badness', psl.badness])
        r.append([tx.name, 'PercentUnknownBases', psl.percent_n])
        r.append([tx.name, 'NumMissingIntrons', num_missing_introns])
        r.append([tx.name, 'NumMissinglExons', num_missing_exons])
    return r


def calculate_evaluations(job, transcript_chunk):
    """
    Calculates the evaluation metrics on this transcript_chunk
    :param transcript_chunk: tuples of ref_tx, tx, psl, biotype, aln_mode
    :return: list of (aln_id, classifier, chromosome, start, stop, strand) tuples
    """
    r = []
    for ref_tx, tx, psl, biotype, aln_mode in transcript_chunk:
        for exon in exon_gain(tx, psl, aln_mode):
            r.append([tx.name, 'ExonGain', exon])
        indels = find_indels(tx, psl, aln_mode)
        for category, interval in indels:
            r.append([tx.name, category, interval])
        if biotype == 'protein_coding' and tx.cds_size > 50:  # we don't want to evalaute tiny ORFs
            ifs = in_frame_stop(tx, psl, aln_mode)
            if ifs is not None:
                r.append([tx.name, 'InFrameStop', ifs])
    # convert all of the ChromosomeInterval objects into a column representation
    return [[name, cat, i.chromosome, i.start, i.stop, i.strand] for name, cat, i in r]


def merge_results(job, r, columns):
    """
    Combines the output of calculate_metrics() or calculate_evaluations() into a DataFrame
    :param r: List of lists of results that needs to be flattened and converted
    :param columns: List of column names to use
    :return: DataFrame
    """
    d = list(itertools.chain.from_iterable(r))
    df = pd.DataFrame(d, columns=columns)
    df.sort_values(columns, inplace=True)
    df.set_index(['TranscriptId', 'classifier'], inplace=True)
    return df


###
# Alignment Classifiers
###


def paralogy(psl_dict):
    """
    Count the number of occurrences of each parental annotation in the target genome
    :param psl_dict: PslDict from psl module of transMap alignments
    :return: collections.Counter
    """
    r = collections.Counter()
    for aln_id in psl_dict:
        r[tools.nameConversions.strip_alignment_numbers(aln_id)] += 1
    return r


def aln_extends_off_contig(aln):
    """
    Does the alignment extend off of a contig or scaffold?
    aligned: #  unaligned: -  whatever: .  edge: |
             query  |---#####....
             target    |#####....
    OR
    aligned: #  unaligned: -  whatever: .  edge: |
             query  ...######---|
             target ...######|

    :param aln: PslRow object
    :return: boolean
    """
    if aln.t_start == 0 and aln.q_start != 0 or aln.t_end == aln.t_size and aln.q_end != aln.q_size:
        return True
    else:
        return False


def alignment_partial_map(aln):
    """
    Does the query sequence not map entirely?

    a.q_size != a.q_end - a.q_start

    :param aln: PslRow object
    :return: boolean
    """
    return True if aln.q_size != aln.q_end - aln.q_start else False


def aln_abuts_unknown_bases(tx, fasta):
    """
    Do any exons in this alignment immediately touch Ns?

    :param tx: a GenePredTranscript object
    :param fasta: pyfasta Fasta object for genome
    :return: boolean
    """
    chrom = tx.chromosome
    for exon in tx.exon_intervals:
        if exon.start == 0:  # we are at the edge of the contig
            left_base = None
        else:
            left_base = fasta[chrom][exon.start - 1]
        if exon.stop >= len(fasta[chrom]):  # we are at the edge of the contig
            right_base = None
        else:
            right_base = fasta[chrom][exon.stop]
        if left_base == 'N' or right_base == 'N':
            return True
    return False


def aln_contains_unknown_bases(tx, fasta):
    """
    Does this alignment contain unknown bases (Ns)?

    :param tx: a GenePredTranscript object
    :param fasta: pyfasta Fasta object for genome
    :return: boolean
    """
    return 'N' not in tx.get_mrna(fasta)


def long_transcript(tx):
    """
    Is this transcript greater in genomic length than long_tx_size?

    :param tx: a GenePredTranscript object
    :return: boolean
    """
    return True if tx.start - tx.stop >= long_tx_size else False


def synteny(ref_gp_dict, gp_dict):
    """
    Attempts to evaluate the synteny of these transcripts. For each transcript, compares the 5 genes up and down stream
    in the reference genome and counts how many match the transMap results.
    :param ref_gp_dict: Dictionary of GenePredTranscript objects from the reference annotation
    :param gp_dict: Dictionary of GenePredTranscript objects from the transMap output
    :return:
    """
    def create_interval_dict(tx_dict):
        """
        Creates a dict mapping chromosome sequences to gene intervals [chrom][gene_id]: [list of tx intervals]
        Skips huge intervals to avoid mapping issues
        """
        interval_dict = collections.defaultdict(lambda: collections.defaultdict(list))
        for tx in tx_dict.itervalues():
            if len(tx.interval) < long_tx_size:
                interval_dict[tx.chromosome][tx.name2].append(tx.interval)
        return interval_dict

    def merge_interval_dict(interval_dict):
        """Merges the above intervals into the one genic interval."""
        merged_interval_dict = collections.defaultdict(dict)
        for chrom in interval_dict:
            for gene_id, gene_intervals in interval_dict[chrom].iteritems():
                merged_intervals = tools.intervals.gap_merge_intervals(gene_intervals, float('inf'))
                assert len(merged_intervals) == 1
                merged_interval = merged_intervals[0]
                if len(merged_interval) >= long_tx_size:
                    continue
                merged_interval.data = gene_id
                merged_interval_dict[chrom][gene_id] = merged_interval
        return merged_interval_dict

    def sort_interval_dict(merged_interval_dict):
        """Sorts the dict produced by create_interval_dict so that we can do list bisection"""
        sorted_interval_dict = {}
        for chrom in merged_interval_dict:
            sorted_interval_dict[chrom] = sorted(merged_interval_dict[chrom].itervalues())
        return sorted_interval_dict

    def make_ref_interval_map(ref_intervals):
        """Creates a dictionary mapping reference intervals to their name"""
        ref_interval_map = {}
        for interval_list in ref_intervals.itervalues():
            for interval in interval_list:
                assert interval.data not in ref_interval_map
                ref_interval_map[interval.data] = interval
        return ref_interval_map

    # create dictionaries mapping chromosome names to all genic intervals present on the chromosome
    tm_chrom_intervals = sort_interval_dict(merge_interval_dict(create_interval_dict(gp_dict)))
    ref_chrom_intervals = sort_interval_dict(merge_interval_dict(create_interval_dict(ref_gp_dict)))

    # convert the reference to a map that is per-name so that we know where to look
    ref_interval_map = make_ref_interval_map(ref_chrom_intervals)

    # synteny score algorithm
    scores = {}
    for tx in gp_dict.itervalues():
        # find the genes from -5 to +5 in the target genome
        target_intervals = tm_chrom_intervals[tx.chromosome]
        target_position = bisect.bisect_left(target_intervals, tx.interval)
        target_genes = {x.data for x in target_intervals[target_position - 5: target_position + 5]}
        # find the same gene list in the reference genome
        ref_interval = ref_interval_map[tx.name2]
        ref_intervals = ref_chrom_intervals[ref_interval.chromosome]
        ref_position = bisect.bisect_left(ref_intervals, ref_interval)
        reference_genes = {x.data for x in ref_intervals[ref_position - 5: ref_position + 5]}
        scores[tx.name] = len(reference_genes & target_genes)
    return scores

###
# Metrics Classifiers
###


def calculate_num_missing_introns(ref_tx, tx, psl, aln_mode, wiggle_distance=7):
    """
    Determines how many of the gaps present in a given transcript are within a wiggle distance of the parent.

    Algorithm:
    1) Convert the coordinates of each block in the transcript to mRNA/CDS depending on the alignment.
    2) Use the mRNA/CDS alignment to calculate a mapping between alignment positions and transcript positions.
    3) Determine if each block gap coordinate is within wiggle_distance of a parental block gap.

    :param ref_tx: GenePredTranscript object representing the parent transcript
    :param tx: GenePredTranscript object representing the target transcript
    :param psl: PslRow object representing the mRNA/CDS alignment between ref_tx and tx
    :param aln_mode: One of ('CDS', 'mRNA'). Determines if we aligned CDS or mRNA.
    :param wiggle_distance: The wiggle distance (in transcript coordinates)
    :return: integer value
    """
    def find_closest(sorted_numeric_list, query_number):
        """Uses list bisection to find the closest member of the tgt_intron list to the current ref_intron"""
        pos = bisect.bisect_left(sorted_numeric_list, query_number)
        if pos == 0:
            return sorted_numeric_list[0]
        if pos == len(sorted_numeric_list):
            return sorted_numeric_list[-1]
        before = sorted_numeric_list[pos - 1]
        after = sorted_numeric_list[pos]
        if after - query_number < query_number - before:
            return after
        else:
            return before

    # before we calculate anything, make sure we have introns to lose
    if len(tx.intron_intervals) == 0:
        return 0

    # generate a sorted list of reference introns in current coordinates (mRNA or CDS)
    ref_introns = sorted(get_intron_coordinates(ref_tx, aln_mode))

    # generate a sorted list of target introns in current coordinates (mRNA or CDS)
    # note that since this PSL is target-referenced, we use query_coordinate_to_target()
    tgt_introns = []
    for intron in get_intron_coordinates(tx, aln_mode):
        p = psl.query_coordinate_to_target(intron)
        if p is not None:
            tgt_introns.append(p)

    # sort tgt_introns in case of negative strand
    tgt_introns = sorted(tgt_introns)

    # count the number of introns not within wiggle distance of each other
    num_missing = 0
    for ref_intron in ref_introns:
        closest = find_closest(tgt_introns, ref_intron)
        if not (closest - wiggle_distance < ref_intron < closest + wiggle_distance):
            num_missing += 1
    return num_missing


def calculate_num_missing_exons(ref_tx, psl, aln_mode, coverage_cutoff=0.8):
    """
    Calculates how many reference exons are missing in this transcript.

    This is determined by using coordinate translations from the reference exons to the target, determining how many
    of the target bases are covered through brute force

    TODO: This could be made faster by using a sample distance, and only looking every N bases. But less accurate

    :param ref_tx: GenePredTranscript object representing the parent transcript
    :param psl: PslRow object representing the mRNA/CDS alignment between ref_tx and tx
    :param aln_mode: One of ('CDS', 'mRNA'). Determines if we aligned CDS or mRNA.
    :param coverage_cutoff: The number of bases of an exon that need to not be mapped to count it as missing
    :return: integer value
    """
    # convert the reference exons to alignment coordinates.
    # We don't need the original exons because we can't produce useful coordinates here
    # which is why this is a metric and not an evaluation
    ref_exons = get_exon_intervals(ref_tx, aln_mode).values()
    # note that since this PSL is target-referenced, we use target_coordinate_to_query()
    num_missing = 0
    for exon in ref_exons:
        deleted_bases = 0
        for i in xrange(exon.start, exon.stop):
            if psl.target_coordinate_to_query(i) is None:
                deleted_bases += 1
        if tools.mathOps.format_ratio(deleted_bases, len(exon)) >= coverage_cutoff:
            num_missing += 1
    return num_missing


###
# Alignment Evaluation Classifiers
###


def exon_gain(tx, psl, aln_mode, coverage_cutoff=0.8):
    """
    Calculates whether we gained an exon in this transcript. Follows the same logic as calculate_num_missing_exons,
    but inverted.

    TODO: This could be made faster by using a sample distance, and only looking every N bases. But less accurate

    :param tx: Target GenePredTranscript object
    :param psl: PslRow object describing mRNA/CDS alignment between ref_tx and tx
    :param aln_mode: One of ('CDS', 'mRNA'). Determines if we aligned CDS or mRNA.
    :param coverage_cutoff: The number of bases of an exon that need to not be mapped to count it as a new exon
    :return: list of ChromosomeInterval objects if a gain exists else []
    """
    # convert the target exons to alignment coordinates
    tgt_exons = get_exon_intervals(tx, aln_mode)
    # note that since this PSL is target-referenced, we use query_coordinate_to_target()
    gained_exons = []
    for exon, converted_exon in tgt_exons.iteritems():
        inserted_bases = 0
        for i in xrange(converted_exon.start, converted_exon.stop):
            if psl.target_coordinate_to_query(i) is None:
                inserted_bases += 1
        if tools.mathOps.format_ratio(inserted_bases, len(converted_exon)) >= coverage_cutoff:
            gained_exons.append(exon)
    return gained_exons


def in_frame_stop(tx, psl, aln_mode):
    """
    Finds the first in frame stop of this transcript, if there are any

    :param tx: Target GenePredTranscript object
    :param psl: PslRow object describing CDS alignment between ref_tx and tx.
    :param aln_mode: One of ('CDS', 'mRNA'). Determines if we aligned CDS or mRNA.
    :return: A ChromosomeInterval object if an in frame stop was found otherwise None
    """
    # if we are in mRNA space, we need to extract the CDS sequence from psl.tgt_seq
    if aln_mode == 'mRNA':
        cds_start = tx.cds_coordinate_to_mrna(0)
        cds_stop = tx.cds_coordinate_to_mrna(tx.cds_size - 1)
        tgt_seq = psl.tgt_seq[cds_start: cds_stop]
    else:
        tgt_seq = psl.tgt_seq
    for pos, codon in tools.bio.read_codons_with_position(tgt_seq):
        if tools.bio.translate_sequence(codon) == '*':
            start = tx.cds_coordinate_to_chromosome(pos)
            stop = tx.cds_coordinate_to_chromosome(pos + 3)
            if tx.strand == '-':
                start, stop = stop, start
            return tools.intervals.ChromosomeInterval(tx.chromosome, start, stop, tx.strand)
    return None


def find_indels(tx, psl, aln_mode):
    """
    Walks the psl alignment looking for alignment gaps. Reports all such gaps in Chromosome Coordinates, marking
    the type of gap (CodingInsertion, CodingMult3Insertion, CodingDeletion, CodingMult3Deletion)

    Insertion/Deletion is relative to the target genome, for example:

    CodingInsertion:
    ref: ATGC--ATGC
    tgt: ATGCGGATGC

    CodingDeletion:
    ref: ATGCGGATGC
    tgt: ATGC--ATGC

    :param tx: GenePredTranscript object representing the target transcript
    :param psl: PslRow object describing CDS alignment between ref_tx and tx.
    :param aln_mode: One of ('CDS', 'mRNA'). Determines if we aligned CDS or mRNA.
    :return: paired list of [category, ChromosomeInterval] objects if a coding insertion exists else []
    """
    def interval_is_coding(tx, i):
        """returns True if the given ChromosomeInterval object is coding in this tx"""
        return i.start >= tx.thick_start and i.stop <= tx.thick_stop

    def convert_coordinates_to_chromosome(left_pos, right_pos, coordinate_fn, strand):
        """convert alignment coordinates to target chromosome coordinates, inverting if negative strand"""
        left_chrom_pos = coordinate_fn(left_pos)
        assert left_chrom_pos is not None
        right_chrom_pos = coordinate_fn(right_pos)
        assert right_chrom_pos is not None
        if strand == '-':
            left_chrom_pos, right_chrom_pos = right_chrom_pos, left_chrom_pos
        assert right_chrom_pos >= left_chrom_pos
        return left_chrom_pos, right_chrom_pos

    def parse_indel(left_pos, right_pos, coordinate_fn, tx, gap_type):
        """Converts either an insertion or a deletion into a output interval"""
        left_chrom_pos, right_chrom_pos = convert_coordinates_to_chromosome(left_pos, right_pos, coordinate_fn,
                                                                            tx.strand)
        if left_chrom_pos is None or right_chrom_pos is None:
            assert aln_mode == 'CDS'
            return []
        i = tools.intervals.ChromosomeInterval(tx.chromosome, left_chrom_pos, right_chrom_pos, tx.strand)
        if interval_is_coding(tx, i):
            this_type = 'CodingMult3' if len(i) % 3 == 0 else 'Coding'
        else:
            this_type = 'NonCoding'
        return [''.join([this_type, gap_type]), i]

    # depending on mode, we convert the coordinates from either CDS or mRNA
    # we also have a different position cutoff to make sure we are not evaluating terminal gaps
    if aln_mode == 'CDS':
        coordinate_fn = tx.cds_coordinate_to_chromosome
    else:
        coordinate_fn = tx.mrna_coordinate_to_chromosome

    # r holds the output
    r = []

    # remember where we were last iteration
    q_pos = 0
    t_pos = 0
    # iterate over block starts[i], q_starts[i + 1], t_starts[i + 1]
    for block_size, q_start, t_start in itertools.izip(*[psl.block_sizes, psl.q_starts[1:], psl.t_starts[1:]]):
        q_offset = q_start - block_size - q_pos
        t_offset = t_start - block_size - t_pos
        assert not (q_offset == t_offset == 0)
        if q_offset != 0:  # query insertion -> insertion in target sequence
            left_pos = q_start - q_offset
            right_pos = q_start
            r.append(parse_indel(left_pos, right_pos, coordinate_fn, tx, 'Insertion'))
        if t_offset != 0:  # target insertion -> insertion in reference sequence
            left_pos = right_pos = q_start
            r.append(parse_indel(left_pos, right_pos, coordinate_fn, tx, 'Deletion'))
        q_pos = q_start
        t_pos = t_start
    return r


###
# Helper functions
###


def convert_cds_frames(ref_tx, tx, aln_mode):
    """
    Wrapper for convert_cds_frame that converts the reference and target GenePredTranscript objects to CDS-frame
    Transcript objects only if the biotype is protein_coding and the transcripts are out of frame

    :param ref_tx: Reference GenePredTranscript object
    :param tx: Target GenePredTranscript object
    :param aln_mode: If we are in CDS mode, we need to convert the transcripts to a CDS-framed object.
    :return: tuple of GenePredTranscript objects (ref_tx, tx)
    """
    if aln_mode == 'CDS':
        if ref_tx.offset != 0:
            ref_tx = convert_cds_frame(ref_tx)
        if tx.offset != 0:
            tx = convert_cds_frame(tx)
    return ref_tx, tx


def convert_cds_frame(tx):
    """
    If this GenePredTranscript object is out of frame, return a new Transcript object representing just the CDS, in
    frame, trimmed to be a multiple of 3

    :param tx: GenePredTranscript object
    :return: Transcript object
    """
    offset = tx.offset
    mod3 = (tx.cds_size - offset) % 3
    if tx.strand == '+':
        b = tx.get_bed(new_start=tx.thick_start + offset, new_stop=tx.thick_stop - mod3)
    else:
        b = tx.get_bed(new_start=tx.thick_start + mod3, new_stop=tx.thick_stop - offset)
    return tools.transcripts.Transcript(b)


def get_intron_coordinates(tx, aln_mode):
    """
    Converts the block_starts coordinates to mRNA or CDS coordinates used in the alignment based on the alignment mode.

    :param tx:GenePredTranscript object
    :param aln_mode: One of ('CDS', 'mRNA'). Used to determine if we aligned in CDS space or mRNA space
    :return: list of integers
    """
    if aln_mode == 'CDS':
        tx = convert_cds_frame(tx)
        introns = [tx.chromosome_coordinate_to_cds(tx.start + x) for x in tx.block_starts[1:]]
    else:
        introns = [tx.chromosome_coordinate_to_mrna(tx.start + x) for x in tx.block_starts[1:]]
    # remove None which means this transcript is protein_coding and that exon is entirely non-coding
    return [x for x in introns if x is not None]


def get_exon_intervals(tx, aln_mode):
    """
    Generates a dict of intervals for this transcript in either mRNA coordinates or CDS coordinates depending on
    alignment mode.

    We maintain a mapping of where the exon came from to deal with CDS conversions and negative strands easily.

    :param tx: GenePredTranscript object
    :param aln_mode: One of ('CDS', 'mRNA'). Used to determine if we aligned in CDS space or mRNA space
    :return: dict of ChromosomeInterval objects {reference:converted}
    """
    if aln_mode == 'CDS':
        tx = convert_cds_frame(tx)
    exons = {}
    for exon in tx.exon_intervals:
        start = tx.chromosome_coordinate_to_mrna(exon.start)
        stop = tx.chromosome_coordinate_to_mrna(exon.stop - 1)  # zero based, half open
        if tx.strand == '-':
            start, stop = stop, start
        i = tools.intervals.ChromosomeInterval(None, start, stop + 1, '.')
        exons[exon] = i
    return exons
