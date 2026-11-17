"""Profile a beet command (e.g. `beet fdup`) with cProfile.

Usage, from the project's virtualenv:
    python scripts/profile_fdup.py [beet arguments...]
e.g.
    python scripts/profile_fdup.py fdup --group-results > /dev/null
    python scripts/profile_fdup.py -c path/to/config.yaml fdup artist:queen

The command's output goes to stdout as usual. The profile goes to stderr: fdup's own functions sorted by cumulative
time, then the slowest 50 overall.

For line-by-line timings of particular modules, use line_profiler's kernprof instead (module names, not paths):
    kernprof -lv -o /tmp/fdup.lprof -p beetsplug.fdup.matching -p beetsplug.fdup.fingerprint "$(which beet)" fdup

(Don't name a script here profile.py: it would hide Python's own `profile` module, which cProfile imports.)
"""

import cProfile
import pstats
import sys

from beets.ui import main


def profile(args: list[str]):
    profiler = cProfile.Profile()
    try:
        profiler.runcall(main, args)
    except SystemExit:
        pass
    stats = pstats.Stats(profiler, stream=sys.stderr).sort_stats("cumulative")
    stats.print_stats(r"beetsplug/fdup/", 40)
    stats.print_stats(50)


if __name__ == "__main__":
    profile(sys.argv[1:])
