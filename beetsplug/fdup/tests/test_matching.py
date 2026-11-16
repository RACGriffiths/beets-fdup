"""Tests for value parsing (normalise rules, qualifiers, version labels) and the matching rules built on it."""
from optparse import Values

import beets
import pytest

from beetsplug.fdup.fdup import _DEFAULT_QUALIFIERS, FDupPlugin, MinItem, ValueParser

DEFAULT_RULES = [(r"\s*&\s*", " and "), (r"(?<=\b\w)\.(?=\w\b)", "")]


@pytest.fixture(autouse=True)
def fresh_config():
    """The plugin's config lives in beets' global config: reset it so settings don't leak between tests."""
    beets.config.clear()
    beets.config.read(user=False, defaults=True)
    yield
    beets.config.clear()
    beets.config.read(user=False, defaults=True)


def find_matches(items: list[tuple], **config) -> dict[tuple[str, str], object]:
    """Run the matcher over (artist, title[, mb_workid]) items; return {(title1, title2): Match}."""
    plugin = FDupPlugin()
    for key, value in config.items():
        plugin.config[key].set(value)
    plugin._set_up(Values({"match_threshold": None, "against_all": None, "group_results": None}))
    item_by_id = {
        i: MinItem({"id": i, "artist": item[0], "title": item[1], "mb_workid": item[2] if len(item) > 2 else ""})
        for i, item in enumerate(items, 1)}
    plugin._index_items(item_by_id)
    ids = set(item_by_id)
    return {
        (item_by_id[id1]["title"], item_by_id[m.id2]["title"]): m
        for id1, matches in plugin._find_matches2(item_by_id, ids, ids) for m in matches}


def matched(matches: dict, title1: str, title2: str) -> bool:
    assert ((title1, title2) in matches) == ((title2, title1) in matches), "matches should be symmetric"
    return (title1, title2) in matches


class TestValueParser:

    def test_normalise_rules(self):
        parser = ValueParser(DEFAULT_RULES, None)
        assert parser.normalise("Simon & Garfunkel") == "simon and garfunkel"
        assert parser.normalise("S.O.S.") == "sos."
        assert parser.normalise("  Café   del  Mar ") == "cafe del mar"

    def test_unsplit_field(self):
        parsed = ValueParser([], None).parse("song (live)")
        assert (parsed.core, parsed.full, parsed.subtitles, parsed.labels) == ("song (live)", "song (live)", (), set())

    def test_brackets_anywhere_and_separators(self):
        parsed = ValueParser([], _DEFAULT_QUALIFIERS).parse("(oh) pretty woman (live) - 2011 remaster")
        assert parsed.core == "pretty woman"
        assert parsed.full == "oh pretty woman"     # Subtitles stay in place, version qualifiers go.
        assert parsed.subtitles == ("oh",)
        assert parsed.labels == {"live", "remaster"}

    def test_nested_brackets(self):
        parsed = ValueParser([], _DEFAULT_QUALIFIERS).parse("song (live (remastered))")
        assert parsed.core == "song"
        assert parsed.labels == {"live", "remaster"}

    def test_colon_subtitles_and_numbers(self):
        parsed = ValueParser([], _DEFAULT_QUALIFIERS).parse("exogenesis: symphony, part 2: cross-pollination")
        assert parsed.core == "exogenesis"
        assert parsed.subtitles == ("symphony, part 2", "cross-pollination")
        assert parsed.numbers == {"2"}

    def test_trailing_roman_i_is_a_number(self):
        parser = ValueParser([], _DEFAULT_QUALIFIERS)
        assert parser.parse("synchronicity i").numbers == {"i"}
        assert parser.parse("i want you").numbers == set()

    def test_nothing_outside_brackets(self):
        parsed = ValueParser([], _DEFAULT_QUALIFIERS).parse("(untitled)")
        assert parsed.core == "(untitled)"
        assert parsed.subtitles == ()

    def test_version_marker_without_label(self):
        parsed = ValueParser([], _DEFAULT_QUALIFIERS).parse("song (album version)")
        assert parsed.core == parsed.full == "song"
        assert parsed.labels == set()


class TestMatching:

    def test_shared_live_qualifier_is_not_a_match(self):
        matches = find_matches([("U2", "Elevation (live from Boston, 2001)"), ("U2", "Kite (live from Boston, 2001)"),
                                ("U2", "Elevation")])
        assert not matched(matches, "Elevation (live from Boston, 2001)", "Kite (live from Boston, 2001)")
        assert matched(matches, "Elevation", "Elevation (live from Boston, 2001)")
        assert matches[("Elevation", "Elevation (live from Boston, 2001)")].versions == {"live"}

    def test_shared_subtitle_is_not_a_match(self):
        matches = find_matches([("Wings", 'Jet (from "One Hand Clapping")'),
                                ("Wings", 'Let Me Roll It (from "One Hand Clapping")')])
        assert matches == {}

    def test_alternate_title_in_brackets(self):
        matches = find_matches([("Andrea Bocelli", "Con Te Partirò"),
                                ("Andrea Bocelli", "Time to Say Goodbye (Con Te Partirò)")])
        assert matched(matches, "Con Te Partirò", "Time to Say Goodbye (Con Te Partirò)")

    def test_swapped_title_and_subtitle(self):
        matches = find_matches([("Barbra Streisand", 'Evergreen (Love Theme From "A Star Is Born")'),
                                ("Barbra Streisand", 'Love Theme From "A Star Is Born" (Evergreen)')])
        assert len(matches) == 2

    def test_short_title_inside_a_longer_one_is_not_a_match(self):
        assert find_matches([("Sheryl Crow", "Up!"), ("Sheryl Crow", "Giddy Up!")]) == {}

    def test_optional_subtitle_still_matches(self):
        matches = find_matches([("C+C Music Factory", "Gonna Make You Sweat"),
                                ("C+C Music Factory", "Gonna Make You Sweat (Everybody Dance Now)")])
        assert len(matches) == 2

    def test_different_part_numbers(self):
        matches = find_matches([("Muse", "Exogenesis: Symphony, Part 2: Cross-Pollination"),
                                ("Muse", "Exogenesis: Symphony, Part 3: Redemption")])
        assert matches == {}

    def test_different_subtitles(self):
        matches = find_matches([("D'Oyly Carte", "The Gondoliers: Take a Pair of Sparkling Eyes"),
                                ("D'Oyly Carte", "The Gondoliers: In Enterprise of Martial Kind")])
        assert matches == {}

    def test_normalise_rules_make_values_identical(self):
        matches = find_matches([("Blur", "Coffee & TV"), ("Blur", "Coffee and TV")])
        assert matches[("Coffee & TV", "Coffee and TV")].fits["title"] == 1.0

    def test_match_versions_false(self):
        items = [("U2", "Elevation"), ("U2", "Elevation (live)"), ("U2", "Elevation (live at Slane)")]
        matches = find_matches(items, field_configs={"title": {"qualifiers": {"match_versions": False}}})
        assert set(matches) == {("Elevation (live)", "Elevation (live at Slane)"),
                                ("Elevation (live at Slane)", "Elevation (live)")}

    def test_version_marker_removed_by_config(self):
        items = [("U2", "Elevation"), ("U2", "Elevation (live)")]
        matches = find_matches(items, field_configs={"title": {"qualifiers": {"version_markers": {"live": None}}}})
        assert matches[("Elevation", "Elevation (live)")].versions == set()   # 'live' is now just a subtitle.

    def test_extra_normalise(self):
        items = [("Joan Jett", "I Love Rock 'n' Roll"), ("Joan Jett", "I Love Rock and Roll")]
        matches = find_matches(items, default_field_config={"extra_normalise": [[r"\s+'n'\s+", " and "]]})
        assert matches[("I Love Rock 'n' Roll", "I Love Rock and Roll")].fits["title"] == 1.0

    def test_splitting_disabled(self):
        items = [("Sheryl Crow", "Up!"), ("Sheryl Crow", "Giddy Up!")]
        matches = find_matches(items, field_configs={"title": {"qualifiers": None, "metric": "overlap"}})
        assert len(matches) == 2    # The old behaviour: the short title is contained in the longer one.


class TestExactKey:

    def test_same_key_matches_despite_different_titles(self):
        matches = find_matches([("Sinatra", "Theme From New York, New York", "w1"), ("Sinatra", "New York, New York", "w1")])
        assert matches[("Theme From New York, New York", "New York, New York")].fits["title"] == 1.0

    def test_different_keys_never_match(self):
        assert find_matches([("Muse", "Intro", "w1"), ("Muse", "Intro", "w2")]) == {}

    def test_missing_key_falls_back_to_fuzzy_matching(self):
        # (Titles differ only in case, so they normalise to the same value but stay distinct here.)
        matches = find_matches([("Muse", "Intro", "w1"), ("Muse", "intro", ""), ("Muse", "INTRO", "w2")])
        # Both keyed items match the unkeyed one, but not each other.
        assert set(matches) == {("Intro", "intro"), ("intro", "Intro"), ("INTRO", "intro"), ("intro", "INTRO")}

    def test_same_key_needs_matching_artists(self):
        assert find_matches([("Leonard Cohen", "Hallelujah", "w1"), ("Jeff Buckley", "Hallelujah", "w1")]) == {}

    def test_exact_key_disabled(self):
        matches = find_matches([("Muse", "Intro", "w1"), ("Muse", "intro", "w2")], field_configs={"title": {"exact_key": None}})
        assert len(matches) == 2
