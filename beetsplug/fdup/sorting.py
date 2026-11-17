"""Sorting the output: the blocks (groups, or tracks with their duplicates), and the tracks within each block.

A sort is a list of terms in beets' syntax, e.g. 'fdup_size- artist+ title+': a field, then + (ascending) or -
(descending), each breaking ties in the ones before. As in beets, artist and albumartist sort by artist_sort and
albumartist_sort where those are set, strings sort case-insensitively unless sort_case_insensitive is off, and a
missing value sorts lowest."""

import dataclasses
import functools
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, TypeVar

import beets
from beets.library import Item
from beets.ui import UserError

T = TypeVar("T")

# Fields sorted by their *_sort field where it's set (e.g. 'Beatles, The'), as beets' SmartArtistSort does.
_SMART_FIELDS = frozenset({"artist", "albumartist"})


@dataclasses.dataclass(frozen=True)
class SortTerm:
    field: str
    ascending: bool
    case_insensitive: bool = True


def is_sort_term(part: str) -> bool:
    """Whether a query part is a sort term, by beets' rule: it ends in + or -, isn't only that, and has no ':'."""
    return part.endswith(("+", "-")) and ":" not in part and len(part) > 1


def parse_sort(parts: str | Iterable[str] | None, setting: str) -> tuple[SortTerm, ...]:
    """Sort terms from a string ('artist+ title+') or a list of them; None or '' for none. setting says where they
    came from, for errors."""
    if isinstance(parts, str):
        parts = parts.split()
    case_insensitive = beets.config["sort_case_insensitive"].get(bool)
    terms: list[SortTerm] = []
    for part in map(str, parts or ()):
        if not is_sort_term(part):
            raise UserError(f"fdup: {setting}: '{part}' isn't a sort term: a field, then + or - (e.g. 'artist+')")
        field = part[:-1]
        if field in Item._getters():
            raise UserError(f"fdup: {setting}: can't sort by {field}: computed fields aren't available")
        terms.append(SortTerm(field, part.endswith("+"), case_insensitive))
    return tuple(terms)


def query_sort(args: Sequence[str] | None) -> tuple[SortTerm, ...]:
    """The sort terms in a query, e.g. 'year+' in 'artist:abba year+' (fetch_items leaves them out of the query)."""
    return parse_sort([arg for arg in args or () if is_sort_term(arg)], "query")


def sort_fields(terms: Iterable[SortTerm]) -> set[str]:
    """The item fields the terms sort by, for fetch_items (leaving out fdup's own values)."""
    fields = {term.field for term in terms if not term.field.startswith("fdup_")}
    return fields | {f"{field}_sort" for field in fields & _SMART_FIELDS}


def item_value(item: Mapping[str, Any], field: str) -> Any:
    """An item's value to sort by for a field (None if it doesn't have it)."""
    if field in _SMART_FIELDS:
        return item.get(f"{field}_sort") or item.get(field)
    return item.get(field)


def sort_by(objs: Iterable[T], terms: Sequence[SortTerm], value: Callable[[T, str], Any]) -> list[T]:
    """The objects sorted by the terms, where value(obj, field) gives an object's value for a field. Objects that tie
    on every term keep their order."""
    objs = list(objs)
    for term in reversed(terms):    # Each sort is stable, so the earlier terms take precedence.
        objs.sort(key=functools.partial(_term_key, term, value), reverse=not term.ascending)
    return objs


def _term_key(term: SortTerm, value: Callable[[Any, str], Any], obj: Any) -> tuple[int, Any]:
    """A key that orders values of any type: missing (None) first, then numbers, then strings (paths included)."""
    v = value(obj, term.field)
    if v is None:
        return (0, 0)
    if isinstance(v, bytes):
        v = os.fsdecode(v)
    if isinstance(v, str):
        return (2, v.lower() if term.case_insensitive else v)
    if isinstance(v, int | float):
        return (1, v)
    return (3, str(v))
