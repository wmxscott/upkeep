from __future__ import annotations

import tomllib

from upkeep import doctor
from upkeep.config import Tool, parse
from upkeep.history import History, ToolRecord


def test_risky_auto_commands():
    assert doctor.risks(Tool("a", "sudo softwareupdate -i -a"))
    assert doctor.risks(Tool("b", "brew update && brew upgrade"))
    assert not doctor.risks(Tool("c", "brew update && brew upgrade --formula"))
    assert doctor.risks(Tool("d", "cd ~/src/x && git pull"))
    assert doctor.risks(Tool("e", "true", preset="brew-cask"))
    assert not doctor.risks(Tool("f", "mise upgrade"))


def test_failure_hints():
    assert "SSH" in doctor.failure_hints("git@host: Permission denied (publickey).")[0]
    assert "sudo" in doctor.failure_hints("sudo: a terminal is required to read the password")[0]
    sudo_rm = "`/usr/bin/sudo -u root -E -- /bin/rm --` exited with 1"
    assert "sudo" in doctor.failure_hints(sudo_rm)[0]
    assert "logged in" in doctor.failure_hints("Update failed: [unauthenticated] Error")[0]
    assert "lock" in doctor.failure_hints("Another `brew update` process is already running.")[0]
    assert doctor.failure_hints("all good") == []


def test_lock_clash():
    cfg = parse(
        tomllib.loads('[tools.brew]\npreset = "brew"\n[tools.codex]\nrun = "brew upgrade codex"\n')
    )
    clashes = doctor.lock_clashes(cfg.tools)
    assert len(clashes) == 1 and "brew, codex" in clashes[0]
    cfg = parse(
        tomllib.loads(
            '[tools.brew]\npreset = "brew"\n'
            '[tools.codex]\nrun = "brew upgrade codex"\nlock = "brew"\n'
        )
    )
    assert doctor.lock_clashes(cfg.tools) == []


def test_invokes_matches_words_only():
    assert doctor.invokes("brew upgrade", "brew")
    assert doctor.invokes("x && uv tool upgrade --all", "uv")
    assert not doctor.invokes("uvx ruff", "uv")
    assert not doctor.invokes("homebrew-thing", "brew")


def test_diagnose(state):
    cfg = parse(
        tomllib.loads(
            '[tools.a]\nrun = "true"\nauto = true\nbogus = 1\n'
            '[tools.b]\nrun = "true"\nrequires = "no-such-exe-xyz"\n'
            '[tools.c]\nrun = "no-such-exe-xyz --update"\n'
        )
    )
    run = History(state).new_run("auto")
    run.tools["a"] = ToolRecord("failed", reason="exit 1", log="a.log")
    run.log_path("a").write_text("$ x\nError: [unauthenticated]\n")
    run.save()
    found = doctor.diagnose(cfg, state, "/usr/bin:/bin", schedule_installed=False)
    text = [(f.level, f.text) for f in found]
    assert ("error", "[tools.a]: unknown key 'bogus'") in text
    assert any(
        lvl == "warn" and "no-such-exe-xyz not found; it will be skipped" in t for lvl, t in text
    )
    assert any("c: no-such-exe-xyz not found on PATH" in t for _, t in text)
    assert any("a: last run failed (exit 1): not logged in" in t for _, t in text)
    assert any("up schedule install" in t for _, t in text)
