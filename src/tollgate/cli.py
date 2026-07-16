from __future__ import annotations

import argparse

from .proxy import create_app, default_capture_path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tollgate",
        description="Observe-only capture proxy for LLM API traffic.",
    )
    parser.add_argument("--port", type=int, default=4141)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--out", default=None, help=f"capture file (default {default_capture_path()})"
    )
    args = parser.parse_args()

    import uvicorn

    print(f"Tollgate listening on http://{args.host}:{args.port}")
    print(f"Capturing to {args.out or default_capture_path()}")
    uvicorn.run(
        create_app(args.out), host=args.host, port=args.port, log_level="warning"
    )


if __name__ == "__main__":
    main()
