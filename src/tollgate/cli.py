from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from . import shell
from .cache_audit import audit
from .pricing import PRICES, PRICES_AS_OF, rate_card
from .proxy import create_app, default_capture_path
from .record import iter_records, load_records
from .replay import compare, replay_live, reprice_iter, summarize

SUBCOMMANDS = (
    "run",
    "serve",
    "connect",
    "disconnect",
    "status",
    "report",
    "replay",
    "cache",
    "rates",
)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if value == 0:
            return "0"
        magnitude = abs(value)
        if magnitude >= 1:
            return f"{value:,.2f}"
        if magnitude >= 1e-4:
            return f"{value:.4f}"
        # Four decimals rounds a real cost to 0.0000, which reads as free —
        # exactly the wrong impression for a cheap model at high volume.
        return f"{value:.2e}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("no records")
        return
    columns = list(rows[0])
    cells = [[_fmt(r.get(c)) for c in columns] for r in rows]
    widths = [
        max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)
    ]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


def live_line(record: dict[str, Any]) -> str:
    """One line per call, for watching traffic arrive.

    The first question anyone has after starting a proxy is whether their app
    is actually going through it. Answering that shouldn't require running a
    second command.
    """
    when = str(record.get("timestamp", ""))[11:19]
    model = (record.get("model") or "?")[:26]
    usage = record.get("usage") or {}
    latency = record.get("latency_ms")
    cost = record.get("cost_usd")
    marks = ""
    if record.get("status", 200) != 200:
        marks += f"  [{record['status']}]"
    if record.get("truncated"):
        marks += "  [truncated]"
    return (
        f"{when}  {model:<26} "
        f"{usage.get('input_tokens', 0):>9,} in {usage.get('output_tokens', 0):>8,} out  "
        f"{'-' if latency is None else format(latency, ',.0f') + 'ms':>10}  "
        f"{'-' if cost is None else '$' + _fmt(cost):>11}{marks}"
    )


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    out = args.out or default_capture_path()
    base = f"http://{args.host}:{args.port}"

    # flush=True throughout: stdout is block-buffered when it isn't a terminal,
    # so under a process manager or a redirect this would never show up.
    print(f"Tollgate listening on {base}", flush=True)
    print(f"Capturing to {out}\n", flush=True)
    print("Point your app at it — no code change needed:\n", flush=True)
    print(f"  export ANTHROPIC_BASE_URL={base}", flush=True)
    print(f"  export OPENAI_BASE_URL={base}/v1\n", flush=True)
    print("Then run your app as usual, and `tollgate report` to see the cost.", flush=True)
    print("-" * 78, flush=True)

    def echo(record: dict[str, Any]) -> None:
        print(live_line(record), flush=True)

    uvicorn.run(
        create_app(out, on_record=None if args.quiet else echo),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


def serve_in_background(app, host: str, port: int):
    """Run the proxy on a thread and return the server once it is listening."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return server, thread
        time.sleep(0.05)
    raise RuntimeError(f"Tollgate could not start on {host}:{port}")


def cmd_run(args: argparse.Namespace) -> None:
    """Run one command with its AI traffic measured, and leave nothing behind.

    This is the safe way to use Tollgate. The proxy starts, the command runs
    with its base URLs pointed here, and both stop together. Nothing is written
    to a shell profile, so there is nothing to remember to undo — and no way to
    end up with a machine configured to talk to a proxy that isn't running.
    """
    command = list(args.command)
    # `tollgate run -- python app.py` is the documented form, and REMAINDER
    # hands the separator through; `tollgate run python app.py` works too.
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        print("give me a command to run, e.g. tollgate run -- python app.py", file=sys.stderr)
        raise SystemExit(2)
    args.command = command

    out = args.out or default_capture_path()
    base = f"http://127.0.0.1:{args.port}"
    before = _count_records(out)

    def echo(record: dict[str, Any]) -> None:
        print(live_line(record), flush=True)

    app = create_app(out, on_record=None if args.quiet else echo)
    server, thread = serve_in_background(app, "127.0.0.1", args.port)

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = base
    env["OPENAI_BASE_URL"] = f"{base}/v1"

    print(f"Measuring: {' '.join(args.command)}", flush=True)
    print("-" * 78, flush=True)
    try:
        completed = subprocess.run(args.command, env=env)
    except FileNotFoundError:
        print(f"\ncouldn't find a program called {args.command[0]!r}", file=sys.stderr)
        raise SystemExit(127) from None
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        app.state.capture_writer.flush()

    print("-" * 78, flush=True)
    new = _count_records(out) - before
    if new <= 0:
        print("No AI calls went through Tollgate.")
        print("If you expected some, the app may set its own base URL in code.")
    else:
        rows = summarize(iter_records(out))
        print(f"{new} call(s) this run. Totals for {out}:\n")
        print_table(rows)
    raise SystemExit(completed.returncode)


def _count_records(path: str) -> int:
    try:
        return sum(1 for _ in iter_records(path))
    except FileNotFoundError:
        return 0


def cmd_connect(args: argparse.Namespace) -> None:
    path = shell.profile_path()
    base = f"http://127.0.0.1:{args.port}"
    changed = shell.connect(path, base)
    print(f"{'Connected' if changed else 'Already connected'} — updated {path}")
    print("\nOpen a new terminal (or restart your app) for this to take effect.")
    print("From then on, this machine's AI traffic goes through Tollgate.\n")
    print("Two things to know:")
    print(f"  Tollgate must be running, or your app can't reach the AI service.")
    print(f"  Start it with:  tollgate")
    print(f"  Undo all of this with:  tollgate disconnect")


def cmd_disconnect(args: argparse.Namespace) -> None:
    path = shell.profile_path()
    changed = shell.disconnect(path)
    if changed:
        print(f"Disconnected — removed the Tollgate settings from {path}")
        print("\nOpen a new terminal (or restart your app) for this to take effect.")
        print("Your app now talks to the AI service directly again.")
    else:
        print(f"Nothing to disconnect — no Tollgate settings found in {path}")
    # Whatever the file says, tell them how to clear the current terminal too.
    if os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OPENAI_BASE_URL"):
        print("\nThis terminal still has the old settings. Close it, or run:")
        print("  unset ANTHROPIC_BASE_URL OPENAI_BASE_URL")


def cmd_status(args: argparse.Namespace) -> None:
    path = shell.profile_path()
    connected = shell.is_connected(path)
    base = f"http://127.0.0.1:{args.port}"

    running = False
    try:
        with socket.create_connection(("127.0.0.1", args.port), timeout=0.4):
            running = True
    except OSError:
        pass

    print(f"Tollgate running on {base}   {'yes' if running else 'no'}")
    print(f"Set up to capture traffic       {'yes' if connected else 'no'}  ({path})")
    env_set = bool(os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OPENAI_BASE_URL"))
    print(f"Active in this terminal         {'yes' if env_set else 'no'}")

    out = args.out or default_capture_path()
    print(f"Capture file                    {out} ({_count_records(out)} call(s))")

    # The one combination that silently breaks someone's app.
    if (connected or env_set) and not running:
        print("\nWarning: traffic is set to go through Tollgate, but Tollgate")
        print("isn't running — apps will fail to reach the AI service.")
        print("Start it with `tollgate`, or undo with `tollgate disconnect`.")


def cmd_report(args: argparse.Namespace) -> None:
    # Streamed, so a log too large to hold in memory still reports.
    rows = summarize(iter_records(args.logs), group_by=args.group_by)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print_table(rows)


def _system_override(path: str | None):
    """Replace the system prompt on every replayed request.

    The common prompt change is an edited system prompt, and the question that
    follows it is always the same: what did that do to cost and latency?
    """
    if not path:
        return None
    with open(os.path.expanduser(path)) as f:
        replacement = f.read()

    def transform(request: dict) -> dict:
        updated = dict(request)
        if "instructions" in updated:  # OpenAI Responses API
            updated["instructions"] = replacement
        else:
            updated["system"] = replacement
        return updated

    return transform


def cmd_replay(args: argparse.Namespace) -> None:
    if not args.live:
        # Offline: deterministic recompute, no network. summarize() reprices.
        rows = summarize(iter_records(args.logs), group_by=args.group_by)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            # Counted from the rows rather than the log, so nothing has to be
            # held in memory just to print a number.
            print(f"repriced {sum(r['calls'] for r in rows)} record(s) from {args.logs}")
            print_table(rows)
        return

    result = compare(
        replay_live(
            load_records(args.logs),
            model=args.model,
            transform=_system_override(args.system_file),
            limit=args.limit,
        )
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print_table(result["rows"])
    print()
    print_table([result["totals"]])


def cmd_cache(args: argparse.Namespace) -> None:
    findings = audit(list(reprice_iter(iter_records(args.logs), keep_bodies=False)))
    if args.json:
        print(json.dumps(findings, indent=2))
        return
    if not findings:
        print("no caching problems found")
        return
    recoverable = sum(f["recoverable_usd"] or 0 for f in findings)
    for finding in findings:
        amount = finding["recoverable_usd"]
        headline = f"[{finding['finding']}] {finding['model']}"
        if amount:
            headline += f" — up to ${amount:,.4f} recoverable"
        print(headline)
        print(f"    {finding['detail']}")
        if finding["prompt_sha"]:
            print(f"    prompt {finding['prompt_sha']}")
        print()
    print(f"total potentially recoverable: ${recoverable:,.4f}")


def cmd_rates(args: argparse.Namespace) -> None:
    models = [args.model] if args.model else sorted(PRICES)
    cards = [c for c in (rate_card(m, args.at) for m in models) if c is not None]
    if not cards:
        print(f"no price known for {args.model}")
        return
    if args.json:
        print(json.dumps(cards, indent=2))
        return
    print(f"per million tokens, table verified {PRICES_AS_OF}\n")
    print_table(cards)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tollgate",
        description="Capture proxy for LLM API traffic, with cost attribution and replay.",
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help="run a command with its AI traffic measured (nothing to undo afterwards)",
    )
    run.add_argument("--port", type=int, default=4141)
    run.add_argument("--out", default=None)
    run.add_argument("--quiet", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    connect = sub.add_parser(
        "connect", help="send this machine's AI traffic through Tollgate from now on"
    )
    connect.add_argument("--port", type=int, default=4141)
    connect.set_defaults(func=cmd_connect)

    disconnect = sub.add_parser("disconnect", help="undo `connect` and stop capturing")
    disconnect.set_defaults(func=cmd_disconnect)

    status = sub.add_parser("status", help="is it running, is it connected, what has it seen")
    status.add_argument("--port", type=int, default=4141)
    status.add_argument("--out", default=None)
    status.set_defaults(func=cmd_status)

    serve = sub.add_parser("serve", help="run the capture proxy (default)")
    serve.add_argument("--port", type=int, default=4141)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument(
        "--out", default=None, help=f"capture file (default {default_capture_path()})"
    )
    serve.add_argument(
        "--quiet", action="store_true", help="don't print a line per proxied request"
    )
    serve.set_defaults(func=cmd_serve)

    report = sub.add_parser("report", help="cost and latency summary of a capture log")
    report.add_argument("--logs", default=default_capture_path())
    report.add_argument("--group-by", default="model", choices=["model", "endpoint", "provider", "prompt_sha"])
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    replay = sub.add_parser("replay", help="recompute offline, or re-issue against the provider")
    replay.add_argument("--logs", default=default_capture_path())
    replay.add_argument(
        "--live", action="store_true", help="re-issue the logged requests (costs money)"
    )
    replay.add_argument("--model", default=None, help="override the model on replay")
    replay.add_argument(
        "--system-file",
        default=None,
        help="replace the system prompt on every replayed request with this file",
    )
    replay.add_argument("--limit", type=int, default=None, help="replay only the first N")
    replay.add_argument("--group-by", default="model", choices=["model", "endpoint", "provider", "prompt_sha"])
    replay.add_argument("--json", action="store_true")
    replay.set_defaults(func=cmd_replay)

    cache = sub.add_parser("cache", help="find prompt caching that isn't working")
    cache.add_argument("--logs", default=default_capture_path())
    cache.add_argument("--json", action="store_true")
    cache.set_defaults(func=cmd_cache)

    rates = sub.add_parser("rates", help="show the effective rate card for a date")
    rates.add_argument("--model", default=None)
    rates.add_argument(
        "--at",
        default=None,
        help="ISO date to price at (default today) — rates are effective-dated",
    )
    rates.add_argument("--json", action="store_true")
    rates.set_defaults(func=cmd_rates)

    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `tollgate --port 4141` keeps working as a bare serve invocation.
    if not argv or (argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as exc:
        # Reporting before anything has been captured is the most likely first
        # command anyone runs, and a traceback is a poor answer to it.
        print(f"no capture log at {exc.filename}", file=sys.stderr)
        print(
            "run `tollgate serve`, point your app at it, then try again",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
