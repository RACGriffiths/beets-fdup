"""Tests for fingerprints, scoring them, and finding matching pairs in bulk."""

import functools

import numpy as np
import pytest

from beetsplug.fdup import fingerprint
from beetsplug.fdup.fingerprint import FingerprintSet, TextPreprocessor, compare_fps, matching_pairs

WORD_SIZE = 1536
PREPROCESSOR = TextPreprocessor(word_size=WORD_SIZE)
COMPARE = functools.partial(compare_fps, inv_word_size=1.0 / WORD_SIZE, diff_pen=0.025)

TITLES = ["hey jude", "hey jude!", "hey judy", "let it be", "let it bee", "yesterday", "yesterdays", "help",
          "help!", "something", "something else", "come together", "come together (remastered)", "get back"]


def score(text1: str, text2: str, use_min: bool = True) -> float:
    fps = FingerprintSet(PREPROCESSOR, [text1, text2])
    return float(COMPARE(fps.words[0], fps.counts[0], fps.words[1], fps.counts[1], use_min=use_min))


def fingerprint_of(text: str) -> tuple[bytes, int]:
    """A text's fingerprint: its bits, and how many are set."""
    fps = FingerprintSet(PREPROCESSOR, [text])
    return fps.words[0].tobytes(), int(fps.counts[0])


class StubPreprocessor:
    """Gives fixed n-gram counts, to test how FingerprintSet sets bits."""
    word_size = 64

    def __init__(self, counts: dict[str, dict[int, int]]):
        self.counts = counts

    def ngram_counts(self, text: str) -> dict[int, int]:
        return self.counts[text]


class TestTextPreprocessor:

    def test_empty_text_has_no_bits(self):
        assert fingerprint_of("")[1] == 0

    def test_case_and_accents_are_ignored(self):
        assert fingerprint_of("Café") == fingerprint_of("CAFE")

    def test_characters_outside_the_alphabet_are_ignored(self):
        assert fingerprint_of("Help!") == fingerprint_of("help")
        assert fingerprint_of("don't") == fingerprint_of("dont")

    def test_spaces_left_by_ignored_characters_are_collapsed(self):
        assert fingerprint_of("lay , lady - lay !") == fingerprint_of("lay lady lay")

    def test_only_characters_outside_the_alphabet_has_no_bits(self):
        assert fingerprint_of("!?")[1] == 0

    def test_repeated_text_sets_more_bits(self):
        assert fingerprint_of("abcde abcde")[1] > fingerprint_of("abcde")[1]


class TestFingerprintSet:

    def test_repeated_ngrams_set_the_following_bits_wrapping_round(self):
        fps = FingerprintSet(StubPreprocessor({"a": {5: 3, 10: 1, 63: 2}, "b": {}, "c": {5: 1, 6: 1}}), "abc")
        # Bit k is byte k // 8's bit 7 - k % 8, as unpackbits reads them (most significant first).
        set_bits = [np.flatnonzero(np.unpackbits(row.view(np.uint8))).tolist() for row in fps.words]
        assert set_bits == [[0, 5, 6, 7, 10, 63], [], [5, 6]]
        assert fps.counts.tolist() == [6, 0, 2]

    def test_rows_are_padded_to_whole_words(self):
        fps = FingerprintSet(TextPreprocessor(word_size=100), ["hey jude", "hey jude!"])
        assert fps.words.shape == (2, 2)    # 100 bits round up to 2 uint64 words.
        assert np.array_equal(fps.words[0], fps.words[1])

    def test_no_texts(self):
        fps = FingerprintSet(PREPROCESSOR, [])
        assert fps.words.shape == (0, WORD_SIZE // 64)
        assert fps.counts.shape == (0,)

    def test_chunks_give_the_same_fingerprints(self, monkeypatch):
        expected = FingerprintSet(PREPROCESSOR, TITLES)
        monkeypatch.setattr(fingerprint, "_TEXTS_PER_CHUNK", 3)
        fps = FingerprintSet(PREPROCESSOR, TITLES)
        assert np.array_equal(fps.words, expected.words)
        assert np.array_equal(fps.counts, expected.counts)


class TestCompare:

    def test_identical_texts_score_one(self):
        assert score("hello world", "hello world") == pytest.approx(1.0)

    def test_typo_scores_well_above_a_different_text(self):
        assert score("bohemian rhapsody", "bohemian rhapsdoy") > 0.75 > score("bohemian rhapsody", "another one")

    def test_overlap_metric_matches_a_contained_value_and_dice_does_not(self):
        assert score("abba", "abba and friends", use_min=True) > 0.75 > score("abba", "abba and friends", use_min=False)

    def test_broadcasts_over_arrays(self):
        fps = FingerprintSet(PREPROCESSOR, TITLES)
        scores = COMPARE(fps.words[:, None], fps.counts[:, None], fps.words[None, :], fps.counts[None, :])
        assert scores.shape == (len(TITLES), len(TITLES))
        assert np.allclose(np.diag(scores), 1.0)
        assert np.allclose(scores, scores.T)


class TestMatchingPairs:

    def test_triangular_yields_each_pair_once(self):
        fps = FingerprintSet(PREPROCESSOR, ["waterloo", "waterlooo", "waterloo!", "hey jude"])
        pairs = {(i, j) for i, j, _ in matching_pairs(fps, COMPARE, 0.75, np.arange(4))}
        assert pairs == {(0, 1), (0, 2), (1, 2)}

    def test_rows_against_cols(self):
        fps = FingerprintSet(PREPROCESSOR, ["waterloo", "hey jude", "waterlooo", "hey judy"])
        pairs = {(i, j) for i, j, _ in matching_pairs(fps, COMPARE, 0.75, np.array([0, 1]), np.array([2, 3]))}
        assert pairs == {(0, 0), (1, 1)}  # Positions within rows and cols, not indices into fps.

    @pytest.mark.parametrize("cols", [None, np.arange(len(TITLES))[::-1]])
    def test_splitting_into_blocks_gives_the_same_pairs(self, monkeypatch, cols):
        fps = FingerprintSet(PREPROCESSOR, TITLES)
        rows = np.arange(len(TITLES))
        expected = sorted(matching_pairs(fps, COMPARE, 0.5, rows, cols))
        assert expected     # (Otherwise this tests nothing.)
        monkeypatch.setattr(fingerprint, "_MAX_BLOCK_CELLS", 1)     # One row per block.
        assert sorted(matching_pairs(fps, COMPARE, 0.5, rows, cols)) == expected
