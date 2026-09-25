"""Command-line interface."""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from upkeep import __version__, due, history, notify, paths
from upkeep.config import Config, ConfigError, Tool, load, locate
from upkeep.history import History
from upkeep.locks import FileLock, is_held
from upkeep.runner import Interrupted, Runner, format_duration, shell_path
from upkeep.ui import LiveReporter, StreamReporter, Style, use_color

COMMANDS_HELP = """\
commands:
  status [--brief]         last result for each tool
  log [TOOL] [--run N]     output of the last (or Nth-last) run
  check [TOOL...]          what each tool has outdated; upgrades nothing
  list                     tools, auto flag, lock, interval, next due
  doctor                   check the config and why unattended runs fail
  init                     write a starter config
  schedule install|remove|status [--dry-run]
                           the hourly job that runs `up --auto`
  run TOOL...              run tools, even ones named like a command

config: {config}
"""
SCHEDULE_LOG_MAX = 1 << 20
SCHEDULE_LOG_KEEP = 256 << 10


class Ctx:
    def __init__(self, env: Mapping[str, str], prog: str, out=None, err=None):
        self.env = env
        self.prog = prog
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.state = paths.state(env)
        self.quiet = False

    def print(self, *args: object) -> None:
        print(*args, file=self.out)

    def warn(self, text: str) -> None:
        if not self.quiet:
            print(f"{self.prog}: {text}", file=self.err)

    def config(self) -> Config:
        cfg = load(self.env)
        if cfg.legacy:
            new = paths.pretty(paths.config_file(self.env), self.env)
            self.warn(
                f"reading the legacy {paths.pretty(cfg.path, self.env)}; "
                f"move it to {new}, or run `{self.prog} init` to import it"
            )
        for problem in cfg.problems:
            self.warn(f"warning: {problem}")
        for name in cfg.tools:
            if name in COMMANDS:
                self.warn(
                    f"warning: tool {name!r} shares a command's name; run it with "
                    f"`{self.prog} run {name}`"
                )
        return cfg

    @property
    def style(self) -> Style:
        return Style(use_color(self.out, self.env))


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise UsageError(f"{message}\n{self.format_usage().rstrip()}")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    prog = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "up"
    if prog not in ("up", "upkeep"):
        prog = "up"
    ctx = Ctx(env, prog)
    try:
        if argv and argv[0] in COMMANDS:
            return COMMANDS[argv[0]](ctx, argv[1:])
        return cmd_update(ctx, argv)
    except UsageError as e:
        print(f"{prog}: {e}", file=ctx.err)
        return 2
    except ConfigError as e:
        print(f"{prog}: {e}", file=ctx.err)
        return 2
    except (KeyboardInterrupt, Interrupted):
        print(file=ctx.err)
        return 130


# running tools


def update_parser(prog: str, env: Mapping[str, str], sub: str | None = None) -> Parser:
    if sub:
        p = Parser(prog=f"{prog} {sub}", description="Run the named tools.")
        p.add_argument("tools", nargs="+", metavar="TOOL")
    else:
        p = Parser(
            prog=prog,
            usage=f"{prog} [TOOL ...] [-a | --auto [--force]] [-v] [-i]\n"
            f"       {prog} COMMAND [options]",
            description="Keep your tools up to date: run the update commands in your config,\n"
            "in parallel. With no arguments, pick the ones to run from a list.",
            epilog=COMMANDS_HELP.format(config=paths.pretty(paths.config_file(env), env)),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("tools", nargs="*", metavar="TOOL", help="tools to run")
        p.add_argument("-a", "--all", action="store_true", help="run every tool")
        p.add_argument(
            "--auto",
            action="store_true",
            help="run the auto tools that are due (what the schedule runs)",
        )
        p.add_argument(
            "--force", action="store_true", help="with --auto: ignore when they last ran"
        )
        p.add_argument("-V", "--version", action="version", version=f"upkeep {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="stream every line of output")
    p.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="run one tool attached to the terminal, for password prompts; output isn't logged",
    )
    return p


def cmd_run(ctx: Ctx, argv: list[str]) -> int:
    args = update_parser(ctx.prog, ctx.env, "run").parse_args(argv)
    args.all = args.auto = args.force = False
    return _update(ctx, args)


def cmd_update(ctx: Ctx, argv: list[str]) -> int:
    return _update(ctx, update_parser(ctx.prog, ctx.env).parse_args(argv))


def _update(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.all and args.auto:
        raise UsageError("--all and --auto don't mix")
    if args.tools and (args.all or args.auto):
        raise UsageError("name tools, or use --all or --auto, not both")
    if args.force and not args.auto:
        raise UsageError("--force only applies to --auto")
    if args.interactive and (args.auto or args.all or len(args.tools) != 1):
        raise UsageError("--interactive runs exactly one named tool")
    cfg = ctx.config()
    if not cfg.tools:
        raise ConfigError(f"no tools in {paths.pretty(cfg.path or '', ctx.env)}")

    if args.auto:
        return _auto(ctx, cfg, args)
    if args.all:
        tools = list(cfg.tools.values())
    elif args.tools:
        unknown = [n for n in args.tools if n not in cfg.tools]
        if unknown:
            raise UsageError(f"unknown tool {unknown[0]!r}; configured: {', '.join(cfg.tools)}")
        tools = [cfg.tools[n] for n in dict.fromkeys(args.tools)]
    else:
        if not (sys.stdin.isatty() and ctx.out.isatty()):
            raise UsageError("name the tools to run, or use --all")
        from upkeep import picker

        usage = history.load_usage(ctx.state, ctx.env)
        chosen = picker.pick(list(cfg.tools), usage)
        if not chosen:
            ctx.print("Nothing selected.")
            return 0
        tools = [cfg.tools[n] for n in chosen]
    if not args.all:
        history.count_usage(ctx.state, [t.name for t in tools], ctx.env)
    return _execute(ctx, cfg, tools, "manual", args)


def _auto(ctx: Ctx, cfg: Config, args: argparse.Namespace) -> int:
    tick = FileLock(ctx.state.locks, "auto")
    if not tick.try_acquire():
        if ctx.out.isatty():
            ctx.print("another `up --auto` is running")
        return 0
    try:
        ctx.state.root.mkdir(parents=True, exist_ok=True)
        ctx.state.last_tick.write_text(history.iso(history.utcnow()) + "\n")
        _trim(ctx.state.schedule_log)
        auto = [t for t in cfg.tools.values() if t.auto]
        tools = auto if args.force else due_tools(cfg, auto, History(ctx.state))
        if not tools:
            if ctx.out.isatty() or args.verbose:
                ctx.print("nothing is due" if auto else "no tools have auto = true")
            return 0
        return _execute(ctx, cfg, tools, "auto", args)
    finally:
        tick.release()


def due_tools(
    cfg: Config, tools: Sequence[Tool], hist: History, now: datetime | None = None
) -> list[Tool]:
    now = now or datetime.now()
    latest = hist.latest({t.name for t in tools})
    return [t for t in tools if due.is_due(t.interval, _last(latest, t.name), now, cfg.at)]


def _last(latest: Mapping[str, tuple], name: str) -> datetime | None:
    if name not in latest:
        return None
    started = latest[name][1].started_at or latest[name][0].started_at
    return started.astimezone().replace(tzinfo=None)


def _trim(log: Path) -> None:
    """Keep the schedule's stdout log from growing without bound."""
    with contextlib.suppress(OSError):
        if log.stat().st_size > SCHEDULE_LOG_MAX:
            with log.open("rb") as f:
                f.seek(-SCHEDULE_LOG_KEEP, os.SEEK_END)
                tail = f.read()
            log.write_bytes(tail[tail.find(b"\n") + 1 :])


def _execute(
    ctx: Ctx, cfg: Config, tools: list[Tool], trigger: str, args: argparse.Namespace
) -> int:
    tty = ctx.out.isatty()
    color = use_color(ctx.out, ctx.env)
    if args.verbose or args.interactive or not tty:
        reporter = StreamReporter(ctx.out, color=color, header=not tty)
    else:
        reporter = LiveReporter(ctx.out, color=color)
    runner = Runner(
        cfg, ctx.state, reporter, trigger=trigger, env=ctx.env, interactive=args.interactive
    )
    with _sigterm_raises():
        run = runner.execute(tools)
    hist = History(ctx.state)
    with contextlib.suppress(OSError):
        hist.prune(cfg.keep_days)
    auto_names = {n for n, t in cfg.tools.items() if t.auto}
    history.update_notice(ctx.state, trigger, run.tools, auto_names)
    failed = [n for n, r in run.tools.items() if r.status == "failed"]
    if trigger == "auto":
        _notify(cfg, run, failed)
    return 1 if failed else 0


@contextlib.contextmanager
def _sigterm_raises():
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        raise Interrupted()

    old = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _notify(cfg: Config, run: history.Run, failed: list[str]) -> None:
    if cfg.notify == "never":
        return
    if failed:
        notify.send("upkeep", history.notice_text(failed))
    elif cfg.notify == "always":
        ok = [n for n, r in run.tools.items() if r.status == "ok"]
        changed = [n for n, r in run.tools.items() if r.version_change]
        text = f"{len(ok)} updated" + (f"; new versions: {', '.join(changed)}" if changed else "")
        notify.send("upkeep", text)


# reporting


def ago(moment: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(moment.tzinfo)
    secs = (now - moment).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400 * 2:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def when(moment: datetime, now: datetime) -> str:
    """A local time relative to `now` (both naive local)."""
    if moment <= now:
        return "now"
    if moment.date() == now.date():
        return f"today {moment:%H:%M}"
    if moment.date() == now.date() + timedelta(days=1):
        return f"tomorrow {moment:%H:%M}"
    if moment - now < timedelta(days=6):
        return f"{moment:%a %H:%M}"
    return f"{moment:%Y-%m-%d %H:%M}"


def _table(
    rows: list[list[str]],
    style: Style | None = None,
    paint: Callable[[int, str], str] | None = None,
) -> list[str]:
    """Aligned columns; `paint(column, text)` colours body cells."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for n, row in enumerate(rows):
        cells = []
        for i, (text, width) in enumerate(zip(row, widths, strict=True)):
            shown = paint(i, text) if paint and n else text
            cells.append(shown + " " * (width - len(text)))
        line = "  ".join(cells).rstrip()
        out.append(style.bold(line) if style and n == 0 else line)
    return out


def _effective(state: paths.State, name: str, rec: history.ToolRecord) -> str:
    if rec.status in ("running", "pending") and not is_held(state.locks, f"tool-{name}"):
        return "interrupted"
    return rec.status


def cmd_status(ctx: Ctx, argv: list[str]) -> int:
    p = Parser(prog=f"{ctx.prog} status", description="Show the last result for each tool.")
    p.add_argument("--brief", action="store_true", help="one line, for shell prompts")
    args = p.parse_args(argv)
    ctx.quiet = args.brief
    cfg = ctx.config()
    latest = History(ctx.state).latest(set(cfg.tools))
    statuses = {n: _effective(ctx.state, n, latest[n][1]) for n in cfg.tools if n in latest}
    failed = [n for n in cfg.tools if statuses.get(n) in ("failed", "interrupted")]

    if args.brief:
        if failed:
            ctx.print(f"upkeep: {len(failed)} failed ({', '.join(failed)})")
        elif latest:
            newest = max(r.started_at or run.started_at for run, r in latest.values())
            ctx.print(f"upkeep: ok, last run {ago(newest)}")
        else:
            ctx.print("upkeep: no runs yet")
        return 0

    s = ctx.style
    rows = [["TOOL", "STATUS", "WHEN", "TOOK", "VERSION", "DETAIL"]]
    for name in cfg.tools:
        if name not in latest:
            rows.append([name, "never", "", "", "", ""])
            continue
        run, rec = latest[name]
        started = rec.started_at or run.started_at
        took = format_duration(rec.duration) if rec.duration is not None else ""
        detail = "" if rec.status == "ok" else rec.reason or ""
        trig = " (auto)" if run.trigger == "auto" else ""
        rows.append([name, statuses[name], ago(started) + trig, took, rec.version, detail])
    colors = {"ok": s.ok, "failed": s.bad, "interrupted": s.bad, "skipped": s.warn}

    def paint(col: int, text: str) -> str:
        return colors[text](text) if col == 1 and text in colors else text

    for line in _table(rows, s, paint):
        ctx.print(line)
    tick = _read_tick(ctx.state)
    if tick:
        ctx.print(s.dim(f"last scheduled tick: {ago(tick)}"))
    if failed:
        ctx.print(s.dim(f"see why: {ctx.prog} log {failed[0]}  ·  {ctx.prog} doctor"))
    history.clear_notice(ctx.state)
    return 0


def _read_tick(state: paths.State) -> datetime | None:
    try:
        return history.parse_iso(state.last_tick.read_text().strip())
    except (OSError, ValueError):
        return None


def cmd_log(ctx: Ctx, argv: list[str]) -> int:
    p = Parser(prog=f"{ctx.prog} log", description="Print the output of a past run.")
    p.add_argument("tool", nargs="?", help="only this tool's output")
    p.add_argument("--run", type=int, default=1, metavar="N", help="the Nth-last run (default 1)")
    p.add_argument("--path", action="store_true", help="print the log's path instead")
    args = p.parse_args(argv)
    if args.run < 1:
        raise UsageError("--run counts from 1, the last run")
    runs = History(ctx.state).runs()
    if args.tool:
        runs = (
            r
            for r in runs
            if r.tools.get(args.tool, None) and r.tools[args.tool].status != "pending"
        )
    run = next((r for i, r in enumerate(runs, 1) if i == args.run), None)
    if run is None:
        what = f" of {args.tool}" if args.tool else ""
        print(f"{ctx.prog}: no run{what} found", file=ctx.err)
        return 1
    started = run.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
    names = [args.tool] if args.tool else list(run.tools)
    if args.path:
        ctx.print(run.log_path(args.tool) if args.tool else run.dir)
        return 0
    s = ctx.style
    ctx.print(s.dim(f"run {run.id}: {run.trigger}, started {started}"))
    for name in names:
        rec = run.tools[name]
        head = f"── {name}: {rec.status}"
        if rec.status != "ok" and rec.reason:
            head += f" ({rec.reason})"
        ctx.print(s.bold(head))
        log = run.log_path(name)
        if log.exists():
            ctx.print(log.read_text(errors="replace").rstrip("\n"))
    return 0


def cmd_check(ctx: Ctx, argv: list[str]) -> int:
    p = Parser(
        prog=f"{ctx.prog} check", description="Ask each tool what is outdated. Nothing is upgraded."
    )
    p.add_argument("tools", nargs="*", metavar="TOOL")
    args = p.parse_args(argv)
    cfg = ctx.config()
    unknown = [n for n in args.tools if n not in cfg.tools]
    if unknown:
        raise UsageError(f"unknown tool {unknown[0]!r}")
    chosen = [cfg.tools[n] for n in args.tools] if args.tools else list(cfg.tools.values())
    with_check = [t for t in chosen if t.check]
    s = ctx.style
    if ctx.out.isatty() and with_check:
        ctx.out.write(s.dim(f"checking {', '.join(t.name for t in with_check)}…") + "\r")
        ctx.out.flush()
    from upkeep.check import run_checks

    results = {r.name: r for r in run_checks(cfg, with_check, ctx.state, ctx.env)}
    if ctx.out.isatty() and with_check:
        ctx.out.write("\x1b[2K")
    width = max(len(t.name) for t in chosen) if chosen else 0
    for tool in chosen:
        r = results.get(tool.name)
        if r is None:
            ctx.print(f"{tool.name:<{width}}  {s.dim('no check')}")
        elif r.status == "updates":
            ctx.print(f"{tool.name:<{width}}  {s.warn(f'{len(r.lines)} outdated')}")
            for line in r.lines[:20]:
                ctx.print(f"  {line}")
            if len(r.lines) > 20:
                ctx.print(s.dim(f"  … {len(r.lines) - 20} more"))
        elif r.status == "current":
            ctx.print(f"{tool.name:<{width}}  {s.ok('up to date')}")
        elif r.status == "skipped":
            ctx.print(f"{tool.name:<{width}}  {s.dim('skipped: ' + r.detail)}")
        else:
            ctx.print(f"{tool.name:<{width}}  {s.bad('check failed (' + r.detail + ')')}")
            for line in r.lines:
                ctx.print(f"  {line}")
    outdated = [r for r in results.values() if r.status == "updates"]
    if outdated:
        items = ", ".join(f"{r.name} ({len(r.lines)})" for r in outdated)
        ctx.print(f"\n{len(outdated)} with updates: {items}")
    return 1 if any(r.status == "failed" for r in results.values()) else 0


def cmd_list(ctx: Ctx, argv: list[str]) -> int:
    Parser(prog=f"{ctx.prog} list", description="List the configured tools.").parse_args(argv)
    cfg = ctx.config()
    latest = History(ctx.state).latest(set(cfg.tools))
    now = datetime.now()
    rows = [["TOOL", "AUTO", "LOCK", "INTERVAL", "NEXT DUE"]]
    for tool in cfg.tools.values():
        if tool.auto:
            nxt = due.next_due(tool.interval, _last(latest, tool.name), now, cfg.at)
            next_due = when(nxt, now)
        else:
            next_due = "manual"
        rows.append(
            [
                tool.name,
                "yes" if tool.auto else "-",
                tool.lock or "-",
                str(tool.interval),
                next_due,
            ]
        )
    for line in _table(rows, ctx.style):
        ctx.print(line)
    return 0


def cmd_doctor(ctx: Ctx, argv: list[str]) -> int:
    Parser(
        prog=f"{ctx.prog} doctor", description="Check the config, and why unattended runs fail."
    ).parse_args(argv)
    from upkeep import doctor, schedule

    s = ctx.style
    path, legacy = locate(ctx.env)
    try:
        ctx.quiet = True
        cfg = ctx.config()
    except ConfigError as e:
        ctx.print(s.bad(f"✗ {e}"))
        return 1
    shell = cfg.shell_words(ctx.env)
    try:
        installed: bool | None = schedule.backend(ctx.env, ctx.state).installed()
    except RuntimeError:
        installed = None
    ctx.print(f"config    {paths.pretty(path, ctx.env)}" + (" (legacy location)" if legacy else ""))
    ctx.print(f"shell     {' '.join(shell)}")
    ctx.print(f"state     {paths.pretty(ctx.state.root, ctx.env)}")
    ctx.print(f"schedule  {'installed' if installed else 'not installed'}")
    ctx.print()
    findings = doctor.diagnose(cfg, ctx.state, shell_path(shell, ctx.env), installed)
    if legacy:
        findings.insert(
            0,
            doctor.Finding(
                "warn",
                f"move the config to {paths.pretty(paths.config_file(ctx.env), ctx.env)}",
            ),
        )
    for f in findings:
        ctx.print(s.bad(f"✗ {f.text}") if f.level == "error" else s.warn(f"! {f.text}"))
    auto = sum(t.auto for t in cfg.tools.values())
    errors = sum(f.level == "error" for f in findings)
    tally = f"{len(cfg.tools)} tools, {auto} auto"
    if errors:
        ctx.print(s.bad(f"{errors} error{'s' if errors > 1 else ''}; {tally}"))
    else:
        ctx.print(
            s.ok(f"✓ config ok; {tally}") + (f", {len(findings)} warnings" if findings else "")
        )
    return 1 if errors else 0


def cmd_init(ctx: Ctx, argv: list[str]) -> int:
    p = Parser(prog=f"{ctx.prog} init", description="Write a starter config.")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.add_argument("--print", dest="print_only", action="store_true", help="print it instead")
    args = p.parse_args(argv)
    import shutil
    import tomllib

    from upkeep import starter

    target = paths.config_file(ctx.env)
    if target.exists() and not (args.force or args.print_only):
        print(
            f"{ctx.prog}: {paths.pretty(target, ctx.env)} exists; --force overwrites it",
            file=ctx.err,
        )
        return 1
    aliases: dict[str, str] = {}
    legacy = paths.legacy_config_file(ctx.env)
    if legacy.exists():
        with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
            data = tomllib.loads(legacy.read_text())
            aliases = {k: v for k, v in data.get("aliases", {}).items() if isinstance(v, str)}
    search = shell_path(Config().shell_words(ctx.env), ctx.env)
    text = starter.render(aliases, lambda exe: shutil.which(exe, path=search) is not None)
    if args.print_only:
        ctx.out.write(text)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    ctx.print(f"wrote {paths.pretty(target, ctx.env)}")
    if aliases:
        ctx.print(
            f"imported {len(aliases)} tools from {paths.pretty(legacy, ctx.env)}; "
            "you can delete that file now"
        )
    return 0


def cmd_schedule(ctx: Ctx, argv: list[str]) -> int:
    p = Parser(
        prog=f"{ctx.prog} schedule",
        description="Manage the hourly job that runs "
        "`up --auto`: a LaunchAgent on macOS, a systemd user timer on Linux.",
    )
    p.add_argument("action", choices=["install", "remove", "status"])
    p.add_argument("--dry-run", action="store_true", help="print the files and commands only")
    args = p.parse_args(argv)
    from upkeep import schedule

    try:
        backend = schedule.backend(ctx.env, ctx.state)
    except RuntimeError as e:
        print(f"{ctx.prog}: {e}", file=ctx.err)
        return 1
    if args.action == "status":
        return _schedule_status(ctx, backend)

    if args.action == "install":
        try:
            cfg = ctx.config()
        except ConfigError:
            cfg = Config()
        argv_job = schedule.job_argv(cfg, schedule.up_program(), ctx.env)
        plan = backend.install_plan(argv_job)
    else:
        if not backend.installed() and not args.dry_run:
            ctx.print("no job is installed")
            return 0
        plan = backend.remove_plan()
    if args.dry_run:
        ctx.print(plan.describe())
        return 0
    if args.action == "install":
        ctx.state.root.mkdir(parents=True, exist_ok=True)
    errors = backend.apply(plan)
    for e in errors:
        print(f"{ctx.prog}: {e}", file=ctx.err)
    if errors:
        return 1
    if args.action == "install":
        ctx.print(f"installed the hourly {backend.name} job: {' '.join(argv_job)}")
        if not any(t.auto for t in cfg.tools.values()):
            ctx.print("no tools have auto = true yet, so it will do nothing")
    else:
        ctx.print(f"removed the {backend.name} job")
    return 0


def _schedule_status(ctx: Ctx, backend) -> int:
    rows = backend.status()
    tick = _read_tick(ctx.state)
    rows.append(("last tick", ago(tick) if tick else "never"))
    with contextlib.suppress(ConfigError):
        ctx.quiet = True
        cfg = ctx.config()
        auto = [t for t in cfg.tools.values() if t.auto]
        if auto:
            latest = History(ctx.state).latest({t.name for t in auto})
            now = datetime.now()
            nxt = min(due.next_due(t.interval, _last(latest, t.name), now, cfg.at) for t in auto)
            rows.append(("next due", when(nxt, now)))
        else:
            rows.append(("next due", "no auto tools"))
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        ctx.print(f"{key:<{width}}  {value}")
    return 0 if backend.installed() else 1


COMMANDS: dict[str, Callable[[Ctx, list[str]], int]] = {
    "status": cmd_status,
    "log": cmd_log,
    "check": cmd_check,
    "list": cmd_list,
    "doctor": cmd_doctor,
    "init": cmd_init,
    "schedule": cmd_schedule,
    "run": cmd_run,
}
