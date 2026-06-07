import cProfile
import collections
import io
import logging
import math
import pstats
import sqlite3
import typing
from math import prod
from optparse import Values
from typing import Any, Generator, Dict

import beets
import line_profiler
from beets.dbcore import FieldQuery, types
from beets.library import Item, parse_query_parts, Library
from beets.library.fields import TYPE_BY_FIELD
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand, print_, UserError
from beets.util.functemplate import Template
from bitarray import bitarray, util
from confuse import templates
from unidecode import unidecode

PLUGIN = "fdup"

class Fingerprint:
    """Stores a bitarray with values set based upon trigrams of the underlying text, also caches the number of set bits."""
    __slots__ = ('bits', 'n')
    def __init__(self, bits: bitarray):
        self.bits: bitarray = bits
        self.n: int = bits.count()

class TextPreprocessor:
    """Preprocesses text entries into bitarrays for fuzzy matching."""

    def __init__(
            self,
            alphabet: str = "abcdefghijklmnopqrstuvwxyz 0123456789",
            prefix: str = ' ',
            suffix: str = ' ',
            word_size: int = 384):
        """Initialize the preprocessor."""

        # Initialise character map:
        self.char_map: Dict[str, int] = {}
        for i, char in enumerate(alphabet):
            self.char_map[char] = i
        self.miss_val = len(self.char_map)
        self.m = len(self.char_map) + 1
        self.word_size = word_size

        # Maybe add distinct value for start effects:
        if prefix in self.char_map:
            self.start_val = self.char_map[prefix]
        else:
            self.start_val = self.m
            self.m = self.m + 1

        # Maybe add distinct value for end effects:
        if suffix in self.char_map:
            self.end_val = self.char_map[suffix]
        else:
            self.end_val = self.m
            self.m = self.m + 1

    def text2fingerprint(self, text: str) -> Fingerprint:

        # Check there is at least some text.
        bits = bitarray(self.word_size)
        if not text:
            return Fingerprint(bits)

        # Sanitize input by:
        #   - converting accented characters to regular form
        #   - lowercasing
        text = unidecode(text).casefold()
        if not text:
            return Fingerprint(bits)

        # Aggregate bits for each trigram in the text.
        counter : Dict[int, int] = collections.defaultdict(int)
        b_val = self.start_val
        c_val = self.char_map.get(text[0], self.miss_val)
        for cChar in text[1:]:
            a_val, b_val, c_val = b_val, c_val, self.char_map.get(cChar, self.miss_val)
            self._add_trigram(counter, a_val, b_val, c_val)

        # Add in end-effects.
        a_val, b_val, c_val = b_val, c_val, self.end_val
        self._add_trigram(counter, a_val, b_val, c_val)
        counter[self._idx_from_2vals(b_val, c_val)] += 1

        # Turn counter into bitarray.
        for k, v in counter.items():
            bits[k] = 1
            for i in range(1, v):
                # Allow for repeated letter combinations by shifting repeated bits to the left.
                bits[(k + i) % self.word_size] = 1

        return Fingerprint(bits)

    def _add_trigram(self, arr: Dict[int, int], a_val: int, b_val: int, c_val: int):
        # Add bits for AB, AC and ABC.
        # Using same hash function & consts for AB and AC somewhat accounts for character swaps, deletes, and adds.
        arr[self._idx_from_2vals(a_val, b_val)] += 1
        arr[self._idx_from_2vals(a_val, c_val)] += 1
        arr[self._idx_from_3vals(a_val, b_val, c_val)] += 1

    def _idx_from_2vals(self, a_val: int, b_val: int):
        # Map to relatively random bit within [0,word_size)
        return (15331 * a_val ^ 17203 * b_val) % self.word_size

    def _idx_from_3vals(self, a_val: int, b_val: int, c_val: int):
        # Map to relatively random bit within [0,word_size)
        return (15331 * (a_val + self.m) ^ 17203 * b_val ^ 19121 * c_val + 12345) % self.word_size

@line_profiler.profile
def _compare_fps(fp_a: Fingerprint, fp_b: Fingerprint, inv_word_size: float, diff_pen: float) -> float:
    avg = fp_a.n * fp_b.n * inv_word_size - 0.001
    hits = util.count_and(fp_a.bits, fp_b.bits)
    denom = (fp_a.n if fp_a.n < fp_b.n else fp_b.n) - avg + diff_pen * (fp_a.n + fp_b.n - hits - hits)
    return (hits - avg) / denom

class MinItem(dict[str, Any]):
    """Repr of dict[str, Any]"""

    def __format__(self, format_spec: str) -> str:
        if not format_spec:
            format_spec = beets.config["format_item"].as_str()
        assert isinstance(format_spec, str)
        return Template(format_spec).substitute(self.formatted())

    def formatted(self) -> dict[str, str]:
        """Get a mapping containing all values on this object formatted
        as human-readable unicode strings. Code shamelessly stolen from beets.
        """
        formatted: dict[str, str] = {}
        for k, v in self.items():
            t = TYPE_BY_FIELD[k] or types.DEFAULT
            value = t.format(v)
            if isinstance(value, bytes):
                value = value.decode("utf-8", "ignore")
            formatted[k] = value
        return formatted

class Match:

    def __init__(self, id1: int, id2: int, key: str, match: float):
        self.id1 = id1
        self.id2 = id2
        self.fits = {key: match}
        self.fit = None

    def with_fit(self, fieldname: str, value: float) -> typing.Self:
        self.fits[fieldname] = value
        self.fit = None
        return self

    def overall_fit(self) -> float:
        if self.fit is None:
            self.fit = math.pow(prod(self.fits.values()), 1/len(self.fits))
        return self.fit

mock_counter: int = 0
def _mock_get_items() -> list[MinItem]:
    global mock_counter
    vals = [
        MinItem({"id": mock_counter + 1, "path": "1", "artist": "aaabbbcccddd1", "title": "qwertyuiop1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 2, "path": "2", "artist": "aaabbbcccddd1", "title": "qwertyuiop2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 3, "path": "3", "artist": "aaabbbcccddd1", "title": "asdfghjkl1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 4, "path": "4", "artist": "aaabbbcccddd1", "title": "asdfghjkl2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 5, "path": "5", "artist": "aaabbbcccddd2", "title": "qwertyuiop1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 6, "path": "6", "artist": "aaabbbcccddd2", "title": "qwertyuiop2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 7, "path": "7", "artist": "aaabbbcccddd2", "title": "asdfghjkl1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 8, "path": "8", "artist": "aaabbbcccddd2", "title": "asdfghjkl2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 9, "path": "9", "artist": "eeefffggghhh1", "title": "qwertyuiop1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 10, "path": "10", "artist": "eeefffggghhh1", "title": "qwertyuiop2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 11, "path": "11", "artist": "eeefffggghhh1", "title": "asdfghjkl1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 12, "path": "12", "artist": "eeefffggghhh1", "title": "asdfghjkl2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 13, "path": "13", "artist": "eeefffggghhh2", "title": "qwertyuiop1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 14, "path": "14", "artist": "eeefffggghhh2", "title": "qwertyuiop2", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 15, "path": "15", "artist": "eeefffggghhh2", "title": "asdfghjkl1", "length": 100 + mock_counter}),
        MinItem({"id": mock_counter + 16, "path": "16", "artist": "eeefffggghhh2", "title": "asdfghjkl2", "length": 100 + mock_counter}),
    ]
    mock_counter += len(vals)
    return vals


def _quick_fetch_items(lib, fields: set[str], args=None) -> list[MinItem]:
    """Fetch desired fields from library, optionally filtered by query args."""
    # return _mock_get_items()

    # todo:
    #  - consider expanding to allow album fetches too.
    #  - or at least additionally operating on album flex fields.

    # Split the desired fields according to whether they are fixed, or flex based.
    desired_fields = fields | {"id"}    # Always return 'id'
    item_fields = Item.all_db_fields - Item.other_db_fields
    desired_fixed_fields = item_fields & desired_fields
    desired_flex_fields = desired_fields - desired_fixed_fields

    # Quick exits for queries without filtering:
    if args is None or len(args) == 0:

        # Query the database to get the desired fields.
        # If the desired fields are not just item fields, query the item flex table too.
        with (lib.transaction() as tx):
            rows = tx.query(f"SELECT {', '.join(desired_fixed_fields)} FROM items")
            flex_rows = [] if len(desired_flex_fields) == 0 else tx.query(
                f"SELECT * FROM item_attributes WHERE key IN ({", ".join(["?"] * len(desired_flex_fields))})",
                list(desired_flex_fields))
        items = _combine_fixed_and_flex(rows, flex_rows)
        return [MinItem(item) for item in items]

    # Otherwise, we have to account for the filter fields too.
    # Parse the query parts into a dbcore.Query, ignore the sort for now.
    parsed_query = parse_query_parts(args, Item)[0]
    query_fields = parsed_query.field_names
    query_fixed_fields = query_fields & item_fields
    query_flex_fields = query_fields - item_fields

    # Prepare the SQL WHERE term for the item table query.
    # Note: monkey-patching to allow non-fast query clauses to be ignored without affecting fast query clauses.
    original_method = FieldQuery.clause
    try:
        FieldQuery.clause = lambda self: self.col_clause() if self.fast else ("1", ())
        where, params = parsed_query.clause()
    finally:
        FieldQuery.clause = original_method

    # Prepare the SQL items query term.
    all_fixed_fields = desired_fixed_fields | query_fixed_fields
    sql_fixed = f"SELECT {', '.join(all_fixed_fields)} FROM items WHERE {where or 1}"
    params_fixed = params

    # Prepare the SQL flexible attributes query term (if any)
    all_flex_fields = desired_flex_fields | query_flex_fields
    needs_flex = len(all_flex_fields) > 0
    if needs_flex:
        sql_flex = (
            "SELECT * FROM item_attributes WHERE "
            f"key IN ({", ".join(["?"] * len(all_flex_fields))}) "
            f"AND entity_id IN (SELECT id FROM items WHERE {where or 1})")
        params_flex = list(all_flex_fields) + params

    # Do the actual query:
    with lib.transaction() as tx:
        rows = tx.query(sql_fixed, params_fixed)
        flex_rows = tx.query(sql_flex, params_flex) if needs_flex else []
    items = _combine_fixed_and_flex(rows, flex_rows)

    # If any of the query filters are based on flexible attributes, re-filter here (slow query)
    if len(query_flex_fields) > 0:
        items = [item for item in items if parsed_query.match(item)]

    # Remove extraneous fields, only return what is desired.
    return [MinItem({k: v for k, v in item.items() if k in desired_fields}) for item in items]

def _combine_fixed_and_flex(fixed_rows: list[sqlite3.Row], flex_rows: list[sqlite3.Row]) -> list[dict]:
    items_fixed_by_id = {row["id"]: dict(row) for row in fixed_rows}
    items_flex_by_id = collections.defaultdict(dict[str, str])
    for row in flex_rows:
        items_flex_by_id[row["entity_id"]][row["key"]] = row["value"]
    return [{**items_flex_by_id.get(id_, {}), **data} for id_, data in items_fixed_by_id.items()]

class FDupPlugin(BeetsPlugin):
    """List duplicate tracks based on fuzzy matching of artist and title."""

    def __init__(self):
        super().__init__("fdup")

        self.config.add({
            "query_separator": "~~~",
            "fields": ["artist", "title"],
            "group_results": False,
            "default_field_config": {
                "alphabet": "abcdefghijklmnopqrstuvwxyz 0123456789",
                "word_size": 1536,
                "match_threshold": 0.75,
                "difference_penalty": 0.025, # Kept low e.g. < 0.05 (additional characters don't necessarily matter much, can include qualifying info e.g. (live) or whatever)
            },
            "field_configs": {},
            "output": {
                "separator": "\n",
                "src_fmt": "*** $artist - $title ($length) - $path\n",
                "dup_fmt": " |_ $artist - $title ($length) - $path\n"
            }
        })

        # Configure logging format with timestamp
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        self._log.handlers.clear()  # Remove default handlers
        self._log.addHandler(handler)
        self._log.propagate = False  # Prevent duplicate logging

    def commands(self):
        _command1 = Subcommand("fdup", help=__doc__)
        _command1.func = self._fdup
        _command1.parser.add_option(
            "--match-threshold",    # todo: split artist and title match thresholds.
            dest="match_threshold",
            type="float",
            help="Overrides the fuzzy-match threshold for this run (1.0 == perfect match, 0.75 recommended)",
        )
        _command1.parser.add_option("--against-all",
            action="store_true",
            help="Find duplicates of query results within the whole library, not just within the query results",
        )
        _command1.parser.add_option("--group-results",
            action="store_true",
            help="Group the results",
        )
        _command2 = Subcommand("fdup-prof", help=__doc__)
        _command2.parser.add_option("--against-all",
            action="store_true",
            help="Find duplicates of query results within the whole library, not just within the query results",
        )
        _command2.func = self._fdup_profile
        return [_command1, _command2]

    def _fdup_profile(self, lib, opts, args):
        pr = cProfile.Profile()
        pr.enable()

        try:
            # your existing plugin logic
            self._fdup(lib, opts, args)
        finally:
            pr.disable()

            s = io.StringIO()
            ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
            ps.print_stats("beetsplug/fdup.py")
            ps.print_stats(200)
            ps.print_callees(50)
            ps.print_callers(50)
            print(s.getvalue())
            
    def _fdup(self, lib: Library, opts: Values, args: list[str]):

        # Set up: config, caches, etc.
        self._set_up(opts)

        # Fetch minimal information about the items for both sets 1 and 2.
        item_by_id, set1, set2 = self._fetch_items(lib, opts, args)

        # Match items 1 against items 2.
        self._log.info("Finding matches...")
        matches_generator = self._find_matches2(item_by_id, set1, set2)
        if not self.config["group_results"].get(bool):
            self._print_matches(item_by_id, matches_generator)
        else:
            self._group_matches(matches_generator, item_by_id, set1, set2)

    def _set_up(self, opts: Values):

        # Override config values with command line args where present and relevant.
        self.config.set_args(opts)

        # Initialise class fields:
        self._fieldnames: list[str] = self.config["fields"].get()
        self._fingerprinters_by_field: dict[str, TextPreprocessor] = {}
        self._compare_fps_by_field: dict[str, typing.Callable[[Fingerprint, Fingerprint], float]] = {}
        self._threshold_by_field: dict[str, float] = {}

        # Default field configs
        defaults = self.config["default_field_config"]
        default_cmp_thresh: float = defaults["match_threshold"].get(float)
        default_diff_pen: float = defaults["difference_penalty"].get(float)
        default_alphabet: str = defaults["alphabet"].get(str)
        default_word_size: int = defaults["word_size"].get(int)

        # Loop over fields:
        for fieldname in self._fieldnames:
            self._log.info(f"Setting up fingerprinting for field: {fieldname}")

            # Get config values (or use defaults if not specified).
            field_config = self.config["field_configs"][fieldname]
            alphabet = field_config["alphabet"].get(templates.String(default_alphabet))
            word_size = field_config["word_size"].get(templates.Integer(default_word_size))
            diff_pen = field_config["difference_penalty"].get(templates.Number(default_diff_pen))
            cmp_thresh = field_config["match_threshold"].get(templates.Number(default_cmp_thresh))
            inv_word_size = 1.0 / word_size

            # Cache configured instances used multiple times per field:
            self._fingerprinters_by_field[fieldname] = TextPreprocessor(alphabet=alphabet, word_size=word_size)
            self._compare_fps_by_field[fieldname] = lambda x, y: _compare_fps(x, y, inv_word_size, diff_pen)
            self._threshold_by_field[fieldname] = cmp_thresh

    def _fetch_items(self, lib: Library, opts: Values, args: list[str]) -> tuple[dict[int, MinItem], set[int], set[int]]:

        # Optionally, split the query into 2 queries for subsets 'A' and 'B' if the separator is detected.
        args1, args2 = self._split_query_args(args, opts.against_all)
        duplicate_sets: bool = args1 == args2

        # Fetch minimal information about the items for both sets A and B.
        self._log.info("Loading items from database...")
        fields = {"id", "path", "artist", "title", "length"} | set(self._fieldnames)
        items1 = _quick_fetch_items(lib, fields, args1)
        self._log.info(f"  ... items loaded for source set: {len(items1)} ({args1 if args1 else 'all items in library'})")
        items2 = _quick_fetch_items(lib, fields, args2) if not duplicate_sets else items1
        self._log.info(f"  ... items loaded for comparison set: {len(items2)} ({args2 if args2 else 'all items in library'})")

        # Reformat as into item_by_id, set1, set2
        item_by_id: dict[int, MinItem] = dict()
        set1: set[int] = set([x["id"] for x in items1])
        set2: set[int] = set([x["id"] for x in items2])
        for item in items1: item_by_id[item["id"]] = item
        for item in items2: item_by_id[item["id"]] = item

        return item_by_id, set1, set2

    @line_profiler.profile
    def _find_matches2(
            self,
            item_by_id: dict[int, MinItem],
            set1: set[int],
            set2: set[int],
            fieldname_idx: int = 0,
    ) -> Generator[tuple[int, list[Match]], Any, None]:
        """"""
        fieldname = self._fieldnames[fieldname_idx]
        compare_fps = self._compare_fps_by_field[fieldname]
        threshold = self._threshold_by_field[fieldname]
        # self._log.info(f"XYZZY: {fieldname}, with threshold {threshold}")

        # Aggregate by fieldname value.
        ids_by_value_1: dict[str, set[int]] = collections.defaultdict(set[int])
        ids_by_value_2: dict[str, set[int]] = collections.defaultdict(set[int])
        for id in set1: ids_by_value_1[unidecode(item_by_id[id][fieldname]).casefold()].add(id)
        for id in set2: ids_by_value_2[unidecode(item_by_id[id][fieldname]).casefold()].add(id)

        # Pull out various views of the sets:
        all_values = ids_by_value_1.keys() | ids_by_value_2.keys()
        common_values = set(ids_by_value_1.keys()) & set(ids_by_value_2.keys())
        set2only_values = set(ids_by_value_2.keys()).difference(ids_by_value_1.keys())

        # Fingerprint items if they haven't already been.
        fingerprinter = self._fingerprinters_by_field[fieldname]
        fp_by_value: dict[str, Fingerprint] = {}
        for value in all_values:
            if value not in fp_by_value:
                fp_by_value[value] = fingerprinter.text2fingerprint(value)

        # Calc matches for common items (will be used multiple times):
        value_matches: dict[str, set[str]] = collections.defaultdict(set[str])
        cmp_values: dict[tuple[str, str], float] = {}
        common_values_list = list(common_values)
        for i, value1 in enumerate(common_values_list):
            fp1 = fp_by_value[value1]
            for value2 in common_values_list[i + 1:]:
                cmp = compare_fps(fp1, fp_by_value[value2])
                if cmp > threshold:
                    value_matches[value1].add(value2)
                    value_matches[value2].add(value1)
                    values_pair = (value1, value2) if value1 < value2 else (value2, value1)
                    cmp_values[values_pair] = cmp

        # Loop over set1 values:
        for value1, ids1 in ids_by_value_1.items():
            is_common1 = value1 in common_values
            fp1 = fp_by_value[value1]

            # Calculate all ids in set2 whose value matches against value1.
            # If value1 is in common set, then we've already calculated all common-value matches:
            matching_ids: set[int] = set()
            cmps: dict[int, float] = {}
            if is_common1:
                matching_ids |= ids_by_value_2[value1]
                cmps.update({x: 1.0 for x in ids_by_value_2[value1]})
                for value2 in value_matches[value1]:
                    matching_ids |= ids_by_value_2[value2]
                    cmp = cmp_values[(value1, value2) if value1 < value2 else (value2, value1)]
                    cmps.update({x: cmp for x in ids_by_value_2[value2]})

            # Check for other non-common-value matches
            remaining_values2 = set2only_values if is_common1 else ids_by_value_2.keys()
            for value2 in remaining_values2:
                cmp = compare_fps(fp1, fp_by_value[value2])
                if cmp > threshold:
                    matching_ids |= ids_by_value_2[value2]
                    cmps.update({x: cmp for x in ids_by_value_2[value2]})
                    # self._log.info(f"    xyzzy: value1: {value1} now has {len(matching_ids)} matching ids")

            # If no matches, skip.
            if len(matching_ids) == 0:
                # self._log.info(f"  xyzzy: value1: {value1} has no matching ids, exiting")
                continue

            # If ids1 and matching_ids are the same single id, skip; we would remove this before returning anyway.
            if len(ids1) == 1 and len(matching_ids) == 1 and ids1 == matching_ids:
                # self._log.info(f"  xyzzy: value1: {value1} only has 1 item, and it matches itself, exiting")
                continue

            # All items in ids1 therefore match with all matching_ids.
            # If we've reached the end of the fieldnames, we can yield all these results.
            if fieldname_idx == len(self._fieldnames) - 1:
                for id1 in ids1:
                    # Remove matches of the item with itself.
                    matches = [Match(id1, x, fieldname, cmps[x]) for x in matching_ids if x != id1]
                    if len(matches) > 0:
                        yield id1, matches
                continue

            # Otherwise, filter these results further by recursively calling this function with the next fieldname.
            for id1, matches in self._find_matches2(item_by_id, ids1, matching_ids, fieldname_idx + 1):
                for match in matches:
                    match.with_fit(fieldname, cmps[match.id2])
                yield id1, matches

    def _print_matches(self, item_by_id: dict[int, MinItem], matches: Generator[tuple[int, list[Match]], Any, None]):
        match_sep = self.config["output"]["separator"].get(str)
        source_fmt = self.config["output"]["src_fmt"].get(str)
        dup_fmt = self.config["output"]["dup_fmt"].get(str)
        n_matches = 0
        for id1, matches in matches:
            item1 = item_by_id[id1]
            print_(format(item1, source_fmt), end='')
            for match in matches:
                item2 = item_by_id[match.id2]
                print_(format(item2, dup_fmt), end='')
            print_(match_sep, end='')
            n_matches += len(matches)
        self._log.info(f"  ... found {n_matches} duplicate pairs")

    def _group_matches(
            self,
            matches_by_id1: Generator[tuple[int, list[Match]], Any, None],
            items_by_id: dict[int, list[MinItem]],
            set1: set[int],
            set2: set[int]
    ):
        self._log.info("Grouping matches...")

        def edge_id(id1: int, id2: int) -> tuple[int, int]:
            return (id1, id2) if id1 < id2 else (id2, id1)

        class Edge:
            id: tuple[int, int]
            weight: float

            def __init__(self, id: tuple[int, int], weight: float):
                self.id = id
                self.weight = weight

        class Node:
            id: int
            # outgoing: list[tuple[int, float]]
            # incoming: list[tuple[int, float]]
            conn_abc: dict[str, set[int]]
            weight_by_id: dict[int, float]
            abc: str

            def __init__(self, id: int, abc: str):
                self.id = id
                self.abc = abc
                self.conn_abc = collections.defaultdict(set[int])
                self.weight_by_id = {}
                # self.incoming = []
                # self.outgoing = []

        class Group:
            ids: set[int]
            ids_abc: dict[str, set[int]]      # ids in group indexed by whether in subset a b or c
            valid_abc: dict[str, set[int]]    # ids connected to all ids in each ids_abc
            potentials: dict[int, float]      # ids that could be added along with their minimum weight.

            def __init__(self):
                self.ids = set()
                self.ids_abc = collections.defaultdict(set[int])
                self.valid_abc = collections.defaultdict(set[int])
                self.potentials = collections.defaultdict(float)

            def calc_weight(self, node: Node) -> tuple[int, float]:
                min_id: int = 0
                min_weight = float('inf')
                for _, conns in node.conn_abc.items():
                    for id in conns:
                        if id in self.ids and node.weight_by_id[id] < min_weight:
                            min_id = id
                            min_weight = node.weight_by_id[id]
                return min_id, min_weight

            def _init_conn_abc(self, node: Node, abc: str):
                self.valid_abc[abc] = node.conn_abc[abc].copy()
                self.valid_abc[abc].difference_update(self.ids_abc[abc]) # Ensure we don't already have those ids.
                self.potentials.update({x: node.weight_by_id[x] for x in self.valid_abc[abc]})

            def _update_conn_abc(self, node: Node, abc: str):
                # Update min_weight for valid nodes also connected to new node.
                # Remove potential entirely for previously valid nodes that aren't connected to new node.
                # print_(f"xyzzy: updating conn_abc for {abc}")
                for id in self.valid_abc[abc]:
                    if id in node.conn_abc[abc]:
                        # print_(f"xyzzy: maybe updating potential for {id}")
                        if node.weight_by_id[id] < self.potentials[id]:
                            # print_(f"xyzzy: updating potential for {id} to {node.weight_by_id[id]}")
                            self.potentials[id] = node.weight_by_id[id]
                    else:
                        # print_(f"xyzzy: removing potential for {id}")
                        self.potentials.pop(id, None)
                self.valid_abc[abc] &= node.conn_abc[abc]

            def add_node(self, node: Node):

                # Add node id:
                # print_(f"xyzzy: adding node {node.id} to group; BEFORE: ids: {self.ids}, ids_abc: {self.ids_abc}, valid_abc: {self.valid_abc}, potentials: {self.potentials}")
                self.ids.add(node.id)
                self.ids_abc[node.abc].add(node.id)

                # If new node is 'b' or 'c', update valid 'a' nodes; initialise if this is first 'b' or 'c' node.
                if node.abc != 'a':
                    if len(self.ids_abc['b']) + len(self.ids_abc['c']) == 1:
                        self._init_conn_abc(node, 'a')
                    else:
                        self._update_conn_abc(node, 'a')

                # Always update valid 'b' nodes; initialise if this is first node in group.
                if len(self.ids) == 1:
                    self._init_conn_abc(node, 'b')
                else:
                    self._update_conn_abc(node, 'b')

                # If new node is 'a' or 'b', update valid 'c' nodes; initialise if this is first 'a' or 'b' node.
                if node.abc != 'c':
                    if len(self.ids_abc['a']) + len(self.ids_abc['b']) == 1:
                        self._init_conn_abc(node, 'c')
                    else:
                        self._update_conn_abc(node, 'c')

                # Remove the node from the valid groups (if still present)
                self.valid_abc[node.abc].discard(node.id)
                self.potentials.pop(node.id, None)
                self.potentials = dict(sorted(self.potentials.items(), key=lambda x: x[1]))
                # print_(f"xyzzy: adding node {node.id} to group; AFTER: ids: {self.ids}, ids_abc: {self.ids_abc}, valid_abc: {self.valid_abc}, potentials: {self.potentials}")


        # Loop over matches: turn into nodes and edges:
        edges_by_ids: dict[tuple[int, int], Edge] = {}
        nodes_by_id: dict[int, Node] = {}
        sets: dict[str, set[int]] = collections.defaultdict(set[int])
        for id1, matches in matches_by_id1:
            for x in matches:

                # Create edge and nodes if they don't already exist
                eid = edge_id(id1, x.id2)
                weight = x.overall_fit()
                edge = Edge(eid, weight)
                node1 = nodes_by_id.get(x.id1, None) or Node(x.id1, 'c' if x.id1 not in set1 else 'b' if x.id1 in set2 else 'a')
                node2 = nodes_by_id.get(x.id2, None) or Node(x.id2, 'c' if x.id2 not in set1 else 'b' if x.id2 in set2 else 'a')

                # Add edge to nodes.
                node1.conn_abc[node2.abc].add(x.id2)
                node2.conn_abc[node1.abc].add(x.id1)
                node1.weight_by_id[x.id2] = weight
                node2.weight_by_id[x.id1] = weight

                # Aggregate.
                edges_by_ids[eid] = edge
                nodes_by_id[x.id1] = node1
                nodes_by_id[x.id2] = node2
                sets[node1.abc].add(node1.id)
                sets[node2.abc].add(node2.id)

        # Sort edges by weight:
        edges_by_ids = dict(sorted(edges_by_ids.items(), key=lambda x: x[1].weight))
        edges_remaining = edges_by_ids.copy()

        # Recursively create groups.
        groups: list[Group] = []
        while len(edges_remaining) > 0:

            # Pop the largest remaining edge:
            eid, edge = edges_remaining.popitem()
            # self._log.info(f" xyzzy: processing edge: {eid} with weight {edge.weight} (remaining edges: {len(edges_remaining)})")

            # Create a new group with the nodes from this edge.
            group = Group()
            group.add_node(nodes_by_id[edge.id[0]])
            group.add_node(nodes_by_id[edge.id[1]])

            # Find which ids (if any) can be added to the group.
            while True:

                # If no nodes can be added, group is complete, move on.
                if len(group.potentials) == 0:
                    break

                # Otherwise, find best potential node and to add to group.
                best_id, best_weight = group.potentials.popitem()
                # best_id: int = 0
                # best_weight: float = float('inf')
                # for potential in potentials:
                #     match_id, match_weight = group.calc_weight(nodes_by_id[potential])
                #     if match_weight < best_weight:
                #         best_weight = match_weight
                #         best_id = potential
                to_add = nodes_by_id[best_id]
                # self._log.info(f"  xyzzy: best_id: {best_id}, best_weight: {best_weight}")

                # Add the new node to the group, and any edges in edges_remaining which are used can be removed.
                for _, conns in to_add.conn_abc.items():
                    for id in conns:
                        if id in group.ids:
                            edges_remaining.pop(edge_id(to_add.id, id), None)
                group.add_node(to_add)

            # Aggregate:
            # self._log.info(f"  ... created group with {len(group.ids)} ids, {len(edges_remaining)} edges remaining")
            groups.append(group)

        # Grouping complete!
        self._log.info(f"  ... grouping finished! Created {len(groups)} groups")

        # Print groups:
        for group in sorted(groups, key=lambda x: len(x.ids), reverse=True):
            for _id in group.ids_abc['a']:
                print_(format(items_by_id[_id], " --< $artist - $title ($length) - $path "))
            for _id in group.ids_abc['b']:
                print_(format(items_by_id[_id], " --- $artist - $title ($length) - $path "))
            for _id in group.ids_abc['c']:
                print_(format(items_by_id[_id], " --> $artist - $title ($length) - $path "))
            print_("")


    def _split_query_args(self, args: list[str], against_all: bool) -> tuple[list[str] | None, list[str] | None]:
        if not args:
            self._log.info("Looking for all duplicates within entire library")
            return None, None
        try:
            separator = self.config["query_separator"].get(str)
            separator_idx = args.index(separator)
            if against_all:
                self._log.error(f"Cannot use `--against-all` with a second query (queries are separated by '{separator}').")
                raise UserError(f"Cannot use `--against-all` with a second query (queries are separated by '{separator}').")
            args1, args2 = args[:separator_idx], args[separator_idx + 1:]
            self._log.info(f"Looking for duplicates within B of items in A, where...\n\tA: {args1}\n\tB: {args2}")
            return args1, args2
        except ValueError:
            if against_all:
                self._log.info(f"Looking for duplicates within entire library for any item matching: {args}")
                return args, None
            self._log.info(f"Looking for duplicates within query results of: {args}")
            return args, args
