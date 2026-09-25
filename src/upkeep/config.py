"""The TOML config: settings, tools and presets.

Problems that leave a usable config (unknown keys, bad values) are collected in
`Config.problems`: `up doctor` reports them as errors, other commands warn and
carry on with defaults. Only an unreadable file stops a command.
"""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

from upkeep import paths
from upkeep.presets import PRESETS

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DURATION_RE = re.compile(r"^(\d+)([smhd])$")
INTERVAL_RE = re.compile(r"^(\d+)([hd])$")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
NOTIFY = ("failure", "always", "never")
POSIX_SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "mksh", "yash")
DEFAULT_TIMEOUT = 3600.0
TOOL_KEYS = (
    "preset",
    "run",
    "auto",
    "lock",
    "requires",
    "version",
    "check",
    "interval",
    "timeout",
    "env",
)
SECTION_KEYS = {
    "schedule": ("at", "shell"),
    "logs": ("keep_days",),
    "notify": ("on",),
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Interval:
    count: int
    unit: str  # "h" or "d"

    @property
    def hours(self) -> int:
        return self.count * (24 if self.unit == "d" else 1)

    def __str__(self) -> str:
        if self.unit == "d" and self.count == 1:
            return "daily"
        if self.unit == "d" and self.count == 7:
            return "weekly"
        return f"{self.count}{self.unit}"


DAILY = Interval(1, "d")


@dataclass(frozen=True)
class Tool:
    name: str
    run: str
    auto: bool = False
    lock: str | None = None
    requires: tuple[str, ...] = ()
    version: str | None = None
    check: str | None = None
    interval: Interval = DAILY
    timeout: float | None = DEFAULT_TIMEOUT
    env: Mapping[str, str] = field(default_factory=dict)
    preset: str | None = None


@dataclass
class Config:
    path: Path | None = None
    at: time = time(8, 0)
    shell: tuple[str, ...] | None = None
    keep_days: int = 30
    notify: str = "failure"
    tools: dict[str, Tool] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    legacy: bool = False

    def shell_words(self, env: Mapping[str, str] = os.environ) -> tuple[str, ...]:
        """How commands are launched: the command string is appended as one argument.

        Defaults to a login `$SHELL` when it speaks POSIX sh, since presets and
        the PATH probe rely on that syntax.
        """
        if self.shell:
            return self.shell
        user_shell = env.get("SHELL", "")
        if (
            user_shell.startswith("/")
            and os.path.basename(user_shell) in POSIX_SHELLS
            and os.access(user_shell, os.X_OK)
        ):
            return (user_shell, "-lc")
        return ("/bin/sh", "-lc")


def parse_duration(value: Any) -> float | None:
    """Seconds from `"30m"`-style text or a number; 0 means none."""
    if isinstance(value, bool):
        raise ValueError('must be a duration like "30m"')
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError("must not be negative")
        return float(value) or None
    if isinstance(value, str):
        m = DURATION_RE.match(value.strip())
        if m:
            return float(int(m[1]) * UNITS[m[2]]) or None
    raise ValueError('must be a duration like "90s", "30m" or "2h"')


def parse_interval(value: Any) -> Interval:
    if value == "daily":
        return DAILY
    if value == "weekly":
        return Interval(7, "d")
    if isinstance(value, str):
        m = INTERVAL_RE.match(value.strip())
        if m and int(m[1]) > 0:
            return Interval(int(m[1]), m[2])
    raise ValueError('must be "daily", "weekly", "<n>h" or "<n>d"')


def parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, str):
        m = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
        if m and int(m[1]) < 24 and int(m[2]) < 60:
            return time(int(m[1]), int(m[2]))
    raise ValueError('must be a local time like "08:00"')


def _str(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty string")
    return value


def _requires(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return tuple(value)
    raise ValueError("must be an executable name or a list of them")


def _env(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("must be a table of strings")
    out = {}
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, str | int | float):
            raise ValueError(f"{k} must be a string")
        out[k] = str(v)
    return out


PARSERS = {
    "run": _str,
    "lock": _str,
    "version": _str,
    "check": _str,
    "requires": _requires,
    "interval": parse_interval,
    "timeout": parse_duration,
    "env": _env,
}


def _tool(name: str, spec: Any, problems: list[str]) -> Tool | None:
    where = f"[tools.{name}]"
    if not NAME_RE.match(name):
        problems.append(f"{where}: name may only use letters, digits, '.', '_' and '-'")
        return None
    if not isinstance(spec, dict):
        problems.append(f"{where}: must be a table")
        return None
    merged: dict[str, Any] = {}
    preset = spec.get("preset")
    known = isinstance(preset, str) and preset in PRESETS
    if preset is not None:
        if known:
            merged.update(PRESETS[preset])
        else:
            problems.append(
                f"{where} preset: unknown preset {preset!r} (known: {', '.join(PRESETS)})"
            )
    fields: dict[str, Any] = {"name": name, "preset": preset if known else None}
    for key, value in spec.items():
        if key == "preset":
            continue
        if key not in TOOL_KEYS:
            problems.append(f"{where}: unknown key {key!r}")
            continue
        merged[key] = value
    for key, value in merged.items():
        if key == "auto":
            if isinstance(value, bool):
                fields["auto"] = value
            else:
                problems.append(f"{where} auto: must be true or false")
            continue
        try:
            fields[key] = PARSERS[key](value)
        except ValueError as e:
            problems.append(f"{where} {key}: {e}")
    if "run" not in fields:
        problems.append(f"{where}: needs a run command or a preset; ignored")
        return None
    return Tool(**fields)


def parse(data: dict[str, Any], path: Path | None = None) -> Config:
    cfg = Config(path=path)
    problems = cfg.problems
    for section, body in data.items():
        if section in ("tools", "aliases"):
            continue
        if section not in SECTION_KEYS:
            problems.append(f"unknown section [{section}]")
            continue
        if not isinstance(body, dict):
            problems.append(f"[{section}] must be a table")
            continue
        for key, value in body.items():
            if key not in SECTION_KEYS[section]:
                problems.append(f"[{section}]: unknown key {key!r}")
                continue
            _setting(cfg, section, key, value)

    aliases = data.get("aliases", {})
    if not isinstance(aliases, dict):
        problems.append("[aliases] must be a table")
        aliases = {}
    for name, cmd in aliases.items():
        tool = _tool(name, {"run": cmd}, problems)
        if tool:
            cfg.tools[name] = tool

    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        problems.append("[tools] must be a table of [tools.<name>] tables")
        tools = {}
    for name, spec in tools.items():
        if name in cfg.tools:
            problems.append(f"{name!r} is in both [aliases] and [tools]; using [tools.{name}]")
        tool = _tool(name, spec, problems)
        if tool:
            cfg.tools[name] = tool
    return cfg


def _setting(cfg: Config, section: str, key: str, value: Any) -> None:
    where = f"[{section}] {key}"
    try:
        if (section, key) == ("schedule", "at"):
            cfg.at = parse_time(value)
        elif (section, key) == ("schedule", "shell"):
            words = tuple(shlex.split(_str(value)))
            if not words:
                raise ValueError("must name a shell")
            cfg.shell = words
        elif (section, key) == ("logs", "keep_days"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("must be a whole number of days, at least 1")
            cfg.keep_days = value
        elif (section, key) == ("notify", "on"):
            if value not in NOTIFY:
                raise ValueError(f"must be one of {', '.join(NOTIFY)}")
            cfg.notify = value
    except ValueError as e:
        cfg.problems.append(f"{where}: {e}")


def locate(env: Mapping[str, str] = os.environ) -> tuple[Path, bool]:
    """The config file to read, and whether it is the legacy one."""
    path = paths.config_file(env)
    if not path.exists() and not env.get("UPKEEP_CONFIG"):
        legacy = paths.legacy_config_file(env)
        if legacy.exists():
            return legacy, True
    return path, False


def load(env: Mapping[str, str] = os.environ) -> Config:
    path, legacy = locate(env)
    if not path.exists():
        raise ConfigError(
            f"no config at {paths.pretty(path, env)}; run `up init` to write a starter one"
        )
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{paths.pretty(path, env)}: {e}") from None
    except OSError as e:
        raise ConfigError(f"{paths.pretty(path, env)}: {e.strerror}") from None
    cfg = parse(data, path)
    cfg.legacy = legacy
    return cfg
