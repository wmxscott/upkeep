from __future__ import annotations

import json

import pytest

from upkeep import __version__, cli, paths, schedule
from upkeep.history import History


@pytest.fixture
def up(env, capsys):
    def call(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv), env)
        out, err = capsys.readouterr()
        return code, out, err

    return call


TOOLS = """
[notify]
on = "failure"
[tools.good]
run = "echo fine"
auto = true
interval = "1h"
[tools.bad]
run = "echo 'Error: [unauthenticated]'; exit 1"
auto = true
interval = "1h"
[tools.manual]
run = "echo manual"
"""


def test_version(up):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0


def test_help_lists_commands(up, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for word in ("status", "log", "check", "doctor", "schedule", "--auto"):
        assert word in out


def test_missing_config_exits_2(up):
    code, _, err = up("--all")
    assert code == 2 and "up init" in err


def test_unknown_tool_exits_2(up, write_config):
    write_config(TOOLS)
    code, _, err = up("nope")
    assert code == 2 and "unknown tool 'nope'" in err


def test_bad_flags_exit_2(up, write_config):
    write_config(TOOLS)
    assert up("--all", "--auto")[0] == 2
    assert up("--force")[0] == 2
    assert up("good", "--all")[0] == 2
    assert up("-i", "good", "bad")[0] == 2


def test_ok_run_exits_0(up, write_config):
    write_config(TOOLS)
    code, out, _ = up("good", "manual")
    assert code == 0
    assert "[good] fine" in out and "2 ok" in out


def test_failed_run_exits_1(up, write_config):
    write_config(TOOLS)
    code, out, _ = up("good", "bad")
    assert code == 1
    assert "1 failed" in out


def test_no_tty_without_tools_is_a_usage_error(up, write_config):
    write_config(TOOLS)
    code, _, err = up()
    assert code == 2 and "--all" in err


def test_usage_counted_for_named_runs(up, write_config, state):
    write_config(TOOLS)
    up("good")
    up("good", "manual")
    up("--all")
    assert json.loads(state.usage.read_text()) == {"good": 2, "manual": 1}


def test_auto_runs_due_tools_once(up, write_config, state, no_side_effects):
    write_config(TOOLS)
    code, out, _ = up("--auto")
    assert code == 1
    assert "[good] fine" in out and "manual" not in out
    assert state.notice.read_text().startswith("upkeep: 1 update failed (bad)")
    assert state.last_tick.exists()
    assert no_side_effects == [("upkeep", "upkeep: 1 update failed (bad) — run: up status")]

    code, out, _ = up("--auto")
    assert code == 0 and out == ""
    assert len(History(state).run_dirs()) == 1

    code, out, _ = up("--auto", "--force")
    assert code == 1 and "[good] fine" in out


def test_notify_never(up, write_config, no_side_effects):
    write_config(TOOLS.replace('on = "failure"', 'on = "never"'))
    up("--auto")
    assert no_side_effects == []


def test_manual_runs_never_notify(up, write_config, no_side_effects):
    write_config(TOOLS)
    up("bad")
    assert no_side_effects == []


def test_auto_skips_when_another_tick_runs(up, write_config, state):
    from upkeep.locks import FileLock

    write_config(TOOLS)
    held = FileLock(state.locks, "auto")
    assert held.try_acquire()
    try:
        assert up("--auto") == (0, "", "")
    finally:
        held.release()
    assert History(state).run_dirs() == []


def test_clean_auto_run_clears_the_notice(up, write_config, state):
    write_config(TOOLS)
    up("--auto")
    assert state.notice.exists()
    write_config(TOOLS.replace("exit 1", "exit 0"))
    up("--auto", "--force")
    assert not state.notice.exists()


def test_status(up, write_config, state):
    write_config(TOOLS)
    up("--auto")
    code, out, _ = up("status", "--brief")
    assert code == 0 and out.strip() == "upkeep: 1 failed (bad)"
    assert state.notice.exists()

    code, out, _ = up("status")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["TOOL", "STATUS", "WHEN", "TOOK", "DETAIL"]
    assert lines[1].split()[:2] == ["good", "ok"]
    assert lines[2].split()[:2] == ["bad", "failed"]
    assert "exit 1" in lines[2]
    assert lines[3].split() == ["manual", "never"]
    assert not state.notice.exists()


def test_status_brief_ok(up, write_config):
    write_config(TOOLS)
    assert up("status", "--brief")[1].strip() == "upkeep: no runs yet"
    up("good")
    assert up("status", "--brief")[1].startswith("upkeep: ok, last run")


def test_log(up, write_config):
    write_config(TOOLS)
    up("good")
    up("bad")
    code, out, _ = up("log", "good")
    assert code == 0 and "fine" in out
    code, out, _ = up("log")
    assert "── bad: failed (exit 1)" in out and "fine" not in out
    code, out, _ = up("log", "--run", "2")
    assert "fine" in out
    code, out, _ = up("log", "good", "--path")
    assert out.strip().endswith("good.log")
    assert up("log", "manual")[0] == 1


def test_list(up, write_config):
    write_config(TOOLS)
    _, out, _ = up("list")
    rows = [line.split() for line in out.splitlines()]
    assert rows[0] == ["TOOL", "AUTO", "LOCK", "INTERVAL", "NEXT", "DUE"]
    assert rows[1] == ["good", "yes", "-", "1h", "now"]
    assert rows[3] == ["manual", "-", "-", "daily", "manual"]


def test_check(up, write_config):
    write_config(
        """
[tools.a]
run = "false"
check = "printf 'x 1 -> 2\\ny 3 -> 4\\n'"
[tools.b]
run = "false"
check = "true"
[tools.c]
run = "false"
check = "echo broken >&2; exit 2"
[tools.d]
run = "false"
"""
    )
    code, out, _ = up("check")
    assert code == 1
    assert "a  2 outdated" in out and "  x 1 -> 2" in out
    assert "b  up to date" in out
    assert "c  check failed (exit 2)" in out and "broken" in out
    assert "d  no check" in out
    assert "1 with updates: a (2)" in out
    assert up("check", "a", "b")[0] == 0


def test_doctor_exit_codes(up, write_config):
    write_config(TOOLS)
    code, out, _ = up("doctor")
    assert code == 0 and "config ok" in out
    write_config(TOOLS + "bogus = 1\n")
    code, out, _ = up("doctor")
    assert code == 1 and "unknown key 'bogus'" in out


def test_runtime_warns_on_unknown_keys(up, write_config):
    write_config(TOOLS + "bogus = 1\n")
    code, _, err = up("good")
    assert code == 0 and "warning: [tools.manual]: unknown key 'bogus'" in err


def test_legacy_config_hint(up, env):
    legacy = paths.legacy_config_file(env)
    legacy.parent.mkdir(parents=True)
    legacy.write_text('[aliases]\nhello = "echo hi"\n')
    code, out, err = up("hello")
    assert code == 0 and "[hello] hi" in out
    assert "legacy" in err and "~/.config/upkeep/config.toml" in err


def test_init(up, env):
    legacy = paths.legacy_config_file(env)
    legacy.parent.mkdir(parents=True)
    legacy.write_text('[aliases]\nhello = "echo \\"hi\\""\n')
    code, out, _ = up("init")
    assert code == 0 and "imported 1" in out
    target = paths.config_file(env)
    from upkeep import config

    cfg = config.load(env)
    assert cfg.path == target and cfg.problems == []
    assert cfg.tools["hello"].run == 'echo "hi"' and not cfg.tools["hello"].auto
    assert up("init")[0] == 1
    assert up("init", "--force")[0] == 0


def test_tool_named_like_a_command(up, write_config):
    write_config('[tools.status]\nrun = "echo shadowed"\n')
    code, out, err = up("run", "status")
    assert code == 0 and "[status] shadowed" in out
    assert "up run status" in err


def test_schedule_dry_run(up, write_config, monkeypatch):
    write_config(TOOLS)
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    monkeypatch.setattr(schedule, "up_program", lambda: ["/opt/bin/upkeep"])
    code, out, _ = up("schedule", "install", "--dry-run")
    assert code == 0
    assert "upkeep.timer" in out and "OnCalendar=hourly" in out
    assert '"/bin/sh" "-c" "/opt/bin/upkeep --auto"' in out
    assert "$ systemctl --user enable --now upkeep.timer" in out


def test_schedule_install_applies(up, write_config, monkeypatch, fake_exec, env):
    write_config(TOOLS)
    fake = fake_exec()
    monkeypatch.setattr(schedule, "default_exec", fake)
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    monkeypatch.setattr(schedule, "up_program", lambda: ["/opt/bin/upkeep"])
    code, out, _ = up("schedule", "install")
    assert code == 0 and "installed" in out
    plist = schedule.Launchd(env, paths.state(env)).plist
    assert plist.exists()
    assert [c[:2] for c in fake.calls] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]
    code, out, _ = up("schedule", "status")
    assert "last tick" in out and "next due" in out
    code, out, _ = up("schedule", "remove")
    assert code == 0 and not plist.exists()


def test_version_string():
    assert __version__.count(".") == 2


def test_schedule_log_is_trimmed(tmp_path):
    log = tmp_path / "schedule.log"
    log.write_bytes(b"old line\n" * 200_000)
    cli._trim(log)
    data = log.read_bytes()
    assert len(data) <= cli.SCHEDULE_LOG_KEEP
    assert data.startswith(b"old line\n")
