from __future__ import annotations

import json
import os
import threading
import time

import pytest

from upkeep.config import Config, Tool
from upkeep.locks import FileLock
from upkeep.runner import Runner, format_duration, lock_groups

SH = ("/bin/sh", "-c")


def make_runner(state, recorder, env, **kw) -> Runner:
    return Runner(Config(shell=SH), state, recorder, env=env, **kw)


def events(path) -> list[str]:
    return path.read_text().split()


def test_lock_groups_keep_config_order():
    tools = [Tool("a", "x", lock="l"), Tool("b", "x"), Tool("c", "x", lock="l")]
    assert [[t.name for t in g] for g in lock_groups(tools)] == [["a", "c"], ["b"]]


def test_shared_lock_serialises(state, recorder, env, tmp_path):
    trace = tmp_path / "trace"
    step = f"echo start-{{0}} >> {trace}; sleep 0.3; echo end-{{0}} >> {trace}"
    tools = [Tool(n, step.format(n), lock="pm") for n in ("a", "b")]
    run = make_runner(state, recorder, env).execute(tools)
    assert events(trace) == ["start-a", "end-a", "start-b", "end-b"]
    assert all(r.status == "ok" for r in run.tools.values())


def test_unlocked_tools_overlap(state, recorder, env, tmp_path):
    trace = tmp_path / "trace"
    step = f"echo start-{{0}} >> {trace}; sleep 0.5; echo end-{{0}} >> {trace}"
    tools = [Tool(n, step.format(n)) for n in ("a", "b")]
    make_runner(state, recorder, env).execute(tools)
    assert events(trace)[:2] == ["start-a", "start-b"] or events(trace)[:2] == [
        "start-b",
        "start-a",
    ]


def test_lock_held_by_another_process_waits(state, recorder, env):
    other = FileLock(state.locks, "lock-pm")
    assert other.try_acquire()
    threading.Timer(0.5, other.release).start()
    t0 = time.monotonic()
    run = make_runner(state, recorder, env).execute([Tool("a", "true", lock="pm")])
    assert time.monotonic() - t0 >= 0.4
    assert run.tools["a"].status == "ok"
    assert ("a", "pm") in recorder.of("waiting")


def test_timeout_kills_the_process_group(state, recorder, env, tmp_path):
    pidfile = tmp_path / "pid"
    cmd = f"sleep 30 & echo $! > {pidfile}; wait"
    run = make_runner(state, recorder, env).execute([Tool("slow", cmd, timeout=1)])
    rec = run.tools["slow"]
    assert rec.status == "failed"
    assert rec.reason == "timed out after 1s"
    assert rec.exit_code is None
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild outlived the timeout")


def test_missing_requirement_skips(state, recorder, env):
    tools = [
        Tool("x", "echo ran", requires=("no-such-exe-xyz",)),
        Tool("y", "true", requires=("sh",)),
    ]
    run = make_runner(state, recorder, env).execute(tools)
    assert run.tools["x"].status == "skipped"
    assert run.tools["x"].reason == "no-such-exe-xyz not found"
    assert not run.log_path("x").exists()
    assert run.tools["y"].status == "ok"


def test_failure_exit_code(state, recorder, env):
    run = make_runner(state, recorder, env).execute([Tool("x", "echo nope; exit 3")])
    rec = run.tools["x"]
    assert (rec.status, rec.exit_code, rec.reason) == ("failed", 3, "exit 3")


def test_env_is_merged(state, recorder, env):
    tool = Tool("x", 'echo "$GREETING from $HOME"', env={"GREETING": "hello"})
    run = make_runner(state, recorder, env).execute([tool])
    assert f"hello from {env['HOME']}" in run.log_path("x").read_text()


def test_version_change_recorded(state, recorder, env, tmp_path):
    ver = tmp_path / "ver"
    ver.write_text("tool 1.0\n")
    tool = Tool("x", f"echo 'tool 1.1' > {ver}", version=f"cat {ver}")
    rec = make_runner(state, recorder, env).execute([tool]).tools["x"]
    assert (rec.version_before, rec.version_after) == ("tool 1.0", "tool 1.1")
    assert rec.version_change == "1.0 → 1.1"


def test_log_and_run_json(state, recorder, env):
    tool = Tool("x", r"printf '\033[31mred\033[0m\n'; printf 'progress 10%%\rprogress 100%%\n'")
    run = make_runner(state, recorder, env, trigger="auto").execute([tool])
    log = run.log_path("x").read_text().splitlines()
    assert log[1:] == ["red", "progress 100%"]
    assert ("x", "red") in recorder.of("output")
    data = json.loads((run.dir / "run.json").read_text())
    assert data["trigger"] == "auto"
    assert data["finished"]
    x = data["tools"]["x"]
    assert x["status"] == "ok" and x["exit_code"] == 0 and x["log"] == "x.log"
    assert x["started"].endswith("Z") and x["duration"] >= 0


def test_background_process_holding_the_pipe_does_not_hang(state, recorder, env, tmp_path):
    pidfile = tmp_path / "pid"
    t0 = time.monotonic()
    tool = Tool("x", f"sleep 20 & echo $! > {pidfile}; echo started")
    run = make_runner(state, recorder, env).execute([tool])
    os.kill(int(pidfile.read_text()), 15)
    assert time.monotonic() - t0 < 10
    assert run.tools["x"].status == "ok"


def test_run_ids_do_not_collide(state, recorder, env):
    a = make_runner(state, recorder, env).execute([Tool("x", "true")])
    b = make_runner(state, recorder, env).execute([Tool("x", "true")])
    assert a.dir != b.dir


def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(65) == "1m05s"
    assert format_duration(3720) == "1h02m"
