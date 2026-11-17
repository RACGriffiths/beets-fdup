"""Tests for fetch_items: it should find the same items, with the same values, as beets' own lib.items().

These guard the parts of fdup that rely on beets internals: if a beets upgrade changes them, these should fail."""

import pytest

from beetsplug.fdup.items import MinItem, fetch_items, template_fields

FIELDS = {"id", "path", "artist", "title", "length", "year", "mood", "mb_workid"}


@pytest.fixture
def env(beets_env):
    beets_env.add_item(artist="ABBA", title="Waterloo", year=1974, length=170.5, mood="happy", mb_workid="w1")
    beets_env.add_item(artist="ABBA", title="SOS", year=1975)
    beets_env.add_item(artist="Queen", title="Bohemian Rhapsody", year=1975, mood="epic")
    beets_env.add_item(artist="Queen", title="Under Pressure", year=1981, mood="happy")
    album = beets_env.add_album(artist="Wings", title="Jet", year=1973)
    album.artpath = b"/covers/jet.jpg"
    album.store()
    return beets_env


@pytest.mark.parametrize("query", [
    [],                                 # Everything.
    ["artist:abba"],                    # A fixed field: filtered in SQL.
    ["year:1974..1975"],
    ["mood:happy"],                     # A flexible attribute: filtered in Python.
    ["artist:queen", "mood:happy"],     # Both.
    ["^mood:happy"],                    # A negated flexible attribute.
    ["artist:abba", ",", "mood:epic"],  # An OR including a flexible attribute.
    ["singleton:false"],                # A computed field: needs full Items.
    ["artpath::jet"],                   # An album-only field: needs a join with the albums table.
    ["path::Waterloo"],
])
def test_same_items_and_values_as_lib_items(env, query):
    expected = {item.id: {f: item.get(f) for f in FIELDS} for item in env.lib.items(query)}
    got = {item["id"]: dict(item) for item in fetch_items(env.lib, FIELDS, query)}
    assert expected     # (Otherwise this tests nothing.)
    assert got == expected


def test_paths_are_absolute(env):
    assert all(item["path"].startswith(env.lib.directory) for item in fetch_items(env.lib, {"path"}))


def test_min_item_formats_like_an_item(env):
    item = env.lib.items("title:waterloo").get()
    min_item = MinItem({f: item.get(f) for f in FIELDS})
    fmt = "$artist - $title ($length) $year $mood $path %upper{$title} %if{$mood,[$mood]}"
    assert format(min_item, fmt) == format(item, fmt)


def test_template_fields():
    # Fields in function arguments count; fdup's own values and computed fields (fetch_items can't get those) don't.
    fmt = "$artist - %if{$album,$album ($year)} %lower{$title}$fdup_labels $fdup_score $filesize"
    assert template_fields(fmt) == {"artist", "album", "year", "title"}
