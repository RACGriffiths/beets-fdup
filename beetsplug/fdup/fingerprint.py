"""Fingerprints: bit arrays of hashed character n-grams, and fast bulk comparison of them with numpy."""

import collections
from collections.abc import Callable, Generator, Iterable
from typing import Any

import numpy as np
from bitarray import bitarray
from unidecode import unidecode


class Fingerprint:
    """Stores a bitarray with values set based upon trigrams of the underlying text, also caches the number of set bits."""
    __slots__ = ('bits', 'n')

    def __init__(self, bits: bitarray):
        self.bits: bitarray = bits
        self.n: int = bits.count()


class TextPreprocessor:
    """Preprocesses text entries into bitarrays for fuzzy matching."""

    def __init__(
            self,
            alphabet: str = "abcdefghijklmnopqrstuvwxyz 0123456789",
            prefix: str = ' ',
            suffix: str = ' ',
            word_size: int = 384):
        """Initialize the preprocessor."""

        # Initialise character map:
        self.char_map: dict[str, int] = {}
        for i, char in enumerate(alphabet):
            self.char_map[char] = i
        self.miss_val = len(self.char_map)
        self.m = len(self.char_map) + 1
        self.word_size = word_size

        # Maybe add distinct value for start effects:
        if prefix in self.char_map:
            self.start_val = self.char_map[prefix]
        else:
            self.start_val = self.m
            self.m = self.m + 1

        # Maybe add distinct value for end effects:
        if suffix in self.char_map:
            self.end_val = self.char_map[suffix]
        else:
            self.end_val = self.m
            self.m = self.m + 1

    def text2fingerprint(self, text: str) -> Fingerprint:

        # Check there is at least some text.
        bits = bitarray(self.word_size)
        if not text:
            return Fingerprint(bits)

        # Sanitize input by:
        #   - converting accented characters to regular form
        #   - lowercasing
        text = unidecode(text).casefold()
        if not text:
            return Fingerprint(bits)

        # Aggregate bits for each trigram in the text.
        counter: dict[int, int] = collections.defaultdict(int)
        b_val = self.start_val
        c_val = self.char_map.get(text[0], self.miss_val)
        for cChar in text[1:]:
            a_val, b_val, c_val = b_val, c_val, self.char_map.get(cChar, self.miss_val)
            self._add_trigram(counter, a_val, b_val, c_val)

        # Add in end-effects.
        a_val, b_val, c_val = b_val, c_val, self.end_val
        self._add_trigram(counter, a_val, b_val, c_val)
        counter[self._idx_from_2vals(b_val, c_val)] += 1

        # Turn counter into bitarray.
        for k, v in counter.items():
            bits[k] = 1
            for i in range(1, v):
                # Allow for repeated letter combinations by shifting repeated bits to the left.
                bits[(k + i) % self.word_size] = 1

        return Fingerprint(bits)

    def _add_trigram(self, arr: dict[int, int], a_val: int, b_val: int, c_val: int):
        # Add bits for AB, AC and ABC.
        # Using same hash function & consts for AB and AC somewhat accounts for character swaps, deletes, and adds.
        arr[self._idx_from_2vals(a_val, b_val)] += 1
        arr[self._idx_from_2vals(a_val, c_val)] += 1
        arr[self._idx_from_3vals(a_val, b_val, c_val)] += 1

    def _idx_from_2vals(self, a_val: int, b_val: int):
        # Map to relatively random bit within [0,word_size)
        return (15331 * a_val ^ 17203 * b_val) % self.word_size

    def _idx_from_3vals(self, a_val: int, b_val: int, c_val: int):
        # Map to relatively random bit within [0,word_size)
        return (15331 * (a_val + self.m) ^ 17203 * b_val ^ 19121 * c_val + 12345) % self.word_size


class FingerprintSet:
    """Fingerprints of a list of texts, packed into a uint64 matrix (one row per text)
    so that numpy can compare whole blocks of them in one call."""

    def __init__(self, preprocessor: TextPreprocessor, texts: Iterable[str]):
        fingerprints = [preprocessor.text2fingerprint(text) for text in texts]
        n_bytes = -(-preprocessor.word_size // 64) * 8     # Round up to whole uint64 words.
        self.words: np.ndarray = np.frombuffer(
            b"".join(fp.bits.tobytes().ljust(n_bytes, b"\0") for fp in fingerprints), dtype=np.uint64,
        ).reshape(len(fingerprints), n_bytes // 8)
        self.counts: np.ndarray = np.array([fp.n for fp in fingerprints], dtype=np.int64)


def compare_fps(
        words_a: np.ndarray, n_a: np.ndarray, words_b: np.ndarray, n_b: np.ndarray,
        inv_word_size: float, diff_pen: float, use_min: bool = True) -> np.ndarray:
    """Score fingerprints 'a' against fingerprints 'b', element-wise with numpy broadcasting.
    words_* hold the packed fingerprint bits (last axis), n_* the corresponding number of set bits.
    use_min: normalise by the smaller fingerprint ('overlap' metric), otherwise by the mean of both ('dice')."""
    hits = np.bitwise_count(words_a & words_b).sum(axis=-1, dtype=np.int64)
    avg = n_a * n_b * inv_word_size - 0.001
    base = np.minimum(n_a, n_b) if use_min else (n_a + n_b) * 0.5
    denom = base - avg + diff_pen * (n_a + n_b - hits - hits)
    return (hits - avg) / denom


# Caps the temporary (rows x cols x words) array built by each compare call in matching_pairs.
_MAX_BLOCK_CELLS = 1 << 20


def matching_pairs(
        fps: FingerprintSet,
        compare: Callable[..., np.ndarray],
        threshold: float,
        rows: np.ndarray,
        cols: np.ndarray | None = None,
) -> Generator[tuple[int, int, float], Any, None]:
    """rows and cols are arrays of fingerprint indices into fps. Yield (row_position, col_position, score) for each
    pair scoring above the threshold. If cols is None, rows are matched against each other (each pair once)."""
    triangular = cols is None
    if triangular:
        cols = rows
    if len(rows) == 0 or len(cols) == 0:
        return
    row_words, row_counts = fps.words[rows], fps.counts[rows]
    col_words, col_counts = (row_words, row_counts) if triangular else (fps.words[cols], fps.counts[cols])
    block = max(1, _MAX_BLOCK_CELLS // (len(cols) * fps.words.shape[1]))
    for r0 in range(0, len(rows), block):
        r1 = min(r0 + block, len(rows))
        c0 = r0 + 1 if triangular else 0    # When triangular, only compare against later values.
        if c0 >= len(cols):
            break
        scores = compare(row_words[r0:r1, None], row_counts[r0:r1, None], col_words[None, c0:], col_counts[None, c0:])
        i, j = np.nonzero(scores > threshold)
        if triangular:
            keep = j >= i                   # i.e. col (c0 + j) comes after row (r0 + i).
            i, j = i[keep], j[keep]
        yield from zip((r0 + i).tolist(), (c0 + j).tolist(), scores[i, j].tolist(), strict=True)
