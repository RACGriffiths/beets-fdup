"""fdup: a beets plugin that finds duplicate tracks by fuzzy matching fields such as artist and title.

How it fits together:
  config.py       defaults, and a FieldMatcher for each configured field
  items.py        fetching just the needed fields from the library (fast), and formatting them
  parsing.py      normalising values, and splitting titles into a core, subtitles and version labels
  fingerprint.py  n-gram fingerprints, compared in bulk with numpy
  matching.py     per-field fuzzy matching, combined into item matches
  grouping.py     grouping matches (--group-results)
  output.py       printing
"""

from optparse import Values

from beets.library import Library
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand, UserError

from .config import DEFAULT_CONFIG, build_field_matchers
from .grouping import group_matches
from .items import MinItem, fetch_items
from .matching import FieldMatcher, find_matches, index_items
from .output import print_groups, print_matches

# Fields fetched for every item, for the default output formats.
_OUTPUT_FIELDS = {"id", "path", "artist", "title", "length"}


class FDupPlugin(BeetsPlugin):
    """List duplicate tracks based on fuzzy matching of artist and title."""

    def __init__(self):
        super().__init__("fdup")
        self.config.add(DEFAULT_CONFIG)

    def commands(self):
        command = Subcommand("fdup", help="list duplicate tracks, by fuzzy matching")
        command.parser.usage += (
            " [QUERY] [~~~ QUERY2]"
            "\nExample: %prog artist:queen ~~~ year:2000..  (duplicates of Queen tracks among tracks from 2000 on)")
        command.parser.add_option(
            "--match-threshold",    # todo: split artist and title match thresholds.
            dest="match_threshold",
            type="float",
            help="Overrides the fuzzy-match threshold for this run (1.0 == perfect match, 0.75 recommended)",
        )
        command.parser.add_option(
            "--against-all",
            action="store_true",
            help="Find duplicates of query results within the whole library, not just within the query results",
        )
        command.parser.add_option(
            "--group-results",
            action="store_true",
            help="Group the results",
        )
        command.func = self._fdup
        return [command]

    def _fdup(self, lib: Library, opts: Values, args: list[str]):

        # Override config values with command line args where present and relevant.
        self.config.set_args(opts)
        fields = build_field_matchers(self.config, getattr(opts, "match_threshold", None))
        self._log.debug("matching on fields: {}", ", ".join(field.name for field in fields))

        # Fetch minimal information about the items for both sets 1 and 2.
        item_by_id, set1, set2 = self._fetch_items(lib, opts, args, fields)

        # Normalise, parse and fingerprint each distinct value of each field once, up front.
        self._log.debug("fingerprinting...")
        index_items(fields, item_by_id)

        # Match items 1 against items 2.
        self._log.debug("finding matches...")
        matches = find_matches(fields, set1, set2)
        if not self.config["group_results"].get(bool):
            output = self.config["output"]
            n_matches = print_matches(matches, item_by_id, output["src_fmt"].get(str), output["dup_fmt"].get(str),
                                      output["separator"].get(str))
            self._log.info("found {} duplicate pairs", n_matches)
        else:
            groups = group_matches(matches, set1, set2)
            print_groups(groups, item_by_id)
            self._log.info("found {} groups of duplicates", len(groups))

    def _fetch_items(
            self, lib: Library, opts: Values, args: list[str], fields: list[FieldMatcher],
    ) -> tuple[dict[int, MinItem], set[int], set[int]]:

        # Optionally, split the query into 2 queries for subsets 'A' and 'B' if the separator is detected.
        args1, args2 = self._split_query_args(args, opts.against_all)
        duplicate_sets: bool = args1 == args2

        # Fetch minimal information about the items for both sets A and B.
        needed = _OUTPUT_FIELDS | {field.name for field in fields} | {
            field.exact_key for field in fields if field.exact_key}
        items1 = fetch_items(lib, needed, args1)
        self._log.debug("items loaded for source set: {} ({})", len(items1), args1 or "all items in library")
        items2 = fetch_items(lib, needed, args2) if not duplicate_sets else items1
        self._log.debug("items loaded for comparison set: {} ({})", len(items2), args2 or "all items in library")

        # Reformat as into item_by_id, set1, set2
        item_by_id: dict[int, MinItem] = {}
        set1: set[int] = {x["id"] for x in items1}
        set2: set[int] = {x["id"] for x in items2}
        for item in items1:
            item_by_id[item["id"]] = item
        for item in items2:
            item_by_id[item["id"]] = item

        return item_by_id, set1, set2

    def _split_query_args(self, args: list[str], against_all: bool) -> tuple[list[str] | None, list[str] | None]:
        if not args:
            self._log.debug("looking for all duplicates within entire library")
            return None, None
        separator = self.config["query_separator"].get(str)
        if separator in args:
            if against_all:
                raise UserError(f"fdup: cannot use --against-all with a second query (queries are separated by "
                                f"'{separator}')")
            separator_idx = args.index(separator)
            args1, args2 = args[:separator_idx], args[separator_idx + 1:]
            self._log.debug("looking for duplicates within B of items in A, where A: {}, B: {}", args1, args2)
            return args1, args2
        if against_all:
            self._log.debug("looking for duplicates within entire library for any item matching: {}", args)
            return args, None
        self._log.debug("looking for duplicates within query results of: {}", args)
        return args, args
