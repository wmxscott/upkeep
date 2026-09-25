"""`up doctor`: config problems, and why unattended runs fail."""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass

from upkeep.config import Config, Tool
from upkeep.history import History
from upkeep.paths import State
from upkeep.presets import UNATTENDED_NOTES

# Commands whose `run` is likely to fail without a person at the keyboard.
RISKY_COMMANDS = [
    (re.compile(r"(^|[\s;&|(])sudo\s"), "runs sudo, which can't ask for a password unattended"),
    (
        re.compile(r"--cask\b|\bbrew\s+upgrade\b(?!.*--formula)"),
        "may upgrade casks, which often need sudo; add --formula (or use preset brew) "
        "and upgrade casks by hand",
    ),
    (
        re.compile(r"\bgit\s+(pull|fetch|push|clone)\b|(^|[\s;&|(])ssh\s"),
        "talks to git or ssh remotes, which needs a key usable with nobody at the keyboard",
    ),
]

# Output of a failed run, and what it most likely means.
FAILURE_HINTS = [
    (
        re.compile(
            r"Permission denied \(publickey|sign_and_send_pubkey|agent refused operation"
            r"|Host key verification failed",
            re.I,
        ),
        "SSH authentication failed: the key needs a passphrase or a touch, or no agent is "
        "reachable from the scheduled job. Use an HTTPS remote or a key the agent can use "
        "unattended, or make the tool manual",
    ),
    (
        re.compile(
            r"a terminal is required|a password is required|no tty present|\bsudo\b.*exited", re.I
        ),
        "needed sudo, which can't prompt unattended: make the tool manual, or leave out "
        "what needs root",
    ),
    (
        re.compile(
            r"unauthenticated|not logged in|not authenticated|authentication required"
            r"|please (log ?in|sign ?in)|401 unauthorized",
            re.I,
        ),
        "not logged in: log the tool in once in a terminal",
    ),
    (
        re.compile(
            r"another .* process is already running|has already locked|could not get lock", re.I
        ),
        "clashed with another run of the same package manager: give those tools the same `lock`",
    ),
    (
        re.compile(r"command not found|not found in PATH", re.I),
        "a command wasn't found: set `schedule.shell` so the job gets your shell's PATH",
    ),
    (
        re.compile(
            r"could not resolve host|connection reset|recv failure|download failed"
            r"|failed to download|stalled|network is unreachable|curl: \(\d+\)",
            re.I,
        ),
        "network trouble; usually transient",
    ),
]

MANAGERS = (
    "brew",
    "mise",
    "npm",
    "uv",
    "pipx",
    "rustup",
    "cargo",
    "apt",
    "apt-get",
    "dnf",
    "pacman",
    "port",
    "gem",
)
SHELL_WORDS = {
    "cd",
    "exec",
    "env",
    "command",
    "eval",
    "export",
    "set",
    "test",
    "true",
    "false",
    "if",
    "for",
    "while",
    "echo",
    "printf",
    "source",
    ".",
    "[",
    "time",
    "nice",
    "nohup",
}


@dataclass
class Finding:
    level: str  # error | warn
    text: str


def invokes(cmd: str, program: str) -> bool:
    return re.search(rf"(^|[\s;&|(]){re.escape(program)}(\s|$)", cmd) is not None


def first_word(cmd: str) -> str | None:
    m = re.match(r"\s*([A-Za-z0-9_.+-]+)(\s|$)", cmd)
    if not m or "=" in m[1] or m[1] in SHELL_WORDS:
        return None
    return m[1]


def failure_hints(text: str) -> list[str]:
    return [hint for rx, hint in FAILURE_HINTS if rx.search(text)]


def risks(tool: Tool) -> list[str]:
    out = [note for rx, note in RISKY_COMMANDS if rx.search(tool.run)]
    if tool.preset in UNATTENDED_NOTES:
        out = [UNATTENDED_NOTES[tool.preset]]
    return out


def lock_clashes(tools: Mapping[str, Tool]) -> list[str]:
    out = []
    for mgr in MANAGERS:
        users = [t for t in tools.values() if invokes(t.run, mgr)]
        locks = {t.lock for t in users}
        if len(users) > 1 and (len(locks) > 1 or None in locks):
            names = ", ".join(t.name for t in users)
            out.append(
                f"{names} all run {mgr} but don't share a lock, so they can run at once "
                f'and collide; set lock = "{mgr}" on each'
            )
    return out


def diagnose(
    cfg: Config,
    state: State,
    search_path: str,
    schedule_installed: bool | None,
) -> list[Finding]:
    found = [Finding("error", p) for p in cfg.problems]
    if not cfg.tools:
        found.append(Finding("warn", "no tools configured"))
    for tool in cfg.tools.values():
        missing = [r for r in tool.requires if not shutil.which(r, path=search_path)]
        if missing:
            found.append(
                Finding("warn", f"{tool.name}: {missing[0]} not found; it will be skipped")
            )
        elif not tool.requires:
            word = first_word(tool.run)
            if word and not shutil.which(word, path=search_path):
                found.append(Finding("warn", f"{tool.name}: {word} not found on PATH"))
        if tool.auto:
            found.extend(Finding("warn", f"{tool.name} (auto): {r}") for r in risks(tool))
    found.extend(Finding("warn", c) for c in lock_clashes(cfg.tools))

    auto = {n for n, t in cfg.tools.items() if t.auto}
    latest = History(state).latest(auto) if auto else {}
    for name in sorted(auto):
        if name not in latest or latest[name][1].status != "failed":
            continue
        run, rec = latest[name]
        try:
            text = run.log_path(name).read_text(errors="replace")
        except OSError:
            text = ""
        hints = failure_hints(text) or ["see `up log " + name + "`"]
        for hint in hints:
            found.append(Finding("warn", f"{name}: last run failed ({rec.reason}): {hint}"))

    if auto and schedule_installed is False:
        found.append(
            Finding(
                "warn", "auto tools are set but no schedule is installed: run `up schedule install`"
            )
        )
    return found
