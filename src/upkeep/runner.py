"""Running tools: in parallel, serialised by lock, with timeouts and logs."""

from __future__ import annotations

import contextlib
import os
import select
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from upkeep.config import Config, Tool
from upkeep.history import History, Run, ToolRecord, iso, strip_ansi, utcnow
from upkeep.locks import FileLock
from upkeep.paths import State

PATH_MARKER = "__UPKEEP_PATH__"
VERSION_TIMEOUT = 30.0
KILL_GRACE = 5.0
DRAIN_GRACE = 2.0


class Reporter(Protocol):
    def begin(self, tools: Sequence[Tool], run: Run) -> None: ...
    def waiting(self, name: str, lock: str) -> None: ...
    def started(self, name: str) -> None: ...
    def output(self, name: str, line: str) -> None: ...
    def finished(self, name: str, rec: ToolRecord, log: Path) -> None: ...
    def end(self, run: Run, elapsed: float) -> None: ...


class Interrupted(Exception):
    pass


def lock_groups(tools: Iterable[Tool]) -> list[list[Tool]]:
    """Tools that share a lock form one group, run in config order."""
    groups: dict[str, list[Tool]] = {}
    for tool in tools:
        key = f"lock:{tool.lock}" if tool.lock else f"tool:{tool.name}"
        groups.setdefault(key, []).append(tool)
    return list(groups.values())


def child_env(tool: Tool, env: Mapping[str, str]) -> dict[str, str]:
    return {**env, **tool.env}


def shell_path(shell: Sequence[str], env: Mapping[str, str], timeout: float = 20.0) -> str:
    """The PATH commands will see through the configured shell, plus ours."""
    ours = env.get("PATH", "")
    probe = f'printf "\\n%s%s\\n" {PATH_MARKER} "$PATH"'
    try:
        out = subprocess.run(
            [*shell, probe],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=timeout,
            start_new_session=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ours
    for line in reversed(out.splitlines()):
        if line.startswith(PATH_MARKER):
            theirs = line[len(PATH_MARKER) :]
            return f"{theirs}:{ours}" if ours else theirs
    return ours


def missing_requirements(tool: Tool, search_path: str) -> list[str]:
    return [r for r in tool.requires if not shutil.which(r, path=search_path)]


def capture_first_line(
    shell: Sequence[str], cmd: str, env: Mapping[str, str], timeout: float = VERSION_TIMEOUT
) -> str | None:
    try:
        proc = subprocess.run(
            [*shell, cmd],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            env=dict(env),
            timeout=timeout,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        line = strip_ansi(line).strip()
        if line:
            return line[:80]
    return None


def kill_group(proc: subprocess.Popen, grace: float = KILL_GRACE) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


def format_duration(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class Runner:
    def __init__(
        self,
        cfg: Config,
        state: State,
        reporter: Reporter,
        *,
        trigger: str = "manual",
        env: Mapping[str, str] = os.environ,
        interactive: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self.state = state
        self.reporter = reporter
        self.trigger = trigger
        self.env = env
        self.interactive = interactive
        self.clock = clock
        self.shell = cfg.shell_words(env)
        self.cancel = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._mutex = threading.Lock()
        self._search_path: str | None = None
        self.run: Run | None = None

    def search_path(self) -> str:
        if self._search_path is None:
            self._search_path = shell_path(self.shell, self.env)
        return self._search_path

    def execute(self, tools: Sequence[Tool]) -> Run:
        run = History(self.state).new_run(self.trigger)
        self.run = run
        for tool in tools:
            run.tools[tool.name] = ToolRecord(status="pending")
        run.save()
        self.reporter.begin(tools, run)
        t0 = self.clock()
        if any(t.requires for t in tools):
            self.search_path()
        threads = [
            threading.Thread(target=self._group, args=(group,), daemon=True)
            for group in lock_groups(tools)
        ]
        for t in threads:
            t.start()
        try:
            for t in threads:
                while t.is_alive():
                    t.join(0.1)
        except (KeyboardInterrupt, Interrupted):
            self.stop()
            for t in threads:
                t.join()
            raise
        finally:
            run.finished = iso(utcnow())
            with self._mutex:
                run.save()
            self.reporter.end(run, self.clock() - t0)
        return run

    def stop(self) -> None:
        self.cancel.set()
        with self._mutex:
            procs = list(self._procs)
        for proc in procs:
            threading.Thread(target=kill_group, args=(proc,), daemon=True).start()

    def _group(self, group: list[Tool]) -> None:
        for tool in group:
            if self.cancel.is_set():
                self._record(tool, ToolRecord(status="failed", reason="interrupted"))
                continue
            try:
                rec = self._tool(tool)
            except Exception as e:  # a bug here must not hide the other tools' results
                rec = ToolRecord(status="failed", reason=f"upkeep error: {e}")
            self._record(tool, rec)

    def _record(self, tool: Tool, rec: ToolRecord) -> None:
        assert self.run is not None
        log = self.run.log_path(tool.name)
        if rec.log is None and log.exists():
            rec.log = log.name
        with self._mutex:
            self.run.tools[tool.name] = rec
            self.run.save()
        self.reporter.finished(tool.name, rec, log)

    def _tool(self, tool: Tool) -> ToolRecord:
        assert self.run is not None
        if tool.requires:
            missing = missing_requirements(tool, self.search_path())
            if missing:
                return ToolRecord(
                    status="skipped", started=iso(utcnow()), reason=f"{missing[0]} not found"
                )
        names = sorted({f"tool-{tool.name}", *([f"lock-{tool.lock}"] if tool.lock else [])})
        held = [FileLock(self.state.locks, n) for n in names]
        acquired: list[FileLock] = []
        try:
            for lock in held:
                label = lock.name.split("-", 1)[1]
                if not lock.acquire(
                    on_wait=lambda _n, label=label: self.reporter.waiting(tool.name, label),
                    cancelled=self.cancel.is_set,
                ):
                    return ToolRecord(status="failed", reason="interrupted")
                acquired.append(lock)
            return self._spawn(tool)
        finally:
            for lock in acquired:
                lock.release()

    def _spawn(self, tool: Tool) -> ToolRecord:
        assert self.run is not None
        env = child_env(tool, self.env)
        rec = ToolRecord(status="running", started=iso(utcnow()), log=f"{tool.name}.log")
        with self._mutex:
            self.run.tools[tool.name] = rec
            self.run.save()
        self.reporter.started(tool.name)
        if tool.version:
            rec.version_before = capture_first_line(self.shell, tool.version, env)
        t0 = self.clock()
        log_path = self.run.log_path(tool.name)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {tool.run}\n")
            log.flush()
            if self.interactive:
                code, reason = self._attached(tool, env, log)
            else:
                code, reason = self._piped(tool, env, log)
        rec.duration = round(self.clock() - t0, 2)
        rec.exit_code = code
        if tool.version and code == 0:
            rec.version_after = capture_first_line(self.shell, tool.version, env)
        if reason:
            rec.status, rec.reason = "failed", reason
        elif code == 0:
            rec.status = "ok"
        else:
            rec.status, rec.reason = "failed", f"exit {code}"
        return rec

    def _popen(self, tool: Tool, env: dict[str, str], **kw) -> subprocess.Popen:
        proc = subprocess.Popen([*self.shell, tool.run], env=env, **kw)
        with self._mutex:
            self._procs.add(proc)
        return proc

    def _forget(self, proc: subprocess.Popen) -> None:
        with self._mutex:
            self._procs.discard(proc)

    def _piped(self, tool: Tool, env: dict[str, str], log) -> tuple[int | None, str | None]:
        proc = self._popen(
            tool,
            env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = threading.Event()
        timer = None
        if tool.timeout:

            def expire() -> None:
                timed_out.set()
                kill_group(proc)

            timer = threading.Timer(tool.timeout, expire)
            timer.daemon = True
            timer.start()
        try:
            for line in _lines(proc):
                clean = strip_ansi(line)
                log.write(clean + "\n")
                log.flush()
                self.reporter.output(tool.name, clean)
            code = proc.wait()
        finally:
            if timer:
                timer.cancel()
            self._forget(proc)
        if timed_out.is_set():
            return None, f"timed out after {format_duration(tool.timeout or 0)}"
        if self.cancel.is_set():
            return None, "interrupted"
        return code, None

    def _attached(self, tool: Tool, env: dict[str, str], log) -> tuple[int | None, str | None]:
        log.write("(interactive run: output went to the terminal and was not captured)\n")
        proc = self._popen(tool, env)
        try:
            code = proc.wait(timeout=tool.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return None, f"timed out after {format_duration(tool.timeout or 0)}"
        finally:
            self._forget(proc)
        return code, None


def _lines(proc: subprocess.Popen) -> Iterable[str]:
    """Output lines, until EOF or shortly after the process exits.

    A background process that inherited the pipe can hold it open forever;
    the grace period stops that from hanging the run.
    """
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    buf = b""
    exited_at: float | None = None
    while True:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                yield line.decode("utf-8", "replace")
            continue
        if proc.poll() is not None:
            exited_at = exited_at or time.monotonic()
            if time.monotonic() - exited_at > DRAIN_GRACE:
                break
    if buf:
        yield buf.decode("utf-8", "replace")
    proc.stdout.close()
