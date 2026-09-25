"""The config `up init` writes."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping

from upkeep.presets import PRESETS

HEADER = """\
# upkeep config: https://github.com/wmxscott/upkeep#configuration

[schedule]
# Daily and weekly tools become due at or after this local time.
at = "08:00"
# How commands, and the scheduled job, are started. Default: "$SHELL -lc".
# shell = "zsh -lic"

[logs]
keep_days = 30

[notify]
# Desktop notification after a scheduled run: "failure", "always" or "never".
on = "failure"

# One [tools.<name>] table per update command. `up <name>` runs it, `up --all`
# runs every tool, and the schedule runs the auto ones when they are due.
#
# [tools.example]
# preset = "brew"           # a built-in recipe; the keys below override it
# run = "brew update && brew upgrade --formula"
# auto = false              # let the schedule run it
# lock = "brew"             # tools sharing a lock never run at the same time
# requires = "brew"         # skip, rather than fail, when this isn't on PATH
# version = "brew --version"  # recorded before and after
# check = "brew outdated"   # what `up check` runs
# interval = "daily"        # "daily", "weekly", "<n>h" or "<n>d"
# timeout = "1h"
# env = { HOMEBREW_NO_ENV_HINTS = "1" }
#
# Presets: {presets}
"""

BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(name: str) -> str:
    return name if BARE_KEY.match(name) else json.dumps(name)


def render(
    aliases: Mapping[str, str] | None = None,
    have: Callable[[str], bool] = lambda _: False,
) -> str:
    """A commented config: imported legacy aliases, then presets found on PATH."""
    out = [HEADER.replace("{presets}", ", ".join(PRESETS))]
    aliases = dict(aliases or {})
    if aliases:
        out.append("# Imported from the old up.toml. Nothing runs unattended until you set auto.\n")
        for name, cmd in aliases.items():
            out.append(f"[tools.{_key(name)}]\nrun = {json.dumps(cmd)}\nauto = false\n")
    detected = [
        p for p in PRESETS if p != "brew-cask" and p not in aliases and have(PRESETS[p]["requires"])
    ]
    if detected:
        out.append(
            "# Found on this machine. Set auto = true on the ones the schedule should run.\n"
        )
        for name in detected:
            out.append(f'[tools.{name}]\npreset = "{name}"\nauto = false\n')
    return "\n".join(out)
