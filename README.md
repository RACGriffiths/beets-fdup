# beets-fdup

A [beets](https://beets.io/) plugin that finds duplicate tracks in your library by **fuzzy matching** their fields, by
default the artist and title.

beets' built-in [`duplicates`](https://beets.readthedocs.io/en/stable/plugins/duplicates.html) plugin finds items
with identical keys (e.g. the same MusicBrainz ID). `fdup` finds tracks that *look* the same even when their tags
differ, for example:

- `The Beatles - Hey Jude` and `Beatles - Hey Jude (Remastered 2009)`
- `Blur - Coffee & TV` and `Blur - Coffee and TV`
- `Andrea Bocelli - Con Te Partirò` and `Andrea Bocelli - Time to Say Goodbye (Con Te Partirò)`

and labels the differences between versions (live, remix, remaster, ...), while not matching titles such as
`Exogenesis: Symphony, Part 2` and `Part 3`.

## Installation

Install it into the same Python environment as beets:

```bash
pip install git+https://gitlab.com/racgriffiths/beet-fdup.git
```

(If you installed beets with pipx: `pipx inject beets git+https://gitlab.com/racgriffiths/beet-fdup.git`.)

Then enable it in your beets config (`beet config -e`):

```yaml
plugins: fdup
```

Requires Python 3.11+ and beets 2.10+.

## Usage

```bash
beet fdup                                   # all duplicates in the library
beet fdup artist:queen                      # duplicates among Queen tracks
beet fdup --against-all album:greatest      # tracks on 'greatest...' albums with a duplicate anywhere in the library
beet fdup year:..1980 ~~~ year:1981..       # duplicates of pre-1981 tracks among later ones
beet fdup --group-results                   # groups of tracks that all match each other
beet fdup --match-threshold 0.9             # stricter matching (1.0 = perfect match)
beet fdup -f '$album: $title'               # print each track with this format (also --format)
```

Queries use the normal [beets query syntax](https://beets.readthedocs.io/en/stable/reference/query.html). With one
query, tracks are only compared with other tracks matching it, unless you add `--against-all`. With two queries
separated by `~~~`, tracks matching the first are compared with tracks matching the second.

Output lists each track, followed by its duplicates (best match first), with any version labels:

```
Queen - One Vision (4:02) - /music/Queen/Greatest Hits II/09 One Vision.mp3
├─ Queen - One Vision (5:11) - /music/Queen/A Kind of Magic/01 One Vision.mp3
└─ Queen - One Vision (Extended Version) (6:26) [extended] - /music/Queen/Singles/12 One Vision.mp3
```

With `--group-results`, each group is printed together, each track marked by which query it came from: `◂` the
first query only, `▸` the second query only (with `--against-all`, the rest of the library), `─` both:

```
┌─◂ Johnny Cash - I Walk the Line (2:41) - /music/Charts/Billboard Hot 100 - 1956/81 I Walk the Line.mp3
├─▸ Johnny Cash - I Walk the Line (2:42) - /music/Johnny Cash/At His Best/03 I Walk the Line.mp3
└─▸ Johnny Cash - I Walk the Line (live) (2:57) [live] - /music/Johnny Cash/At San Quentin/03 I Walk the Line.mp3
```

## Configuration

These are the defaults; set any of them under `fdup:` in your beets config.

```yaml
fdup:
  fields: [artist, title]     # items must match on all of these
  group_results: no           # as --group-results
  query_separator: "~~~"

  default_field_config:       # settings for every field...
    match_threshold: 0.75     # minimum score to match (1.0 = identical)
    metric: overlap           # overlap: relative to the shorter value, so 'abba' matches 'abba & friends';
                              # dice: relative to both, so a short value doesn't match a longer one containing it
    difference_penalty: 0.025
    word_size: 1536           # fingerprint size in bits
    alphabet: "abcdefghijklmnopqrstuvwxyz 0123456789"
    normalise:                # [regex, replacement] pairs, applied after removing accents and lower-casing
      - ['\s*&\s*', ' and ']
      - ['(?<=\b\w)\.(?=\w\b)', '']     # s.o.s. -> sos
    extra_normalise: []       # more rules, applied after these (so you keep the defaults)
    qualifiers: null          # how to split values into a core and qualifiers; null: don't (see below)
    exact_key: null           # a field which, when both items have it, decides the match on its own

  field_configs:              # ...overridden per field
    title:
      metric: dice
      exact_key: mb_workid    # same MusicBrainz work: match, even if the titles differ; different works: don't
      qualifiers: { ... }     # the defaults below

  output:
    format: "$artist - $title ($length)$fdup_labels - $path"   # each track (as -f/--format), after its prefix
    matches:                  # each track, then its duplicates
      # format: ...           # overrides output.format for this mode
      prefix:                 # by the line's position in the block
        first: ""             # the track
        middle: "├─ "         # its duplicates...
        last: "└─ "           # ...and the last one
      separator: "\n"         # printed between blocks ("\n": a blank line)
    groups:                   # with --group-results
      # format: ...
      prefix:
        first: "┌─"
        middle: "├─"
        last: "└─"
      marker:                 # after the prefix: which query the track came from
        query1: "◂ "          # the first query only
        both: "─ "
        query2: "▸ "          # the second query only (with --against-all, the rest of the library)
      separator: "\n"
```

Each line is the prefix (and in groups, the marker), then the track, formatted as a
[beets template](https://beets.readthedocs.io/en/stable/reference/pathformat.html). It can use any item field
(`$album`, `$year`, flexible attributes, ...), template functions such as `%upper{}` and `%if{}`, and fdup's own:

- `$fdup_labels`: the track's version labels, e.g. ` [live, remaster]` (with a leading space, or empty if none).
- `$fdup_score`: in the default mode, a duplicate's match score with the track above it (empty on the track's own
  line); in groups, the track's weakest match with the others in its group (that it was compared with).
- `$fdup_versions`: in the default mode, the version labels that differ between a duplicate and the track above it,
  in the same form as `$fdup_labels` (empty on the track's own line, and in groups).

Computed fields such as `$filesize` aren't available, and nor are `%aunique{}`, `%sunique{}` and `%ifdef{}`, which need
the whole item or the library. If your terminal can't show the tree characters, use ASCII ones, e.g. `|- ` and `` `- ``.

### Qualifiers

With `qualifiers` set, a value is split into a core and qualifiers: bracketed parts, and anything after a separator
such as ` - `. A qualifier containing a version marker (e.g. `live`) gives a version label; any other is a subtitle.
So `(Oh) Pretty Woman (Live) - 2011 Remaster` has the core `pretty woman`, the subtitle `oh` and the labels
`live` and `remaster`.

```yaml
qualifiers:
  brackets: ["()", "[]", "{}"]
  separators: ['\s+-+\s+', ':\s+']    # regexes
  version_markers:            # word: label ('' marks a version without a label). Set a word to null to remove it.
    live: live
    remastered: remaster
    # ... see DEFAULT_QUALIFIERS in beetsplug/fdup/config.py for the full list
  version_marker_patterns:    # regex: label
    '\b(19|20)\d\d\b': ''
  alias_threshold: 0.9        # how well a subtitle alone must match the other title (alternate titles)
  subtitle_threshold: 0.75    # if both have subtitles, how well at least one pair must match
  check_numbers: yes          # different part/movement numbers (part 2 vs part 3) mean different tracks
  match_versions: yes         # match different versions (live vs studio, ...); no: only the same version
```

For example, to stop reporting live versions and remixes as duplicates of the studio track, and treat `(cover)` as a
version marker:

```yaml
fdup:
  field_configs:
    title:
      qualifiers:
        match_versions: no
        version_markers:
          cover: cover
```

## How it works

1. **Normalise** each value: remove accents, lower-case, apply the `normalise` rules.
2. **Split** values into a core, subtitles and version labels (fields with `qualifiers`).
3. **Fingerprint** each distinct value: hash its character n-grams into a bit array.
4. **Compare** fingerprints in bulk with numpy, scoring the shared bits against what chance would give. Candidate
   pairs are then checked with the qualifier rules (shared subtitles, part numbers, alternate titles, ...).
5. **Combine** fields: items match if they match on every field (fields are matched in order, each narrowing the
   candidates for the next). An `exact_key` overrides the fuzzy result where both items have one.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"   # editable install: code changes take effect without reinstalling
.venv/bin/pytest                    # tests
.venv/bin/ruff check .              # lint
```

Layout:

| Path | What's there |
|---|---|
| `beetsplug/fdup/__init__.py` | the plugin: the `fdup` command and its options |
| `beetsplug/fdup/config.py` | default config, building a `FieldMatcher` per field and the output settings |
| `beetsplug/fdup/items.py` | fast fetching of just the needed fields, and formatting them (uses some beets internals) |
| `beetsplug/fdup/parsing.py` | normalising, splitting values into core / subtitles / labels |
| `beetsplug/fdup/fingerprint.py` | n-gram fingerprints, bulk comparison with numpy |
| `beetsplug/fdup/matching.py` | per-field fuzzy matching, combined into item matches |
| `beetsplug/fdup/grouping.py` | `--group-results` |
| `beetsplug/fdup/output.py` | printing matches and groups, with their prefixes |
| `tests/` | pytest tests; `test_plugin.py` runs `beet fdup` end to end on a temporary library |
| `scripts/profile_fdup.py` | profile a `beet fdup` run with cProfile (or see its docstring for line_profiler) |

`beetsplug/` deliberately has no `__init__.py`: it's a namespace package shared with beets and its other plugins.
