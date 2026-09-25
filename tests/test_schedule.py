from __future__ import annotations

import os
import plistlib
import stat

import pytest

from upkeep import schedule
from upkeep.config import Config

PROGRAM = ["/opt/bin/upkeep"]


def test_job_argv_runs_auto_through_the_shell(env):
    argv = schedule.job_argv(Config(shell=("sh", "-lc")), PROGRAM, env)
    assert argv == ["/bin/sh", "-lc", "/opt/bin/upkeep --auto"]


def test_job_argv_default_shell(env):
    assert schedule.job_argv(Config(), PROGRAM, env)[:2] == ["/bin/sh", "-lc"]


def test_job_argv_quotes_paths(env):
    argv = schedule.job_argv(Config(shell=("/bin/sh", "-c")), ["/a b/upkeep"], env)
    assert argv[-1] == "'/a b/upkeep' --auto"


def test_launchd_plist(env, state):
    b = schedule.Launchd(env, state)
    plan = b.install_plan(["/bin/zsh", "-lic", "/opt/bin/upkeep --auto"])
    assert list(plan.files) == [b.plist]
    assert str(b.plist).startswith(env["HOME"])
    assert b.plist.name == "io.github.wmxscott.upkeep.plist"
    data = plistlib.loads(plan.files[b.plist].encode())
    assert data["Label"] == "io.github.wmxscott.upkeep"
    assert data["ProgramArguments"] == ["/bin/zsh", "-lic", "/opt/bin/upkeep --auto"]
    assert data["StartInterval"] == 3600
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"] == str(state.schedule_log)
    assert data["EnvironmentVariables"]["XDG_STATE_HOME"] == env["XDG_STATE_HOME"]
    assert plan.before == [["launchctl", "bootout", f"gui/{os.getuid()}/io.github.wmxscott.upkeep"]]
    assert plan.after == [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(b.plist)]]


def test_launchd_install_and_remove(env, state, fake_exec):
    run = fake_exec({"bootout": (3, "")})
    b = schedule.Launchd(env, state, run)
    errors = b.apply(b.install_plan(["/bin/sh", "-c", "x --auto"]))
    assert errors == []
    assert b.installed()
    assert [c[1] for c in run.calls] == ["bootout", "bootstrap"]
    b.apply(b.remove_plan())
    assert not b.installed()
    assert run.calls[-1][1] == "bootout"


def test_launchd_bootstrap_failure_is_reported(env, state, fake_exec):
    b = schedule.Launchd(env, state, fake_exec({"bootstrap": (5, "Input/output error")}))
    errors = b.apply(b.install_plan(["/bin/sh", "-c", "x"]))
    assert len(errors) == 1 and "Input/output error" in errors[0]


def test_launchd_status(env, state, fake_exec):
    out = "\tstate = not running\n\truns = 4\n\tlast exit code = 1\n\tother = x\n"
    run = fake_exec({"print": (0, out)})
    b = schedule.Launchd(env, state, run)
    assert b.status() == [("job", "not installed")]
    assert run.calls == []
    b.plist.parent.mkdir(parents=True)
    b.plist.write_text("x")
    rows = dict(b.status())
    assert rows["loaded"] == "yes"
    assert rows["runs"] == "4" and rows["last exit code"] == "1"
    assert "other" not in rows


def test_systemd_units(env, state):
    b = schedule.Systemd(env, state)
    plan = b.install_plan(["/usr/bin/zsh", "-lc", "/home/u/.local/bin/upkeep --auto"])
    assert b.service.parent == schedule.paths.config_home(env) / "systemd/user"
    service, timer = plan.files[b.service], plan.files[b.timer]
    assert 'ExecStart="/usr/bin/zsh" "-lc" "/home/u/.local/bin/upkeep --auto"' in service
    assert "Type=oneshot" in service
    assert f'Environment="XDG_CONFIG_HOME={env["XDG_CONFIG_HOME"]}"' in service
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert plan.after == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "upkeep.timer"],
    ]


def test_systemd_quoting():
    assert schedule._systemd_quote('a "b" $HOME 50%') == '"a \\"b\\" $$HOME 50%%"'


def test_systemd_install_remove_status(env, state, fake_exec):
    show = (
        "UnitFileState=enabled\nActiveState=active\n"
        "LastTriggerUSec=Thu 2026-03-12 09:00:01 UTC\n"
        "NextElapseUSecRealtime=Thu 2026-03-12 10:00:00 UTC\n"
    )
    run = fake_exec({"--user": (0, show)})
    b = schedule.Systemd(env, state, run)
    assert b.apply(b.install_plan(["/bin/sh", "-c", "x"])) == []
    assert b.installed()
    rows = dict(b.status())
    assert rows["enabled"] == "enabled" and rows["next tick"].endswith("10:00:00 UTC")
    b.apply(b.remove_plan())
    assert not b.service.exists() and not b.timer.exists()
    assert ["systemctl", "--user", "disable", "--now", "upkeep.timer"] in run.calls


def test_backend_by_platform(env, state):
    assert isinstance(schedule.backend(env, state, "darwin"), schedule.Launchd)
    assert isinstance(schedule.backend(env, state, "linux"), schedule.Systemd)
    with pytest.raises(RuntimeError):
        schedule.backend(env, state, "win32")


def _exe(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_up_program_prefers_upkeep_sibling(tmp_path):
    up = _exe(tmp_path / "up")
    _exe(tmp_path / "upkeep")
    assert schedule.up_program(str(up)) == [str(tmp_path / "upkeep")]


def test_up_program_absolute_without_resolving_symlinks(tmp_path, monkeypatch):
    real = tmp_path / "cellar"
    real.mkdir()
    target = _exe(real / "upkeep")
    link = tmp_path / "bin" / "upkeep"
    link.parent.mkdir()
    link.symlink_to(target)
    monkeypatch.chdir(tmp_path)
    assert schedule.up_program("bin/upkeep") == [str(link)]


def test_up_program_module(tmp_path):
    argv = schedule.up_program(str(tmp_path / "__main__.py"))
    assert argv[1:] == ["-m", "upkeep"]
