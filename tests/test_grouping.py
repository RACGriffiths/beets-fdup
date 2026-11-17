"""Tests for grouping pairwise matches."""

import collections

from beetsplug.fdup.grouping import group_matches
from beetsplug.fdup.matching import Match


def groups_of(scores: dict[tuple[int, int], float], set1: set[int], set2: set[int]) -> list[set[int]]:
    """Group matches given as {(id1, id2): score}, as find_matches would yield them (i.e. both ways round where
    both items are in both sets); return the groups' ids."""
    matches_by_id1: dict[int, list[Match]] = collections.defaultdict(list)
    for (id1, id2), score in scores.items():
        for a, b in ((id1, id2), (id2, id1)):
            if a in set1 and b in set2:
                matches_by_id1[a].append(Match(a, b, "title", score))
    return [group.ids for group in group_matches(matches_by_id1.items(), set1, set2)]


def test_no_matches_no_groups():
    assert groups_of({}, {1, 2}, {1, 2}) == []


def test_items_that_all_match_form_one_group():
    ids = {1, 2, 3}
    assert groups_of({(1, 2): 0.9, (2, 3): 0.8, (1, 3): 0.85}, ids, ids) == [{1, 2, 3}]


def test_a_chain_is_not_one_group():
    # 1 and 3 both match 2, but not each other: two overlapping groups, the stronger match first.
    ids = {1, 2, 3}
    assert groups_of({(1, 2): 0.9, (2, 3): 0.8}, ids, ids) == [{1, 2}, {2, 3}]


def test_items_on_the_same_side_need_not_match():
    # 2 and 3 were never compared (both only in set 2), so matching 1 is enough for them to be grouped.
    assert groups_of({(1, 2): 0.9, (1, 3): 0.8}, {1}, {2, 3}) == [{1, 2, 3}]


def test_separate_groups():
    ids = {1, 2, 3, 4}
    assert groups_of({(1, 2): 0.9, (3, 4): 0.8}, ids, ids) == [{1, 2}, {3, 4}]
