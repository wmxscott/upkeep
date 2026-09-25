"""The interactive checkbox picker `up` opens with no arguments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def order(names: Sequence[str], usage: Mapping[str, int]) -> list[str]:
    """Most used first; ties keep config order."""
    return sorted(names, key=lambda n: -usage.get(n, 0))


def pick(names: Sequence[str], usage: Mapping[str, int]) -> list[str]:
    import questionary
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

    kb = KeyBindings()

    @kb.add("q")
    def _quit(event):
        event.app.exit(result=None)

    question = questionary.checkbox(
        "Select updates to run (space to toggle, enter to confirm, q to quit):",
        choices=[questionary.Choice(title=n, value=n) for n in order(names, usage)],
    )
    question.application.key_bindings = merge_key_bindings([question.application.key_bindings, kb])
    return question.ask() or []
