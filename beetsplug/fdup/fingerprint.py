"""Fingerprints: bit arrays of hashed character n-grams, and fast bulk comparison of them with numpy."""

import collections
from collections.abc import Callable, Generator, Iterable
from typing import Any

import numpy as np
from unidecode import unidecode


class TextPreprocessor:
    """Turns text into hashed character n-grams: the bits of its fingerprint (see FingerprintSet)."""

    def __init__(
            self,
            alphabet: str = "abcdefghijklmnopqrstuvwxyz 0123456789",
            prefix: str = " ",
            suffix: str = " ",
            word_size: int = 1536):
        """Initialize the preprocessor."""

        # Initialise character map. Characters outside it are ignored (see ngram_counts).
        self.char_map: dict[str, int] = {}
        for i, char in enumerate(alphabet):
            self.char_map[char] = i
        # m: the number of distinct values hashed (the alphabet, plus start and end markers if they aren't in it).
        self.m = len(self.char_map)
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

    def ngram_counts(self, text: str) -> dict[int, int]:
        """The text's character n-grams, hashed to bit indices in [0, word_size), and how often each index occurs."""

        # Sanitize input by:
        #   - converting accented characters to regular form
        #   - lowercasing
        #   - dropping characters outside the alphabet (e.g. punctuation: 'help!' -> 'help'), then any extra spaces
        #     that leaves ('lay , lady' -> 'lay lady')
        text = "".join(ch for ch in unidecode(text).casefold() if ch in self.char_map)
        text = " ".join(word for word in text.split(" ") if word)
        if not text:
            return {}

        # For each run of three characters A, B, C (with start and end markers around the text), hash AB, AC and ABC
        # to a bit index. Using the same hash for AB and AC somewhat allows for characters swapped, missing or added.
        # The hashes are inlined, and counted by Counter (in C): this loop is most of fingerprinting's time.
        ws, m = self.word_size, self.m
        codes = [self.start_val, *map(self.char_map.__getitem__, text), self.end_val]
        hashes: list[int] = []
        for a, b, c in zip(codes, codes[1:], codes[2:], strict=False):     # (Stops at the last run of three.)
            hashes += ((15331 * a ^ 17203 * b) % ws,
                       (15331 * a ^ 17203 * c) % ws,
                       (15331 * (a + m) ^ 17203 * b ^ 19121 * c + 12345) % ws)
        hashes.append((15331 * codes[-2] ^ 17203 * codes[-1]) % ws)   # The last character and the end marker.
        return collections.Counter(hashes)


# Texts fingerprinted per chunk in FingerprintSet: caps the size of its temporary arrays (a few MB per chunk).
_TEXTS_PER_CHUNK = 4096


class FingerprintSet:
    """Fingerprints of a list of texts, packed into a uint64 matrix (one row per text) so that numpy can compare whole
    blocks of them in one call; counts holds each row's number of set bits.

    A fingerprint sets the bit of each of the text's hashed n-grams (see TextPreprocessor.ngram_counts). An index k
    occurring v times sets bits k, k+1, ..., k+v-1 (wrapping round), so repeats count. Bit k is bit 7 - k % 8 of
    byte k // 8 (most significant first), and each row is padded with zeros to whole uint64 words."""

    def __init__(self, preprocessor: TextPreprocessor, texts: Iterable[str]):
        texts = list(texts)
        word_size = preprocessor.word_size
        n_bytes = -(-word_size // 64) * 8     # Round up to whole uint64 words.
        packed = np.zeros((len(texts), n_bytes), dtype=np.uint8)
        flat = packed.reshape(-1)   # (A view: setting its bits sets packed's.)
        for start in range(0, len(texts), _TEXTS_PER_CHUNK):
            # (A generator, not a list: each text's counts are freed once used. Thousands of dicts kept alive would
            # make Python's garbage collector run more often, which costs more than the fingerprinting saves.)
            counters = (preprocessor.ngram_counts(text) for text in texts[start:start + _TEXTS_PER_CHUNK])
            rows, bits = _bits_to_set(counters, word_size)
            # ufunc.at, unlike |=, applies every one of several bits in the same byte.
            np.bitwise_or.at(flat, (start + rows) * n_bytes + (bits >> 3), (0x80 >> (bits & 7)).astype(np.uint8))
        self.words: np.ndarray = packed.view(np.uint64)
        self.counts: np.ndarray = np.bitwise_count(packed).sum(axis=1, dtype=np.int64)


def _bits_to_set(counters: Iterable[dict[int, int]], word_size: int) -> tuple[np.ndarray, np.ndarray]:
    """The bits to set for each text's n-gram counts, as (row, bit) arrays, where row is the text's position in
    counters: an index k occurring v times gives bits k, k+1, ..., k+v-1 (wrapping round). Done in bulk with numpy:
    much faster than setting bits one by one in Python."""
    keys: list[int] = []
    repeats: list[int] = []
    lengths: list[int] = []
    for counter in counters:
        keys.extend(counter)
        repeats.extend(counter.values())
        lengths.append(len(counter))
    repeats_a = np.array(repeats, dtype=np.intp)
    # Each bit's offset from its n-gram's first bit: its position in the output, minus that first bit's position.
    firsts = np.repeat(np.cumsum(repeats_a) - repeats_a, repeats_a)
    offsets = np.arange(len(firsts)) - firsts
    bits = (np.repeat(np.array(keys, dtype=np.intp), repeats_a) + offsets) % word_size
    rows = np.repeat(np.repeat(np.arange(len(lengths)), np.array(lengths, dtype=np.intp)), repeats_a)
    return rows, bits


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
