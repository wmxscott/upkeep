"""The hourly `up --auto` job: a LaunchAgent on macOS, a systemd user timer on Linux.

The job only supplies the tick. `up --auto` decides what is due, so a machine
that slept through 08:00 catches up on its first tick after waking.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from upkeep import paths
from upkeep.config import Config

LABEL = "io.github.wmxscott.upkeep"
UNIT = "upkeep"
INTERVAL = 3600
PASS_ENV = ("UPKEEP_CONFIG", "XDG_CONFIG_HOME", "XDG_STATE_HOME")

Exec = Callable[[Sequence[str]], subprocess.CompletedProcess]


def default_exec(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@dataclass
class Plan:
    files: dict[Path, str] = field(default_factory=dict)
    remove: list[Path] = field(default_factory=list)
    before: list[list[str]] = field(default_factory=list)  # failures are fine
    after: list[list[str]] = field(default_factory=list)  # failures are reported

    def describe(self) -> str:
        out = []
        for argv in self.before:
            out.append(f"$ {shlex.join(argv)}  # ok if this fails")
        for path in self.remove:
            out.append(f"remove {path}")
        for path, text in self.files.items():
            out.append(f"write {path}:")
            out.extend(f"    {line}" for line in text.rstrip("\n").splitlines())
        for argv in self.after:
            out.append(f"$ {shlex.join(argv)}")
        return "\n".join(out)


def up_program(argv0: str | None = None) -> list[str]:
    """How the job should start upkeep, as an absolute path.

    Prefers the `upkeep` name when it sits next to `up`, since Homebrew and
    others ship an unrelated `up`.
    """
    argv0 = argv0 if argv0 is not None else sys.argv[0]
    base = os.path.basename(argv0)
    if base.endswith(".py") or base == "-c":
        return [sys.executable, "-m", "upkeep"]
    path = argv0 if "/" in argv0 else shutil.which(argv0)
    if not path:
        path = shutil.which("upkeep")
    if not path:
        return [sys.executable, "-m", "upkeep"]
    path = os.path.abspath(path)
    sibling = os.path.join(os.path.dirname(path), "upkeep")
    if os.path.basename(path) != "upkeep" and os.access(sibling, os.X_OK):
        path = sibling
    return [path]


def job_argv(cfg: Config, program: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """The shell, resolved to an absolute path, running `up --auto`."""
    shell = list(cfg.shell_words(env))
    if not shell[0].startswith("/"):
        found = shutil.which(shell[0], path=env.get("PATH"))
        if found:
            shell[0] = found
    return [*shell, shlex.join([*program, "--auto"])]


def job_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: env[k] for k in PASS_ENV if env.get(k)}


class Backend:
    name = ""

    def __init__(self, env: Mapping[str, str], state: paths.State, run: Exec | None = None):
        self.env = env
        self.state = state
        self.exec = run or default_exec

    def install_plan(self, argv: list[str]) -> Plan:
        raise NotImplementedError

    def remove_plan(self) -> Plan:
        raise NotImplementedError

    def installed(self) -> bool:
        raise NotImplementedError

    def status(self) -> list[tuple[str, str]]:
        raise NotImplementedError

    def apply(self, plan: Plan) -> list[str]:
        """Carry out a plan; returns error messages."""
        for argv in plan.before:
            self.exec(argv)
        for path in plan.remove:
            path.unlink(missing_ok=True)
        for path, text in plan.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        errors = []
        for argv in plan.after:
            proc = self.exec(argv)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                errors.append(f"{shlex.join(argv)} failed" + (f": {detail[-1]}" if detail else ""))
        return errors


class Launchd(Backend):
    name = "launchd"

    @property
    def plist(self) -> Path:
        return paths.home(self.env) / "Library/LaunchAgents" / f"{LABEL}.plist"

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    def render(self, argv: list[str]) -> str:
        log = str(self.state.schedule_log)
        data: dict = {
            "Label": LABEL,
            "ProgramArguments": argv,
            "StartInterval": INTERVAL,
            "RunAtLoad": True,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
        extra = job_env(self.env)
        if extra:
            data["EnvironmentVariables"] = extra
        return plistlib.dumps(data, sort_keys=False).decode()

    def install_plan(self, argv: list[str]) -> Plan:
        return Plan(
            files={self.plist: self.render(argv)},
            before=[["launchctl", "bootout", f"{self.domain}/{LABEL}"]],
            after=[["launchctl", "bootstrap", self.domain, str(self.plist)]],
        )

    def remove_plan(self) -> Plan:
        return Plan(
            remove=[self.plist],
            before=[["launchctl", "bootout", f"{self.domain}/{LABEL}"]],
        )

    def installed(self) -> bool:
        return self.plist.exists()

    def status(self) -> list[tuple[str, str]]:
        if not self.installed():
            return [("job", "not installed")]
        rows = [("file", str(self.plist))]
        proc = self.exec(["launchctl", "print", f"{self.domain}/{LABEL}"])
        if proc.returncode != 0:
            rows.append(("loaded", "no"))
            return rows
        rows.append(("loaded", "yes"))
        for line in proc.stdout.splitlines():
            key, _, value = line.strip().partition(" = ")
            if key in ("state", "runs", "last exit code"):
                rows.append((key, value))
        return rows


def _systemd_quote(arg: str, dollar: bool = True) -> str:
    arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if dollar:
        arg = arg.replace("$", "$$")
    return f'"{arg}"'


class Systemd(Backend):
    name = "systemd"

    @property
    def unit_dir(self) -> Path:
        return paths.config_home(self.env) / "systemd/user"

    @property
    def service(self) -> Path:
        return self.unit_dir / f"{UNIT}.service"

    @property
    def timer(self) -> Path:
        return self.unit_dir / f"{UNIT}.timer"

    def render_service(self, argv: list[str]) -> str:
        lines = [
            "[Unit]",
            "Description=upkeep: run the updates that are due",
            "",
            "[Service]",
            "Type=oneshot",
            "ExecStart=" + " ".join(_systemd_quote(a) for a in argv),
        ]
        for k, v in job_env(self.env).items():
            lines.append("Environment=" + _systemd_quote(f"{k}={v}", dollar=False))
        return "\n".join(lines) + "\n"

    def render_timer(self) -> str:
        return (
            "[Unit]\n"
            "Description=Hourly upkeep tick\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=hourly\n"
            "Persistent=true\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )

    def install_plan(self, argv: list[str]) -> Plan:
        return Plan(
            files={self.service: self.render_service(argv), self.timer: self.render_timer()},
            after=[
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"],
            ],
        )

    def remove_plan(self) -> Plan:
        return Plan(
            remove=[self.timer, self.service],
            before=[["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"]],
            after=[["systemctl", "--user", "daemon-reload"]],
        )

    def installed(self) -> bool:
        return self.timer.exists() and self.service.exists()

    def status(self) -> list[tuple[str, str]]:
        if not self.installed():
            return [("job", "not installed")]
        rows = [("files", f"{self.service}, {self.timer.name}")]
        proc = self.exec(
            [
                "systemctl",
                "--user",
                "show",
                f"{UNIT}.timer",
                "--property=UnitFileState,ActiveState,LastTriggerUSec,NextElapseUSecRealtime",
            ]
        )
        if proc.returncode != 0:
            rows.append(("timer", "unknown (systemctl failed)"))
            return rows
        props = dict(line.partition("=")[::2] for line in proc.stdout.splitlines() if "=" in line)
        rows.append(("enabled", props.get("UnitFileState") or "no"))
        rows.append(("active", props.get("ActiveState") or "unknown"))
        if props.get("LastTriggerUSec"):
            rows.append(("last tick", props["LastTriggerUSec"]))
        if props.get("NextElapseUSecRealtime"):
            rows.append(("next tick", props["NextElapseUSecRealtime"]))
        return rows


def backend(
    env: Mapping[str, str],
    state: paths.State,
    platform: str | None = None,
    run: Exec | None = None,
) -> Backend:
    platform = platform or sys.platform
    if platform == "darwin":
        return Launchd(env, state, run)
    if platform.startswith("linux"):
        return Systemd(env, state, run)
    raise RuntimeError(f"scheduling is not supported on {platform}")
