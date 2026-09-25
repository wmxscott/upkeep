# upkeep

[![CI](https://github.com/wmxscott/upkeep/actions/workflows/ci.yml/badge.svg)](https://github.com/wmxscott/upkeep/actions/workflows/ci.yml)

Keep your tools up to date. `up` runs the update commands you list in a TOML file: the ones you pick, all of them in parallel, or, from an hourly job, the ones you've opted in that are due.

"Update everything" scripts tend to fail quietly when nobody is watching: a cask wants sudo, a git remote wants an SSH key touch, a CLI isn't logged in, two tools run `brew` at once. upkeep is built around that. Nothing runs unattended unless you mark it `auto`, tools that share a package manager take turns, every run is logged, and failures leave a one-line notice for your next shell. [topgrade](https://github.com/topgrade-rs/topgrade) is the big prior art and knows hundreds of tools. upkeep knows nine presets and runs whatever else you tell it to: small, and config first.

## Install

```sh
brew install wmxscott/tap/upkeep
# or
uv tool install git+https://github.com/wmxscott/upkeep
# or
pipx install git+https://github.com/wmxscott/upkeep
```

Needs Python 3.11 or later, on macOS or Linux. Both `up` and `upkeep` are installed. Homebrew core has an unrelated `up`, so the formula conflicts with it; every command here works as `upkeep` too.

## Quick start

```sh
up init                    # write ~/.config/upkeep/config.toml with the presets it finds
up brew mise               # run two tools
up                         # pick from a list, most used first
up --all                   # run every tool, in parallel
up schedule install        # an hourly job that runs the auto tools when they're due
```

Set `auto = true` on the tools you trust to run unattended, then check the setup with `up doctor`.

## Configuration

`~/.config/upkeep/config.toml`, or `$XDG_CONFIG_HOME/upkeep/config.toml`. Set `UPKEEP_CONFIG` to use another file.

```toml
[schedule]
at = "08:00"            # daily and weekly tools become due at or after this local time
shell = "zsh -lic"      # how commands and the scheduled job start; default "$SHELL -lc"

[logs]
keep_days = 30

[notify]
on = "failure"          # desktop notification after a scheduled run: failure, always, never

[tools.brew]
preset = "brew"         # a built-in recipe; any key below overrides it
auto = true

[tools.claude]
run = "claude update"
version = "claude --version"
auto = true

[tools.codex]
run = "brew upgrade codex"
lock = "brew"           # never at the same time as the brew tool
auto = true

[tools.dotfiles]
run = "cd ~/dotfiles && git pull"   # an SSH remote: better kept manual
```

| Tool key | Default | |
|---|---|---|
| `run` | | The command, run through `shell`. Required unless a preset gives one |
| `preset` | | A built-in recipe, below |
| `auto` | `false` | Run by `up --auto`, which is what the schedule runs |
| `lock` | | Tools with the same lock never run at once, in this or any other `up` process |
| `requires` | | An executable, or a list. When one isn't on `PATH`, the tool is skipped rather than failed |
| `version` | | A command that prints the version on its first line. Recorded before and after, so you see what changed |
| `check` | | What `up check` runs. It should print one line per outdated item and nothing otherwise |
| `interval` | `"daily"` | `"daily"`, `"weekly"`, `"<n>h"` or `"<n>d"`. Only `--auto` uses it |
| `timeout` | `"1h"` | `"90s"`, `"30m"`, `"2h"`, or `0` for none. The whole process group is killed |
| `env` | | Extra environment variables, as a table |

Commands run through `shell`, with stdin closed and in their own process group. The default is your `$SHELL -lc`, so they see your login `PATH`, when that shell speaks POSIX sh (sh, bash, zsh, dash, ksh); otherwise it's `/bin/sh -lc`. A tool named like a command (`status`, `log`, …) runs with `up run <name>`.

Unknown keys and bad values are errors in `up doctor`. Everywhere else they're warnings, and the default is used.

The older `up` script's config, `~/.config/up/up.toml`, still works: each `name = "command"` in its `[aliases]` table is a manual tool. upkeep reads that file when there's no new one, and says so. `up init` imports it.

### Presets

| Preset | Runs | Lock | Check |
|---|---|---|---|
| `brew` | `brew update && brew upgrade --formula` | `brew` | `brew outdated --formula` |
| `brew-cask` | `brew update && brew upgrade --cask` | `brew` | `brew outdated --cask` |
| `mise` | `mise upgrade` | `mise` | `mise outdated --no-header` |
| `npm` | `npm update -g` | `npm` | `npm outdated -g --parseable` |
| `uv` | `uv tool upgrade --all` | `uv` | |
| `pipx` | `pipx upgrade-all` | `pipx` | |
| `rustup` | `rustup update` | `rustup` | `rustup check`, updates only |
| `cargo` | `cargo install-update --all` ([cargo-update](https://github.com/nabijaczleweli/cargo-update)) | `cargo` | `cargo install-update --list`, updates only |
| `gh-extensions` | `gh extension upgrade --all` | | `gh extension upgrade --all --dry-run` |

Each preset also sets `requires`, and most set `version`. `brew` leaves casks alone on purpose: casks often need sudo, which an unattended run can't provide. Keep `brew-cask` manual.

## Commands

| Command | |
|---|---|
| `up` | Pick tools from a checkbox list, most used first. `space` toggles, `enter` runs, `q` quits |
| `up TOOL...` | Run those tools |
| `up -a`, `--all` | Run every tool |
| `up --auto [--force]` | Run the auto tools that are due. `--force` ignores when they last ran |
| `-v`, `--verbose` | Print every line of output as `[tool] line` |
| `-i`, `--interactive` | Run one tool attached to the terminal, so it can ask for a password. Its output isn't logged |
| `up status [--brief]` | Each tool's last result (ok, failed or skipped), when, how long, and any version change. `--brief` prints one line, for a prompt |
| `up log [TOOL] [--run N]` | Output of the last run, or the Nth-last. `--path` prints where the log is |
| `up check [TOOL...]` | Run each tool's `check` and sum up what's outdated. Upgrades nothing |
| `up list` | Tools, auto flag, lock, interval, and when each is next due |
| `up doctor` | Validate the config and explain why unattended runs fail. Exits 1 on config errors |
| `up init [--force] [--print]` | Write a starter config |
| `up schedule install\|remove\|status [--dry-run]` | Manage the hourly job |
| `up --version` | |

A run exits 1 if any tool failed, 2 on a usage or config error, and 130 when interrupted. In a terminal each tool gets one live status line, and only failures show output: their last 20 lines and the log's path. When piped, or with `-v`, every line is printed with its tool's prefix. `NO_COLOR` turns colour off.

## Scheduling

`up schedule install` sets up a job that runs `up --auto` every hour through `shell`, using the absolute path of the upkeep you installed it with.

- **macOS:** `~/Library/LaunchAgents/io.github.wmxscott.upkeep.plist`, loaded with `launchctl bootstrap gui/$UID`. `StartInterval` 3600 and `RunAtLoad`. Output goes to `~/.local/state/upkeep/schedule.log`.
- **Linux:** `~/.config/systemd/user/upkeep.service` and `upkeep.timer` (`OnCalendar=hourly`, `Persistent=true`), enabled with `systemctl --user enable --now`. Output goes to the journal: `journalctl --user -u upkeep`.

`--dry-run` prints the files and commands instead. `up schedule status` shows whether the job is loaded, the last tick, and when the next tool is due. `up schedule remove` undoes the install. upkeep only manages its own job. A LaunchAgent or cron entry you wrote for an older update script is left alone, so remove that yourself.

Each tick only asks what's due. A tool is due when it has no attempt, successful or failed, within its interval. Daily and weekly intervals are anchored to `schedule.at`: a daily tool runs once a day, on the first tick at or after 08:00. A laptop asleep at 08:00 catches up on its first tick after waking, and a failed tool is retried the next day rather than every hour. Hour intervals such as `"6h"` count from the last attempt. Manual runs count as attempts too. Ticks never overlap: a second one exits at once.

### The failure notice

After a scheduled run with failures, upkeep writes one line to `~/.local/state/upkeep/notice`:

```
upkeep: 2 updates failed (brew, cursor) — run: up status
```

`up status` deletes it, and so does the next scheduled run without failures. To see it when you open a shell, without starting Python, add this to your `.zshrc` or `.bashrc`:

```sh
[[ -s ${XDG_STATE_HOME:-$HOME/.local/state}/upkeep/notice ]] && cat "${XDG_STATE_HOME:-$HOME/.local/state}/upkeep/notice"
```

With `[notify] on = "failure"`, the default, you also get a desktop notification: `terminal-notifier` or `osascript` on macOS, `notify-send` on Linux, when available. Manual runs never notify.

## Troubleshooting unattended runs

Run `up doctor` first. It flags auto tools that look risky, and reads the log of each auto tool whose last run failed to say why.

- **SSH keys.** `git pull` over SSH needs a key the job can use: in an agent it can reach, with no passphrase prompt and no hardware-key touch. Otherwise it fails with `Permission denied (publickey)`. Use an HTTPS remote, or keep the tool manual.
- **sudo.** Nobody is there to type a password. Casks are the usual culprit: keep them in a manual `brew-cask` tool and run `up -i brew-cask` when you're at the keyboard.
- **Logins.** CLIs that update through an account (`[unauthenticated]`, `not logged in`) need logging in once, in a terminal.
- **PATH.** The job gets your shell's `PATH` only through `shell`. If tools are skipped as not found, or fail with `command not found`, set `shell` to match your terminal, such as `"zsh -lic"` when your `PATH` is set in `.zshrc`.
- **Two tools, one package manager.** `Another brew update process is already running` means two tools ran `brew` at once. Give them the same `lock`; `up doctor` spots this.
- **Network.** Download failures are usually transient. The tool is retried at its next slot.

## Files

| Path | |
|---|---|
| `~/.config/upkeep/config.toml` | Config. Follows `$XDG_CONFIG_HOME`; `$UPKEEP_CONFIG` overrides it |
| `~/.local/state/upkeep/runs/<UTC time>/` | One directory per run: `run.json`, and `<tool>.log` with colour codes stripped. Kept for `keep_days`, except that each tool's latest run is always kept. Follows `$XDG_STATE_HOME` |
| `~/.local/state/upkeep/notice` | The failure notice |
| `~/.local/state/upkeep/usage.json` | How often you've picked each tool, for sorting the picker. Imported once from the older `up` script |
| `~/.local/state/upkeep/locks/` | Lock files |

For each tool, `run.json` records `status` (`ok`, `failed` or `skipped`), `exit_code`, `started`, `duration`, `version_before`, `version_after` and `reason`, alongside the run's `trigger` (`manual` or `auto`).

## Development

```sh
uv run pytest
uv run ruff check && uv run ruff format --check
```

## License

[MIT](LICENSE)
