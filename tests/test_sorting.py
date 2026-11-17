"""Tests for sort terms and sorting by them."""

import beets
import pytest
from beets.ui import UserError

from beetsplug.fdup.sorting import SortTerm, item_value, parse_sort, query_sort, sort_by, sort_fields


def sorted_items(items: list[dict], spec: str) -> list[dict]:
    return sort_by(items, parse_sort(spec, "test"), item_value)


@pytest.mark.parametrize("spec", ["artist+ year-", ["artist+", "year-"]])
def test_parse_sort(spec):
    assert parse_sort(spec, "test") == (SortTerm("artist", True), SortTerm("year", False))


@pytest.mark.parametrize("spec", [None, "", []])
def test_no_sort(spec):
    assert parse_sort(spec, "test") == ()


@pytest.mark.parametrize(("spec", "message"), [
    ("artist", "isn't a sort term"),        # no direction
    ("year:2000+", "isn't a sort term"),
    ("filesize-", "computed field"),        # a getter: fetch_items can't provide it
])
def test_bad_sort_is_an_error(spec, message):
    with pytest.raises(UserError, match=message):
        parse_sort(spec, "test")


def test_query_sort_takes_only_the_sort_terms():
    assert query_sort(["artist:abba", "year+", "~~~", "title-", "path:/a/b"]) == (
        SortTerm("year", True), SortTerm("title", False))
    assert query_sort(None) == ()


def test_sort_fields():
    # fdup's own values aren't fetched; artist sorts by artist_sort where it's set, so that's fetched too.
    assert sort_fields(parse_sort("fdup_size- artist+ year-", "test")) == {"artist", "artist_sort", "year"}


def test_later_terms_break_ties_and_each_has_its_own_direction():
    items = [{"id": 1, "artist": "B", "year": 1990}, {"id": 2, "artist": "A", "year": 1980},
             {"id": 3, "artist": "B", "year": 2000}, {"id": 4, "artist": "A", "year": 1985}]
    assert [x["id"] for x in sorted_items(items, "artist+ year-")] == [4, 2, 3, 1]
    assert [x["id"] for x in sorted_items(items, "artist- year+")] == [1, 3, 2, 4]


def test_ties_keep_their_order():
    items = [{"id": i, "artist": "A"} for i in (3, 1, 2)]
    assert [x["id"] for x in sorted_items(items, "artist+")] == [3, 1, 2]
    assert [x["id"] for x in sorted_items(items, "artist-")] == [3, 1, 2]


def test_missing_values_sort_lowest():
    items = [{"id": 1, "mood": "happy"}, {"id": 2}, {"id": 3, "mood": None}, {"id": 4, "mood": "angry"}]
    assert [x["id"] for x in sorted_items(items, "mood+")] == [2, 3, 4, 1]
    assert [x["id"] for x in sorted_items(items, "mood-")] == [1, 4, 2, 3]


def test_mixed_types_and_paths():
    # A flexible attribute can hold numbers for some items and strings for others; paths are bytes.
    items = [{"id": 1, "x": "a"}, {"id": 2, "x": 5}, {"id": 3, "x": b"/music/b"}, {"id": 4, "x": 2.5}]
    assert [x["id"] for x in sorted_items(items, "x+")] == [4, 2, 3, 1]


def test_case_insensitive_unless_configured():
    items = [{"id": 1, "title": "b"}, {"id": 2, "title": "C"}, {"id": 3, "title": "a"}]
    assert [x["id"] for x in sorted_items(items, "title+")] == [3, 1, 2]
    beets.config["sort_case_insensitive"].set(False)
    assert [x["id"] for x in sorted_items(items, "title+")] == [2, 3, 1]


def test_artist_sorts_by_artist_sort_where_set():
    items = [{"id": 1, "artist": "Queen", "artist_sort": ""},
             {"id": 2, "artist": "The Beatles", "artist_sort": "Beatles, The"},
             {"id": 3, "artist": "ABBA", "artist_sort": ""}]
    assert [x["id"] for x in sorted_items(items, "artist+")] == [3, 2, 1]
    # Other fields don't.
    items = [{**item, "title": item["artist"], "title_sort": item["artist_sort"]} for item in items]
    assert [x["id"] for x in sorted_items(items, "title+")] == [3, 1, 2]
