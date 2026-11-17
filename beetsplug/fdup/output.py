"""Printing matches and groups.

Each is printed as a block of lines, one per item: a prefix, set by the line's position in the block (and, in
groups, a marker for which query the item came from), then the item, formatted with the output format. The blocks,
and the items within each, are sorted as the output settings say (see sorting.py)."""

import collections
import dataclasses
from collections.abc import Callable, Iterable
from typing import Any

from beets.ui import print_

from .grouping import Group
from .items import MinItem
from .matching import Match
from .sorting import SortTerm, item_value, sort_by

# A line's position in its block. Every block has at least two lines (an item and one match, or a group of two), so
# there's always a first and a last.
POSITIONS = ("first", "middle", "last")

# Group markers, from their config name to the subset they mark (see grouping.py).
MARKER_SUBSETS = {"query1": "a", "both": "b", "query2": "c"}


@dataclasses.dataclass(frozen=True)
class OutputFormat:
    """How to print each block, for one mode (groups or by_track): see the 'output' config."""
    format: str                 # the item itself: a beets template
    prefix: dict[str, str]      # by position (see POSITIONS)
    separator: str              # printed between blocks
    marker: dict[str, str] = dataclasses.field(default_factory=dict)  # groups only: by subset ('a', 'b' or 'c')
    sort: tuple[SortTerm, ...] = ()         # the blocks
    sort_tracks: tuple[SortTerm, ...] = ()  # the items within each block (in groups, within each subset)


@dataclasses.dataclass(frozen=True)
class _Line:
    item: MinItem               # with fdup's own values added, for the output format (see _line)
    score: float | None = None  # as $fdup_score
    marker: str = ""            # groups only: which query the item came from


def print_matches(
        matches: Iterable[tuple[int, list[Match]]], item_by_id: dict[int, MinItem], fmt: OutputFormat,
        labels: Callable[[int], frozenset[str]]) -> int:
    """Print each item, then its matches (as yielded by find_matches); return the number of matches. The blocks are
    sorted by fmt.sort and each item's matches by fmt.sort_tracks, and otherwise by id. labels gives an item's
    version labels, by id."""
    blocks: list[list[_Line]] = []
    for id1, item_matches in sorted(matches, key=lambda x: x[0]):
        item_matches = sorted(item_matches, key=lambda m: m.id2)
        lines = [_line(item_by_id[m.id2], labels(m.id2), m.overall_fit(), m.versions) for m in item_matches]
        blocks.append([_line(item_by_id[id1], labels(id1)), *sort_by(lines, fmt.sort_tracks, _line_value)])
    _print_blocks(sort_by(blocks, fmt.sort, _block_value), fmt)
    return sum(len(lines) - 1 for lines in blocks)


def print_groups(
        groups: list[Group], item_by_id: dict[int, MinItem], fmt: OutputFormat,
        labels: Callable[[int], frozenset[str]]):
    """Print the groups: sorted by fmt.sort, and otherwise in the order they were made (each from the best match
    left). Each group lists the items from the first query only, then both, then the second only, each sorted by
    fmt.sort_tracks, and otherwise in the order they joined the group. labels gives an item's version labels, by id."""
    blocks: list[list[_Line]] = []
    for group in groups:
        lines_by_abc: dict[str, list[_Line]] = collections.defaultdict(list)
        for id_, abc in group.members():
            lines_by_abc[abc].append(
                _line(item_by_id[id_], labels(id_), group.score_by_id[id_], marker=fmt.marker[abc]))
        blocks.append([line for abc in "abc" for line in sort_by(lines_by_abc[abc], fmt.sort_tracks, _line_value)])
    _print_blocks(sort_by(blocks, fmt.sort, _block_value), fmt)


def _line(item: MinItem, labels: frozenset[str], score: float | None = None, versions: Iterable[str] = (),
          marker: str = "") -> _Line:
    """The item's line, with fdup's own values added to the item for the output format."""
    return _Line(MinItem({
        **item,
        "fdup_score": "" if score is None else f"{score:.2f}",
        "fdup_labels": _label_list(labels),
        "fdup_versions": _label_list(versions),
    }), score, marker)


def _label_list(labels: Iterable[str]) -> str:
    """E.g. ' [live, remaster]', or '' if there are none (the leading space saves needing %if in the format)."""
    labels = sorted(labels)
    return f" [{', '.join(labels)}]" if labels else ""


def _line_value(line: _Line, field: str) -> Any:
    """A line's value to sort by: its score (as a number), or its item's value."""
    return line.score if field == "fdup_score" else item_value(line.item, field)


def _block_value(lines: list[_Line], field: str) -> Any:
    """A block's value to sort by: its number of items, its lowest score, or otherwise its first line's value."""
    if field == "fdup_size":
        return len(lines)
    if field == "fdup_score":
        return min((line.score for line in lines if line.score is not None), default=None)
    return _line_value(lines[0], field)


def _print_blocks(blocks: list[list[_Line]], fmt: OutputFormat):
    for n_blocks, lines in enumerate(blocks):
        _print_block(lines, fmt, n_blocks > 0)


def _print_block(lines: list[_Line], fmt: OutputFormat, separate: bool):
    """Print the lines, each item after its prefix and marker; after a separator if separate. One write per block:
    print_ flushes stdout each time."""
    positions = ["first", *["middle"] * (len(lines) - 2), "last"]
    text = [f"{fmt.prefix[pos]}{line.marker}{format(line.item, fmt.format)}\n"
            for line, pos in zip(lines, positions, strict=True)]
    print_((fmt.separator if separate else "") + "".join(text), end="")
