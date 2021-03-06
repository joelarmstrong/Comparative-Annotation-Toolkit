"""
Perform name conversions on transMap/AugustusTMR transcripts.
"""

import re


def remove_alignment_number(aln_id, aln_re=re.compile("-[0-9]+$")):
    """
    If the name of the transcript ends with -d as in
    ENSMUST00000169901.2-1, return ENSMUST00000169901.2
    :param aln_id: name string
    :param aln_re: compiled regular expression
    :return: string
    """
    return aln_re.split(aln_id)[0]


def remove_augustus_alignment_number(aln_id, aug_re=re.compile("^aug(TM|TMR|CGP)-")):
    """
    removes the alignment numbers prepended by AugustusTM/AugustusTMR
    Format: aug(TM|TMR)-ENSMUST00000169901.2-1
    :param aln_id: name string
    :param aug_re: compiled regular expression
    :return: string
    """
    return aug_re.split(aln_id)[-1]


def strip_alignment_numbers(aln_id):
    """
    Convenience function for stripping both Augustus and transMap alignment IDs from a aln_id
    :param aln_id: name string
    :return: string
    """
    return remove_alignment_number(remove_augustus_alignment_number(aln_id))


def aln_id_is_augustus(aln_id):
    """
    Uses remove_augustus_alignment_number to determine if this transcript is an Augustus transcript
    :param aln_id: name string
    :return: boolean
    """
    return True if remove_augustus_alignment_number(aln_id) != aln_id else False


def aln_id_is_transmap(aln_id):
    """
    Uses remove_augustus_alignment_number to determine if this transcript is an Augustus transcript
    :param aln_id: name string
    :return: boolean
    """
    return True if remove_alignment_number(aln_id) != aln_id else False


def aln_id_is_augustus_tm(aln_id):
    return 'augTM-' in aln_id


def aln_id_is_augustus_tmr(aln_id):
    return 'augTMR-' in aln_id


def aln_id_is_cgp(aln_id):
    return aln_id.startswith('jg')


def alignment_type(aln_id):
    """returns what type of alignment this ID is"""
    if aln_id_is_augustus_tmr(aln_id):
        return 'augTMR'
    elif aln_id_is_augustus_tm(aln_id):
        return 'augTM'
    elif aln_id_is_cgp(aln_id):
        return 'augCGP'
    elif aln_id_is_transmap(aln_id):
        return 'transMap'
    else:
        raise RuntimeError('Alignment ID: {} was not valid.'.format(aln_id))


def extract_unique_txs(aln_ids):
    """finds all unique transcript names in a list of alignment IDs"""
    return {strip_alignment_numbers(x) for x in aln_ids}


def extract_unique_genes(aln_ids, tx_gene_map):
    """finds all unique gene names in a list of alignment ids using a tx_gene_map"""
    return {tx_gene_map[strip_alignment_numbers(x)] for x in aln_ids}
