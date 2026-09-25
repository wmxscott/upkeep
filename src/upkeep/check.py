"""`up check`: ask each tool what is outdated, without upgrading anything."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from upkeep.config import Config, Tool
from upkeep.history import strip_ansi
from upkeep.locks import FileLock
from upkeep.paths import State
from upkeep.runner import child_env, lock_groups, missing_requirements, shell_path

CHECK_TIMEOUT = 300.0


@dataclass
class CheckResult:
    name: str
    status: str  # updates | current | failed | skipped
    lines: list[str] = field(default_factory=list)
    detail: str = ""


def check_one(tool: Tool, shell: Sequence[str], env: Mapping[str, str]) -> CheckResult:
    assert tool.check
    try:
        proc = subprocess.run(
            [*shell, tool.check],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            env=child_env(tool, env),
            timeout=min(tool.timeout or CHECK_TIMEOUT, CHECK_TIMEOUT),
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(tool.name, "failed", detail="timed out")
    except OSError as e:
        return CheckResult(tool.name, "failed", detail=str(e))
    lines = [s for s in (strip_ansi(x).rstrip() for x in proc.stdout.splitlines()) if s.strip()]
    if lines:
        return CheckResult(tool.name, "updates", lines)
    if proc.returncode != 0:
        err = [strip_ansi(x) for x in proc.stderr.strip().splitlines()]
        return CheckResult(tool.name, "failed", err[-5:], detail=f"exit {proc.returncode}")
    return CheckResult(tool.name, "current")


def run_checks(
    cfg: Config, tools: Sequence[Tool], state: State, env: Mapping[str, str] = os.environ
) -> list[CheckResult]:
    shell = cfg.shell_words(env)
    search = shell_path(shell, env) if any(t.requires for t in tools) else ""
    results: dict[str, CheckResult] = {}

    def group(members: list[Tool]) -> None:
        for tool in members:
            missing = missing_requirements(tool, search)
            if missing:
                results[tool.name] = CheckResult(
                    tool.name, "skipped", detail=f"{missing[0]} not found"
                )
                continue
            locks = [FileLock(state.locks, f"lock-{tool.lock}")] if tool.lock else []
            for lock in locks:
                lock.acquire()
            try:
                results[tool.name] = check_one(tool, shell, env)
            finally:
                for lock in locks:
                    lock.release()

    threads = [threading.Thread(target=group, args=(g,), daemon=True) for g in lock_groups(tools)]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            t.join(0.1)
    return [results[t.name] for t in tools]
