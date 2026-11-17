"""Printing matches and groups.

Each is printed as a block of lines, one per item: a prefix, set by the line's position in the block (and, in
groups, a marker for which query the item came from), then the item, formatted with the output format."""

import dataclasses
from collections.abc import Callable, Iterable

from beets.ui import print_

from .grouping import Group
from .items import MinItem
from .matching import Match

# A line's position in its block. Every block has at least two lines (an item and one match, or a group of two), so
# there's always a first and a last.
POSITIONS = ("first", "middle", "last")

# Group markers, from their config name to the subset they mark (see grouping.py).
MARKER_SUBSETS = {"query1": "a", "both": "b", "query2": "c"}


@dataclasses.dataclass(frozen=True)
class OutputFormat:
    """How to print each block, for one mode (matches or groups): see the 'output' config."""
    format: str                 # the item itself: a beets template
    prefix: dict[str, str]      # by position (see POSITIONS)
    separator: str              # printed between blocks
    marker: dict[str, str] = dataclasses.field(default_factory=dict)  # groups only: by subset ('a', 'b' or 'c')


def print_matches(
        matches: Iterable[tuple[int, list[Match]]], item_by_id: dict[int, MinItem], fmt: OutputFormat,
        labels: Callable[[int], frozenset[str]]) -> int:
    """Print each item, then its matches (as yielded by find_matches), best first; return the number of matches.
    labels gives an item's version labels, by id."""
    n_matches = 0
    for n_blocks, (id1, item_matches) in enumerate(matches):
        item_matches = sorted(item_matches, key=lambda m: (-m.overall_fit(), m.id2))
        lines = [_item(item_by_id[id1], labels(id1))]
        lines += [_item(item_by_id[m.id2], labels(m.id2), m.overall_fit(), m.versions) for m in item_matches]
        _print_block(lines, [""] * len(lines), fmt, n_blocks > 0)
        n_matches += len(item_matches)
    return n_matches


def print_groups(
        groups: list[Group], item_by_id: dict[int, MinItem], fmt: OutputFormat,
        labels: Callable[[int], frozenset[str]]):
    """Print the groups, largest first. labels gives an item's version labels, by id."""
    for n_blocks, group in enumerate(sorted(groups, key=lambda x: len(x.ids), reverse=True)):
        members = group.members()
        lines = [_item(item_by_id[id_], labels(id_), group.score_by_id[id_]) for id_, _ in members]
        _print_block(lines, [fmt.marker[abc] for _, abc in members], fmt, n_blocks > 0)


def _item(item: MinItem, labels: frozenset[str], score: float | None = None,
          versions: Iterable[str] = ()) -> MinItem:
    """The item with fdup's own values added, for the output format."""
    return MinItem({
        **item,
        "fdup_score": "" if score is None else f"{score:.2f}",
        "fdup_labels": _label_list(labels),
        "fdup_versions": _label_list(versions),
    })


def _label_list(labels: Iterable[str]) -> str:
    """E.g. ' [live, remaster]', or '' if there are none (the leading space saves needing %if in the format)."""
    labels = sorted(labels)
    return f" [{', '.join(labels)}]" if labels else ""


def _print_block(items: list[MinItem], markers: list[str], fmt: OutputFormat, separate: bool):
    """Print the items, each after its prefix and marker; after a separator if separate. One write per block: print_
    flushes stdout each time."""
    positions = ["first", *["middle"] * (len(items) - 2), "last"]
    lines = [f"{fmt.prefix[pos]}{marker}{format(item, fmt.format)}\n"
             for item, marker, pos in zip(items, markers, positions, strict=True)]
    print_((fmt.separator if separate else "") + "".join(lines), end="")
