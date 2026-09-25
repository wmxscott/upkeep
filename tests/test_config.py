from __future__ import annotations

import tomllib
from datetime import time

import pytest

from upkeep import config, paths
from upkeep.config import DAILY, ConfigError, Interval, parse
from upkeep.presets import PRESETS


def p(text: str) -> config.Config:
    return parse(tomllib.loads(text))


def test_full_tool():
    cfg = p(
        """
        [schedule]
        at = "07:30"
        shell = "zsh -lic"
        [logs]
        keep_days = 7
        [notify]
        on = "always"
        [tools.brew]
        run = "brew update && brew upgrade --formula"
        auto = true
        lock = "brew"
        requires = "brew"
        version = "brew --version"
        check = "brew outdated"
        interval = "weekly"
        timeout = "30m"
        env = { HOMEBREW_NO_ENV_HINTS = "1" }
        """
    )
    assert cfg.problems == []
    assert cfg.at == time(7, 30)
    assert cfg.shell == ("zsh", "-lic")
    assert cfg.keep_days == 7
    assert cfg.notify == "always"
    brew = cfg.tools["brew"]
    assert brew.auto and brew.lock == "brew" and brew.requires == ("brew",)
    assert brew.interval == Interval(7, "d") and str(brew.interval) == "weekly"
    assert brew.timeout == 1800
    assert brew.env == {"HOMEBREW_NO_ENV_HINTS": "1"}


def test_defaults():
    cfg = p('[tools.x]\nrun = "true"')
    x = cfg.tools["x"]
    assert (x.auto, x.lock, x.requires, x.interval, x.timeout) == (False, None, (), DAILY, 3600)
    assert cfg.at == time(8, 0) and cfg.keep_days == 30 and cfg.notify == "failure"


def test_legacy_aliases_are_manual_tools():
    cfg = p('[aliases]\nfoo = "echo foo"\nbar = "echo bar"')
    assert list(cfg.tools) == ["foo", "bar"]
    assert cfg.tools["foo"].run == "echo foo"
    assert not cfg.tools["foo"].auto
    assert cfg.problems == []


def test_tools_win_over_aliases():
    cfg = p('[aliases]\nfoo = "old"\n[tools.foo]\nrun = "new"')
    assert cfg.tools["foo"].run == "new"
    assert any("both" in x for x in cfg.problems)


def test_preset_fills_and_fields_override():
    cfg = p('[tools.b]\npreset = "brew"\nlock = "pm"\nauto = true')
    b = cfg.tools["b"]
    assert b.run == PRESETS["brew"]["run"]
    assert b.check == PRESETS["brew"]["check"]
    assert b.requires == ("brew",)
    assert b.lock == "pm"
    assert b.preset == "brew"


@pytest.mark.parametrize("name", list(PRESETS))
def test_every_preset_is_valid(name):
    cfg = p(f'[tools.t]\npreset = "{name}"')
    assert cfg.problems == []
    assert cfg.tools["t"].run and cfg.tools["t"].requires


def test_brew_preset_is_formulae_only():
    assert "--formula" in PRESETS["brew"]["run"]
    assert PRESETS["brew"]["lock"] == PRESETS["brew-cask"]["lock"]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ('[tools.x]\nrun = "a"\nbogus = 1', "unknown key 'bogus'"),
        ('[tools.x]\nrun = "a"\ninterval = "sometimes"', "interval"),
        ('[tools.x]\nrun = "a"\ntimeout = "forever"', "timeout"),
        ('[tools.x]\nrun = "a"\nauto = "yes"', "auto"),
        ('[tools.x]\nrun = "a"\nenv = { A = [1] }', "env"),
        ('[tools.x]\nrun = "a"\nrequires = 3', "requires"),
        ('[tools.x]\npreset = "nope"\nrun = "a"', "unknown preset"),
        ("[tools.x]\nauto = true", "needs a run"),
        ('[tools."bad name"]\nrun = "a"', "name may only"),
        ('[schedule]\nat = "25:00"', "[schedule] at"),
        ('[notify]\non = "sometimes"', "[notify] on"),
        ("[logs]\nkeep_days = 0", "keep_days"),
        ("[logs]\nkeep = 3", "unknown key 'keep'"),
        ("[extra]\na = 1", "unknown section [extra]"),
    ],
)
def test_problems(body, fragment):
    cfg = p(body)
    assert any(fragment in x for x in cfg.problems), cfg.problems


def test_bad_value_falls_back_to_default():
    cfg = p('[tools.x]\nrun = "a"\ninterval = "sometimes"\ntimeout = "2h"')
    assert cfg.tools["x"].interval == DAILY
    assert cfg.tools["x"].timeout == 7200


def test_tool_without_run_is_dropped():
    cfg = p("[tools.x]\nauto = true")
    assert "x" not in cfg.tools


def test_durations_and_intervals():
    assert config.parse_duration("90s") == 90
    assert config.parse_duration("2h") == 7200
    assert config.parse_duration(0) is None
    assert config.parse_duration("0m") is None
    assert config.parse_interval("6h") == Interval(6, "h")
    assert config.parse_interval("3d") == Interval(3, "d")
    with pytest.raises(ValueError):
        config.parse_interval("0h")


def test_shell_words_default(env):
    assert config.Config().shell_words(env) == ("/bin/sh", "-lc")
    assert config.Config().shell_words({}) == ("/bin/sh", "-lc")
    assert config.Config().shell_words({"SHELL": "/usr/bin/fish"}) == ("/bin/sh", "-lc")
    assert config.Config(shell=("zsh", "-lic")).shell_words(env) == ("zsh", "-lic")


def test_load_missing(env):
    with pytest.raises(ConfigError, match="up init"):
        config.load(env)


def test_load_bad_toml(env, write_config):
    write_config("[tools.x\n")
    with pytest.raises(ConfigError):
        config.load(env)


def test_load_legacy(env):
    legacy = paths.legacy_config_file(env)
    legacy.parent.mkdir(parents=True)
    legacy.write_text('[aliases]\nfoo = "echo foo"\n')
    cfg = config.load(env)
    assert cfg.legacy and cfg.path == legacy
    assert "foo" in cfg.tools


def test_new_location_beats_legacy(env, write_config):
    legacy = paths.legacy_config_file(env)
    legacy.parent.mkdir(parents=True)
    legacy.write_text('[aliases]\nold = "x"\n')
    write_config('[tools.new]\nrun = "x"\n')
    cfg = config.load(env)
    assert not cfg.legacy and list(cfg.tools) == ["new"]


def test_env_override(env, tmp_path):
    other = tmp_path / "elsewhere.toml"
    other.write_text('[tools.z]\nrun = "x"\n')
    cfg = config.load({**env, "UPKEEP_CONFIG": str(other)})
    assert list(cfg.tools) == ["z"]
