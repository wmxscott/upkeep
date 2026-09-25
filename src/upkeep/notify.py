"""Desktop notifications. Best effort: never fails a run."""

from __future__ import annotations

import shutil
import subprocess
import sys


def command(title: str, message: str, platform: str = sys.platform) -> list[str] | None:
    if platform == "darwin":
        if shutil.which("terminal-notifier"):
            return ["terminal-notifier", "-title", title, "-message", message]
        script = f"display notification {_applescript(message)} with title {_applescript(title)}"
        return ["osascript", "-e", script]
    if shutil.which("notify-send"):
        return ["notify-send", "--app-name=upkeep", title, message]
    return None


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def send(title: str, message: str) -> bool:
    argv = command(title, message)
    if not argv:
        return False
    try:
        subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
