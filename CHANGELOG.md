# Changelog

All notable changes to beets-fdup are listed here. Versions follow [semantic versioning](https://semver.org/).

## 0.1.0 (unreleased)

First public release.

- `beet fdup` finds duplicate tracks by fuzzy matching their fields, by default the artist and title, using character
  n-gram fingerprints compared in bulk with numpy. It only reports what it finds: it never changes your library.
- Compare the tracks matching one query with each other, with the whole library (`--against-all`), or with the
  tracks matching a second query (`query1 ~~~ query2`).
- Results are listed in groups of tracks that all match each other, or with `--by-track` as each track followed by its
  duplicates. The output format (`-f/--format`, with `$fdup_labels`, `$fdup_score` and `$fdup_versions`), the tree
  prefixes and the sort order are all configurable.
- Titles are split into a core title, subtitles and version labels (live, remix, remaster, ...), so different
  versions of a track match and are labelled (or, with `match_versions: no`, don't match), while different part
  numbers, such as `Part 2` and `Part 3`, don't match.
- Where both tracks have a MusicBrainz work ID (`exact_key`), it decides whether their titles match.
- Per-field settings: the match threshold, the scoring metric, extra normalisation rules, and the qualifier rules.
- Requires Python 3.11+, beets 2.10+ (tested up to 2.14) and numpy 2.0+.
