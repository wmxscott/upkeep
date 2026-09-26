"""The interactive checkbox picker `up` opens with no arguments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

MESSAGE = "Select updates"
INSTRUCTION = "(↑↓ move, space select, a all, i invert, enter run, q quit)"


def order(names: Sequence[str], usage: Mapping[str, int]) -> list[str]:
    """Most used first; ties keep config order."""
    return sorted(names, key=lambda n: -usage.get(n, 0))


def question(names: Sequence[str], usage: Mapping[str, int], **kwargs):
    """The checkbox question; kwargs go to the prompt_toolkit Application (tests pass I/O)."""
    import questionary
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

    kb = KeyBindings()

    @kb.add("q")
    def _quit(event):
        event.app.exit(result=None)

    q = questionary.checkbox(
        MESSAGE,
        choices=[questionary.Choice(title=n, value=n) for n in order(names, usage)],
        instruction=INSTRUCTION,
        **kwargs,
    )
    q.application.key_bindings = merge_key_bindings([q.application.key_bindings, kb])
    return q


def pick(names: Sequence[str], usage: Mapping[str, int]) -> list[str]:
    return question(names, usage).ask() or []
