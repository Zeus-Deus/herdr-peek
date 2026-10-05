#!/usr/bin/env python3
"""Print an agent-style transcript that mentions files, then wait.

Used by the e2e test and the screenshots so the pane looks like a coding agent
just finished. It is a canned transcript, not a real agent.
"""

import os
import sys
import time

O = "\033[38;2;240;160;112m"
D = "\033[2m"
B = "\033[1m"
R = "\033[0m"

lines = [
    "%s>%s screenshot the login flow and write up the results" % (D, R),
    "",
    "%s●%s I captured both themes, recorded the flow and wrote a short report." % (O, R),
    # an OSC 8 hyperlink, the way `ls --hyperlink` / `eza --hyperlink` print paths
    "  Coverage: \033]8;;file://%s/docs/coverage.html\033\\docs/coverage.html\033]8;;\033\\" % os.getcwd(),
    "",
    "%s●%s Ran %spython3 src.py --themes light,dark%s" % (O, R, B, R),
    "  %s⎿  2 screenshots · 1 recording · 6.0s%s" % (D, R),
    "",
    "%s●%s Saved to:" % (O, R),
    "    docs/report.md",
    "    data/results.csv",
    "    docs/report.pdf",
    "    shots/flow.mp4",
    "    shots/loading.gif",
    "    shots/login-light.png",
    "    shots/login-dark.png",
    "",
    "  Both themes pass; dark renders ~0.1s faster.",
    "",
]

sys.stdout.write("\033[2J\033[H" + "\n".join(lines) + "\n")
sys.stdout.flush()
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    pass
