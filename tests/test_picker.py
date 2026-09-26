from __future__ import annotations

import io

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.plain_text import PlainTextOutput

from upkeep import picker

NAMES = ["brew", "mise", "npm"]
DOWN = "\x1b[B"


def answer(keys: str, usage=None) -> tuple[list[str] | None, str]:
    """Drive the picker with keystrokes; returns its answer and what it drew."""
    out = io.StringIO()
    with create_pipe_input() as inp:
        inp.send_text(keys)
        q = picker.question(NAMES, usage or {}, input=inp, output=PlainTextOutput(out))
        result = q.unsafe_ask()
    return result, out.getvalue()


def test_order_most_used_first():
    assert picker.order(NAMES, {"npm": 3, "mise": 1}) == ["npm", "mise", "brew"]


def test_prompt_has_one_instruction():
    _, drawn = answer("\r")
    first = drawn.splitlines()[0]
    assert first == "? Select updates (↑↓ move, space select, a all, i invert, enter run, q quit)"
    assert len(first) <= 80
    assert "Use arrow keys" not in drawn


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (" \r", ["brew"]),
        (DOWN + " \r", ["mise"]),
        ("a\r", NAMES),
        ("aa\r", []),
        (" i\r", ["mise", "npm"]),
        (" q", None),
    ],
)
def test_keys_do_what_the_instruction_says(keys, expected):
    assert answer(keys)[0] == expected
