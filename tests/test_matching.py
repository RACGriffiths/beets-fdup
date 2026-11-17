"""Tests for the matching rules: which pairs of items match, and how (scores, version labels, exact keys)."""

from beetsplug.fdup import FDupPlugin
from beetsplug.fdup.config import build_field_matchers
from beetsplug.fdup.items import MinItem
from beetsplug.fdup.matching import Match, find_matches, index_items


def matches_of(items: list[tuple], **config) -> dict[tuple[str, str], Match]:
    """Run the matcher over (artist, title[, mb_workid]) items; return {(title1, title2): Match}."""
    plugin = FDupPlugin()   # Adds the default config.
    for key, value in config.items():
        plugin.config[key].set(value)
    fields = build_field_matchers(plugin.config)
    item_by_id = {
        i: MinItem({"id": i, "artist": item[0], "title": item[1], "mb_workid": item[2] if len(item) > 2 else ""})
        for i, item in enumerate(items, 1)}
    index_items(fields, item_by_id)
    ids = set(item_by_id)
    return {
        (item_by_id[id1]["title"], item_by_id[m.id2]["title"]): m
        for id1, matches in find_matches(fields, ids, ids) for m in matches}


def matched(matches: dict, title1: str, title2: str) -> bool:
    assert ((title1, title2) in matches) == ((title2, title1) in matches), "matches should be symmetric"
    return (title1, title2) in matches


class TestMatching:

    def test_shared_live_qualifier_is_not_a_match(self):
        matches = matches_of([("U2", "Elevation (live from Boston, 2001)"), ("U2", "Kite (live from Boston, 2001)"),
                              ("U2", "Elevation")])
        assert not matched(matches, "Elevation (live from Boston, 2001)", "Kite (live from Boston, 2001)")
        assert matched(matches, "Elevation", "Elevation (live from Boston, 2001)")
        assert matches[("Elevation", "Elevation (live from Boston, 2001)")].versions == {"live"}

    def test_shared_subtitle_is_not_a_match(self):
        matches = matches_of([("Wings", 'Jet (from "One Hand Clapping")'),
                              ("Wings", 'Let Me Roll It (from "One Hand Clapping")')])
        assert matches == {}

    def test_alternate_title_in_brackets(self):
        matches = matches_of([("Andrea Bocelli", "Con Te Partirò"),
                              ("Andrea Bocelli", "Time to Say Goodbye (Con Te Partirò)")])
        assert matched(matches, "Con Te Partirò", "Time to Say Goodbye (Con Te Partirò)")

    def test_swapped_title_and_subtitle(self):
        matches = matches_of([("Barbra Streisand", 'Evergreen (Love Theme From "A Star Is Born")'),
                              ("Barbra Streisand", 'Love Theme From "A Star Is Born" (Evergreen)')])
        assert len(matches) == 2

    def test_short_title_inside_a_longer_one_is_not_a_match(self):
        assert matches_of([("Sheryl Crow", "Up!"), ("Sheryl Crow", "Giddy Up!")]) == {}

    def test_optional_subtitle_still_matches(self):
        matches = matches_of([("C+C Music Factory", "Gonna Make You Sweat"),
                              ("C+C Music Factory", "Gonna Make You Sweat (Everybody Dance Now)")])
        assert len(matches) == 2

    def test_punctuation_is_ignored(self):
        matches = matches_of([("The Beatles", "Help"), ("The Beatles", "Help!"),
                              ("Bob Dylan", "Lay Lady Lay"), ("Bob Dylan", "Lay, Lady, Lay"),
                              ("ABBA", "S.O.S."), ("ABBA", "SOS")])
        assert len(matches) == 6
        for title1, title2 in [("Help", "Help!"), ("Lay Lady Lay", "Lay, Lady, Lay"), ("S.O.S.", "SOS")]:
            assert matched(matches, title1, title2)
            assert matches[(title1, title2)].fits["title"] == 1.0

    def test_hyphens_within_words(self):
        matches = matches_of([("Gene Vincent", "Be-Bop-A-Lula"), ("Gene Vincent", "Be Bop A Lula"),
                              ("Tammy Wynette", "D-I-V-O-R-C-E"), ("Tammy Wynette", "D.I.V.O.R.C.E.")])
        assert matched(matches, "Be-Bop-A-Lula", "Be Bop A Lula")
        assert matched(matches, "D-I-V-O-R-C-E", "D.I.V.O.R.C.E.")

    def test_different_emoji_titles_dont_match(self):
        assert matches_of([("Coldplay", "👋"), ("Coldplay", "🌈"), ("Coldplay", "✨"), ("Coldplay", "🪐")]) == {}

    def test_same_emoji_title_matches(self):
        matches = matches_of([("Coldplay", "❤️"), ("Coldplay", "❤")])  # With and without the emoji variation selector.
        assert matched(matches, "❤️", "❤")

    def test_values_emptied_by_normalising_dont_match(self):
        # Both titles normalise to a value with nothing to fingerprint: '!' and '?'.
        matches = matches_of([("Muse", "abc!"), ("Muse", "def?")],
                             field_configs={"title": {"extra_normalise": [["[a-z]", ""]]}})
        assert matches == {}

    def test_different_part_numbers(self):
        matches = matches_of([("Muse", "Exogenesis: Symphony, Part 2: Cross-Pollination"),
                              ("Muse", "Exogenesis: Symphony, Part 3: Redemption")])
        assert matches == {}

    def test_part_numbers_in_words(self):
        matches = matches_of([("Diddy", "I Need a Girl, Part One"), ("Diddy", "I Need a Girl, Part Two"),
                              ("Diddy", "I Need a Girl, Part 1"), ("Diddy", "I Need a Girl (Part 2)")])
        assert matched(matches, "I Need a Girl, Part One", "I Need a Girl, Part 1")
        assert matched(matches, "I Need a Girl, Part Two", "I Need a Girl (Part 2)")
        assert not matched(matches, "I Need a Girl, Part One", "I Need a Girl, Part Two")
        assert not matched(matches, "I Need a Girl, Part One", "I Need a Girl (Part 2)")
        assert len(matches) == 4

    def test_part_numbers_in_roman_numerals(self):
        matches = matches_of([("Destiny's Child", "Independent Women, Part I"),
                              ("Destiny's Child", "Independent Women, Part 1"),
                              ("Destiny's Child", "Independent Women, Part Two"),
                              ("Destiny's Child", "Independent Women, Part II")])
        assert matched(matches, "Independent Women, Part I", "Independent Women, Part 1")
        assert matched(matches, "Independent Women, Part Two", "Independent Women, Part II")
        assert not matched(matches, "Independent Women, Part I", "Independent Women, Part II")
        assert len(matches) == 4

    def test_number_words_outside_part_numbers_still_match(self):
        # Number words only count as part numbers after a word such as 'part'. Elsewhere they're just words: counting
        # them would give e.g. 'three lions' {3} vs "three lions '98" {3, 98}, which wouldn't match.
        matches = matches_of([("Queen", "One Vision"), ("Queen", "One Vision (2011 remaster)"),
                              ("Alicia Keys", "No One"), ("Alicia Keys", "No One (live)"),
                              ("Lightning Seeds", "Three Lions"), ("Lightning Seeds", "Three Lions '98")])
        assert matched(matches, "One Vision", "One Vision (2011 remaster)")
        assert matched(matches, "No One", "No One (live)")
        assert matched(matches, "Three Lions", "Three Lions '98")

    def test_different_subtitles(self):
        matches = matches_of([("D'Oyly Carte", "The Gondoliers: Take a Pair of Sparkling Eyes"),
                              ("D'Oyly Carte", "The Gondoliers: In Enterprise of Martial Kind")])
        assert matches == {}

    def test_normalise_rules_make_values_identical(self):
        matches = matches_of([("Blur", "Coffee & TV"), ("Blur", "Coffee and TV")])
        assert matches[("Coffee & TV", "Coffee and TV")].fits["title"] == 1.0

    def test_match_versions_false(self):
        items = [("U2", "Elevation"), ("U2", "Elevation (live)"), ("U2", "Elevation (live at Slane)")]
        matches = matches_of(items, field_configs={"title": {"qualifiers": {"match_versions": False}}})
        assert set(matches) == {("Elevation (live)", "Elevation (live at Slane)"),
                              ("Elevation (live at Slane)", "Elevation (live)")}

    def test_version_marker_removed_by_config(self):
        items = [("U2", "Elevation"), ("U2", "Elevation (live)")]
        matches = matches_of(items, field_configs={"title": {"qualifiers": {"version_markers": {"live": None}}}})
        assert matches[("Elevation", "Elevation (live)")].versions == set()   # 'live' is now just a subtitle.

    def test_extra_normalise(self):
        items = [("Joan Jett", "I Love Rock 'n' Roll"), ("Joan Jett", "I Love Rock and Roll")]
        matches = matches_of(items, default_field_config={"extra_normalise": [[r"\s+'n'\s+", " and "]]})
        assert matches[("I Love Rock 'n' Roll", "I Love Rock and Roll")].fits["title"] == 1.0

    def test_splitting_disabled(self):
        items = [("Sheryl Crow", "Up!"), ("Sheryl Crow", "Giddy Up!")]
        matches = matches_of(items, field_configs={"title": {"qualifiers": None, "metric": "overlap"}})
        assert len(matches) == 2    # The old behaviour: the short title is contained in the longer one.


class TestExactKey:

    def test_same_key_matches_despite_different_titles(self):
        matches = matches_of([("Sinatra", "Theme From New York, New York", "w1"), ("Sinatra", "New York, New York", "w1")])
        assert matches[("Theme From New York, New York", "New York, New York")].fits["title"] == 1.0

    def test_different_keys_never_match(self):
        assert matches_of([("Muse", "Intro", "w1"), ("Muse", "Intro", "w2")]) == {}

    def test_missing_key_falls_back_to_fuzzy_matching(self):
        # (Titles differ only in case, so they normalise to the same value but stay distinct here.)
        matches = matches_of([("Muse", "Intro", "w1"), ("Muse", "intro", ""), ("Muse", "INTRO", "w2")])
        # Both keyed items match the unkeyed one, but not each other.
        assert set(matches) == {("Intro", "intro"), ("intro", "Intro"), ("INTRO", "intro"), ("intro", "INTRO")}

    def test_same_key_needs_matching_artists(self):
        assert matches_of([("Leonard Cohen", "Hallelujah", "w1"), ("Jeff Buckley", "Hallelujah", "w1")]) == {}

    def test_exact_key_disabled(self):
        matches = matches_of([("Muse", "Intro", "w1"), ("Muse", "intro", "w2")], field_configs={"title": {"exact_key": None}})
        assert len(matches) == 2
