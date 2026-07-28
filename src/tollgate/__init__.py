"""Tollgate — a local capture proxy for LLM API traffic, with cost attribution."""

from .pricing import cost_usd, normalize_usage, price_for
from .proxy import (
    accumulate_anthropic,
    accumulate_openai_chat,
    accumulate_openai_responses,
    create_app,
)
from .record import build_record, load_records, log_record, prompt_sha, request_sha
from .replay import compare, replay_live, reprice, summarize

__version__ = "0.2.0"
__all__ = [
    "accumulate_anthropic",
    "accumulate_openai_chat",
    "accumulate_openai_responses",
    "build_record",
    "compare",
    "cost_usd",
    "create_app",
    "load_records",
    "log_record",
    "normalize_usage",
    "price_for",
    "prompt_sha",
    "replay_live",
    "reprice",
    "request_sha",
    "summarize",
]
