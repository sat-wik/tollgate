from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .proxy import create_app, default_capture_path
from .record import load_records
from .replay import compare, replay_live, summarize

SUBCOMMANDS = ("serve", "report", "replay")


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 1 else f"{value:,.2f}"
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
    rows = summarize(load_records(args.logs), group_by=args.group_by)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print_table(rows)


def cmd_replay(args: argparse.Namespace) -> None:
    records = load_records(args.logs)
    if not args.live:
        # Offline: deterministic recompute, no network. summarize() reprices.
        rows = summarize(records, group_by=args.group_by)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(f"repriced {len(records)} record(s) from {args.logs}")
            print_table(rows)
        return

    result = compare(replay_live(records, model=args.model, limit=args.limit))
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print_table(result["rows"])
    print()
    print_table([result["totals"]])


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
    replay.add_argument("--limit", type=int, default=None, help="replay only the first N")
    replay.add_argument("--group-by", default="model", choices=["model", "endpoint", "provider", "prompt_sha"])
    replay.add_argument("--json", action="store_true")
    replay.set_defaults(func=cmd_replay)

    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `tollgate --port 4141` keeps working as a bare serve invocation.
    if not argv or (argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
