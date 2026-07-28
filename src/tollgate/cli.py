from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .cache_audit import audit
from .pricing import PRICES, PRICES_AS_OF, rate_card
from .proxy import create_app, default_capture_path
from .record import iter_records, load_records
from .replay import compare, replay_live, reprice_iter, summarize

SUBCOMMANDS = ("serve", "report", "replay", "cache", "rates")


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


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    print(f"Tollgate listening on http://{args.host}:{args.port}")
    print(f"Capturing to {args.out or default_capture_path()}")
    uvicorn.run(create_app(args.out), host=args.host, port=args.port, log_level="warning")


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

    serve = sub.add_parser("serve", help="run the capture proxy (default)")
    serve.add_argument("--port", type=int, default=4141)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument(
        "--out", default=None, help=f"capture file (default {default_capture_path()})"
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
