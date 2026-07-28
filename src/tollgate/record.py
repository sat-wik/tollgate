"""The capture record: one JSON object per line, one line per LLM call.

A record holds the raw request/response pair plus everything derived from it —
normalized usage, cost, latency. The raw pair is what makes the log replayable;
the derived fields are recomputed from it on every `tollgate report`, so a log
written by an older Tollgate re-prices correctly under a newer price table.

Two fingerprints make change analysis possible:

    prompt_sha   sha256 over (system, messages, input, tools) only — stable
                 across model swaps, so it groups "the same prompt on a
                 different model"
    request_sha  sha256 over the whole request — changes when anything does

Both are computed over canonical JSON (sorted keys, no incidental whitespace),
so the same logical request always hashes the same way.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

from datetime import datetime, timezone
from typing import Any

from .pricing import cost_usd, normalize_usage, response_model

PROVIDERS = {
    "/v1/chat/completions": "openai",
    "/v1/responses": "openai",
    "/v1/messages": "anthropic",
}

#: Fields that define the prompt itself, as opposed to how it is run.
_PROMPT_FIELDS = ("system", "messages", "input", "instructions", "tools")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()[:16]


def prompt_sha(request: dict[str, Any]) -> str:
    return _sha({k: request[k] for k in _PROMPT_FIELDS if k in request})


def request_sha(request: dict[str, Any]) -> str:
    return _sha(request)


def build_record(
    endpoint: str,
    request: dict[str, Any],
    response: dict[str, Any] | None,
    *,
    status: int = 200,
    stream: bool = False,
    latency_ms: float | None = None,
    ttft_ms: float | None = None,
) -> dict[str, Any]:
    model = response_model(request, response)
    usage = normalize_usage(response)
    # A rejected request is not billed, and pricing it as `None` would poison
    # the total of every group it lands in. Note this under-counts a stream
    # that failed partway: tokens already emitted do bill.
    cost = 0.0 if status != 200 else cost_usd(model, usage)
    return {
        "id": uuid.uuid4().hex[:16],
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "endpoint": endpoint,
        "provider": PROVIDERS.get(endpoint, "unknown"),
        "model": model,
        "stream": stream,
        "status": status,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
        "usage": usage,
        "cost_usd": cost,
        "prompt_sha": prompt_sha(request),
        "request_sha": request_sha(request),
        "request": request,
        "response": response,
    }


def log_record(path: str, record: dict[str, Any]) -> None:
    """Append one record as a single line, safely against other writers.

    Two Tollgate processes can share a capture file, and records are large —
    whole request and response bodies. On a local POSIX filesystem O_APPEND
    already makes each append atomic (measured: 8 processes writing 2MB lines
    interleave cleanly without this lock), so the flock is insurance for the
    cases where that guarantee doesn't hold — NFS being the usual one, and
    `~/.tollgate/captured.jsonl` on an NFS home directory is not exotic. It
    costs one syscall pair per write. Platforms without flock proceed
    unlocked.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            # Flush inside the lock — write() only fills the buffer, so
            # unlocking first would let the actual syscall land unprotected.
            f.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_records(path: str) -> list[dict[str, Any]]:
    """Read a capture log, skipping lines that aren't parseable JSON objects.

    A log is appended to by a live proxy, so the last line can be a partial
    write; that shouldn't break analysis of everything before it.
    """
    records: list[dict[str, Any]] = []
    with open(os.path.expanduser(path)) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records
