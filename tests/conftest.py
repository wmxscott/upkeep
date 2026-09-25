from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from upkeep import notify, paths, schedule
from upkeep.history import Run, ToolRecord


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """An environment rooted in tmp_path, so nothing touches the real home."""
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "PATH": "/usr/bin:/bin",
        "SHELL": "/bin/sh",
    }


@pytest.fixture
def state(env) -> paths.State:
    return paths.state(env)


@pytest.fixture
def write_config(env):
    def write(body: str, header: str = '[schedule]\nshell = "/bin/sh -c"\n') -> Path:
        path = paths.config_file(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header + body)
        return path

    return write


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    """No test may post a notification or reach launchctl/systemctl."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "send", lambda title, msg: sent.append((title, msg)) or True)

    def refuse(argv):
        raise AssertionError(f"test tried to run {argv}")

    monkeypatch.setattr(schedule, "default_exec", refuse)
    return sent


class FakeExec:
    def __init__(self, results: dict[str, tuple[int, str]] | None = None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        code, out = self.results.get(argv[1] if len(argv) > 1 else "", (0, ""))
        return subprocess.CompletedProcess(argv, code, out, "")


@pytest.fixture
def fake_exec():
    return FakeExec


class Recorder:
    """A reporter that keeps every event."""

    def __init__(self):
        self.events: list[tuple] = []
        self.lock = threading.Lock()

    def _add(self, *event):
        with self.lock:
            self.events.append(event)

    def begin(self, tools, run: Run):
        self._add("begin", [t.name for t in tools])

    def waiting(self, name, lock):
        self._add("waiting", name, lock)

    def started(self, name):
        self._add("started", name)

    def output(self, name, line):
        self._add("output", name, line)

    def finished(self, name, rec: ToolRecord, log):
        self._add("finished", name, rec.status)

    def end(self, run, elapsed):
        self._add("end")

    def of(self, kind):
        return [e[1:] for e in self.events if e[0] == kind]


@pytest.fixture
def recorder():
    return Recorder()
