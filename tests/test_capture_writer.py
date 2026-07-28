"""The background capture writer.

Capture is a tee, not a step. These pin the properties that follow from that:
the caller is never blocked, a failing writer never dies, and backpressure
costs observations rather than requests.
"""

import json
import threading
import time

import pytest

from tollgate.record import CaptureWriter, build_record


def _record(i=0):
    return build_record(
        "/v1/messages",
        {"model": "claude-opus-5", "messages": [{"role": "user", "content": f"hi {i}"}]},
        {"model": "claude-opus-5", "usage": {"input_tokens": 10, "output_tokens": 5}},
    )


@pytest.fixture
def writer(tmp_path):
    w = CaptureWriter(str(tmp_path / "captured.jsonl"))
    w.start()
    yield w
    w.stop()


def test_records_reach_the_file_after_a_flush(writer):
    for i in range(20):
        assert writer.submit(_record(i)) is True
    assert writer.flush() is True

    lines = [ln for ln in open(writer.path).read().splitlines() if ln]
    assert len(lines) == 20
    for line in lines:
        json.loads(line)


def test_submit_does_not_block_on_a_slow_disk(tmp_path):
    """The whole point: a stalled write must not stall the caller."""
    released = threading.Event()

    class _Slow(CaptureWriter):
        def _drain(self):
            released.wait(5)
            super()._drain()

    slow = _Slow(str(tmp_path / "captured.jsonl"))
    slow.start()
    try:
        started = time.perf_counter()
        for i in range(500):
            slow.submit(_record(i))
        elapsed = time.perf_counter() - started
        # Nothing has been written yet — the drain thread is parked.
        assert elapsed < 0.5, f"submit blocked for {elapsed:.2f}s"
    finally:
        released.set()
        slow.stop()


def test_a_full_queue_drops_records_rather_than_blocking(tmp_path):
    blocked = threading.Event()

    class _Stuck(CaptureWriter):
        def _drain(self):
            blocked.wait(5)
            super()._drain()

    stuck = _Stuck(str(tmp_path / "captured.jsonl"), maxsize=10)
    stuck.start()
    try:
        accepted = sum(1 for i in range(50) if stuck.submit(_record(i)))
        assert accepted == 10
        assert stuck.dropped == 40
    finally:
        blocked.set()
        stuck.stop()


def test_a_failing_write_does_not_kill_the_writer(tmp_path):
    """One bad record must not stop every record after it."""
    seen = []
    w = CaptureWriter(str(tmp_path / "captured.jsonl"))
    w.start(on_error=seen.append)
    try:
        unserializable = _record()
        unserializable["request"] = {"bad": object()}
        w.submit(unserializable)
        w.submit(_record(1))
        w.flush()

        assert len(seen) == 1
        assert isinstance(seen[0], TypeError)
        # The good record still landed.
        lines = [ln for ln in open(w.path).read().splitlines() if ln]
        assert len(lines) == 1
        assert json.loads(lines[0])["model"] == "claude-opus-5"
    finally:
        w.stop()


def test_stop_drains_what_is_still_queued(tmp_path):
    w = CaptureWriter(str(tmp_path / "captured.jsonl"))
    w.start()
    for i in range(50):
        w.submit(_record(i))
    w.stop()  # shutdown must not lose queued records
    assert len([ln for ln in open(w.path).read().splitlines() if ln]) == 50


def test_stop_is_idempotent(writer):
    writer.submit(_record())
    writer.stop()
    writer.stop()


def test_flush_reports_timeout_rather_than_hanging(tmp_path):
    blocked = threading.Event()

    class _Stuck(CaptureWriter):
        def _drain(self):
            blocked.wait(5)
            super()._drain()

    stuck = _Stuck(str(tmp_path / "captured.jsonl"))
    stuck.start()
    try:
        stuck.submit(_record())
        assert stuck.flush(timeout=0.2) is False
    finally:
        blocked.set()
        stuck.stop()


def test_concurrent_submitters_produce_intact_lines(tmp_path):
    w = CaptureWriter(str(tmp_path / "captured.jsonl"))
    w.start()
    try:

        def submit_many():
            for i in range(100):
                w.submit(_record(i))

        threads = [threading.Thread(target=submit_many) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w.flush()

        lines = [ln for ln in open(w.path).read().splitlines() if ln]
        assert len(lines) == 600
        for line in lines:
            json.loads(line)
    finally:
        w.stop()
