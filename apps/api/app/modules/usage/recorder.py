"""Writes usage events. ``UsageRecorder.record`` never raises: metering must not fail a model call.

Counts, latency and an estimated cost only; never prompt or answer text (nothing here receives it).
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.modules.usage.models import UsageEvent

log = logging.getLogger(__name__)

SessionFor = Callable[[uuid.UUID], AbstractContextManager[Session]]


@dataclass(frozen=True)
class UsageContext:
    org_id: uuid.UUID
    kind: str  # chat | embed | research | graph
    user_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None


def estimate_cost_micros(
    prices_json: str, provider: str, model: str, tokens_in: int, tokens_out: int, tokens_cached: int
) -> int:
    """Micro currency units. Prices are per 1M tokens; unknown models or bad config cost 0."""
    try:
        table: Any = json.loads(prices_json or "{}")
        entry = table.get(f"{provider}/{model}") or table.get(f"{provider}/*")
        if not isinstance(entry, dict):
            return 0
        p_in, p_out, p_cached = (int(entry.get(k, 0)) for k in ("in", "out", "cached"))
    except (ValueError, TypeError, AttributeError):
        return 0
    return max(0, (tokens_in * p_in + tokens_out * p_out + tokens_cached * p_cached) // 1_000_000)


def _genai_hook(*args: Any) -> None:
    """M13.01: spans and metrics from the same numbers as the usage event (no second counting)."""
    with contextlib.suppress(Exception):  # telemetry never fails a model call
        from app.core import tracing_genai

        tracing_genai.on_usage(*args)


class UsageRecorder:
    def __init__(self, sessions: SessionFor, prices_json: str | None = None) -> None:
        self._sessions = sessions
        self._prices = prices_json

    def _prices_json(self) -> str:
        if self._prices is not None:
            return self._prices
        from app.core.config import get_settings

        return get_settings().USAGE_PRICES_JSON

    def record(
        self,
        ctx: UsageContext,
        *,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        tokens_cached: int = 0,
        latency_ms: int = 0,
    ) -> None:
        try:
            cost = estimate_cost_micros(
                self._prices_json(), provider, model, tokens_in, tokens_out, tokens_cached
            )
            _genai_hook(
                ctx.kind, provider, model, tokens_in, tokens_out, tokens_cached, latency_ms, cost
            )
            with self._sessions(ctx.org_id) as session:
                session.execute(
                    insert(UsageEvent).values(
                        id=uuid.uuid4(),
                        org_id=ctx.org_id,
                        workspace_id=ctx.workspace_id,
                        user_id=ctx.user_id,
                        kind=ctx.kind,
                        provider=provider[:64],
                        model=model[:200],
                        tokens_in=max(0, tokens_in),
                        tokens_out=max(0, tokens_out),
                        tokens_cached=max(0, tokens_cached),
                        latency_ms=max(0, latency_ms),
                        est_cost_micros=cost,
                        run_id=ctx.run_id,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - metering never fails a model call
            log.warning("usage: event not recorded (%s)", type(exc).__name__)
