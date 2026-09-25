from __future__ import annotations

import io

from upkeep.config import Config, Tool
from upkeep.runner import Runner
from upkeep.ui import LiveReporter, StreamReporter


def run_with(reporter, state, env, tools):
    return Runner(Config(shell=("/bin/sh", "-c")), state, reporter, env=env).execute(tools)


TOOLS = [
    Tool("good", "echo fine"),
    Tool("bad", "for i in 1 2 3; do echo line$i; done; exit 4"),
    Tool("gone", "true", requires=("no-such-exe-xyz",)),
]


def test_live_board_shows_only_failures(state, env):
    out = io.StringIO()
    run_with(LiveReporter(out, color=False, interval=0.05), state, env, TOOLS)
    text = out.getvalue()
    final = text.rsplit("\x1b[3F", 1)[-1]
    assert "✓ good" in final
    assert "✗ bad" in final and "exit 4" in final
    assert "\u2013 gone" in final and "skipped: no-such-exe-xyz not found" in final
    assert "── bad: exit 4" in text
    assert "  line3" in text and "log: " in text
    assert "fine" not in final.split("── bad")[1]
    assert text.rstrip().splitlines()[-1].startswith("1 ok, 1 failed, 1 skipped in ")
    assert "\x1b[?25h" in text


def test_stream_prefixes_every_line(state, env):
    out = io.StringIO()
    run_with(StreamReporter(out, color=False, header=True), state, env, TOOLS)
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("upkeep: manual run at ")
    assert "[good] fine" in lines
    assert "[bad] line2" in lines
    assert "[gone] skipped: no-such-exe-xyz not found" in lines
    assert any(line.startswith("[bad] failed (exit 4)") for line in lines)
    assert "\x1b[" not in out.getvalue()


def test_colour(state, env):
    out = io.StringIO()
    run_with(StreamReporter(out, color=True), state, env, TOOLS[:1])
    assert "\x1b[1;36m[good]\x1b[0m fine" in out.getvalue()
