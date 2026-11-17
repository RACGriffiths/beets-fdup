"""End-to-end tests: run `beet fdup` against a temporary library, the way a user would."""

import re

import pytest
from beets.ui import UserError

# Machine-readable output: each source item's title, then its duplicates' titles indented, with any version labels.
OUTPUT = {"separator": "", "src_fmt": "$title\n", "dup_fmt": "  $title$fdup_versions\n"}

GROUP_LINE = re.compile(r"^ (?:--<|---|-->) .*? - (?P<title>.*) \(\d+:\d\d\) - ")


def add(env, *tracks: tuple[str, str], **fields):
    for artist, title in tracks:
        env.add_item(artist=artist, title=title, **fields)


def fdup(env, capsys, *args: str) -> dict[str, set[str]]:
    """Run `beet fdup *args`; return {title: {titles of its duplicates, with version labels}}."""
    env.config["fdup"]["output"].set(OUTPUT)
    capsys.readouterr()
    env.run_command("fdup", *args)
    found: dict[str, set[str]] = {}
    src = ""
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("  "):
            found[src].add(line.strip())
        elif line:
            src = line
            found[src] = set()
    return found


def fdup_groups(env, capsys, *args: str) -> set[frozenset[str]]:
    """Run `beet fdup --group-results *args`; return the groups, as sets of titles."""
    capsys.readouterr()
    env.run_command("fdup", "--group-results", *args)
    blocks = capsys.readouterr().out.strip("\n").split("\n\n")
    return {frozenset(GROUP_LINE.match(line)["title"] for line in block.splitlines()) for block in blocks if block}


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
