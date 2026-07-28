import json
import multiprocessing

import pytest

from tollgate.cli import build_parser, main
from tollgate.record import build_record, load_records, log_record


def _write(path, model="claude-opus-5", in_tok=1_000_000, out_tok=0, status=200):
    log_record(
        str(path),
        build_record(
            "/v1/messages",
            {"model": model, "messages": [{"role": "user", "content": "hi"}]},
            {
                "model": model,
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            },
            status=status,
            latency_ms=100.0,
        ),
    )


def test_bare_args_still_mean_serve():
    # `tollgate --port 4141` predates subcommands and has to keep working.
    args = build_parser().parse_args(["serve", "--port", "9999"])
    assert args.command == "serve"
    assert args.port == 9999


def test_report_prints_a_table(tmp_path, capsys):
    path = tmp_path / "c.jsonl"
    _write(path)
    main(["report", "--logs", str(path)])
    out = capsys.readouterr().out
    assert "claude-opus-5" in out
    assert "cost_usd" in out


def test_report_json_is_machine_readable(tmp_path, capsys):
    path = tmp_path / "c.jsonl"
    _write(path)
    _write(path, model="claude-haiku-4-5")
    main(["report", "--logs", str(path), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert {r["model"] for r in rows} == {"claude-opus-5", "claude-haiku-4-5"}
    assert rows[0]["cost_usd"] == 5.0  # sorted most expensive first


def test_report_surfaces_failures_rather_than_hiding_them(tmp_path, capsys):
    path = tmp_path / "c.jsonl"
    _write(path)
    _write(path, status=429)
    main(["report", "--logs", str(path), "--json"])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["calls"] == 2
    assert row["errors"] == 1
    # The failed call contributes latency but no cost.
    assert row["cost_usd"] == 5.0


def test_offline_replay_needs_no_network(tmp_path, capsys):
    path = tmp_path / "c.jsonl"
    _write(path)
    main(["replay", "--logs", str(path)])
    assert "repriced 1 record" in capsys.readouterr().out


def test_empty_log_reports_cleanly(tmp_path, capsys):
    path = tmp_path / "c.jsonl"
    path.write_text("")
    main(["report", "--logs", str(path)])
    assert "no records" in capsys.readouterr().out


def _append_many(args):
    """Run in a separate process — each writer opens its own descriptor."""
    path, tag, count = args
    for _ in range(count):
        log_record(
            path,
            build_record(
                "/v1/messages",
                {"model": "claude-opus-5", "messages": [{"role": "user", "content": tag * 40_000}]},
                {"model": "claude-opus-5", "usage": {"input_tokens": 1, "output_tokens": 1}},
            ),
        )


@pytest.mark.skipif(
    multiprocessing.get_start_method(allow_none=True) == "spawn" and __name__ != "__main__",
    reason="needs a fork-capable start method",
)
def test_concurrent_writers_do_not_tear_lines(tmp_path):
    """Multi-process appends stay whole.

    This passes with or without the flock on a local POSIX filesystem, where
    O_APPEND is already atomic — it guards the property, and would catch a
    regression that replaced the single append with a read-modify-write or a
    long-lived shared handle.
    """
    path = str(tmp_path / "c.jsonl")
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(4) as pool:
        pool.map(_append_many, [(path, chr(ord("a") + i), 15) for i in range(4)])

    lines = [ln for ln in open(path).read().splitlines() if ln]
    assert len(lines) == 60
    # load_records silently skips torn lines, so parse directly: a dropped
    # record here would otherwise look like a clean read.
    for line in lines:
        json.loads(line)
    assert len(load_records(path)) == 60


def test_cache_command_reports_findings(tmp_path, capsys):
    path = tmp_path / "c.jsonl"
    for _ in range(6):
        log_record(
            str(path),
            build_record(
                "/v1/messages",
                {"model": "claude-opus-5", "messages": [{"role": "user", "content": "big"}]},
                {"model": "claude-opus-5", "usage": {"input_tokens": 20_000, "output_tokens": 10}},
            ),
        )
    main(["cache", "--logs", str(path)])
    out = capsys.readouterr().out
    assert "never_cached" in out
    assert "recoverable" in out


def test_cache_command_is_quiet_when_nothing_is_wrong(tmp_path, capsys):
    path = tmp_path / "c.jsonl"
    _write(path)
    main(["cache", "--logs", str(path)])
    assert "no caching problems found" in capsys.readouterr().out


def test_rates_prices_at_a_given_date(capsys):
    main(["rates", "--model", "claude-sonnet-5", "--at", "2026-08-15", "--json"])
    intro = json.loads(capsys.readouterr().out)[0]
    main(["rates", "--model", "claude-sonnet-5", "--at", "2026-09-15", "--json"])
    standard = json.loads(capsys.readouterr().out)[0]
    assert intro["input"] == 2.0
    assert standard["input"] == 3.0


def test_rates_for_an_unknown_model_says_so(capsys):
    main(["rates", "--model", "not-a-model"])
    assert "no price known" in capsys.readouterr().out


def test_report_prices_each_call_at_its_own_date(tmp_path, capsys):
    """A log spanning a price change must not be repriced at today's rate."""
    path = tmp_path / "c.jsonl"
    for stamp in ("2026-08-15T00:00:00+00:00", "2026-09-15T00:00:00+00:00"):
        rec = build_record(
            "/v1/messages",
            {"model": "claude-sonnet-5", "messages": []},
            {"model": "claude-sonnet-5", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
        )
        rec["timestamp"] = stamp
        log_record(str(path), rec)
    main(["report", "--logs", str(path), "--json"])
    row = json.loads(capsys.readouterr().out)[0]
    # $2/MTok under introductory pricing plus $3/MTok after it lapsed.
    assert row["cost_usd"] == pytest.approx(5.0)
