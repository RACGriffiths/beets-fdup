"""fdup's configuration: the defaults, building a FieldMatcher for each configured field, and the output settings."""

import functools
from collections.abc import Callable
from typing import Any

import confuse
from beets.ui import UserError

from .fingerprint import TextPreprocessor, compare_fps
from .matching import FieldMatcher
from .output import MARKER_SUBSETS, POSITIONS, OutputFormat
from .parsing import ValueParser

# How titles are split into a core and qualifiers (see ValueParser). Also the fallback for any key missing from a
# field's own 'qualifiers' config.
DEFAULT_QUALIFIERS: dict[str, Any] = {
    "brackets": ["()", "[]", "{}"],         # Opening and closing character of each pair.
    "separators": [r"\s+-+\s+", r":\s+"],   # Regexes (without capturing groups); text after one is a qualifier.
    # Words marking a qualifier as a version rather than part of the title, mapped to the label they give
    # ('' marks a version without giving a label). Merged with the user's config: set a word to null to remove it.
    "version_markers": {
        "live": "live", "unplugged": "live",
        "remix": "remix", "mix": "remix", "rmx": "remix", "dub": "remix",
        "edit": "edit", "extended": "extended",
        "remaster": "remaster", "remastered": "remaster",
        "demo": "demo", "acoustic": "acoustic", "instrumental": "instrumental", "karaoke": "instrumental",
        "mono": "mono", "stereo": "stereo", "commentary": "commentary", "reprise": "reprise",
        "alternate": "alternate", "alternative": "alternate", "outtake": "alternate",
        "rehearsal": "rehearsal", "session": "session", "sessions": "session",
        "version": "", "bonus": "", "explicit": "", "feat": "", "featuring": "", "ft": "",
    },
    "version_marker_patterns": {r"\b(19|20)\d\d\b": ""},  # Regex -> label, as for version_markers.
    "alias_threshold": 0.9,         # A subtitle alone must match the other's title this well (alternate titles).
    "subtitle_threshold": 0.75,     # If both have subtitles, at least one pair must match this well.
    "check_numbers": True,          # Differing part/movement numbers (part 2 vs part 3) mean different tracks.
    "match_versions": True,         # Match different versions (e.g. live vs studio), labelled; false: don't.
}

DEFAULT_CONFIG: dict[str, Any] = {
    "query_separator": "~~~",
    "fields": ["artist", "title"],
    "group_results": False,
    "default_field_config": {
        "alphabet": "abcdefghijklmnopqrstuvwxyz 0123456789",   # Characters fingerprinted; others are ignored.
        "word_size": 1536,
        "match_threshold": 0.75,
        "difference_penalty": 0.025,  # Kept low e.g. < 0.05 (additional characters don't necessarily matter much, can include qualifying info e.g. (live) or whatever)
        # 'overlap': shared n-grams relative to the shorter value, so 'abba' matches 'abba & friends'.
        # 'dice': relative to both values, so a short value doesn't match a longer one just by being in it.
        "metric": "overlap",
        # [regex, replacement] pairs, applied in order after accents are removed and text is lower-cased.
        # extra_normalise is applied afterwards: add rules there to keep these defaults.
        "normalise": [
            [r"\s*&\s*", " and "],
            [r"(?<=\b\w)\.(?=\w\b)", ""],   # Dots in initialisms: s.o.s. -> sos
            [r"(?<=\b[a-z])-(?=[a-z]\b)", ""],  # Hyphens between single letters: d-i-v-o-r-c-e -> divorce
            # Other hyphens within words: be-bop-a-lula -> be bop a lula. (Fingerprints ignore punctuation, so a
            # hyphen left in would join the words. Hyphens with spaces around them separate qualifiers: not touched.)
            [r"(?<=\w)-(?=\w)", " "],
        ],
        "extra_normalise": [],
        "qualifiers": None,     # How to split values into core and qualifiers, null: don't. See DEFAULT_QUALIFIERS.
        # An item field which, when both items have it, decides the match: equal values match, different don't.
        "exact_key": None,
    },
    "field_configs": {
        "title": {
            "metric": "dice",
            "exact_key": "mb_workid",
            "qualifiers": DEFAULT_QUALIFIERS,
        },
    },
    "output": {
        # Each item's line, after its prefix: a beets template, which can also use $fdup_labels, $fdup_score and
        # $fdup_versions (see the README). Each mode below can set its own 'format', overriding this one.
        "format": "$artist - $title ($length)$fdup_labels - $path",
        # The default mode: each item, then its matches.
        "matches": {
            "prefix": {"first": "", "middle": "├─ ", "last": "└─ "},  # by the line's position in the block
            "separator": "\n",  # printed between blocks ("\n": a blank line)
        },
        # --group-results: groups of items that all match each other.
        "groups": {
            "prefix": {"first": "┌─", "middle": "├─", "last": "└─"},
            # After the prefix: which query the item came from. The first query only, both, or the second query only
            # (with --against-all, the rest of the library).
            "marker": {"query1": "◂ ", "both": "─ ", "query2": "▸ "},
            "separator": "\n",
        },
    },
}

# Output settings that have been replaced, and by what.
_REPLACED_OUTPUT_KEYS = {
    "src_fmt": "output.format and output.matches.prefix",
    "dup_fmt": "output.format and output.matches.prefix",
    "separator": "output.matches.separator and output.groups.separator",
}


def build_field_matchers(config: confuse.ConfigView, threshold: float | None = None) -> list[FieldMatcher]:
    """A FieldMatcher for each configured field, set up from the field's own config or the defaults.
    threshold, if given, overrides every field's match_threshold (the --match-threshold option)."""
    fields: list[FieldMatcher] = []
    for fieldname in config["fields"].get(list):
        field_config = functools.partial(_field_config, config, fieldname)
        word_size = field_config("word_size").get(int)
        metric = field_config("metric").as_choice(["overlap", "dice"])
        qualifiers = _qualifiers_config(field_config("qualifiers"))
        # (partial binds the current values; a lambda would see the last loop iteration's values instead).
        compare = functools.partial(
            compare_fps, inv_word_size=1.0 / word_size, diff_pen=field_config("difference_penalty").as_number(),
            use_min=metric == "overlap")
        fields.append(FieldMatcher(
            fieldname,
            TextPreprocessor(alphabet=field_config("alphabet").get(str), word_size=word_size),
            ValueParser(_normalise_rules(field_config), qualifiers),
            compare,
            field_config("match_threshold").as_number() if threshold is None else threshold,
            field_config("exact_key").get(),
            qualifiers))
    return fields


def build_output_format(config: confuse.ConfigView, grouped: bool, format_override: str | None = None) -> OutputFormat:
    """The output settings for printing groups or matches. format_override, if given, overrides the configured
    format (the --format option)."""
    output = config["output"]
    for key, replacement in _REPLACED_OUTPUT_KEYS.items():
        if output[key].exists():
            raise UserError(f"fdup: the output.{key} setting has been replaced by {replacement} (see the README)")
    mode = output["groups" if grouped else "matches"]
    return OutputFormat(
        format=format_override or mode["format"].get(confuse.Optional(str)) or output["format"].get(str),
        prefix={position: mode["prefix"][position].get(str) for position in POSITIONS},
        separator=mode["separator"].get(str),
        marker={abc: mode["marker"][name].get(str) for name, abc in MARKER_SUBSETS.items()} if grouped else {},
    )


def _field_config(config: confuse.ConfigView, fieldname: str, key: str) -> confuse.ConfigView:
    """The field's own setting for key if it has one, otherwise the default."""
    view = config["field_configs"][fieldname][key]
    return view if view.exists() else config["default_field_config"][key]


def _normalise_rules(field_config: Callable[[str], confuse.ConfigView]) -> list[tuple[str, str]]:
    rules = field_config("normalise").get(list) + field_config("extra_normalise").get(list)
    if not all(isinstance(rule, list) and len(rule) == 2 for rule in rules):
        raise UserError("fdup: normalise rules must be [regex, replacement] pairs")
    return [(str(pattern), str(replacement)) for pattern, replacement in rules]


def _qualifiers_config(view: confuse.ConfigView) -> dict[str, Any] | None:
    if view.get() is None:
        return None

    def get(key: str) -> Any:
        if not view[key].exists():
            return DEFAULT_QUALIFIERS[key]
        return dict(view[key].flatten()) if isinstance(DEFAULT_QUALIFIERS[key], dict) else view[key].get()

    qualifiers = {key: get(key) for key in DEFAULT_QUALIFIERS}
    # Drop entries set to null (that's how to remove a default), and match case with normalised text.
    for key in ("version_markers", "version_marker_patterns"):
        qualifiers[key] = {str(k): str(label) for k, label in qualifiers[key].items() if label is not None}
    qualifiers["version_markers"] = {w.casefold(): label for w, label in qualifiers["version_markers"].items()}
    if any(len(pair) != 2 for pair in qualifiers["brackets"]):
        raise UserError("fdup: each qualifiers.brackets entry must be an opening and closing character, e.g. '()'")
    return qualifiers
