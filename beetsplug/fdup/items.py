"""Fetching just the fields fdup needs from the library, and formatting them for output.

This reads the database more directly than lib.items() does, which makes it much faster on large libraries, but it
relies on some beets internals (Item._fields, Item._awaken, Item.relation_join, Template.expr,
DefaultTemplateFunctions, ...). If a beets upgrade breaks fdup, look here first; tests/test_items.py compares the
results against lib.items()."""

import collections
import functools
from collections.abc import Callable
from typing import Any

import beets
from beets import context, plugins
from beets.dbcore import AndQuery, Query, types
from beets.library import Item, Library, parse_query_parts
from beets.library.fields import TYPE_BY_FIELD
from beets.library.models import DefaultTemplateFunctions
from beets.util.functemplate import Call, Expression, Symbol, Template


@functools.lru_cache(maxsize=128)
def _template(fmt: str) -> Template:
    """A compiled (cached) Template. beets has its own cached version of this, but renamed it in 2.14
    (template -> get_template), so fdup keeps its own to work with both."""
    return Template(fmt)


@functools.cache
def _template_functions() -> dict[str, Callable[..., str]]:
    """beets' template functions (%upper{}, %if{}, ...) and plugins' ones, for formatting MinItems. Without the whole
    item and the library, %aunique{} and %sunique{} give '', and %ifdef{} is left out (so it's printed as is).
    (item and lib are passed as None explicitly: they're optional before beets 2.14, required since.)"""
    functions = DefaultTemplateFunctions(None, None).functions()
    functions.pop("ifdef", None)
    functions.update(plugins.template_funcs())
    return functions


def template_fields(fmt: str) -> set[str]:
    """The item fields a template uses ($name, including in function arguments), for fetch_items. Leaves out fdup's
    own $fdup_* values, and computed fields (e.g. $filesize), which fetch_items can't provide."""
    fields: set[str] = set()

    def walk(expr: Expression):
        for part in expr.parts:
            if isinstance(part, Symbol):
                fields.add(part.ident)
            elif isinstance(part, Call):
                for arg in part.args:
                    walk(arg)

    walk(_template(fmt).expr)
    return {field for field in fields if not field.startswith("fdup_")} - Item._getters().keys()


class MinItem(dict[str, Any]):
    """A few of an item's fields, as a plain dict, which can be formatted like an Item (e.g. format(item, '$title'))."""

    def __format__(self, format_spec: str) -> str:
        if not format_spec:
            format_spec = beets.config["format_item"].as_str()
        assert isinstance(format_spec, str)
        # Cached: constructing a new Template per item dominated output time.
        return _template(format_spec).substitute(self.formatted(), _template_functions())

    def formatted(self) -> dict[str, str]:
        """Get a mapping containing all values on this object formatted
        as human-readable unicode strings. Code shamelessly stolen from beets.
        """
        formatted: dict[str, str] = {}
        for k, v in self.items():
            t = TYPE_BY_FIELD.get(k) or types.DEFAULT     # .get: flex attributes and fdup_* output fields.
            value = t.format(v)
            if isinstance(value, bytes):
                value = value.decode("utf-8", "ignore")
            formatted[k] = value
        return formatted


def fetch_items(lib: Library, fields: set[str], args: list[str] | None = None) -> list[MinItem]:
    """Fetch only the given fields of the items matching the query args (all items if there are none).

    Much faster than lib.items(), which reads every column (lyrics included) and every flexible attribute of every
    item, then builds a full Item for each. Values come back as an Item would give them (e.g. paths made absolute,
    NULL as the field's default); a flexible attribute an item doesn't have is None."""

    # todo:
    #  - consider expanding to allow album fetches too.
    #  - or at least additionally operating on album flex fields.

    fields = set(fields) | {"id"}

    # Like lib.items(): resolve path queries and stored (relative) paths against this library's directory.
    with context.music_dir(lib.directory):
        query = parse_query_parts(args, Item)[0] if args else AndQuery([])
        from_, where, params, slow = _sql_filter(query)

        # Slow parts are checked on Items built from just the fields they need, unless they use computed fields
        # (getters like 'filesize'), or don't say what they need: then build full Items, as beets would.
        needed = fields | (slow.field_names if slow else set())
        full = slow is not None and (any(not q.field_names for q in slow.subqueries)
                                     or bool(needed & Item._getters().keys()))
        fixed = ["id", *sorted((Item._fields.keys() if full else needed & Item._fields.keys()) - {"id"})]
        flex = None if full else sorted(needed - Item._fields.keys())   # None: all of them.

        with lib.transaction() as tx:
            rows = tx.query(f"SELECT {', '.join(f'items.{f}' for f in fixed)} FROM {from_} WHERE {where}", params)
            ids_sql = f"SELECT items.id FROM {from_} WHERE {where}"
            if flex is None:
                flex_rows = tx.query(
                    f"SELECT entity_id, key, value FROM item_attributes WHERE entity_id IN ({ids_sql})", params)
            elif flex:
                flex_rows = tx.query(
                    f"SELECT entity_id, key, value FROM item_attributes "
                    f"WHERE key IN ({', '.join('?' * len(flex))}) AND entity_id IN ({ids_sql})", [*flex, *params])
            else:
                flex_rows = []

        flex_by_id: dict[int, dict[str, Any]] = collections.defaultdict(dict)
        for entity_id, key, value in flex_rows:
            flex_by_id[entity_id][key] = value

        # Convert the raw SQL values as Item does (relative path -> absolute, NULL -> '' or 0, ...).
        fixed_types = [(f, Item._type(f).from_sql) for f in fields & Item._fields.keys()]
        flex_types = [(f, Item._type(f).from_sql) for f in fields - Item._fields.keys()]
        items: list[MinItem] = []
        for row in rows:
            values = dict(zip(fixed, row, strict=True))
            flex_values = flex_by_id.get(values["id"], {})
            if slow and not slow.match(_item_from_db(lib, values, flex_values)):
                continue
            item = MinItem({f: from_sql(values[f]) for f, from_sql in fixed_types})
            item.update({f: from_sql(flex_values[f]) if f in flex_values else None for f, from_sql in flex_types})
            items.append(item)
    return items


def _item_from_db(lib: Library, fixed_values: dict[str, Any], flex_values: dict[str, Any]) -> Item:
    """An Item made from raw database values (converted when used, as beets does), to check slow queries against."""
    if hasattr(Item, "_awaken"):    # beets < 2.14
        return Item._awaken(lib, fixed_values, flex_values)
    return Item(lib, fixed_values=fixed_values, flex_values=flex_values)


def _sql_filter(query: Query) -> tuple[str, str, list[Any], AndQuery | None]:
    """Split a query (an AND of parts) into SQL for the parts SQLite can evaluate (FROM and WHERE terms, params) and
    the rest ('slow' parts, e.g. on flexible attributes), to check in Python. (lib.items() checks *every* part in
    Python if any part is slow.) A part containing a slow NOT or OR goes to Python whole, so it stays correct."""
    parts = query.subqueries if isinstance(query, AndQuery) else [query]
    clauses: list[str] = []
    params: list[Any] = []
    fast: list[Query] = []
    slow: list[Query] = []
    for part in parts:
        clause, part_params = part.clause()
        if clause:
            clauses.append(f"({clause})")
            params.extend(part_params)
            fast.append(part)
        else:
            slow.append(part)
    from_ = "items"
    if any(part.field_names & Item.other_db_fields for part in fast):
        from_ += f" {Item.relation_join}"   # Album-only fields (e.g. artpath) are queried via a join, as in beets.
    return from_, " AND ".join(clauses) or "1", params, AndQuery(slow) if slow else None
