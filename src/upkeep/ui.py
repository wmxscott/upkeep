"""Terminal output: a live status board for a TTY, prefixed lines otherwise."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from upkeep import paths
from upkeep.config import Tool
from upkeep.history import Run, ToolRecord
from upkeep.runner import format_duration

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PALETTE = ("36", "33", "32", "35", "34", "91")
TAIL_LINES = 20


def use_color(stream: TextIO, env: Mapping[str, str] = os.environ) -> bool:
    if env.get("NO_COLOR") or env.get("TERM") == "dumb":
        return False
    return stream.isatty()


class Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color and text else text

    def ok(self, t: str) -> str:
        return self(t, "32")

    def bad(self, t: str) -> str:
        return self(t, "31")

    def warn(self, t: str) -> str:
        return self(t, "33")

    def dim(self, t: str) -> str:
        return self(t, "2")

    def bold(self, t: str) -> str:
        return self(t, "1")


def summary(run: Run, elapsed: float, style: Style) -> str:
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    for rec in run.tools.values():
        if rec.status in counts:
            counts[rec.status] += 1
    parts = [style.ok(f"{counts['ok']} ok")]
    if counts["failed"]:
        parts.append(style.bad(f"{counts['failed']} failed"))
    if counts["skipped"]:
        parts.append(style.warn(f"{counts['skipped']} skipped"))
    return ", ".join(parts) + f" in {format_duration(elapsed)}"


def outcome(rec: ToolRecord) -> str:
    if rec.status == "ok":
        return rec.version_change or ""
    if rec.status == "skipped":
        return f"skipped: {rec.reason}"
    return rec.reason or "failed"


def log_tail(log: Path, n: int = TAIL_LINES) -> list[str]:
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return lines[1:][-n:]


class StreamReporter:
    """`[tool] line` for every output line: pipes, logs, and `-v`."""

    def __init__(self, out: TextIO = sys.stdout, color: bool = False, header: bool = False):
        self.out = out
        self.style = Style(color)
        self.header = header
        self.lock = threading.Lock()
        self.colors: dict[str, str] = {}
        self.failed: list[tuple[str, Path]] = []

    def _prefix(self, name: str) -> str:
        return self.style(f"[{name}]", "1;" + self.colors.get(name, "0"))

    def _print(self, text: str) -> None:
        with self.lock:
            print(text, file=self.out, flush=True)

    def begin(self, tools: Sequence[Tool], run: Run) -> None:
        for i, tool in enumerate(tools):
            self.colors[tool.name] = PALETTE[i % len(PALETTE)]
        if self.header:
            when = run.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
            names = ", ".join(t.name for t in tools)
            self._print(f"upkeep: {run.trigger} run at {when}: {names}")

    def waiting(self, name: str, lock: str) -> None:
        self._print(f"{self._prefix(name)} {self.style.dim(f'waiting for {lock}')}")

    def started(self, name: str) -> None:
        pass

    def output(self, name: str, line: str) -> None:
        self._print(f"{self._prefix(name)} {line}")

    def finished(self, name: str, rec: ToolRecord, log: Path) -> None:
        dur = f" in {format_duration(rec.duration)}" if rec.duration is not None else ""
        if rec.status == "ok":
            change = f" ({rec.version_change})" if rec.version_change else ""
            text = self.style.ok(f"done{dur}") + change
        elif rec.status == "skipped":
            text = self.style.warn(f"skipped: {rec.reason}")
        else:
            text = self.style.bad(f"failed ({outcome(rec)}){dur}")
            if log.exists():
                text += f" — log: {paths.pretty(log)}"
        self._print(f"{self._prefix(name)} {text}")

    def end(self, run: Run, elapsed: float) -> None:
        self._print(summary(run, elapsed, self.style))


class LiveReporter:
    """One redrawn status line per tool; full output only for failures."""

    def __init__(self, out: TextIO = sys.stdout, color: bool = True, interval: float = 0.1):
        self.out = out
        self.style = Style(color)
        self.interval = interval
        self.lock = threading.Lock()
        self.names: list[str] = []
        self.state: dict[str, dict] = {}
        self.drawn = 0
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.frame = 0
        self.board = True

    def begin(self, tools: Sequence[Tool], run: Run) -> None:
        self.names = [t.name for t in tools]
        self.width = max(len(n) for n in self.names) if self.names else 0
        self.state = {
            t.name: {
                "status": "pending",
                "detail": f"queued (lock {t.lock})" if t.lock else "",
                "t0": None,
            }
            for t in tools
        }
        rows = shutil.get_terminal_size().lines
        self.board = len(self.names) <= rows - 4
        if self.board:
            self.out.write("\x1b[?25l")
            self._draw()
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def waiting(self, name: str, lock: str) -> None:
        with self.lock:
            self.state[name].update(status="waiting", detail=f"waiting for {lock}")

    def started(self, name: str) -> None:
        with self.lock:
            self.state[name].update(status="running", detail="", t0=time.monotonic())

    def output(self, name: str, line: str) -> None:
        if line.strip():
            with self.lock:
                self.state[name]["detail"] = line.strip()

    def finished(self, name: str, rec: ToolRecord, log: Path) -> None:
        with self.lock:
            self.state[name].update(status=rec.status, rec=rec, log=log)
        if not self.board:
            with self.lock:
                print(self._line(name), file=self.out, flush=True)

    def end(self, run: Run, elapsed: float) -> None:
        if self.thread:
            self.stop.set()
            self.thread.join()
        if self.board:
            self._draw()
            self.out.write("\x1b[?25h")
        for name in self.names:
            st = self.state[name]
            if st["status"] != "failed" or "log" not in st:
                continue
            log: Path = st["log"]
            tail = log_tail(log)
            print(file=self.out)
            print(self.style.bad(f"── {name}: {outcome(st['rec'])}"), file=self.out)
            for line in tail:
                print(f"  {line}", file=self.out)
            if log.exists():
                print(self.style.dim(f"  log: {paths.pretty(log)}"), file=self.out)
        print(summary(run, elapsed, self.style), file=self.out, flush=True)

    def _loop(self) -> None:
        while not self.stop.wait(self.interval):
            self.frame += 1
            self._draw()

    def _draw(self) -> None:
        with self.lock:
            lines = [self._line(n) for n in self.names]
            buf = f"\x1b[{self.drawn}F" if self.drawn else ""
            buf += "".join(f"\x1b[2K{line}\n" for line in lines)
            self.out.write(buf)
            self.out.flush()
            self.drawn = len(lines)

    def _line(self, name: str) -> str:
        st = self.state[name]
        s = self.style
        status = st["status"]
        rec: ToolRecord | None = st.get("rec")
        cols = shutil.get_terminal_size().columns - 1
        if status in ("pending", "waiting"):
            sym, when, detail = s.dim("·"), "", st["detail"] or "queued"
            paint = s.dim
        elif status == "running":
            sym = s(SPINNER[self.frame % len(SPINNER)], "36")
            when = format_duration(time.monotonic() - st["t0"])
            detail, paint = st["detail"], s.dim
        else:
            assert rec is not None
            dur = rec.duration
            when = format_duration(dur) if dur is not None else ""
            detail = outcome(rec)
            sym, paint = {
                "ok": (s.ok("✓"), lambda t: t),
                "skipped": (s.warn("–"), s.warn),  # noqa: RUF001
            }.get(status, (s.bad("✗"), s.bad))
        head = f"{name:<{self.width}}  {when:>6}  "
        room = max(cols - 2 - len(head), 0)
        if len(detail) > room:
            detail = detail[: max(room - 1, 0)] + "…" if room else ""
        return f"{sym} {head}{paint(detail)}"
