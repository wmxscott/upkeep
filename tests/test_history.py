from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from upkeep import history, paths
from upkeep.history import History, ToolRecord, strip_ansi

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def make_run(state, when, trigger="manual", **tools):
    run = History(state).new_run(trigger, when)
    for name, status in tools.items():
        run.tools[name] = ToolRecord(status=status, started=history.iso(when))
        run.log_path(name).write_text("$ x\n")
    run.save()
    return run


def test_strip_ansi():
    assert strip_ansi("\x1b[1;32mok\x1b[0m") == "ok"
    assert strip_ansi("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\") == "link"
    assert strip_ansi("10%\r50%\r100%") == "100%"
    assert strip_ansi("line\r") == "line"


def test_short_version():
    assert history.short_version("Homebrew 4.6.3") == "4.6.3"
    assert history.short_version("rustc 1.90.0 (1159e78c4 2025-09-14)") == "1.90.0"
    assert history.short_version("gh version 2.101.0 (2026-09-15)") == "2.101.0"
    assert history.short_version("v1.3.12-canary.4") == "1.3.12-canary.4"
    assert history.short_version("  nightly build  ") == "nightly build"
    assert history.short_version("x" * 40) == "x" * 23 + "…"


def test_version_display():
    assert ToolRecord("ok", version_before="t 1.0", version_after="t 1.1").version == "1.0 → 1.1"
    assert ToolRecord("ok", version_before="t 1.0", version_after="t 1.0").version == "1.0"
    assert ToolRecord("failed", version_before="t 1.0").version == "1.0"
    assert ToolRecord("ok").version == ""
    # Same number, different build text: no change to report.
    same = ToolRecord("ok", version_before="t 1.0 (a)", version_after="t 1.0 (b)")
    assert same.version_change is None and same.version == "1.0"


def test_latest_per_tool(state):
    make_run(state, NOW - timedelta(days=2), a="failed", b="ok")
    make_run(state, NOW - timedelta(days=1), a="ok")
    latest = History(state).latest()
    assert latest["a"][1].status == "ok"
    assert latest["b"][1].status == "ok"
    assert latest["b"][0].started_at == NOW - timedelta(days=2)


def test_prune_keeps_recent_and_each_tools_latest(state):
    old_only_b = make_run(state, NOW - timedelta(days=40), b="ok")
    old_a = make_run(state, NOW - timedelta(days=39), a="ok")
    recent_a = make_run(state, NOW - timedelta(days=1), a="ok")
    removed = History(state).prune(30, NOW)
    assert removed == [old_a.dir]
    assert old_only_b.dir.exists() and recent_a.dir.exists()


def test_corrupt_run_is_ignored(state):
    make_run(state, NOW, a="ok")
    bad = state.runs / "20990101T000000Z"
    bad.mkdir()
    (bad / "run.json").write_text("{not json")
    assert [r.id for r in History(state).runs()] == ["20260310T120000Z"]


def test_usage_migrates_legacy_once(env, state):
    legacy = paths.legacy_usage_file(env)
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"brew": 5, "junk": "x"}))
    assert history.load_usage(state, env) == {"brew": 5}
    assert json.loads(state.usage.read_text()) == {"brew": 5}
    history.count_usage(state, ["brew", "mise"], env)
    assert history.load_usage(state, env) == {"brew": 6, "mise": 1}
    assert json.loads(legacy.read_text()) == {"brew": 5, "junk": "x"}


def test_notice_text():
    text = history.notice_text(["brew", "cursor"])
    assert text == "upkeep: 2 updates failed (brew, cursor) — run: up status"
    assert history.notice_text(["x"]).startswith("upkeep: 1 update failed")


def test_notice_lifecycle(state):
    fail = {"a": ToolRecord("failed"), "b": ToolRecord("ok")}
    history.update_notice(state, "auto", fail, {"a", "b"})
    assert "(a)" in state.notice.read_text()

    history.update_notice(state, "auto", {"b": ToolRecord("ok")}, {"a", "b"})
    assert not state.notice.exists()


def test_later_auto_failures_add_to_the_notice(state):
    history.update_notice(state, "auto", {"a": ToolRecord("failed")}, {"a", "b", "c"})
    history.update_notice(
        state, "auto", {"b": ToolRecord("failed"), "c": ToolRecord("ok")}, {"a", "b", "c"}
    )
    assert history.read_notice(state) == ["a", "b"]
    history.update_notice(
        state, "auto", {"a": ToolRecord("ok"), "c": ToolRecord("failed")}, {"a", "b", "c"}
    )
    assert history.read_notice(state) == ["b", "c"]


def test_manual_run_trims_the_notice(state):
    make_run(state, NOW - timedelta(hours=2), "auto", a="failed", b="failed")
    history.write_notice(state, ["a", "b"])
    make_run(state, NOW - timedelta(hours=1), a="ok")
    history.update_notice(state, "manual", {"a": ToolRecord("ok")}, {"a", "b"})
    assert "(b)" in state.notice.read_text()

    make_run(state, NOW, b="ok")
    history.update_notice(state, "manual", {"b": ToolRecord("ok")}, {"a", "b"})
    assert not state.notice.exists()


def test_manual_failure_does_not_create_a_notice(state):
    history.update_notice(state, "manual", {"a": ToolRecord("failed")}, {"a"})
    assert not state.notice.exists()
