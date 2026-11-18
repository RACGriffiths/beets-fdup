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

Fuzzy matching can give false positives and false negatives, so fdup only reports what it finds: it never changes,
moves or deletes anything. Human review of the output, rather than automated processing, is highly recommended.

Where both tracks have a MusicBrainz work ID, fdup uses it to decide whether their titles match, so there its results
are close to those of `beet duplicates --strict -k mb_artistid -k mb_workid`. The difference is that fdup also finds
duplicates among tracks that haven't been matched with MusicBrainz, or have no `mb_workid`, by matching their titles.
That works well on its own: on a 42,000-track test library, with work IDs ignored, fdup found 98% of the pairs of
tracks with the same artist and work IDs, and where both tracks in a pair it found had a work ID, it was the same work
96% of the time.

Care has been taken to keep it efficient: in testing, a 42,000-track library took about 5 seconds (on a laptop, with
the default config). But fuzzy matching compares every pair of values, so the time grows with the square of the
library's size, and *extremely* large libraries may still take a while.

## Installation

Install it into the same Python environment as beets:

```bash
pip install beets-fdup
```

(If you installed beets with pipx: `pipx inject beets beets-fdup`.)

Then enable it in your beets config (`beet config -e`):

```yaml
plugins: fdup
```

Requires Python 3.11+ and beets 2.10+.

## Usage

```bash
beet fdup                                   # all duplicates in the library, in groups that all match each other
beet fdup artist:queen                      # duplicates among Queen tracks
beet fdup --against-all added:-1w..         # tracks added in the last week with a duplicate anywhere in the library
beet fdup year:..1980 ~~~ year:1981..       # duplicates of pre-1981 tracks among later ones
beet fdup --by-track                        # each track, followed by its duplicates, instead of groups (longer output)
beet fdup --match-threshold 0.9             # stricter matching (1.0 = perfect match; the default, 0.75, is recommended)
beet fdup -f '$album: $title'               # print each track with this format (also --format)
beet fdup artist:queen year-                # sorted by year, newest first, instead of the configured sort
```

Queries use the normal [beets query syntax](https://beets.readthedocs.io/en/stable/reference/query.html). With one
query, tracks are only compared with other tracks matching it, unless you add `--against-all`. With two queries
separated by `~~~`, tracks matching the first are compared with tracks matching the second.

Output lists groups of tracks that all match each other, largest first and then by artist and title, with any version
labels. Within each group, tracks are sorted by path (see [Sorting](#sorting)):

```
┌── Queen - One Vision (5:11) - /music/Queen/A Kind of Magic/01 One Vision.mp3
├── Queen - One Vision (4:02) - /music/Queen/Greatest Hits II/09 One Vision.mp3
└── Queen - One Vision (Extended Version) (6:26) [extended] - /music/Queen/Singles/12 One Vision.mp3
```

A track can be in more than one group, when it matches tracks that don't match each other: `Revolution` matches both
`Revolution 1` and `Revolution 9`, which have different numbers, so it's listed with each.

Each track is marked by which query it came from: `◂` the first query only, `▸` the second query only (with
`--against-all`, the rest of the library), `─` both (so always `─` with one query). For example, with two queries:

```
┌─◂ Johnny Cash - I Walk the Line (2:41) - /music/Charts/Billboard Hot 100 - 1956/81 I Walk the Line.mp3
├─▸ Johnny Cash - I Walk the Line (2:42) - /music/Johnny Cash/At His Best/03 I Walk the Line.mp3
└─▸ Johnny Cash - I Walk the Line (live) (2:57) [live] - /music/Johnny Cash/At San Quentin/03 I Walk the Line.mp3
```

With `--by-track`, each track is listed instead (by artist and title), followed by its duplicates (best match first).
With one query, each pair of duplicates appears twice, once under each track:

```
Queen - One Vision (4:02) - /music/Queen/Greatest Hits II/09 One Vision.mp3
├─ Queen - One Vision (5:11) - /music/Queen/A Kind of Magic/01 One Vision.mp3
└─ Queen - One Vision (Extended Version) (6:26) [extended] - /music/Queen/Singles/12 One Vision.mp3
```

## Configuration

Set these under `fdup:` in your beets config. The settings you're most likely to change, with their defaults:

```yaml
fdup:
  fields: [artist, title]     # text fields; tracks must match on all of these
  group_results: yes          # no: list each track with its duplicates, as --by-track

  default_field_config:       # settings for every field...
    match_threshold: 0.75     # how alike values must be, from 0 to 1 (1.0 = identical)
    extra_normalise: []       # [regex, replacement] rules that make values equal before they're compared

  field_configs:              # ...and for one field, overriding those
    title:
      exact_key: mb_workid    # same MusicBrainz work: a match, even if the titles differ; different works: never
      qualifiers:
        match_versions: yes   # no: don't match different versions of a track (live, remix, ...)

  output:
    format: "$artist - $title ($length)$fdup_labels - $path"   # each track (as -f/--format)
```

Set only what you want to change: everything else keeps its default. A setting under `field_configs.<field>`
overrides `default_field_config` for that field. For example, to require closer artist matches but leave titles as
they are:

```yaml
fdup:
  field_configs:
    artist:
      match_threshold: 0.9
```

`--match-threshold` sets the threshold of every field for one run. `exact_key: null` matches titles on their text
alone, even where both tracks have a work ID.

`extra_normalise` rules are regular expressions, applied to each value after it's lower-cased and its accents
removed. Use them to make spellings fdup doesn't know count as identical, e.g. `St. Elmo's Fire` and
`Saint Elmo's Fire`:

```yaml
fdup:
  default_field_config:
    extra_normalise:
      - ['\bst\b', 'saint']
```

The other settings, such as the scoring and the output layout, are under
[Advanced configuration](#advanced-configuration).

### Output format

Each track is printed after a tree-style prefix, formatted with `output.format` (or `-f`) as a
[beets template](https://beets.readthedocs.io/en/stable/reference/pathformat.html). It can use any item field
(`$album`, `$year`, flexible attributes, ...), template functions such as `%upper{}` and `%if{}`, and fdup's own:

- `$fdup_labels`: the track's version labels, e.g. ` [live, remaster]` (with a leading space, or empty if none).
- `$fdup_score`: in groups, the track's weakest match with the others in its group (that it was compared with); with
  `--by-track`, a duplicate's match score with the track above it (empty on the track's own line).
- `$fdup_versions`: with `--by-track`, the version labels that differ between a duplicate and the track above it, in
  the same form as `$fdup_labels` (empty on the track's own line, and in groups).

Computed fields such as `$filesize` aren't available, and nor are `%aunique{}`, `%sunique{}` and `%ifdef{}`, which need
the whole item or the library.

### Sorting

Each mode has two sorts, in beets'
[sort syntax](https://beets.readthedocs.io/en/stable/reference/query.html#sort-order): fields, each followed by `+`
(ascending) or `-` (descending), with each field breaking ties in the ones before it. The defaults:

```yaml
fdup:
  output:
    groups:
      sort: fdup_size- artist+ title+   # the groups: largest first, then by their first track
      sort_tracks: path+                # the tracks in each group, after the query order the markers show
    by_track:
      sort: artist+ title+ path+        # each track (with its duplicates)
      sort_tracks: fdup_score- path+    # each track's duplicates: best match first
```

- `sort` orders the blocks (groups, or tracks with their duplicates). A field sorts by the block's first line, except
  `fdup_size`, the number of tracks in the block, and `fdup_score`, its lowest score.
- `sort_tracks` orders the tracks within each block. In groups, tracks stay in query order (`◂`, then `─`, then `▸`)
  and are sorted within that; with `--by-track`, only the duplicates are sorted, under their track.

Any item field can be used, flexible attributes included, as can `fdup_score` and fdup's other `$fdup_*` values. As in
beets, `artist` and `albumartist` sort by `artist_sort` and `albumartist_sort` where those are set, text sorts
case-insensitively (unless beets' `sort_case_insensitive` is off), and a missing value sorts lowest. Computed fields
such as `filesize` can't be used. An empty sort leaves that order unsorted: groups in the order they were made (each
from the best match left), tracks in a group in the order they joined it, and with `--by-track`, by id.

A sort in the query replaces `sort` for that run, as in `beet ls`: `beet fdup artist:queen year-` lists the groups by
the year of their first track, newest first.

### Versions

Titles are split into a core and qualifiers: bracketed parts, and anything after a separator such as ` - `. A
qualifier containing a version marker (e.g. `live`) gives the track a version label; any other is a subtitle. So
`(Oh) Pretty Woman (Live) - 2011 Remaster` has the core `pretty woman`, the subtitle `oh` and the labels `live` and
`remaster`.

Different versions of a track match, with their labels shown. For example, to stop reporting live versions and remixes
as duplicates of the studio track, and treat `(cover)` as a version marker:

```yaml
fdup:
  field_configs:
    title:
      qualifiers:
        match_versions: no
        version_markers:
          cover: cover
```

The default version markers, and how titles are split, are under [Qualifiers](#qualifiers).

### Empty values

A track with an empty value in a matched field (no title, say) matches nothing, so untagged tracks aren't all reported
as duplicates of each other. A value that a `normalise` rule removes entirely counts as empty too. For example, to skip
generic titles such as `Intro`, which match between unrelated albums by the same artist:

```yaml
fdup:
  field_configs:
    title:
      extra_normalise:
        - ['^(intro|outro|interlude)$', '']
```

## How it works

1. **Normalise** each value: remove accents, lower-case, apply the `normalise` rules. A value of only symbols, such as
   an emoji title, is spelled out with Unicode names instead (`👋` → `waving hand sign`), so different symbols differ.
   An empty value matches nothing.
2. **Split** values into a core, subtitles and version labels (fields with `qualifiers`).
3. **Fingerprint** each distinct value: hash its character n-grams into a bit array, ignoring characters that aren't in
   the `alphabet` (so `Help!` and `Help` are the same).
4. **Compare** fingerprints in bulk with numpy, scoring the shared bits against what chance would give. Candidate
   pairs are then checked with the qualifier rules (shared subtitles, part numbers, alternate titles, ...).
5. **Combine** fields: items match if they match on every field (fields are matched in order, each narrowing the
   candidates for the next). An `exact_key` overrides the fuzzy result where both items have one.

## Limitations

- fdup compares tags, not audio. Different recordings with the same tags match, and duplicates whose tags are wrong
  aren't found. For copies of the same audio, beets' `duplicates` plugin can compare checksums (`--checksum`).
- Different songs with the same title by the same artist match, e.g. two songs called `Intro`. Where both tracks have
  a MusicBrainz work ID, `exact_key` keeps different works apart; you can also skip generic titles (see
  [Empty values](#empty-values)).
- Artists are compared with the `overlap` metric, so an artist matches one whose name contains theirs: `Jimi Hendrix`
  and `The Jimi Hendrix Experience`, or `Calvin Harris` and `Calvin Harris feat. Rihanna`, but also `Prince` and
  `Prince Buster`. `metric: dice` for `artist` is stricter: it still matches `The Beatles` and `Beatles`, but none of
  those three.

## Advanced configuration

These settings tune how values are scored and how the output is laid out. The defaults were tuned on a 42,000-track
library, so change them with care, and compare the results before and after. All of them, with their defaults:

```yaml
fdup:
  fields: [artist, title]
  group_results: yes          # no: as --by-track (and --group-results overrides no)
  query_separator: "~~~"      # separates two queries on the command line

  default_field_config:       # settings for every field...
    match_threshold: 0.75     # minimum score to match, from 0 to 1 (1.0 = identical)
    metric: overlap           # overlap: relative to the shorter value, so 'abba' matches 'abba & friends';
                              # dice: relative to both, so a short value doesn't match a longer one containing it
    difference_penalty: 0.025 # how much n-grams in only one of the values lower the score
    word_size: 1536           # fingerprint size in bits
    alphabet: "abcdefghijklmnopqrstuvwxyz 0123456789"   # characters that count; others (punctuation, ...) are ignored
    normalise:                # [regex, replacement] pairs, applied after removing accents and lower-casing
      - ['\s*&\s*', ' and ']
      - ['(?<=\b\w)\.(?=\w\b)', '']     # s.o.s. -> sos
      - ['(?<=\b[a-z])-(?=[a-z]\b)', '']  # d-i-v-o-r-c-e -> divorce
      - ['(?<=\w)-(?=\w)', ' ']         # be-bop-a-lula -> be bop a lula
    extra_normalise: []       # more rules, applied after these (so you keep the defaults)
    qualifiers: null          # how to split values into a core and qualifiers; null: don't (see Qualifiers)
    exact_key: null           # a field which, when both items have it, decides the match on its own

  field_configs:              # ...overridden per field
    title:
      metric: dice
      exact_key: mb_workid    # same MusicBrainz work: match, even if the titles differ; different works: don't
      qualifiers: { ... }     # see Qualifiers

  output:
    format: "$artist - $title ($length)$fdup_labels - $path"   # each track (as -f/--format), after its prefix
    groups:                   # groups of tracks that all match each other
      # format: ...           # overrides output.format for this mode
      prefix:                 # by the line's position in the block
        first: "┌─"
        middle: "├─"
        last: "└─"
      marker:                 # after the prefix: which query the track came from
        query1: "◂ "          # the first query only
        both: "─ "
        query2: "▸ "          # the second query only (with --against-all, the rest of the library)
      separator: "\n"         # printed between blocks ("\n": a blank line)
      sort: fdup_size- artist+ title+   # see Sorting
      sort_tracks: path+
    by_track:                 # with --by-track: each track, then its duplicates
      # format: ...
      prefix:
        first: ""             # the track
        middle: "├─ "         # its duplicates...
        last: "└─ "           # ...and the last one
      separator: "\n"
      sort: artist+ title+ path+
      sort_tracks: fdup_score- path+
```

### Scoring

Each value's fingerprint has a bit set for each of its character n-grams (see [How it works](#how-it-works)). Two
values score by how many bits they share, beyond what chance would give:

- `metric`: what the shared bits are relative to. With `overlap`, the shorter value's bits, so a value contained in a
  longer one scores highly (`abba` and `abba & friends`, or an artist and the same artist featuring another). With
  `dice`, both values' bits, so it doesn't. Titles use `dice`, so that `Up!` doesn't match `Giddy Up!`.
- `difference_penalty`: how much each bit in only one of the two values lowers the score. Raising it makes extra
  words count against a match more (with `overlap`, a short value inside a long one scores lower). Keep it low (under
  about 0.05): extra words, such as a `(live)` qualifier, don't make a value a different song.
- `word_size`: the size of each fingerprint, in bits. In larger ones, different n-grams share a bit by chance less
  often, so long values score more precisely, but matching takes longer and uses more memory.
- `alphabet`: the characters that count. Values are lower-cased and their accents removed first, so it only needs
  lower-case letters.

### Qualifiers

How a field's values are split into a core and qualifiers (see [Versions](#versions)). Only titles are split by
default. Each setting you leave out keeps its default:

```yaml
qualifiers:
  brackets: ["()", "[]", "{}"]
  separators: ['\s+-+\s+', ':\s+']    # regexes
  version_markers:            # word: label ('' marks a version without a label). Set a word to null to remove it.
    live: live
    remastered: remaster
    # ... (the full list is below)
  version_marker_patterns:    # regex: label
    '\b(19|20)\d\d\b': ''
  alias_threshold: 0.9        # how well a subtitle alone must match the other title (alternate titles)
  subtitle_threshold: 0.75    # if both have subtitles, how well at least one pair must match
  check_numbers: yes          # different part/movement numbers (part 2 vs part 3) mean different tracks
  match_versions: yes         # match different versions (live vs studio, ...); no: only the same version
```

The default version markers, by the label they give:

| Label | Words |
|---|---|
| `live` | live, unplugged |
| `remix` | remix, mix, rmx, dub |
| `remaster` | remaster, remastered |
| `instrumental` | instrumental, karaoke |
| `alternate` | alternate, alternative, outtake |
| `session` | session, sessions |
| (the word itself) | edit, extended, demo, acoustic, mono, stereo, commentary, reprise, rehearsal |
| (no label) | version, bonus, explicit, feat, featuring, ft, and a year (`version_marker_patterns`) |

Your `version_markers` are added to these.

A subtitle can be an alternate title: `Con Te Partirò` matches `Time to Say Goodbye (Con Te Partirò)` because the
subtitle alone matches the other title, by at least `alias_threshold`. When both titles have subtitles that they don't
share, at least one pair of them must match by `subtitle_threshold`, so two movements of a work
(`The Gondoliers: Take a Pair of Sparkling Eyes` and `The Gondoliers: In Enterprise of Martial Kind`) don't match.

`check_numbers` compares the numbers in the core and subtitles, as digits: any number in digits or Roman numerals (a
lone `I` only as the last word, since elsewhere it's usually the pronoun), and number words after a word such as
`part`, `pt`, `movement`, `chapter`, `act`, `book`, `episode` or `vol`. So `Part Two`, `Part II` and `Part 2` are the
same number, and `Part One` vs `Part Two` don't match. Other number words are just words (`One Love`, `No One`,
`Take Five`).

### Output layout

Each line is a prefix, set by the line's position in its block, then (in groups) the marker for which query the track
came from, then the track in the output format. Each mode can also have its own `format`, overriding
`output.format`. If your terminal can't show the tree characters, use ASCII ones, e.g. `|- ` and `` `- ``.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"   # editable install: code changes take effect without reinstalling
.venv/bin/pytest                    # tests
.venv/bin/ruff check .              # lint
```

CI (`.github/workflows/ci.yml`) runs the tests on every push and pull request, and weekly, with the oldest supported
versions (Python 3.11, beets 2.10, numpy 2.0) and with the latest beets on Python 3.12 to 3.14, runs ruff, and builds
the package. `.github/workflows/beets-dev.yml` also runs the tests weekly against beets' development branch, to find
beets changes that break fdup before they're released: when they fail, it opens an issue. Pushing a version tag
publishes that version to PyPI (`.github/workflows/release.yml`, which describes the steps).

Layout:

| Path | What's there |
|---|---|
| `beetsplug/fdup/__init__.py` | the plugin: the `fdup` command and its options |
| `beetsplug/fdup/config.py` | default config, building a `FieldMatcher` per field and the output settings |
| `beetsplug/fdup/items.py` | fast fetching of just the needed fields, and formatting them (uses some beets internals) |
| `beetsplug/fdup/parsing.py` | normalising, splitting values into core / subtitles / labels |
| `beetsplug/fdup/fingerprint.py` | n-gram fingerprints, bulk comparison with numpy |
| `beetsplug/fdup/matching.py` | per-field fuzzy matching, combined into item matches |
| `beetsplug/fdup/grouping.py` | grouping matches into groups that all match each other (the default output) |
| `beetsplug/fdup/sorting.py` | sort terms (beets' syntax), and sorting the output by them |
| `beetsplug/fdup/output.py` | printing matches and groups, sorted, with their prefixes |
| `tests/` | pytest tests; `test_plugin.py` runs `beet fdup` end to end on a temporary library |
| `scripts/profile_fdup.py` | profile a `beet fdup` run with cProfile (or see its docstring for line_profiler) |

`beetsplug/` deliberately has no `__init__.py`: it's a namespace package shared with beets and its other plugins.

## License

MIT: see [LICENSE](https://github.com/RACGriffiths/beets-fdup/blob/main/LICENSE).
