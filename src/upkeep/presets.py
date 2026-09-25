"""Built-in recipes for common package managers.

A `check` prints one line per outdated item and nothing when all is current,
so `up check` can count lines.
"""

from __future__ import annotations

from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "brew": {
        "run": "brew update && brew upgrade --formula",
        "requires": "brew",
        "lock": "brew",
        "version": "brew --version",
        "check": "brew outdated --formula",
    },
    "brew-cask": {
        "run": "brew update && brew upgrade --cask",
        "requires": "brew",
        "lock": "brew",
        "check": "brew outdated --cask",
    },
    "mise": {
        "run": "mise upgrade",
        "requires": "mise",
        "lock": "mise",
        "version": "mise --version",
        "check": "mise outdated --no-header",
    },
    "npm": {
        "run": "npm update -g",
        "requires": "npm",
        "lock": "npm",
        "version": "npm --version",
        "check": "npm outdated -g --parseable || true",
    },
    "pnpm": {
        "run": "pnpm self-update && pnpm update --global",
        "requires": "pnpm",
        "lock": "pnpm",
        "version": "pnpm --version",
    },
    "bun": {
        "run": "bun upgrade && bun update --global",
        "requires": "bun",
        "lock": "bun",
        "version": "bun --version",
    },
    "uv": {
        "run": "uv tool upgrade --all",
        "requires": "uv",
        "lock": "uv",
        "version": "uv --version",
    },
    "pipx": {
        "run": "pipx upgrade-all",
        "requires": "pipx",
        "lock": "pipx",
        "version": "pipx --version",
    },
    "rustup": {
        "run": "rustup update",
        "requires": "rustup",
        "lock": "rustup",
        "version": "rustc --version",
        "check": "rustup check | grep -F 'Update available' || true",
    },
    "cargo": {
        "run": "cargo install-update --all",
        "requires": "cargo-install-update",
        "lock": "cargo",
        "check": "cargo install-update --list | awk '$NF == \"Yes\"'",
    },
    "gh-extensions": {
        "run": "gh extension upgrade --all",
        "requires": "gh",
        "check": (
            "gh extension upgrade --all --dry-run 2>&1 | grep -F 'would have upgraded' || true"
        ),
    },
}

# Shown by `up doctor` for auto tools built on these presets.
UNATTENDED_NOTES = {
    "brew-cask": "casks often need sudo to install or remove files, which an unattended run lacks",
}
