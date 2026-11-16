import cProfile
import collections
import functools
import io
import logging
import math
import pstats
import re
import sqlite3
import typing
from math import prod
from optparse import Values
from typing import Any, Generator, Dict, NamedTuple

import beets
import confuse
import line_profiler
import numpy as np
from beets.dbcore import FieldQuery, types
from beets.library import Item, parse_query_parts, Library
from beets.library.fields import TYPE_BY_FIELD
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand, print_, UserError
from beets.util.functemplate import template
from bitarray import bitarray
from unidecode import unidecode

PLUGIN = "fdup"

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
        self.char_map: Dict[str, int] = {}
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
        counter : Dict[int, int] = collections.defaultdict(int)
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

    def _add_trigram(self, arr: Dict[int, int], a_val: int, b_val: int, c_val: int):
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

    def __init__(self, preprocessor: TextPreprocessor, texts: typing.Iterable[str]):
        fingerprints = [preprocessor.text2fingerprint(text) for text in texts]
        n_bytes = -(-preprocessor.word_size // 64) * 8     # Round up to whole uint64 words.
        self.words: np.ndarray = np.frombuffer(
            b"".join(fp.bits.tobytes().ljust(n_bytes, b"\0") for fp in fingerprints), dtype=np.uint64,
        ).reshape(len(fingerprints), n_bytes // 8)
        self.counts: np.ndarray = np.array([fp.n for fp in fingerprints], dtype=np.int64)

@line_profiler.profile
def _compare_fps(
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

# Caps the temporary (rows x cols x words) array built by each _compare_fps call in _matching_pairs.
_MAX_BLOCK_CELLS = 1 << 20

@line_profiler.profile
def _matching_pairs(
        fps: FingerprintSet,
        compare: typing.Callable[..., np.ndarray],
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
        yield from zip((r0 + i).tolist(), (c0 + j).tolist(), scores[i, j].tolist())

_WORD = re.compile(r"[a-z0-9]+")
_PLACEHOLDER = re.compile(r"\0(\d+)\0")
_ROMAN_NUMERALS = frozenset("ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split())

def _collapse(text: str) -> str:
    return " ".join(text.split())

def _numbers(text: str) -> set[str]:
    """Part/movement numbers in a text, e.g. 'part 2' -> {'2'}, 'synchronicity ii' -> {'ii'}.
    A lone 'i' only counts as the last word (elsewhere it's usually the pronoun)."""
    words = _WORD.findall(text)
    numbers = {w for w in words if w.isdigit() or w in _ROMAN_NUMERALS}
    if len(words) > 1 and words[-1] == "i":
        numbers.add("i")
    return numbers

class ParsedValue(NamedTuple):
    """A normalised field value, split into a core and qualifiers by ValueParser."""
    core: str                   # e.g. 'falling in love with you'
    full: str                   # the core with subtitles left in place, e.g. "i can't help falling in love with you"
    subtitles: tuple[str, ...]  # qualifiers that aren't version markers: subtitles, alternate titles, movements...
    labels: frozenset[str]      # labels from version markers, e.g. {'live', 'remaster'}
    numbers: frozenset[str]     # part/movement numbers in the core and subtitles

class ValueParser:
    """Normalises one field's values (see the 'normalise' config) and, if 'qualifiers' are configured, splits them.

    Qualifiers are bracketed groups (anywhere in the value, nested or not) and anything after a separator such as
    ' - '. A qualifier containing a version marker (live, remix, ...) gives version labels; any other qualifier is
    kept as a subtitle, e.g. '(oh) pretty woman (live) - 2011 remaster' -> core 'pretty woman',
    full 'oh pretty woman', subtitles ('oh',), labels {'live', 'remaster'}."""

    def __init__(self, rules: list[tuple[str, str]], qualifiers: dict[str, Any] | None):
        self._rules = [(re.compile(pattern), replacement) for pattern, replacement in rules]
        self.splits = qualifiers is not None
        if not self.splits:
            return
        brackets: list[str] = qualifiers["brackets"]
        any_bracket = re.escape("".join(brackets))
        self._bracket_re = re.compile("|".join(
            f"{re.escape(o)}([^{any_bracket}]*){re.escape(c)}" for o, c in brackets)) if brackets else None
        separators: list[str] = qualifiers["separators"]
        self._separator_re = re.compile("|".join(f"(?:{s})" for s in separators)) if separators else None
        self._markers: dict[str, str] = qualifiers["version_markers"]
        self._marker_patterns = [(re.compile(p), label) for p, label in qualifiers["version_marker_patterns"].items()]

    def normalise(self, text: str) -> str:
        text = unidecode(text).casefold()
        for pattern, replacement in self._rules:
            text = pattern.sub(replacement, text)
        return _collapse(text)

    def _version_labels(self, qualifier: str) -> set[str] | None:
        """The labels if the qualifier marks a version (e.g. 'live at x' -> {'live'}), otherwise None."""
        found = [self._markers[w] for w in _WORD.findall(qualifier) if w in self._markers]
        found += [label for pattern, label in self._marker_patterns if pattern.search(qualifier)]
        return {label for label in found if label} if found else None

    def parse(self, value: str) -> ParsedValue:
        """Split an already normalised value."""
        if not self.splits:
            return ParsedValue(value, value, (), frozenset(), frozenset())
        unsplit = ParsedValue(value, value, (), frozenset(), frozenset(_numbers(value)))

        # Replace bracketed groups with placeholders, innermost first, keeping each group's (expanded) text.
        groups: list[str] = []
        def expand(text: str) -> str:
            return _PLACEHOLDER.sub(lambda m: f" {groups[int(m.group(1))]} ", text)
        def stash(m: re.Match) -> str:
            groups.append(expand(next(g for g in m.groups() if g is not None)))
            return f"\0{len(groups) - 1}\0"
        text, n = value, 1
        while n and self._bracket_re:
            text, n = self._bracket_re.subn(stash, text)

        # Anything after a separator is a qualifier too.
        head, *tail = self._separator_re.split(text) if self._separator_re else (text,)
        core = _collapse(_PLACEHOLDER.sub(" ", head))
        if not core or (not groups and not tail):   # E.g. '(untitled)', or simply nothing to split.
            return unsplit

        # Classify the qualifiers: version markers give labels, the rest are subtitles.
        tail = [_collapse(expand(t)) for t in tail]
        labels_by_qualifier = {q: self._version_labels(q) for q in [*(groups[int(i)] for i in _PLACEHOLDER.findall(head)), *tail]}
        labels = frozenset().union(*(found for found in labels_by_qualifier.values() if found is not None))
        subtitles = tuple(dict.fromkeys(
            _collapse(q) for q, found in labels_by_qualifier.items() if found is None and q.strip()))

        # 'full' keeps subtitles where they were (word order matters to n-grams), dropping version qualifiers.
        def subtitle_or_nothing(m: re.Match) -> str:
            q = groups[int(m.group(1))]
            return f" {q} " if labels_by_qualifier[q] is None else " "
        full = _collapse(" ".join([_PLACEHOLDER.sub(subtitle_or_nothing, head), *(t for t in tail if labels_by_qualifier[t] is None)]))
        numbers = frozenset(_numbers(core).union(*(_numbers(s) for s in subtitles)))
        return ParsedValue(core, full, subtitles, labels, numbers)

class FieldMatcher:
    """Fuzzy matching for one field: its settings and, once index_items() has run, the distinct (normalised) values
    of the loaded items, parsed, with fingerprints of each value's match keys:
      - main keys: the core and the full value (just the value itself if the field isn't split),
      - alias keys: each subtitle on its own, so e.g. 'con te partiro' matches 'time to say goodbye (con te partiro)'.
    Candidate pairs come from comparing keys in bulk (numpy); for split fields each candidate pair is then checked
    properly by _decide()."""

    def __init__(
            self, name: str, preprocessor: TextPreprocessor, parser: ValueParser,
            compare: typing.Callable[..., np.ndarray], threshold: float,
            exact_key: str | None, qualifiers: dict[str, Any] | None):
        self.name = name
        self.preprocessor = preprocessor
        self.parser = parser
        self.compare = compare
        self.threshold = threshold
        self.exact_key = exact_key
        qualifiers = qualifiers or {}
        self.alias_threshold: float = qualifiers.get("alias_threshold", math.inf)
        self.subtitle_threshold: float = qualifiers.get("subtitle_threshold", 0.0)
        self.check_numbers: bool = qualifiers.get("check_numbers", False)
        self.match_versions: bool = qualifiers.get("match_versions", True)

    def index_items(self, item_by_id: dict[int, "MinItem"]):
        normalised: dict[str, str] = {}
        index_by_value: dict[str, int] = {}
        self.value_idx_by_id: dict[int, int] = {}
        for id_, item in item_by_id.items():
            raw = item[self.name]
            if raw not in normalised:
                normalised[raw] = self.parser.normalise(raw)
            self.value_idx_by_id[id_] = index_by_value.setdefault(normalised[raw], len(index_by_value))
        self.parsed: list[ParsedValue] = [self.parser.parse(value) for value in index_by_value]

        key_idx_by_text: dict[str, int] = {}
        def key(text: str) -> int:
            return key_idx_by_text.setdefault(text, len(key_idx_by_text))
        self.main_keys: list[list[int]] = [sorted({key(p.core), key(p.full)}) for p in self.parsed]
        self.alias_keys: list[list[int]] = [sorted({key(s) for s in p.subtitles}) for p in self.parsed]
        self.fps = FingerprintSet(self.preprocessor, key_idx_by_text)
        self._key_idx_by_text = key_idx_by_text
        self._extra_fps: dict[str, tuple[np.ndarray, np.int64]] = {}
        self._decided: dict[tuple[int, int], float | None] = {}

        self.exact_key_by_id: dict[int, Any] = (
            {id_: item.get(self.exact_key) or None for id_, item in item_by_id.items()} if self.exact_key else {})

    def labels(self, id_: int) -> frozenset[str]:
        return self.parsed[self.value_idx_by_id[id_]].labels

    def versions(self, id1: int, id2: int) -> frozenset[str]:
        """Version labels that differ between two items, e.g. {'live'} for 'song' vs 'song (live)'."""
        return self.labels(id1) ^ self.labels(id2)

    def _entries(self, values: np.ndarray) -> tuple[np.ndarray, list[int], list[bool]]:
        """Flatten values into their keys: (key indices, the value owning each key, whether each is an alias key)."""
        entries = [(k, v, False) for v in values.tolist() for k in self.main_keys[v]]
        if self.parser.splits:
            entries += [(k, v, True) for v in values.tolist() for k in self.alias_keys[v]]
        return np.array([e[0] for e in entries], dtype=np.intp), [e[1] for e in entries], [e[2] for e in entries]

    @line_profiler.profile
    def matching_values(
            self, rows: np.ndarray, cols: np.ndarray | None = None) -> Generator[tuple[int, int, float], Any, None]:
        """Yield (row_value, col_value, score) for each pair of matching values, comparing rows against cols,
        or rows against each other (each pair once) if cols is None."""
        triangular = cols is None
        if len(rows) == 0 or (len(rows) < 2 if triangular else len(cols) == 0):
            return
        row_keys, row_owners, row_alias = self._entries(rows)
        col_keys, col_owners, col_alias = (row_keys, row_owners, row_alias) if triangular else self._entries(cols)

        # All keys in one pass (fewer, larger numpy calls), then filter: main keys must beat the threshold against
        # each other, an alias key needs alias_threshold against a main key, and alias keys aren't compared together.
        best: dict[tuple[int, int], float] = {}
        threshold = min(self.threshold, self.alias_threshold)
        for i, j, score in _matching_pairs(self.fps, self.compare, threshold, row_keys, None if triangular else col_keys):
            v1, v2 = row_owners[i], col_owners[j]
            if v1 == v2 or (row_alias[i] and col_alias[j]):
                continue
            if score <= (self.alias_threshold if row_alias[i] or col_alias[j] else self.threshold):
                continue
            if triangular and v1 > v2:
                v1, v2 = v2, v1
            if score > best.get((v1, v2), -math.inf):
                best[(v1, v2)] = score

        for (v1, v2), score in best.items():
            if self.parser.splits:
                score = self._decide(v1, v2)
                if score is None:
                    continue
            yield v1, v2, score

    def _decide(self, v1: int, v2: int) -> float | None:
        """The score for a candidate pair of (different) values of a split field, or None if they don't match."""
        pair = (v1, v2) if v1 < v2 else (v2, v1)
        if pair not in self._decided:
            self._decided[pair] = self._decide_uncached(*pair)
        return self._decided[pair]

    def _decide_uncached(self, v1: int, v2: int) -> float | None:
        p1, p2 = self.parsed[v1], self.parsed[v2]
        if not self.match_versions and p1.labels != p2.labels:
            return None
        if self.check_numbers and p1.numbers and p2.numbers and p1.numbers != p2.numbers:
            return None     # E.g. 'part 2' vs 'part 3'.

        # Subtitles both share (e.g. the same '(from "some film")') say nothing about whether they're the same song.
        shared = set(p1.subtitles) & set(p2.subtitles)
        subs1 = [s for s in p1.subtitles if s not in shared]
        subs2 = [s for s in p2.subtitles if s not in shared]
        main1 = {p1.core, _collapse(" ".join([p1.core, *subs1])) if shared else p1.full}
        main2 = {p2.core, _collapse(" ".join([p2.core, *subs2])) if shared else p2.full}

        main = max(self._score(a, b) for a in main1 for b in main2)
        alias = max([self._score(a, b) for a in subs1 for b in main2] +
                    [self._score(a, b) for a in main1 for b in subs2], default=-math.inf)
        if main > self.threshold:
            # Both have subtitles, but different ones (e.g. two movements of a work): not the same,
            # unless one's subtitle is the other's title.
            if subs1 and subs2 and alias <= self.alias_threshold and \
                    max(self._score(a, b) for a in subs1 for b in subs2) <= self.subtitle_threshold:
                return None
            return main
        return alias if alias > self.alias_threshold else None

    def _score(self, text1: str, text2: str) -> float:
        if text1 == text2:
            return 1.0
        return float(self.compare(*self._fingerprint(text1), *self._fingerprint(text2)))

    def _fingerprint(self, text: str) -> tuple[np.ndarray, np.int64]:
        k = self._key_idx_by_text.get(text)
        if k is not None:
            return self.fps.words[k], self.fps.counts[k]
        if text not in self._extra_fps:     # Keys only built for a particular pair, e.g. without shared subtitles.
            fps = FingerprintSet(self.preprocessor, [text])
            self._extra_fps[text] = fps.words[0], fps.counts[0]
        return self._extra_fps[text]

    def split_by_exact_key(
            self, ids1: set[int], cmps: dict[int, float], ids2_by_key: dict[Any, set[int]],
    ) -> Generator[tuple[set[int], dict[int, float]], Any, None]:
        """Apply the exact key (e.g. mb_workid) to ids1 (all with the same value) and their fuzzy matches (cmps).
        Where both items have the key, it decides: equal keys match (score 1.0, even if the values differ),
        different keys don't. Yields (ids, matches) for each group of ids1 sharing a key."""
        if not self.exact_key:
            yield ids1, cmps
            return
        ids1_by_key: dict[Any, set[int]] = collections.defaultdict(set)
        for id1 in ids1:
            ids1_by_key[self.exact_key_by_id[id1]].add(id1)
        for key, ids in ids1_by_key.items():
            if key is None:
                yield ids, cmps
                continue
            part = {x: cmp for x, cmp in cmps.items() if self.exact_key_by_id[x] in (None, key)}
            labels = self.labels(next(iter(ids)))
            for x in ids2_by_key.get(key, ()):
                if self.match_versions or self.labels(x) == labels:
                    part[x] = 1.0
            yield ids, part

class MinItem(dict[str, Any]):
    """Repr of dict[str, Any]"""

    def __format__(self, format_spec: str) -> str:
        if not format_spec:
            format_spec = beets.config["format_item"].as_str()
        assert isinstance(format_spec, str)
        # template() caches the compiled Template; constructing a new one per item dominated output time.
        return template(format_spec).substitute(self.formatted())

    def formatted(self) -> dict[str, str]:
        """Get a mapping containing all values on this object formatted
        as human-readable unicode strings. Code shamelessly stolen from beets.
        """
        formatted: dict[str, str] = {}
        for k, v in self.items():
            t = TYPE_BY_FIELD.get(k) or types.DEFAULT     # .get: flex attributes and fdup_* output fields.
            value = t.format(v)
            if isinstance(value, bytes):
                value = value.decode("utf-8", "ignore")
            formatted[k] = value
        return formatted

class Match:

    def __init__(self, id1: int, id2: int, key: str, match: float, versions: frozenset[str] = frozenset()):
        self.id1 = id1
        self.id2 = id2
        self.fits = {key: match}
        self.fit = None
        self.versions: set[str] = set(versions)     # Version labels that differ between the two items.

    def with_fit(self, fieldname: str, value: float, versions: frozenset[str] = frozenset()) -> typing.Self:
        self.fits[fieldname] = value
        self.versions |= versions
        self.fit = None
        return self

    def overall_fit(self) -> float:
        if self.fit is None:
            self.fit = math.pow(prod(self.fits.values()), 1/len(self.fits))
        return self.fit

mock_counter: int = 0
def _mock_get_items() -> list[MinItem]:
    global mock_counter
    vals = [
        MinItem({"id": mock_counter + 1, "path": "1", "artist": "aaabbbcccddd1", "title": "qwertyuiop1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 2, "path": "2", "artist": "aaabbbcccddd1", "title": "qwertyuiop2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 3, "path": "3", "artist": "aaabbbcccddd1", "title": "asdfghjkl1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 4, "path": "4", "artist": "aaabbbcccddd1", "title": "asdfghjkl2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 5, "path": "5", "artist": "aaabbbcccddd2", "title": "qwertyuiop1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 6, "path": "6", "artist": "aaabbbcccddd2", "title": "qwertyuiop2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 7, "path": "7", "artist": "aaabbbcccddd2", "title": "asdfghjkl1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 8, "path": "8", "artist": "aaabbbcccddd2", "title": "asdfghjkl2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 9, "path": "9", "artist": "eeefffggghhh1", "title": "qwertyuiop1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 10, "path": "10", "artist": "eeefffggghhh1", "title": "qwertyuiop2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 11, "path": "11", "artist": "eeefffggghhh1", "title": "asdfghjkl1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 12, "path": "12", "artist": "eeefffggghhh1", "title": "asdfghjkl2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 13, "path": "13", "artist": "eeefffggghhh2", "title": "qwertyuiop1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 14, "path": "14", "artist": "eeefffggghhh2", "title": "qwertyuiop2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 15, "path": "15", "artist": "eeefffggghhh2", "title": "asdfghjkl1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 16, "path": "16", "artist": "eeefffggghhh2", "title": "asdfghjkl2", "length": 100 + mock_counter}),
    ]
    mock_counter += len(vals)
    return vals


def _quick_fetch_items(lib, fields: set[str], args=None) -> list[MinItem]:
    """Fetch desired fields from library, optionally filtered by query args."""
    # return _mock_get_items()

    # todo:
    #  - consider expanding to allow album fetches too.
    #  - or at least additionally operating on album flex fields.

    # Split the desired fields according to whether they are fixed, or flex based.
    desired_fields = fields | {"id"}    # Always return 'id'
    item_fields = Item.all_db_fields - Item.other_db_fields
    desired_fixed_fields = item_fields & desired_fields
    desired_flex_fields = desired_fields - desired_fixed_fields

    # Quick exits for queries without filtering:
    if args is None or len(args) == 0:

        # Query the database to get the desired fields.
        # If the desired fields are not just item fields, query the item flex table too.
        with (lib.transaction() as tx):
            rows = tx.query(f"SELECT {', '.join(desired_fixed_fields)} FROM items")
            flex_rows = [] if len(desired_flex_fields) == 0 else tx.query(
                f"SELECT * FROM item_attributes WHERE key IN ({", ".join(["?"] * len(desired_flex_fields))})",
                list(desired_flex_fields))
        items = _combine_fixed_and_flex(rows, flex_rows)
        return [MinItem(item) for item in items]

    # Otherwise, we have to account for the filter fields too.
    # Parse the query parts into a dbcore.Query, ignore the sort for now.
    parsed_query = parse_query_parts(args, Item)[0]
    query_fields = parsed_query.field_names
    query_fixed_fields = query_fields & item_fields
    query_flex_fields = query_fields - item_fields

    # Prepare the SQL WHERE term for the item table query.
    # Note: monkey-patching to allow non-fast query clauses to be ignored without affecting fast query clauses.
    original_method = FieldQuery.clause
    try:
        FieldQuery.clause = lambda self: self.col_clause() if self.fast else ("1", ())
        where, params = parsed_query.clause()
    finally:
        FieldQuery.clause = original_method

    # Prepare the SQL items query term.
    all_fixed_fields = desired_fixed_fields | query_fixed_fields
    sql_fixed = f"SELECT {', '.join(all_fixed_fields)} FROM items WHERE {where or 1}"
    params_fixed = params

    # Prepare the SQL flexible attributes query term (if any)
    all_flex_fields = desired_flex_fields | query_flex_fields
    needs_flex = len(all_flex_fields) > 0
    if needs_flex:
        sql_flex = (
            "SELECT * FROM item_attributes WHERE "
            f"key IN ({", ".join(["?"] * len(all_flex_fields))}) "
            f"AND entity_id IN (SELECT id FROM items WHERE {where or 1})")
        params_flex = list(all_flex_fields) + params

    # Do the actual query:
    with lib.transaction() as tx:
        rows = tx.query(sql_fixed, params_fixed)
        flex_rows = tx.query(sql_flex, params_flex) if needs_flex else []
    items = _combine_fixed_and_flex(rows, flex_rows)

    # If any of the query filters are based on flexible attributes, re-filter here (slow query)
    if len(query_flex_fields) > 0:
        items = [item for item in items if parsed_query.match(item)]

    # Remove extraneous fields, only return what is desired.
    return [MinItem({k: v for k, v in item.items() if k in desired_fields}) for item in items]

def _combine_fixed_and_flex(fixed_rows: list[sqlite3.Row], flex_rows: list[sqlite3.Row]) -> list[dict]:
    items_fixed_by_id = {row["id"]: dict(row) for row in fixed_rows}
    items_flex_by_id = collections.defaultdict(dict[str, str])
    for row in flex_rows:
        items_flex_by_id[row["entity_id"]][row["key"]] = row["value"]
    return [{**items_flex_by_id.get(id_, {}), **data} for id_, data in items_fixed_by_id.items()]

# How titles are split into a core and qualifiers (see ValueParser). Also the fallback for any key missing from a
# field's own 'qualifiers' config.
_DEFAULT_QUALIFIERS: dict[str, Any] = {
    "brackets": ["()", "[]", "{}"],         # Opening and closing character of each pair.
    "separators": [r"\s+-+\s+", r":\s+"],   # Regexes (without capturing groups); text after one is a qualifier.
    # Words marking a qualifier as a version rather than part of the title, mapped to the label they give
    # ('' marks a version without giving a label). Merged with the user's config: set a word to null to remove it.
    "version_markers": {
        "live": "live", "unplugged": "live",
        "remix": "remix", "mix": "remix", "rmx": "remix", "dub": "remix",
        "edit": "edit", "extended": "extended",
        "remaster": "remaster", "remastered": "remaster",
        "demo": "demo", "acoustic": "acoustic", "instrumental": "instrumental", "karaoke": "instrumental",
        "mono": "mono", "stereo": "stereo", "commentary": "commentary", "reprise": "reprise",
        "alternate": "alternate", "alternative": "alternate", "outtake": "alternate",
        "rehearsal": "rehearsal", "session": "session", "sessions": "session",
        "version": "", "bonus": "", "explicit": "", "feat": "", "featuring": "", "ft": "",
    },
    "version_marker_patterns": {r"\b(19|20)\d\d\b": ""},  # Regex -> label, as for version_markers.
    "alias_threshold": 0.9,         # A subtitle alone must match the other's title this well (alternate titles).
    "subtitle_threshold": 0.75,     # If both have subtitles, at least one pair must match this well.
    "check_numbers": True,          # Differing part/movement numbers (part 2 vs part 3) mean different tracks.
    "match_versions": True,         # Match different versions (e.g. live vs studio), labelled; false: don't.
}

class FDupPlugin(BeetsPlugin):
    """List duplicate tracks based on fuzzy matching of artist and title."""

    def __init__(self):
        super().__init__("fdup")

        self.config.add({
            "query_separator": "~~~",
            "fields": ["artist", "title"],
            "group_results": False,
            "default_field_config": {
                "alphabet": "abcdefghijklmnopqrstuvwxyz 0123456789",
                "word_size": 1536,
                "match_threshold": 0.75,
                "difference_penalty": 0.025, # Kept low e.g. < 0.05 (additional characters don't necessarily matter much, can include qualifying info e.g. (live) or whatever)
                # 'overlap': shared n-grams relative to the shorter value, so 'abba' matches 'abba & friends'.
                # 'dice': relative to both values, so a short value doesn't match a longer one just by being in it.
                "metric": "overlap",
                # [regex, replacement] pairs, applied in order after accents are removed and text is lower-cased.
                # extra_normalise is applied afterwards: add rules there to keep these defaults.
                "normalise": [
                    [r"\s*&\s*", " and "],
                    [r"(?<=\b\w)\.(?=\w\b)", ""],   # Dots in initialisms: s.o.s. -> sos
                ],
                "extra_normalise": [],
                "qualifiers": None,     # How to split values into core and qualifiers, null: don't. See _DEFAULT_QUALIFIERS.
                # An item field which, when both items have it, decides the match: equal values match, different don't.
                "exact_key": None,
            },
            "field_configs": {
                "title": {
                    "metric": "dice",
                    "exact_key": "mb_workid",
                    "qualifiers": _DEFAULT_QUALIFIERS,
                },
            },
            "output": {
                "separator": "\n",
                "src_fmt": "*** $artist - $title ($length) - $path\n",
                # Also available here: $fdup_score (overall match score), $fdup_versions (differing version labels).
                "dup_fmt": " |_ $artist - $title ($length)$fdup_versions - $path\n"
            }
        })

        # Configure logging format with timestamp
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        self._log.handlers.clear()  # Remove default handlers
        self._log.addHandler(handler)
        self._log.propagate = False  # Prevent duplicate logging

    def commands(self):
        _command1 = Subcommand("fdup", help=__doc__)
        _command1.func = self._fdup
        _command1.parser.add_option(
            "--match-threshold",    # todo: split artist and title match thresholds.
            dest="match_threshold",
            type="float",
            help="Overrides the fuzzy-match threshold for this run (1.0 == perfect match, 0.75 recommended)",
        )
        _command1.parser.add_option("--against-all",
            action="store_true",
            help="Find duplicates of query results within the whole library, not just within the query results",
        )
        _command1.parser.add_option("--group-results",
            action="store_true",
            help="Group the results",
        )
        _command2 = Subcommand("fdup-prof", help=__doc__)
        _command2.parser.add_option("--against-all",
            action="store_true",
            help="Find duplicates of query results within the whole library, not just within the query results",
        )
        _command2.func = self._fdup_profile
        return [_command1, _command2]

    def _fdup_profile(self, lib, opts, args):
        pr = cProfile.Profile()
        pr.enable()

        try:
            # your existing plugin logic
            self._fdup(lib, opts, args)
        finally:
            pr.disable()

            s = io.StringIO()
            ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
            ps.print_stats("beetsplug/fdup.py")
            ps.print_stats(200)
            ps.print_callees(50)
            ps.print_callers(50)
            print(s.getvalue())
            
    def _fdup(self, lib: Library, opts: Values, args: list[str]):

        # Set up: config, caches, etc.
        self._set_up(opts)

        # Fetch minimal information about the items for both sets 1 and 2.
        item_by_id, set1, set2 = self._fetch_items(lib, opts, args)

        # Normalise, parse and fingerprint each distinct value of each field once, up front.
        self._index_items(item_by_id)

        # Match items 1 against items 2.
        self._log.info("Finding matches...")
        matches_generator = self._find_matches2(item_by_id, set1, set2)
        if not self.config["group_results"].get(bool):
            self._print_matches(item_by_id, matches_generator)
        else:
            self._group_matches(matches_generator, item_by_id, set1, set2)

    def _set_up(self, opts: Values):

        # Override config values with command line args where present and relevant.
        self.config.set_args(opts)

        # Command line overrides, applied to every field.
        cli_threshold: float | None = getattr(opts, "match_threshold", None)

        # Set up a matcher per field, from its own config or the defaults:
        self._fieldnames: list[str] = self.config["fields"].get()
        self._fields: list[FieldMatcher] = []
        for fieldname in self._fieldnames:
            self._log.info(f"Setting up fingerprinting for field: {fieldname}")
            config = functools.partial(self._field_config, fieldname)
            word_size = config("word_size").get(int)
            threshold = config("match_threshold").as_number() if cli_threshold is None else cli_threshold
            metric = config("metric").as_choice(["overlap", "dice"])
            qualifiers = self._qualifiers_config(config("qualifiers"))
            # (partial binds the current values; a lambda would see the last loop iteration's values instead).
            compare = functools.partial(
                _compare_fps, inv_word_size=1.0 / word_size, diff_pen=config("difference_penalty").as_number(),
                use_min=metric == "overlap")
            self._fields.append(FieldMatcher(
                fieldname,
                TextPreprocessor(alphabet=config("alphabet").get(str), word_size=word_size),
                ValueParser(self._normalise_rules(config), qualifiers),
                compare, threshold, config("exact_key").get(), qualifiers))

    def _field_config(self, fieldname: str, key: str) -> confuse.ConfigView:
        """The field's own setting for key if it has one, otherwise the default."""
        view = self.config["field_configs"][fieldname][key]
        return view if view.exists() else self.config["default_field_config"][key]

    @staticmethod
    def _normalise_rules(config: typing.Callable[[str], confuse.ConfigView]) -> list[tuple[str, str]]:
        rules = config("normalise").get(list) + config("extra_normalise").get(list)
        if not all(isinstance(rule, list) and len(rule) == 2 for rule in rules):
            raise UserError("fdup: normalise rules must be [regex, replacement] pairs")
        return [(str(pattern), str(replacement)) for pattern, replacement in rules]

    @staticmethod
    def _qualifiers_config(view: confuse.ConfigView) -> dict[str, Any] | None:
        if view.get() is None:
            return None
        def get(key: str) -> Any:
            if not view[key].exists():
                return _DEFAULT_QUALIFIERS[key]
            return dict(view[key].flatten()) if isinstance(_DEFAULT_QUALIFIERS[key], dict) else view[key].get()
        qualifiers = {key: get(key) for key in _DEFAULT_QUALIFIERS}
        # Drop entries set to null (that's how to remove a default), and match case with normalised text.
        for key in ("version_markers", "version_marker_patterns"):
            qualifiers[key] = {str(k): str(label) for k, label in qualifiers[key].items() if label is not None}
        qualifiers["version_markers"] = {w.casefold(): label for w, label in qualifiers["version_markers"].items()}
        if any(len(pair) != 2 for pair in qualifiers["brackets"]):
            raise UserError("fdup: each qualifiers.brackets entry must be an opening and closing character, e.g. '()'")
        return qualifiers

    def _fetch_items(self, lib: Library, opts: Values, args: list[str]) -> tuple[dict[int, MinItem], set[int], set[int]]:

        # Optionally, split the query into 2 queries for subsets 'A' and 'B' if the separator is detected.
        args1, args2 = self._split_query_args(args, opts.against_all)
        duplicate_sets: bool = args1 == args2

        # Fetch minimal information about the items for both sets A and B.
        self._log.info("Loading items from database...")
        fields = {"id", "path", "artist", "title", "length"} | set(self._fieldnames) | {
            field.exact_key for field in self._fields if field.exact_key}
        items1 = _quick_fetch_items(lib, fields, args1)
        self._log.info(f"  ... items loaded for source set: {len(items1)} ({args1 if args1 else 'all items in library'})")
        items2 = _quick_fetch_items(lib, fields, args2) if not duplicate_sets else items1
        self._log.info(f"  ... items loaded for comparison set: {len(items2)} ({args2 if args2 else 'all items in library'})")

        # Reformat as into item_by_id, set1, set2
        item_by_id: dict[int, MinItem] = dict()
        set1: set[int] = set([x["id"] for x in items1])
        set2: set[int] = set([x["id"] for x in items2])
        for item in items1: item_by_id[item["id"]] = item
        for item in items2: item_by_id[item["id"]] = item

        return item_by_id, set1, set2

    def _index_items(self, item_by_id: dict[int, MinItem]):
        self._log.info("Fingerprinting...")
        for field in self._fields:
            field.index_items(item_by_id)

    @line_profiler.profile
    def _find_matches2(
            self,
            item_by_id: dict[int, MinItem],
            set1: set[int],
            set2: set[int],
            fieldname_idx: int = 0,
    ) -> Generator[tuple[int, list[Match]], Any, None]:
        """"""
        field = self._fields[fieldname_idx]
        fieldname = field.name

        # Aggregate by (normalised) fieldname value, as an index into the field's values.
        ids_by_value_1: dict[int, set[int]] = collections.defaultdict(set[int])
        ids_by_value_2: dict[int, set[int]] = collections.defaultdict(set[int])
        for id in set1: ids_by_value_1[field.value_idx_by_id[id]].add(id)
        for id in set2: ids_by_value_2[field.value_idx_by_id[id]].add(id)

        # Ids in set2 by their exact key (e.g. mb_workid), if the field has one.
        ids2_by_key: dict[Any, set[int]] = collections.defaultdict(set[int])
        if field.exact_key:
            for id in set2:
                if (key := field.exact_key_by_id[id]) is not None:
                    ids2_by_key[key].add(id)

        # Pull out various views of the sets, as arrays of value indices:
        def as_array(values: typing.Collection[int]) -> np.ndarray:
            return np.fromiter(values, dtype=np.intp, count=len(values))
        common_values = as_array(ids_by_value_1.keys() & ids_by_value_2.keys())
        set1only_values = as_array(ids_by_value_1.keys() - ids_by_value_2.keys())
        set2only_values = as_array(ids_by_value_2.keys() - ids_by_value_1.keys())

        # Find all matching pairs of values:
        #   - common values against each other (each pair compared once, recorded both ways),
        #   - common values against values only in set2,
        #   - values only in set1 against all values in set2.
        matched_values: dict[int, dict[int, float]] = collections.defaultdict(dict)
        for value1, value2, cmp in field.matching_values(common_values):
            matched_values[value1][value2] = cmp
            matched_values[value2][value1] = cmp
        for value1, value2, cmp in field.matching_values(common_values, set2only_values):
            matched_values[value1][value2] = cmp
        if len(set1only_values) > 0:
            all_values2 = as_array(ids_by_value_2.keys())
            for value1, value2, cmp in field.matching_values(set1only_values, all_values2):
                matched_values[value1][value2] = cmp

        # Loop over set1 values:
        for value1, ids1_all in ids_by_value_1.items():

            # Collect all ids in set2 whose value matches value1, along with their match value.
            # Ids with exactly the same value are a perfect match.
            cmps_all: dict[int, float] = dict.fromkeys(ids_by_value_2.get(value1, ()), 1.0)
            for value2, cmp in matched_values.get(value1, {}).items():
                cmps_all.update(dict.fromkeys(ids_by_value_2[value2], cmp))

            # Where both items have an exact key, it overrides the fuzzy match (see split_by_exact_key).
            for ids1, cmps in field.split_by_exact_key(ids1_all, cmps_all, ids2_by_key):
                matching_ids = cmps.keys()

                # If no matches, skip.
                if len(matching_ids) == 0:
                    continue

                # If ids1 and matching_ids are the same single id, skip; we would remove this before returning anyway.
                if len(ids1) == 1 and len(matching_ids) == 1 and ids1 == matching_ids:
                    continue

                # All items in ids1 therefore match with all matching_ids.
                # If we've reached the end of the fieldnames, we can yield all these results.
                if fieldname_idx == len(self._fields) - 1:
                    for id1 in ids1:
                        # Remove matches of the item with itself.
                        matches = [Match(id1, x, fieldname, cmp, field.versions(id1, x))
                                   for x, cmp in cmps.items() if x != id1]
                        if len(matches) > 0:
                            yield id1, matches
                    continue

                # Otherwise, filter these results further by recursively calling this function with the next fieldname.
                for id1, matches in self._find_matches2(item_by_id, ids1, set(matching_ids), fieldname_idx + 1):
                    for match in matches:
                        match.with_fit(fieldname, cmps[match.id2], field.versions(match.id1, match.id2))
                    yield id1, matches

    def _print_matches(self, item_by_id: dict[int, MinItem], matches: Generator[tuple[int, list[Match]], Any, None]):
        match_sep = self.config["output"]["separator"].get(str)
        source_fmt = self.config["output"]["src_fmt"].get(str)
        dup_fmt = self.config["output"]["dup_fmt"].get(str)
        n_matches = 0
        for id1, matches in matches:
            item1 = item_by_id[id1]
            print_(format(item1, source_fmt), end='')
            for match in matches:
                item2 = MinItem({
                    **item_by_id[match.id2],
                    "fdup_score": f"{match.overall_fit():.2f}",
                    "fdup_versions": f" [{', '.join(sorted(match.versions))}]" if match.versions else "",
                })
                print_(format(item2, dup_fmt), end='')
            print_(match_sep, end='')
            n_matches += len(matches)
        self._log.info(f"  ... found {n_matches} duplicate pairs")

    def _group_matches(
            self,
            matches_by_id1: Generator[tuple[int, list[Match]], Any, None],
            items_by_id: dict[int, list[MinItem]],
            set1: set[int],
            set2: set[int]
    ):
        self._log.info("Grouping matches...")

        def edge_id(id1: int, id2: int) -> tuple[int, int]:
            return (id1, id2) if id1 < id2 else (id2, id1)

        class Edge:
            id: tuple[int, int]
            weight: float

            def __init__(self, id: tuple[int, int], weight: float):
                self.id = id
                self.weight = weight

        class Node:
            id: int
            # outgoing: list[tuple[int, float]]
            # incoming: list[tuple[int, float]]
            conn_abc: dict[str, set[int]]
            weight_by_id: dict[int, float]
            abc: str

            def __init__(self, id: int, abc: str):
                self.id = id
                self.abc = abc
                self.conn_abc = collections.defaultdict(set[int])
                self.weight_by_id = {}
                # self.incoming = []
                # self.outgoing = []

        class Group:
            ids: set[int]
            ids_abc: dict[str, set[int]]      # ids in group indexed by whether in subset a b or c
            valid_abc: dict[str, set[int]]    # ids connected to all ids in each ids_abc
            potentials: dict[int, float]      # ids that could be added along with their minimum weight.

            def __init__(self):
                self.ids = set()
                self.ids_abc = collections.defaultdict(set[int])
                self.valid_abc = collections.defaultdict(set[int])
                self.potentials = collections.defaultdict(float)

            def calc_weight(self, node: Node) -> tuple[int, float]:
                min_id: int = 0
                min_weight = float('inf')
                for _, conns in node.conn_abc.items():
                    for id in conns:
                        if id in self.ids and node.weight_by_id[id] < min_weight:
                            min_id = id
                            min_weight = node.weight_by_id[id]
                return min_id, min_weight

            def _init_conn_abc(self, node: Node, abc: str):
                self.valid_abc[abc] = node.conn_abc[abc].copy()
                self.valid_abc[abc].difference_update(self.ids_abc[abc]) # Ensure we don't already have those ids.
                self.potentials.update({x: node.weight_by_id[x] for x in self.valid_abc[abc]})

            def _update_conn_abc(self, node: Node, abc: str):
                # Update min_weight for valid nodes also connected to new node.
                # Remove potential entirely for previously valid nodes that aren't connected to new node.
                # print_(f"xyzzy: updating conn_abc for {abc}")
                for id in self.valid_abc[abc]:
                    if id in node.conn_abc[abc]:
                        # print_(f"xyzzy: maybe updating potential for {id}")
                        if node.weight_by_id[id] < self.potentials[id]:
                            # print_(f"xyzzy: updating potential for {id} to {node.weight_by_id[id]}")
                            self.potentials[id] = node.weight_by_id[id]
                    else:
                        # print_(f"xyzzy: removing potential for {id}")
                        self.potentials.pop(id, None)
                self.valid_abc[abc] &= node.conn_abc[abc]

            def add_node(self, node: Node):

                # Add node id:
                # print_(f"xyzzy: adding node {node.id} to group; BEFORE: ids: {self.ids}, ids_abc: {self.ids_abc}, valid_abc: {self.valid_abc}, potentials: {self.potentials}")
                self.ids.add(node.id)
                self.ids_abc[node.abc].add(node.id)

                # If new node is 'b' or 'c', update valid 'a' nodes; initialise if this is first 'b' or 'c' node.
                if node.abc != 'a':
                    if len(self.ids_abc['b']) + len(self.ids_abc['c']) == 1:
                        self._init_conn_abc(node, 'a')
                    else:
                        self._update_conn_abc(node, 'a')

                # Always update valid 'b' nodes; initialise if this is first node in group.
                if len(self.ids) == 1:
                    self._init_conn_abc(node, 'b')
                else:
                    self._update_conn_abc(node, 'b')

                # If new node is 'a' or 'b', update valid 'c' nodes; initialise if this is first 'a' or 'b' node.
                if node.abc != 'c':
                    if len(self.ids_abc['a']) + len(self.ids_abc['b']) == 1:
                        self._init_conn_abc(node, 'c')
                    else:
                        self._update_conn_abc(node, 'c')

                # Remove the node from the valid groups (if still present)
                self.valid_abc[node.abc].discard(node.id)
                self.potentials.pop(node.id, None)
                self.potentials = dict(sorted(self.potentials.items(), key=lambda x: x[1]))
                # print_(f"xyzzy: adding node {node.id} to group; AFTER: ids: {self.ids}, ids_abc: {self.ids_abc}, valid_abc: {self.valid_abc}, potentials: {self.potentials}")


        # Loop over matches: turn into nodes and edges:
        edges_by_ids: dict[tuple[int, int], Edge] = {}
        nodes_by_id: dict[int, Node] = {}
        sets: dict[str, set[int]] = collections.defaultdict(set[int])
        for id1, matches in matches_by_id1:
            for x in matches:

                # Create edge and nodes if they don't already exist
                eid = edge_id(id1, x.id2)
                weight = x.overall_fit()
                edge = Edge(eid, weight)
                node1 = nodes_by_id.get(x.id1, None) or Node(x.id1, 'c' if x.id1 not in set1 else 'b' if x.id1 in set2 else 'a')
                node2 = nodes_by_id.get(x.id2, None) or Node(x.id2, 'c' if x.id2 not in set1 else 'b' if x.id2 in set2 else 'a')

                # Add edge to nodes.
                node1.conn_abc[node2.abc].add(x.id2)
                node2.conn_abc[node1.abc].add(x.id1)
                node1.weight_by_id[x.id2] = weight
                node2.weight_by_id[x.id1] = weight

                # Aggregate.
                edges_by_ids[eid] = edge
                nodes_by_id[x.id1] = node1
                nodes_by_id[x.id2] = node2
                sets[node1.abc].add(node1.id)
                sets[node2.abc].add(node2.id)

        # Sort edges by weight:
        edges_by_ids = dict(sorted(edges_by_ids.items(), key=lambda x: x[1].weight))
        edges_remaining = edges_by_ids.copy()

        # Recursively create groups.
        groups: list[Group] = []
        while len(edges_remaining) > 0:

            # Pop the largest remaining edge:
            eid, edge = edges_remaining.popitem()
            # self._log.info(f" xyzzy: processing edge: {eid} with weight {edge.weight} (remaining edges: {len(edges_remaining)})")

            # Create a new group with the nodes from this edge.
            group = Group()
            group.add_node(nodes_by_id[edge.id[0]])
            group.add_node(nodes_by_id[edge.id[1]])

            # Find which ids (if any) can be added to the group.
            while True:

                # If no nodes can be added, group is complete, move on.
                if len(group.potentials) == 0:
                    break

                # Otherwise, find best potential node and to add to group.
                best_id, best_weight = group.potentials.popitem()
                # best_id: int = 0
                # best_weight: float = float('inf')
                # for potential in potentials:
                #     match_id, match_weight = group.calc_weight(nodes_by_id[potential])
                #     if match_weight < best_weight:
                #         best_weight = match_weight
                #         best_id = potential
                to_add = nodes_by_id[best_id]
                # self._log.info(f"  xyzzy: best_id: {best_id}, best_weight: {best_weight}")

                # Add the new node to the group, and any edges in edges_remaining which are used can be removed.
                for _, conns in to_add.conn_abc.items():
                    for id in conns:
                        if id in group.ids:
                            edges_remaining.pop(edge_id(to_add.id, id), None)
                group.add_node(to_add)

            # Aggregate:
            # self._log.info(f"  ... created group with {len(group.ids)} ids, {len(edges_remaining)} edges remaining")
            groups.append(group)

        # Grouping complete!
        self._log.info(f"  ... grouping finished! Created {len(groups)} groups")

        # Print groups:
        for group in sorted(groups, key=lambda x: len(x.ids), reverse=True):
            for _id in group.ids_abc['a']:
                print_(format(items_by_id[_id], " --< $artist - $title ($length) - $path "))
            for _id in group.ids_abc['b']:
                print_(format(items_by_id[_id], " --- $artist - $title ($length) - $path "))
            for _id in group.ids_abc['c']:
                print_(format(items_by_id[_id], " --> $artist - $title ($length) - $path "))
            print_("")


    def _split_query_args(self, args: list[str], against_all: bool) -> tuple[list[str] | None, list[str] | None]:
        if not args:
            self._log.info("Looking for all duplicates within entire library")
            return None, None
        try:
            separator = self.config["query_separator"].get(str)
            separator_idx = args.index(separator)
            if against_all:
                self._log.error(f"Cannot use `--against-all` with a second query (queries are separated by '{separator}').")
                raise UserError(f"Cannot use `--against-all` with a second query (queries are separated by '{separator}').")
            args1, args2 = args[:separator_idx], args[separator_idx + 1:]
            self._log.info(f"Looking for duplicates within B of items in A, where...\n\tA: {args1}\n\tB: {args2}")
            return args1, args2
        except ValueError:
            if against_all:
                self._log.info(f"Looking for duplicates within entire library for any item matching: {args}")
                return args, None
            self._log.info(f"Looking for duplicates within query results of: {args}")
            return args, args
