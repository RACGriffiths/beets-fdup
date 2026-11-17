"""End-to-end tests: run `beet fdup` against a temporary library, the way a user would."""

import pytest
from beets.ui import UserError

# Machine-readable output: groups: each group's titles, groups separated by blank lines; --by-track: each source item's
# title, then its duplicates' titles indented, with any differing version labels.
OUTPUT = {
    "format": "$title$fdup_versions",
    "by_track": {"prefix": {"first": "", "middle": "  ", "last": "  "}, "separator": ""},
    "groups": {"format": "$title", "prefix": {"first": "", "middle": "", "last": ""},
               "marker": {"query1": "", "both": "", "query2": ""}},
}


def add(env, *tracks: tuple[str, str], **fields):
    for artist, title in tracks:
        env.add_item(artist=artist, title=title, **fields)


def run(env, capsys, *args: str) -> str:
    """Run `beet fdup *args`; return what it printed."""
    capsys.readouterr()
    env.run_command("fdup", *args)
    return capsys.readouterr().out


def fdup(env, capsys, *args: str) -> dict[str, set[str]]:
    """Run `beet fdup --by-track *args`; return {title: {titles of its duplicates, with version labels}}."""
    env.config["fdup"]["output"].set(OUTPUT)
    found: dict[str, set[str]] = {}
    src = ""
    for line in run(env, capsys, "--by-track", *args).splitlines():
        if line.startswith("  "):
            found[src].add(line.strip())
        elif line:
            src = line
            found[src] = set()
    return found


def fdup_groups(env, capsys, *args: str) -> set[frozenset[str]]:
    """Run `beet fdup *args` (grouped, the default); return the groups, as sets of titles."""
    env.config["fdup"]["output"].set(OUTPUT)
    blocks = run(env, capsys, *args).split("\n\n")
    return {frozenset(block.splitlines()) for block in blocks if block}


def blocks(output: str) -> set[str]:
    """The blocks printed, separated by blank lines (the default separator)."""
    assert output.endswith("\n") and not output.endswith("\n\n")    # (No separator after the last block.)
    return {block.strip("\n") for block in output.split("\n\n")}


def test_plugin_loads_and_finds_nothing_in_an_empty_library(beets_env, capsys):
    assert fdup(beets_env, capsys) == {}


def test_finds_fuzzy_duplicates(beets_env, capsys):
    add(beets_env, ("The Beatles", "Hey Jude"), ("Beatles", "Hey Jude (Remastered 2009)"),
        ("The Beatles", "Let It Be"), ("ABBA", "Waterloo"))
    assert fdup(beets_env, capsys) == {
        "Hey Jude": {"Hey Jude (Remastered 2009) [remaster]"},
        "Hey Jude (Remastered 2009)": {"Hey Jude [remaster]"},
    }


def test_query_limits_both_sides(beets_env, capsys):
    add(beets_env, ("ABBA", "Waterloo"), ("ABBA", "Waterloo (live)"),
        ("Queen", "Bohemian Rhapsody"), ("Queen", "Bohemian Rhapsody (live)"))
    assert fdup(beets_env, capsys, "artist:abba") == {
        "Waterloo": {"Waterloo (live) [live]"},
        "Waterloo (live)": {"Waterloo [live]"},
    }


def test_query_on_a_flexible_attribute(beets_env, capsys):
    # Flexible attributes can't be queried in SQL, so this checks the slower, Python-side filtering.
    add(beets_env, ("ABBA", "Waterloo"), ("ABBA", "Waterloo (live)"), mood="happy")
    add(beets_env, ("ABBA", "Waterloo (demo)"))
    assert fdup(beets_env, capsys, "mood:happy") == {
        "Waterloo": {"Waterloo (live) [live]"},
        "Waterloo (live)": {"Waterloo [live]"},
    }


def test_query_results_without_duplicates_among_themselves(beets_env, capsys):
    add(beets_env, ("ABBA", "Waterloo"), album="Gold")
    add(beets_env, ("ABBA", "Waterloo (live)"), album="Live")
    assert fdup(beets_env, capsys, "album:gold") == {}


def test_against_all(beets_env, capsys):
    add(beets_env, ("ABBA", "Waterloo"), album="Gold")
    add(beets_env, ("ABBA", "Waterloo (live)"), album="Live")
    assert fdup(beets_env, capsys, "--against-all", "album:gold") == {"Waterloo": {"Waterloo (live) [live]"}}


def test_two_queries(beets_env, capsys):
    add(beets_env, ("ABBA", "Waterloo"), year=1974)
    add(beets_env, ("ABBA", "Waterloo (live)"), year=1979)
    add(beets_env, ("ABBA", "Waterloo - 2001 Remaster"), year=2001)
    assert fdup(beets_env, capsys, "year:..1980", "~~~", "year:1980..") == {
        "Waterloo": {"Waterloo - 2001 Remaster [remaster]"},
        "Waterloo (live)": {"Waterloo - 2001 Remaster [live, remaster]"},
    }


def test_two_queries_with_against_all_is_an_error(beets_env, capsys):
    with pytest.raises(UserError):
        fdup(beets_env, capsys, "--against-all", "year:..1980", "~~~", "year:1980..")


def test_exact_key_read_from_the_library(beets_env, capsys):
    add(beets_env, ("Frank Sinatra", "Theme From New York, New York"), ("Frank Sinatra", "New York, New York"),
        mb_workid="w1")
    assert fdup(beets_env, capsys) == {
        "Theme From New York, New York": {"New York, New York"},
        "New York, New York": {"Theme From New York, New York"},
    }


@pytest.mark.parametrize(("args", "expected"), [
    ((), {"Waterloo": {"Waterlooo"}, "Waterlooo": {"Waterloo"}}),
    (("--match-threshold", "0.99"), {}),
])
def test_match_threshold_option(beets_env, capsys, args, expected):
    add(beets_env, ("ABBA", "Waterloo"), ("ABBA", "Waterlooo"))
    assert fdup(beets_env, capsys, *args) == expected


def test_group_results(beets_env, capsys):
    add(beets_env, ("ABBA", "Waterloo"), ("ABBA", "Waterloo (live)"), ("ABBA", "Waterloo - Remastered"),
        ("The Beatles", "Hey Jude"), ("The Beatles", "Hey Jude (live)"), ("The Beatles", "Let It Be"))
    assert fdup_groups(beets_env, capsys) == {
        frozenset({"Waterloo", "Waterloo (live)", "Waterloo - Remastered"}),
        frozenset({"Hey Jude", "Hey Jude (live)"}),
    }


@pytest.mark.parametrize(("setting", "args", "grouped"), [
    (None, (), True),                       # grouped by default
    (None, ("--by-track",), False),
    (False, (), False),
    (False, ("--group-results",), True),    # each option overrides the setting
    (True, ("--by-track",), False),
    (None, ("--by-track", "--group-results"), True),  # the last option wins
])
def test_group_results_setting_and_options(beets_env, capsys, setting, args, grouped):
    add(beets_env, ("ABBA", "Waterloo"), ("ABBA", "Waterloo (live)"))
    if setting is not None:
        beets_env.config["fdup"]["group_results"].set(setting)
    expected = ({"┌── Waterloo\n└── Waterloo (live)"} if grouped
                else {"Waterloo\n└─ Waterloo (live)", "Waterloo (live)\n└─ Waterloo"})
    assert blocks(run(beets_env, capsys, *args, "-f", "$title")) == expected


def test_matches_layout(beets_env, capsys):
    # The default prefixes: each block is a tree, with the matches best first (exact title matches before fuzzy ones).
    add(beets_env, ("ABBA", "Waterloo"), ("ABBA", "Waterloo (live)"), ("ABBA", "Waterlooo"))
    assert blocks(run(beets_env, capsys, "--by-track", "-f", "$title$fdup_labels")) == {
        "Waterloo\n├─ Waterloo (live) [live]\n└─ Waterlooo",
        "Waterloo (live) [live]\n├─ Waterloo\n└─ Waterlooo",
        "Waterlooo\n├─ Waterloo\n└─ Waterloo (live) [live]",
    }


def test_groups_layout(beets_env, capsys):
    # The default prefixes and markers: tracks from the first query only, then both, then the second only; each in the
    # order they joined the group (the best match first).
    add(beets_env, ("ABBA", "Waterloo"), year=1974)
    add(beets_env, ("ABBA", "Waterlooo (live)"), year=1979)
    add(beets_env, ("ABBA", "Waterloo - 2001 Remaster"), year=2001)
    add(beets_env, ("Queen", "Bohemian Rhapsody"), ("Queen", "Bohemian Rhapsody"), year=1980)
    output = run(beets_env, capsys, "-f", "$title$fdup_labels", "year:..1980", "~~~", "year:1980..")
    assert blocks(output) == {
        "┌─◂ Waterloo\n├─◂ Waterlooo (live) [live]\n└─▸ Waterloo - 2001 Remaster [remaster]",
        "┌── Bohemian Rhapsody\n└── Bohemian Rhapsody",
    }


def test_group_scores(beets_env, capsys):
    # Each track's weakest match with the tracks it was compared with (not those from the same query).
    add(beets_env, ("ABBA", "Waterloo"), year=1974)
    add(beets_env, ("ABBA", "Waterlooo"), year=1979)
    add(beets_env, ("ABBA", "Waterloo (live)"), year=2001)
    output = run(beets_env, capsys, "-f", "$title $fdup_score", "year:..1980", "~~~", "year:1980..")
    scores = dict(line[4:].rsplit(" ", 1) for line in output.splitlines())
    assert scores["Waterloo"] == "1.00"
    assert scores["Waterlooo"] == scores["Waterloo (live)"] < "1.00"


def test_format_can_use_any_item_field(beets_env, capsys):
    # Fields used in the format are fetched, even if fdup doesn't otherwise need them.
    add(beets_env, ("ABBA", "Waterloo"), album="Gold", mood="happy")
    add(beets_env, ("ABBA", "Waterloo (live)"), album="Live", mood="happy")
    assert blocks(run(beets_env, capsys, "--by-track", "-f", "$album: %upper{$title} $mood")) == {
        "Gold: WATERLOO happy\n└─ Live: WATERLOO (LIVE) happy",
        "Live: WATERLOO (LIVE) happy\n└─ Gold: WATERLOO happy",
    }


def test_each_mode_can_override_the_format(beets_env, capsys):
    add(beets_env, ("ABBA", "Waterloo"), ("ABBA", "Waterloo (live)"))
    beets_env.config["fdup"]["output"].set({"format": "$title", "groups": {"format": "$artist: $title"}})
    assert blocks(run(beets_env, capsys)) == {"┌── ABBA: Waterloo\n└── ABBA: Waterloo (live)"}
    assert blocks(run(beets_env, capsys, "--by-track")) == {
        "Waterloo\n└─ Waterloo (live)", "Waterloo (live)\n└─ Waterloo"}
    # The --format option overrides both.
    assert blocks(run(beets_env, capsys, "--group-results", "-f", "$title")) == {"┌── Waterloo\n└── Waterloo (live)"}


@pytest.mark.parametrize("key", ["src_fmt", "dup_fmt", "separator"])
def test_replaced_output_settings_are_an_error(beets_env, capsys, key):
    beets_env.config["fdup"]["output"][key].set("x")
    with pytest.raises(UserError, match=f"output.{key}"):
        run(beets_env, capsys)
