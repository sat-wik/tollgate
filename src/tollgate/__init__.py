"""Tollgate — a local capture proxy for LLM API traffic, with cost attribution."""

from .cache_audit import audit
from .pricing import (
    PRICES_AS_OF,
    Rate,
    RatePeriod,
    cost_usd,
    normalize_usage,
    price_for,
    prompt_tokens,
    rate_card,
)
from .proxy import (
    accumulate_anthropic,
    accumulate_openai_chat,
    accumulate_openai_responses,
    create_app,
)
from .record import build_record, load_records, log_record, prompt_sha, request_sha
from .replay import compare, replay_live, reprice, summarize

__version__ = "0.3.0"
__all__ = [
    "PRICES_AS_OF",
    "Rate",
    "RatePeriod",
    "accumulate_anthropic",
    "accumulate_openai_chat",
    "accumulate_openai_responses",
    "audit",
    "build_record",
    "compare",
    "cost_usd",
    "create_app",
    "load_records",
    "log_record",
    "normalize_usage",
    "price_for",
    "prompt_sha",
    "prompt_tokens",
    "rate_card",
    "replay_live",
    "reprice",
    "request_sha",
    "summarize",
]
