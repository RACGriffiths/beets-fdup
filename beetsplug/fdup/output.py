"""Printing matches and groups."""

from collections.abc import Iterable

from beets.ui import print_

from .grouping import Group
from .items import MinItem
from .matching import Match

# Group output: one line per item, marked by its subset (see grouping.py).
_GROUP_FMT = {
    'a': " --< $artist - $title ($length) - $path ",
    'b': " --- $artist - $title ($length) - $path ",
    'c': " --> $artist - $title ($length) - $path ",
}


def print_matches(
        matches: Iterable[tuple[int, list[Match]]], item_by_id: dict[int, MinItem],
        src_fmt: str, dup_fmt: str, separator: str) -> int:
    """Print each item with its matches (as yielded by find_matches); return the number of matches printed."""
    n_matches = 0
    for id1, item_matches in matches:
        print_(format(item_by_id[id1], src_fmt), end='')
        for match in item_matches:
            item2 = MinItem({
                **item_by_id[match.id2],
                "fdup_score": f"{match.overall_fit():.2f}",
                "fdup_versions": f" [{', '.join(sorted(match.versions))}]" if match.versions else "",
            })
            print_(format(item2, dup_fmt), end='')
        print_(separator, end='')
        n_matches += len(item_matches)
    return n_matches


def print_groups(groups: list[Group], item_by_id: dict[int, MinItem]):
    """Print the groups, largest first, separated by blank lines."""
    for group in sorted(groups, key=lambda x: len(x.ids), reverse=True):
        for abc in 'abc':
            for id_ in group.ids_abc[abc]:
                print_(format(item_by_id[id_], _GROUP_FMT[abc]))
        print_("")
