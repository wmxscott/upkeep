"""Where upkeep keeps its config and its state."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP = "upkeep"


def _base(env: Mapping[str, str], var: str, fallback: Path) -> Path:
    value = env.get(var, "")
    return Path(value) if value.startswith("/") else fallback


def home(env: Mapping[str, str] = os.environ) -> Path:
    value = env.get("HOME", "")
    return Path(value) if value.startswith("/") else Path.home()


def config_home(env: Mapping[str, str] = os.environ) -> Path:
    return _base(env, "XDG_CONFIG_HOME", home(env) / ".config")


def state_home(env: Mapping[str, str] = os.environ) -> Path:
    return _base(env, "XDG_STATE_HOME", home(env) / ".local/state")


def config_file(env: Mapping[str, str] = os.environ) -> Path:
    """`$UPKEEP_CONFIG`, else `$XDG_CONFIG_HOME/upkeep/config.toml`."""
    if env.get("UPKEEP_CONFIG"):
        return Path(env["UPKEEP_CONFIG"]).expanduser()
    return config_home(env) / APP / "config.toml"


def legacy_config_file(env: Mapping[str, str] = os.environ) -> Path:
    """The config of the script upkeep replaces."""
    return home(env) / ".config/up/up.toml"


def legacy_usage_file(env: Mapping[str, str] = os.environ) -> Path:
    return state_home(env) / "up/usage.json"


def pretty(path: Path | str, env: Mapping[str, str] = os.environ) -> str:
    """`path` with the home directory shortened to `~`."""
    text = str(path)
    h = str(home(env))
    if text == h:
        return "~"
    if text.startswith(h + "/"):
        return "~" + text[len(h) :]
    return text


@dataclass(frozen=True)
class State:
    root: Path

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def locks(self) -> Path:
        return self.root / "locks"

    @property
    def usage(self) -> Path:
        return self.root / "usage.json"

    @property
    def notice(self) -> Path:
        return self.root / "notice"

    @property
    def last_tick(self) -> Path:
        return self.root / "last-auto"

    @property
    def schedule_log(self) -> Path:
        return self.root / "schedule.log"


def state(env: Mapping[str, str] = os.environ) -> State:
    return State(state_home(env) / APP)
