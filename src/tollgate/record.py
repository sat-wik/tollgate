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
import queue
import threading
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from .pricing import cost_usd, normalize_usage, response_model, service_tier

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
    truncated: bool = False,
) -> dict[str, Any]:
    model = response_model(request, response)
    usage = normalize_usage(response)
    tier = service_tier(request, response)
    timestamp = datetime.now(timezone.utc)
    # A rejected request is not billed, and pricing it as `None` would poison
    # the total of every group it lands in. Note this under-counts a stream
    # that failed partway: tokens already emitted do bill.
    cost = (
        0.0
        if status != 200
        else cost_usd(model, usage, at=timestamp, tier=tier)
    )
    return {
        "id": uuid.uuid4().hex[:16],
        "timestamp": timestamp.isoformat(timespec="milliseconds"),
        "endpoint": endpoint,
        "provider": PROVIDERS.get(endpoint, "unknown"),
        "model": model,
        "service_tier": tier,
        "stream": stream,
        "status": status,
        # The client hung up mid-stream: the usage below is a lower bound and
        # the latency is time-to-disconnect, not time-to-completion.
        "truncated": truncated,
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


class CaptureWriter:
    """A background writer so observation never sits on the request path.

    Capture is a tee, not a step: the caller's response must not wait on a
    disk. Writing inline — even off the event loop in a worker thread — still
    makes every response wait for the file lock, which a second Tollgate
    process can hold for as long as it likes. Records are handed to a bounded
    queue and drained by one thread instead.

    A single writer also means the exclusive lock is uncontended within a
    process, so it only ever costs anything when another process really is
    writing to the same file.

    If the queue fills, records are dropped rather than blocking the proxy.
    Under backpressure severe enough to outrun a sequential file append, losing
    observations is the right thing to lose.
    """

    def __init__(self, path: str, maxsize: int = 10_000) -> None:
        self.path = path
        self.dropped = 0
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize)
        self._thread: threading.Thread | None = None
        self._on_error: Callable[[BaseException], None] | None = None

    def start(self, on_error: Callable[[BaseException], None] | None = None) -> None:
        if self._thread is not None:
            return
        self._on_error = on_error
        self._thread = threading.Thread(target=self._drain, daemon=True, name="tollgate-capture")
        self._thread.start()

    def submit(self, record: dict[str, Any]) -> bool:
        """Queue a record. Returns False if it was dropped."""
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until everything queued so far has reached the file.

        Writing is asynchronous, so a caller that wants to read the log right
        after making a request needs a sync point. Returns False on timeout.
        """
        with self._queue.all_tasks_done:
            deadline = time.monotonic() + timeout
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Drain what is queued, then stop. Called on shutdown."""
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout)
        self._thread = None

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            try:
                if record is None:
                    return
                log_record(self.path, record)
            except Exception as exc:  # noqa: BLE001 - a writer must not die
                if self._on_error is not None:
                    self._on_error(exc)
            finally:
                self._queue.task_done()


def iter_records(path: str) -> Iterator[dict[str, Any]]:
    """Stream a capture log one record at a time.

    Records hold whole request and response bodies, so a busy service writes
    gigabytes a day. Reading the file into a list would put all of that in
    memory at once; analysis only ever needs one record in hand.

    Lines that aren't parseable JSON objects are skipped — a log is appended to
    by a live proxy, so the last line can be a partial write, and that shouldn't
    break analysis of everything before it.
    """
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
                yield obj


def load_records(path: str) -> list[dict[str, Any]]:
    """Read a whole capture log into memory. Prefer `iter_records` for analysis."""
    return list(iter_records(path))
