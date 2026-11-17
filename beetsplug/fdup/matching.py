"""Matching items: per-field fuzzy matching of values (FieldMatcher), and combining fields into item matches."""

import collections
import math
from collections.abc import Callable, Collection, Generator
from typing import Any, Self

import numpy as np

from .fingerprint import FingerprintSet, TextPreprocessor, matching_pairs
from .items import MinItem
from .parsing import ParsedValue, ValueParser, collapse


class FieldMatcher:
    """Fuzzy matching for one field: its settings and, once index_items() has run, the distinct (normalised) values
    of the loaded items, parsed, with fingerprints of each value's match keys:
      - main keys: the core and the full value (just the value itself if the field isn't split),
      - alias keys: each subtitle on its own, so e.g. 'con te partiro' matches 'time to say goodbye (con te partiro)'.
    Candidate pairs come from comparing keys in bulk (numpy); for split fields each candidate pair is then checked
    properly by _decide()."""

    def __init__(
            self, name: str, preprocessor: TextPreprocessor, parser: ValueParser,
            compare: Callable[..., np.ndarray], threshold: float,
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

    def index_items(self, item_by_id: dict[int, MinItem]):
        normalised: dict[str, str] = {}
        index_by_value: dict[str, int] = {}
        self.value_idx_by_id: dict[int, int] = {}
        for id_, item in item_by_id.items():
            raw = item[self.name] or ""     # E.g. None: a flexible attribute the item doesn't have.
            if raw not in normalised:
                normalised[raw] = self.parser.normalise(raw)
            self.value_idx_by_id[id_] = index_by_value.setdefault(normalised[raw], len(index_by_value))
        # The empty value's index, if any item has it (e.g. no title, or a value a normalise rule removed entirely).
        # It matches nothing: see find_matches.
        self.empty_value: int | None = index_by_value.get("")
        self.parsed: list[ParsedValue] = [self.parser.parse(value) for value in index_by_value]

        key_idx_by_text: dict[str, int] = {}

        def key(text: str) -> int:
            return key_idx_by_text.setdefault(text, len(key_idx_by_text))

        self.main_keys: list[list[int]] = [sorted({key(p.core), key(p.full)}) for p in self.parsed]
        self.alias_keys: list[list[int]] = [sorted({key(s) for s in p.subtitles}) for p in self.parsed]
        self.fps = FingerprintSet(self.preprocessor, key_idx_by_text)
        # Keys with nothing to fingerprint (e.g. only punctuation left by a normalise rule) would score 1.0 against each
        # other: leave them out of fuzzy matching. (Items with the same non-empty value still match.)
        empty = set(np.flatnonzero(self.fps.counts == 0).tolist())
        if empty:
            self.main_keys = [[k for k in keys if k not in empty] for keys in self.main_keys]
            self.alias_keys = [[k for k in keys if k not in empty] for keys in self.alias_keys]
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
        pairs = matching_pairs(self.fps, self.compare, threshold, row_keys, None if triangular else col_keys)
        for i, j, score in pairs:
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
        main1 = {p1.core, collapse(" ".join([p1.core, *subs1])) if shared else p1.full}
        main2 = {p2.core, collapse(" ".join([p2.core, *subs2])) if shared else p2.full}

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


class Match:
    """A match between two items: the score for each field so far, and the version labels that differ."""

    def __init__(self, id1: int, id2: int, key: str, match: float, versions: frozenset[str] = frozenset()):
        self.id1 = id1
        self.id2 = id2
        self.fits = {key: match}
        self.fit = None
        self.versions: set[str] = set(versions)     # Version labels that differ between the two items.

    def with_fit(self, fieldname: str, value: float, versions: frozenset[str] = frozenset()) -> Self:
        self.fits[fieldname] = value
        self.versions |= versions
        self.fit = None
        return self

    def overall_fit(self) -> float:
        if self.fit is None:
            self.fit = math.pow(math.prod(self.fits.values()), 1/len(self.fits))
        return self.fit


def index_items(fields: list[FieldMatcher], item_by_id: dict[int, MinItem]):
    """Normalise, parse and fingerprint each distinct value of each field once, up front."""
    for field in fields:
        field.index_items(item_by_id)


def version_labels(fields: list[FieldMatcher], id_: int) -> frozenset[str]:
    """An indexed item's version labels, from all fields, e.g. {'live'} for the title 'song (live)'."""
    return frozenset().union(*(field.labels(id_) for field in fields))


def find_matches(
        fields: list[FieldMatcher],
        set1: set[int],
        set2: set[int],
        field_idx: int = 0,
) -> Generator[tuple[int, list[Match]], Any, None]:
    """Match the items in set1 against those in set2 (ids of indexed items, see index_items), where every field
    matches. Yields (id1, [matches of item id1]) for each item in set1 with any matches, other than itself.

    Matches on the first field, then recurses to filter each group of matches by the next field."""
    field = fields[field_idx]
    fieldname = field.name

    # Aggregate by (normalised) fieldname value, as an index into the field's values.
    ids_by_value_1: dict[int, set[int]] = collections.defaultdict(set[int])
    ids_by_value_2: dict[int, set[int]] = collections.defaultdict(set[int])
    for id_ in set1:
        ids_by_value_1[field.value_idx_by_id[id_]].add(id_)
    for id_ in set2:
        ids_by_value_2[field.value_idx_by_id[id_]].add(id_)
    # An empty value (e.g. no title) says nothing about which track it is, so items with one match nothing. Otherwise
    # every untagged track would be a duplicate of every other.
    ids_by_value_1.pop(field.empty_value, None)
    ids_by_value_2.pop(field.empty_value, None)

    # Ids in set2 by their exact key (e.g. mb_workid), if the field has one.
    ids2_by_key: dict[Any, set[int]] = collections.defaultdict(set[int])
    if field.exact_key:
        for id_ in set2:
            if field.value_idx_by_id[id_] != field.empty_value and (key := field.exact_key_by_id[id_]) is not None:
                ids2_by_key[key].add(id_)

    # Pull out various views of the sets, as arrays of value indices:
    def as_array(values: Collection[int]) -> np.ndarray:
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
            if field_idx == len(fields) - 1:
                for id1 in ids1:
                    # Remove matches of the item with itself.
                    matches = [Match(id1, x, fieldname, cmp, field.versions(id1, x))
                               for x, cmp in cmps.items() if x != id1]
                    if len(matches) > 0:
                        yield id1, matches
                continue

            # Otherwise, filter these results further by recursively calling this function with the next fieldname.
            for id1, matches in find_matches(fields, ids1, set(matching_ids), field_idx + 1):
                for match in matches:
                    match.with_fit(fieldname, cmps[match.id2], field.versions(match.id1, match.id2))
                yield id1, matches
