import array
from collections import Counter
from typing import List, Optional, Tuple, Union

import numpy as np
import pysam
from joblib import delayed
from numba import njit
from tqdm import tqdm

from . import utils

NUCLEOTIDES_STRICT = ['A', 'C', 'G', 'T']
NUCLEOTIDES_PERMISSIVE = ['R', 'Y', 'S', 'W', 'K', 'M', 'B', 'D', 'H', 'V', 'N']
NUCLEOTIDES = NUCLEOTIDES_STRICT + NUCLEOTIDES_PERMISSIVE
NUCLEOTIDES_AMBIGUOUS = {
    'N': ('A', 'C', 'G', 'T'),
    'R': ('A', 'G'),
    'Y': ('C', 'T'),
    'S': ('G', 'C'),
    'W': ('A', 'T'),
    'K': ('G', 'T'),
    'M': ('A', 'C'),
    'B': ('C', 'G', 'T'),
    'D': ('A', 'G', 'T'),
    'H': ('A', 'C', 'T'),
    'V': ('A', 'C', 'G'),
}
NUCLEOTIDE_COMPLEMENT = {
    'A': 'T',
    'C': 'G',
    'G': 'C',
    'T': 'A',
    'N': 'N',
    'R': 'Y',
    'Y': 'R',
    'S': 'W',
    'W': 'S',
    'K': 'M',
    'M': 'K',
    'B': 'V',
    'D': 'H',
    'H': 'D',
    'V': 'B',
}
NUCLEOTIDE_MASKS = {
    n: np.array([
        _n in NUCLEOTIDES_AMBIGUOUS.get(n, [n]) for _n in NUCLEOTIDES_STRICT
    ],
                dtype=bool)
    for n in NUCLEOTIDES
}


class SequenceError(Exception):
    pass


def _sequence_to_array(
        sequence: str,
        l: Optional[int] = None  # noqa: E741
) -> np.ndarray:  # noqa: E741
    sequence = sequence.upper()
    for c in sequence:
        if c not in NUCLEOTIDES:
            raise SequenceError(f'Unknown nucleotide `{c}`')

    arr = np.zeros((len(NUCLEOTIDES_STRICT), l or len(sequence)), dtype=bool)
    for i, c in enumerate(sequence):
        arr[NUCLEOTIDE_MASKS[c], i] = True
    return arr


def _qualities_to_array(
        qualities: Union[str, array.array],
        l: Optional[int] = None  # noqa: E741
) -> np.ndarray:
    if l and l < len(qualities):
        raise SequenceError('`l` can not be smaller than length of `qualities`')

    arr = np.array(
        pysam.qualitystring_to_array(qualities)
        if isinstance(qualities, str) else qualities,
        dtype=np.uint8
    )
    if l:
        arr.resize(l)
    return arr


def _most_likely_sequence(positional_probs: np.ndarray) -> str:
    # TODO: deal with ties
    indices = positional_probs.argmax(axis=0)
    return ''.join(NUCLEOTIDES_STRICT[i] for i in indices)


def _disambiguate_sequence(sequence: np.ndarray) -> List[str]:
    sequences = ['']
    for pos in sequence.T:
        new_sequences = []
        for i in pos.nonzero()[0]:
            new_sequences += [
                f'{seq}{NUCLEOTIDES_STRICT[i]}' for seq in sequences
            ]
        sequences = new_sequences
    return sequences


def _calculate_positional_probs(
        sequences: np.ndarray, qualities: np.ndarray
) -> np.ndarray:
    positional_probs = np.zeros(sequences[0].shape, dtype=int)
    for seq, qual in zip(sequences, qualities):
        np.add(positional_probs, qual, out=positional_probs, where=seq)
    return positional_probs


def call_consensus_with_qualities(
    sequences: List[str],
    qualities: Union[List[str], List[array.array]],
    q_threshold: int = 30,
    proportion: float = 0.05,
    return_qualities: bool = False,
) -> Union[Tuple[List[str], np.ndarray], Tuple[List[str], np.ndarray,
                                               List[str]]]:
    """Given a list of sequences and their base qualities, constructs a *set* of consensus
    sequences by iteratively constructing a consensus (by selecting the most likely
    base at each position) and assigning sequences with match probability <=
    max(min(match probability), `q_threshold` * (`proportion` * length of longest sequence))
    to this consensus. Then, the consensus is updated by constructing the consensus only
    among these sequences. The match probability of a sequence to a consensus is the sum of
    the quality values where they do not match (equivalent to negative log probability that
    all mismatches were sequencing errors). Provided work well for most cases.
    """
    # Check number of sequences and their lengths match with provided qualities
    if len(sequences) != len(qualities):
        raise Exception(
            f'{len(sequences)} sequences and {len(qualities)} qualities were provided'
        )
    if any(len(seq) != len(qual) for seq, qual in zip(sequences, qualities)):
        raise Exception(
            'length of each sequence must match length of each quality string'
        )

    def _call_consensus(seqs, quals, thresh):
        if len(seqs) == 1:
            return _most_likely_sequence(seqs[0]), np.array([True], dtype=bool)

        positional_probs = _calculate_positional_probs(seqs, quals)
        consensus_indices = positional_probs.argmax(axis=0)

        # For each sequence, calculate the probability that the sequence was actually
        # equal the consensus, but the different bases are due to sequencing errors
        # NOTE: should we also be looking at probability that matches are correct?
        probs = []
        for seq, qual in zip(seqs, quals):
            p = np.sum(qual[consensus_indices != seq.argmax(axis=0)])
            probs.append(p)
        probs = np.array(probs)
        assigned = probs <= max(thresh, min(probs))

        # NOTE: we construct a new consensus from assigned sequences
        assigned_seqs = seqs[assigned]
        assigned_quals = quals[assigned]
        return _most_likely_sequence(
            _calculate_positional_probs(assigned_seqs, assigned_quals)
        ), assigned

    # Convert sequences to array representations
    l = max(len(s) for s in sequences)  # noqa: E741
    sequences_arrays = np.array([
        _sequence_to_array(sequence, l=l) for sequence in sequences
    ])
    # Convert quality strings to quality values (integers)
    qualities_arrays = np.array([
        _qualities_to_array(quals, l=l) for quals in qualities
    ])

    # Iteratively call consensus sequences. This used to be done recursively, but there were cases
    # when Python's recursion limit would be reached. Thankfully, all recursive algorithms can be
    # rewritten to be iterative.
    threshold = q_threshold * (l * proportion)
    consensuses = []
    assignments = np.full(len(sequences), -1, dtype=int)
    index_transform = {i: i for i in range(len(sequences))}
    _sequences_arrays = sequences_arrays.copy()
    _qualities_arrays = qualities_arrays.copy()
    while True:
        consensus, assigned = _call_consensus(
            _sequences_arrays, _qualities_arrays, threshold
        )
        if consensus in consensuses:
            label = consensuses.index(consensus)
        else:
            label = len(consensuses)
            consensuses.append(consensus)

        assigned_indices = assigned.nonzero()[0]
        unassigned_indices = (~assigned).nonzero()[0]
        assignments[[index_transform[i] for i in assigned_indices]] = label
        if all(assigned):
            break
        index_transform = {
            i: index_transform[j]
            for i, j in enumerate(unassigned_indices)
        }
        _sequences_arrays = _sequences_arrays[~assigned]
        _qualities_arrays = _qualities_arrays[~assigned]

    # Compute qualities for each consensus sequence if return_qualities = True
    if return_qualities:
        consensuses_qualities = []
        for i, consensus in enumerate(consensuses):
            assigned = assignments == i
            assigned_sequences = sequences_arrays[assigned]
            assigned_qualities = qualities_arrays[assigned]
            consensus_array = _sequence_to_array(consensus, l)

            # (assigned_sequences & consensus_array) is a 3-dimensional array
            # First dimension contains each sequence, second contains base identity,
            # third contains positions, so (assigned_sequences & consensus_array) contains True
            # in positions of each sequence where the sequence has the same base as the
            # consensus. Taking the any(axis=1) of this gives a 2D matrix where each row
            # corresponds to each sequence and each column contains True if the base at that
            # position in that sequence matches the consensus. Multiplying this
            # boolean mask with the assigned_qualities gives the base quality of each
            # sequence only at the positions where the base matches that of the consensus.
            # Then, we take the maximum quality among all these bases.
            consensus_qualities = (
                assigned_qualities *
                (assigned_sequences & consensus_array).any(axis=1)
            ).max(axis=0)
            consensuses_qualities.append(
                pysam.qualities_to_qualitystring(consensus_qualities)
            )
        return consensuses, assignments, consensuses_qualities
    else:
        return consensuses, assignments


@njit
def _mismatch_mask(sequence1: np.ndarray, sequence2: np.ndarray) -> int:
    not_and = ~(sequence1 & sequence2)
    result = np.ones(not_and.shape[1], dtype=np.bool_)
    for i in range(not_and.shape[0]
                   ):  # np.all with arguments isn't available in @njit
        result &= not_and[i]
    return result


@njit
def _mismatch_masks(
        sequence: np.ndarray, whitelist: np.ndarray, d: int
) -> np.ndarray:
    indices = []
    masks = []
    for i, bc_array in enumerate(whitelist):
        mask = _mismatch_mask(sequence, bc_array)
        if mask.sum() > d:
            continue
        indices.append(i)
        masks.append(mask)
    return indices, masks


@njit
def _hamming_distance(sequence1: np.ndarray, sequence2: np.ndarray) -> int:
    return _mismatch_mask(sequence1, sequence2).sum()


def hamming_distance(sequence1: str, sequence2: str) -> int:
    """Calculate the hamming distance between two sequences.
    """
    if len(sequence1) != len(sequence2):
        raise SequenceError('Unequal lengths')

    return _hamming_distance(
        _sequence_to_array(sequence1), _sequence_to_array(sequence2)
    )


@njit
def _hamming_distances(
        sequence: np.ndarray, sequences: np.ndarray
) -> np.ndarray:
    distances = np.zeros(sequences.shape[0], dtype=np.uint)
    for i, seq in enumerate(sequences):
        distances[i] = _hamming_distance(sequence, seq)
    return distances


def hamming_distances(sequence: str, sequences: List[str]) -> np.ndarray:
    """Calculate the hamming distance between a sequence and a list of sequences.
    """
    if any(len(sequence) != len(seq) for seq in sequences):
        raise SequenceError('All sequences must be equal length')

    sequence = _sequence_to_array(sequence)
    sequences = np.array([_sequence_to_array(seq) for seq in sequences])
    return _hamming_distances(sequence, sequences)


@njit
def _hamming_distance_matrix(
        sequences1: np.ndarray, sequences2: np.ndarray
) -> np.ndarray:
    distances = np.zeros((sequences1.shape[0], sequences2.shape[0]),
                         dtype=np.uint)
    for i, seq1 in enumerate(sequences1):
        distances[i] = _hamming_distances(seq1, sequences2)
    return distances


def hamming_distance_matrix(
        sequences1: List[str], sequences2: List[str]
) -> np.ndarray:
    """Calculate all pairwise hamming distances between two lists of sequences.
    """
    if any(len(sequences1[0]) != len(seq) for seq in sequences1 + sequences2):
        raise SequenceError('All sequences must be equal length')

    sequences1 = np.array([
        _sequence_to_array(sequence) for sequence in sequences1
    ])
    sequences2 = np.array([
        _sequence_to_array(sequence) for sequence in sequences2
    ])
    return _hamming_distance_matrix(sequences1, sequences2)


@njit
def _pairwise_hamming_distances(sequences: np.ndarray) -> np.ndarray:
    distances = np.zeros((len(sequences), len(sequences)), dtype=np.uint)
    for i in range(sequences.shape[0]):
        for j in range(i, sequences.shape[0]):
            d = _hamming_distance(sequences[i], sequences[j])
            distances[i, j] = d
            distances[j, i] = d
    return distances


def pairwise_hamming_distances(sequences: List[str]) -> np.ndarray:
    """Calculate all pairwise hamming distances between combinations of sequences
    from a single list.
    """
    if any(len(sequences[0]) != len(seq) for seq in sequences):
        raise SequenceError('All sequences must be equal length')
    sequences = np.array([
        _sequence_to_array(sequence) for sequence in sequences
    ])
    return _pairwise_hamming_distances(sequences)


@njit
def _correct_to_whitelist(
        qualities: np.ndarray,
        indices: np.ndarray,
        masks: np.ndarray,
        log10_proportions: np.ndarray,
) -> Tuple[int, float]:
    best_bc = -1
    max_log10_likelihood = -np.inf
    log10_likelihoods = []
    for i, mask in zip(indices, masks):
        log10p_edit = -(qualities[mask].sum() / 10)
        log10_likelihood = log10_proportions[i] + log10p_edit
        log10_likelihoods.append(log10_likelihood)

        if log10_likelihood > max_log10_likelihood:
            max_log10_likelihood = log10_likelihood
            best_bc = i

    log10_confidence = 0
    if best_bc >= 0:
        log10_confidence = max_log10_likelihood - np.log10(
            (np.power(10, np.array(log10_likelihoods))).sum()
        )
    return best_bc, log10_confidence


def correct_sequences_to_whitelist(
        sequences: List[str],
        qualities: Union[List[str], List[array.array]],
        whitelist: List[str],
        d: int = 1,
        confidence: float = 0.9,
        n_threads: int = 1
) -> List[Union[str, None]]:
    """Correct a list of sequences to a whitelist within `d` hamming distance.
    Note that `sequences` can contain duplicates, but `whitelist` can not.

    For a given sequence, if there are multiple barcodes in the whitelist to which
    its distance is <= `d`, the sequence is assigned to a barcode by using the
    prior probability that the sequence originated from the barcode. If the confidence
    that the sequence originated from the most likely barcode is less than `confidence`,
    assignment is skipped.

    https://github.com/10XGenomics/cellranger/blob/a83c753ce641db6409a59ad817328354fbe7187e/lib/rust/annotate_reads/src/barcodes.rs
    """
    # Check number of sequences and their lengths match with provided qualities
    if len(sequences) != len(qualities):
        raise Exception(
            f'{len(sequences)} sequences and {len(qualities)} qualities were provided'
        )
    if any(len(seq) != len(qual) for seq, qual in zip(sequences, qualities)):
        raise Exception(
            'length of each sequence must match length of each quality string'
        )
    if len(set(whitelist)) != len(whitelist):
        raise SequenceError('`whitelist` contains duplicates')
    for seq in sequences:
        if len(seq) != len(sequences[0]):
            raise SequenceError(
                'all sequences in `sequences` must be of same length'
            )
    for bc in whitelist:
        if len(bc) != len(whitelist[0]):
            raise SequenceError(
                'all sequences in `whitelist` must be of same length'
            )
    if len(sequences[0]) != len(whitelist[0]):
        raise SequenceError(
            'all sequences in `sequences` and `whitelist` must be of same length'
        )

    # Step 1: find any sequences that are exact matches of the whitelist
    counts = Counter(sequences)
    unique_sequences = sorted(counts.keys())
    whitelist_arrays = np.array([_sequence_to_array(bc) for bc in whitelist])
    mismatch_cache = {}
    whitelist_counts = np.zeros(len(whitelist), dtype=int)
    matches = {}
    corrections = [None] * len(sequences)
    for i, (indices, masks) in enumerate(utils.ParallelWithProgress(
            n_jobs=n_threads, total=len(unique_sequences),
            desc='[1/2] Constructing mismatch masks'
    )(delayed(_mismatch_masks)(_sequence_to_array(seq), whitelist_arrays, d=d)
      for seq in unique_sequences)):
        indices = np.array(indices, dtype=int)
        masks = np.array(masks, dtype=bool)
        mismatch_cache[unique_sequences[i]] = (indices, masks)

        if indices.shape[0] == 0:
            continue

        # Check if there is an exact match to more than one barcode. If there was,
        # don't add the count to the whitelisted count.
        match_indices = indices[masks.sum(axis=1) == 0]
        if len(match_indices) == 1:
            matches[unique_sequences[i]] = whitelist[match_indices[0]]
        for i in match_indices:
            whitelist_counts[i] += counts[whitelist[i]] / len(match_indices)
    progress = tqdm(
        total=len(sequences), desc='[2/2] Correcting sequences', smoothing=0
    )
    for i, sequence in enumerate(sequences):
        if sequence in matches:
            corrections[i] = matches[sequence]
            progress.update(1)

    # Calculate proportions
    whitelist_pseudo = sum(whitelist_counts) + len(whitelist)
    whitelist_log10_proportions = np.log10(
        (whitelist_counts + 1) / whitelist_pseudo
    )

    # Step 2: correct all other sequences to whitelist
    confidence = np.log10(confidence)
    for i, seq in enumerate(sequences):
        if corrections[i] is not None:
            continue

        best_bc, log10_confidence = _correct_to_whitelist(
            _qualities_to_array(qualities[i]), mismatch_cache[seq][0],
            mismatch_cache[seq][1].reshape(1, -1), whitelist_log10_proportions
        )
        if best_bc >= 0 and log10_confidence >= confidence:
            corrections[i] = whitelist[best_bc]
        progress.update(1)

    return corrections
