"""Normalising field values, and splitting them into a core and qualifiers (subtitles, version markers, numbers)."""

import re
import unicodedata
from typing import Any, NamedTuple

from unidecode import unidecode

_WORD = re.compile(r"[a-z0-9]+")
_PLACEHOLDER = re.compile(r"\0(\d+)\0")
# Roman numerals counted as numbers, as digits. (A lone 'i' only counts as the last word, see _numbers.)
_ROMAN_NUMERALS = {w: str(n) for n, w in enumerate(
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split(), 1)}
# Number words, with their values. They only count as part numbers after one of _PART_WORDS: elsewhere they're
# usually just words ('one love', 'no one', 'take five', 'cloud number nine').
_UNITS = "one two three four five six seven eight nine".split()
_TEENS = "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_NUMBER_WORDS = {w: n for n, w in enumerate(_UNITS + _TEENS, 1)} | {w: 10 * n for n, w in enumerate(_TENS, 2)}
_PART_WORDS = "part parts pt pts movement movements mvt chapter chapters act acts book episode vol vols volume volumes"


def _alternatives(words: list[str]) -> str:
    return "|".join(sorted(words, key=len, reverse=True))    # Longest first, so e.g. 'seventeen' isn't cut short.


_SPELLED_NUMBER = re.compile(rf"\b(?:(?:{_alternatives(_TENS)})(?: (?:{_alternatives(_UNITS)}))?"
                             rf"|{_alternatives(_UNITS + _TEENS)})\b")
# E.g. 'part two', 'parts one and two', 'chapter twenty one', in a value's words joined by single spaces.
_SPELLED_PART_NUMBERS = re.compile(
    rf"\b(?:{_alternatives(_PART_WORDS.split())}) ({_SPELLED_NUMBER.pattern}(?: and {_SPELLED_NUMBER.pattern})*)")
# Unicode categories of characters with no look of their own, left out when spelling out symbols: combining marks,
# variation selectors (e.g. the one in '❤️') and joiners (e.g. inside '👨‍👩‍👧').
_INVISIBLE = frozenset({"Mn", "Me", "Cf"})


def collapse(text: str) -> str:
    """Collapse runs of whitespace to single spaces, and trim the ends."""
    return " ".join(text.split())


def _transliterate(text: str) -> str:
    """The text in ASCII, with accents removed (by unidecode). But a value of only symbols, which unidecode would leave
    with no letter or digit (e.g. emoji, which it removes, or '!!!'), is spelled out with each character's Unicode
    name instead, e.g. '👋' -> 'WAVING HAND SIGN', so that different symbols stay different values."""
    ascii_text = unidecode(text)
    if any(ch.isalnum() for ch in ascii_text):
        return ascii_text
    # (A character this Python's Unicode data doesn't know yet, e.g. a new emoji, gets its code point instead.)
    names = [unicodedata.name(ch, f"U+{ord(ch):X}") for ch in text
             if not ch.isspace() and unicodedata.category(ch) not in _INVISIBLE]
    return " ".join(names) or ascii_text


def _numbers(text: str) -> set[str]:
    """Part/movement numbers in a text, as digits: any number in digits or Roman numerals, and number words after a
    word such as 'part', e.g. 'part 2', 'synchronicity ii' and 'part two' all give {'2'}.
    A lone 'i' only counts as the last word (elsewhere it's usually the pronoun)."""
    words = _WORD.findall(text)
    numbers = {w if w.isdigit() else _ROMAN_NUMERALS[w] for w in words
               if w.isdigit() or (w in _ROMAN_NUMERALS and w != "i")}
    if len(words) > 1 and words[-1] == "i":
        numbers.add("1")
    for m in _SPELLED_PART_NUMBERS.finditer(" ".join(words)):
        numbers.update(str(sum(_NUMBER_WORDS[w] for w in spelled.split()))
                       for spelled in _SPELLED_NUMBER.findall(m.group(1)))
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
        text = _transliterate(text).casefold()
        for pattern, replacement in self._rules:
            text = pattern.sub(replacement, text)
        return collapse(text)

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
        core = collapse(_PLACEHOLDER.sub(" ", head))
        if not core or (not groups and not tail):   # E.g. '(untitled)', or simply nothing to split.
            return unsplit

        # Classify the qualifiers: version markers give labels, the rest are subtitles.
        tail = [collapse(expand(t)) for t in tail]
        labels_by_qualifier = {
            q: self._version_labels(q) for q in [*(groups[int(i)] for i in _PLACEHOLDER.findall(head)), *tail]}
        labels = frozenset().union(*(found for found in labels_by_qualifier.values() if found is not None))
        subtitles = tuple(dict.fromkeys(
            collapse(q) for q, found in labels_by_qualifier.items() if found is None and q.strip()))

        # 'full' keeps subtitles where they were (word order matters to n-grams), dropping version qualifiers.
        def subtitle_or_nothing(m: re.Match) -> str:
            q = groups[int(m.group(1))]
            return f" {q} " if labels_by_qualifier[q] is None else " "

        full = collapse(" ".join([_PLACEHOLDER.sub(subtitle_or_nothing, head),
                                  *(t for t in tail if labels_by_qualifier[t] is None)]))
        numbers = frozenset(_numbers(core).union(*(_numbers(s) for s in subtitles)))
        return ParsedValue(core, full, subtitles, labels, numbers)
