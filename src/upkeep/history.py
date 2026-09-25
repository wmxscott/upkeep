"""Run records, usage counts and the failure notice."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from upkeep import paths

RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
VERSION_RE = re.compile(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """Drop escape sequences, and keep only what a `\\r` redraw left visible."""
    text = ANSI_RE.sub("", text)
    if "\r" in text:
        text = text.rstrip("\r")
        text = text[text.rfind("\r") + 1 :]
    return text


def short_version(text: str) -> str:
    """The version number in a `version` command's output, else the text itself."""
    m = VERSION_RE.search(text)
    if m:
        return m[0]
    text = text.strip()
    return text if len(text) <= 24 else text[:23] + "…"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


@dataclass
class ToolRecord:
    status: str  # ok | failed | skipped | running
    started: str | None = None
    duration: float | None = None
    exit_code: int | None = None
    version_before: str | None = None
    version_after: str | None = None
    reason: str | None = None
    log: str | None = None

    @property
    def started_at(self) -> datetime | None:
        return parse_iso(self.started) if self.started else None

    @property
    def version_change(self) -> str | None:
        """`1.0 → 1.1` when the version changed."""
        before, after = self.version_before, self.version_after
        if before and after and short_version(before) != short_version(after):
            return f"{short_version(before)} → {short_version(after)}"
        return None

    @property
    def version(self) -> str:
        """For display: the change, else the latest known version, else blank."""
        latest = self.version_after or self.version_before
        return self.version_change or (short_version(latest) if latest else "")


@dataclass
class Run:
    id: str
    dir: Path
    trigger: str
    started: str
    finished: str | None = None
    tools: dict[str, ToolRecord] = field(default_factory=dict)

    @property
    def started_at(self) -> datetime:
        return parse_iso(self.started)

    def log_path(self, name: str) -> Path:
        return self.dir / f"{name}.log"

    def save(self) -> None:
        from upkeep import __version__

        data = {
            "id": self.id,
            "upkeep": __version__,
            "trigger": self.trigger,
            "started": self.started,
            "finished": self.finished,
            "tools": {name: asdict(rec) for name, rec in self.tools.items()},
        }
        _write_json(self.dir / "run.json", data)

    @classmethod
    def load(cls, run_dir: Path) -> Run | None:
        try:
            data = json.loads((run_dir / "run.json").read_text())
            tools = {name: ToolRecord(**rec) for name, rec in data["tools"].items()}
            return cls(
                id=data["id"],
                dir=run_dir,
                trigger=data["trigger"],
                started=data["started"],
                finished=data.get("finished"),
                tools=tools,
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None


class History:
    def __init__(self, state: paths.State):
        self.state = state

    def new_run(self, trigger: str, now: datetime | None = None) -> Run:
        now = now or utcnow()
        base = now.astimezone(UTC).strftime(RUN_ID_FORMAT)
        self.state.runs.mkdir(parents=True, exist_ok=True)
        for n in range(1, 1000):
            run_id = base if n == 1 else f"{base}-{n}"
            try:
                (self.state.runs / run_id).mkdir()
            except FileExistsError:
                continue
            return Run(run_id, self.state.runs / run_id, trigger, iso(now))
        raise RuntimeError("could not create a run directory")

    def run_dirs(self) -> list[Path]:
        """Newest first."""
        try:
            dirs = [p for p in self.state.runs.iterdir() if p.is_dir() and p.name[:1].isdigit()]
        except FileNotFoundError:
            return []
        return sorted(dirs, key=lambda p: _id_key(p.name), reverse=True)

    def runs(self) -> Iterator[Run]:
        """Newest first, skipping unreadable ones."""
        for d in self.run_dirs():
            run = Run.load(d)
            if run:
                yield run

    def latest(self, names: set[str] | None = None) -> dict[str, tuple[Run, ToolRecord]]:
        """Each tool's most recent record, with the run it belongs to."""
        found: dict[str, tuple[Run, ToolRecord]] = {}
        for run in self.runs():
            for name, rec in run.tools.items():
                if name not in found and rec.status != "pending":
                    found[name] = (run, rec)
            if names is not None and names <= found.keys():
                break
        return found

    def prune(self, keep_days: int, now: datetime | None = None) -> list[Path]:
        """Delete runs older than `keep_days`, except each tool's latest.

        Keeping the latest record per tool means a weekly tool is not forgotten,
        and re-run early, when `keep_days` is shorter than its interval.
        """
        now = now or utcnow()
        cutoff = now - timedelta(days=keep_days)
        keep = {run.id for run, _ in self.latest().values()}
        removed = []
        for d in self.run_dirs():
            started = _id_time(d.name)
            if started is None or started >= cutoff or d.name in keep:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
        return removed


def _id_time(run_id: str) -> datetime | None:
    try:
        return datetime.strptime(run_id.split("-")[0], RUN_ID_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _id_key(run_id: str) -> tuple[str, int]:
    base, _, n = run_id.partition("-")
    return base, int(n) if n.isdigit() else 1


# usage counts, for sorting the picker


def load_usage(state: paths.State, env: Mapping[str, str] = os.environ) -> dict[str, int]:
    """Usage counts, migrating the legacy file on first use."""
    source = state.usage
    if not source.exists():
        legacy = paths.legacy_usage_file(env)
        if not legacy.exists():
            return {}
        source = legacy
    try:
        data = json.loads(source.read_text())
    except (OSError, ValueError):
        return {}
    usage = {k: v for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}
    if source != state.usage:
        save_usage(state, usage)
    return usage


def save_usage(state: paths.State, usage: dict[str, int]) -> None:
    state.root.mkdir(parents=True, exist_ok=True)
    _write_json(state.usage, usage)


def count_usage(state: paths.State, names: list[str], env: Mapping[str, str] = os.environ) -> None:
    usage = load_usage(state, env)
    for name in names:
        usage[name] = usage.get(name, 0) + 1
    save_usage(state, usage)


# the failure notice


def notice_text(failed: list[str]) -> str:
    n = len(failed)
    noun = "update" if n == 1 else "updates"
    return f"upkeep: {n} {noun} failed ({', '.join(failed)}) — run: up status"


def write_notice(state: paths.State, failed: list[str]) -> None:
    state.root.mkdir(parents=True, exist_ok=True)
    tmp = state.notice.with_name(".notice.tmp")
    tmp.write_text(notice_text(failed) + "\n")
    os.replace(tmp, state.notice)


def read_notice(state: paths.State) -> list[str]:
    """The tools the current notice names."""
    try:
        m = re.search(r"\(([^)]*)\)", state.notice.read_text())
    except OSError:
        return []
    return [n.strip() for n in m[1].split(",") if n.strip()] if m else []


def clear_notice(state: paths.State) -> None:
    state.notice.unlink(missing_ok=True)


def update_notice(
    state: paths.State, trigger: str, results: Mapping[str, ToolRecord], auto_tools: set[str]
) -> None:
    """Keep the notice in step with a finished run.

    A scheduled run with failures adds them to it, dropping tools that have
    since succeeded, and a clean one clears it. A manual run only trims it:
    once no auto tool's latest result is a failure, it goes.
    """
    failed_now = [n for n, r in results.items() if r.status == "failed"]
    if trigger == "auto":
        if failed_now:
            earlier = [n for n in read_notice(state) if n not in results]
            write_notice(state, sorted({*earlier, *failed_now}))
        else:
            clear_notice(state)
        return
    if not state.notice.exists():
        return
    latest = History(state).latest()
    still = sorted(n for n in auto_tools if n in latest and latest[n][1].status == "failed")
    if still:
        write_notice(state, still)
    else:
        clear_notice(state)
