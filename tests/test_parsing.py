"""Tests for value parsing: normalise rules, and splitting values into a core, subtitles, labels and numbers."""

from beetsplug.fdup.config import DEFAULT_CONFIG, DEFAULT_QUALIFIERS
from beetsplug.fdup.parsing import ValueParser

DEFAULT_RULES = [(pattern, replacement) for pattern, replacement in DEFAULT_CONFIG["default_field_config"]["normalise"]]


class TestValueParser:

    def test_normalise_rules(self):
        parser = ValueParser(DEFAULT_RULES, None)
        assert parser.normalise("Simon & Garfunkel") == "simon and garfunkel"
        assert parser.normalise("S.O.S.") == "sos."
        assert parser.normalise("  Café   del  Mar ") == "cafe del mar"
        assert parser.normalise("Be-Bop-A-Lula") == "be bop a lula"
        assert parser.normalise("D-I-V-O-R-C-E") == "divorce"
        assert parser.normalise("1-2-3") == "1 2 3"

    def test_normalise_rules_keep_separators(self):
        parsed = ValueParser(DEFAULT_RULES, DEFAULT_QUALIFIERS).parse(
            ValueParser(DEFAULT_RULES, None).normalise("Exogenesis: Part 2 - Cross-Pollination"))
        assert parsed.core == "exogenesis"
        assert parsed.subtitles == ("part 2", "cross pollination")

    def test_symbols_only_are_spelled_out(self):
        parser = ValueParser([], None)
        assert parser.normalise("👋") == "waving hand sign"     # (unidecode alone would give '')
        assert parser.normalise("❤️") == parser.normalise("❤") == "heavy black heart"    # Variation selector left out.
        assert parser.normalise("!!!") == "exclamation mark exclamation mark exclamation mark"

    def test_symbols_among_text_are_dropped(self):
        assert ValueParser([], None).normalise("A★Teens") == "ateens"

    def test_unsplit_field(self):
        parsed = ValueParser([], None).parse("song (live)")
        assert (parsed.core, parsed.full, parsed.subtitles, parsed.labels) == ("song (live)", "song (live)", (), set())

    def test_brackets_anywhere_and_separators(self):
        parsed = ValueParser([], DEFAULT_QUALIFIERS).parse("(oh) pretty woman (live) - 2011 remaster")
        assert parsed.core == "pretty woman"
        assert parsed.full == "oh pretty woman"     # Subtitles stay in place, version qualifiers go.
        assert parsed.subtitles == ("oh",)
        assert parsed.labels == {"live", "remaster"}

    def test_nested_brackets(self):
        parsed = ValueParser([], DEFAULT_QUALIFIERS).parse("song (live (remastered))")
        assert parsed.core == "song"
        assert parsed.labels == {"live", "remaster"}

    def test_colon_subtitles_and_numbers(self):
        parsed = ValueParser([], DEFAULT_QUALIFIERS).parse("exogenesis: symphony, part 2: cross-pollination")
        assert parsed.core == "exogenesis"
        assert parsed.subtitles == ("symphony, part 2", "cross-pollination")
        assert parsed.numbers == {"2"}

    def test_trailing_roman_i_is_a_number(self):
        parser = ValueParser([], DEFAULT_QUALIFIERS)
        assert parser.parse("synchronicity i").numbers == {"i"}
        assert parser.parse("i want you").numbers == set()

    def test_nothing_outside_brackets(self):
        parsed = ValueParser([], DEFAULT_QUALIFIERS).parse("(untitled)")
        assert parsed.core == "(untitled)"
        assert parsed.subtitles == ()

    def test_version_marker_without_label(self):
        parsed = ValueParser([], DEFAULT_QUALIFIERS).parse("song (album version)")
        assert parsed.core == parsed.full == "song"
        assert parsed.labels == set()
