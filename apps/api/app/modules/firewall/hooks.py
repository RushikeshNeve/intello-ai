"""Glue for the chat and research executors (kept here so the hooks there stay tiny).

Every function is fail-safe per policy mode and never raises. Metadata is counts and rule ids only.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy.orm import Session

from app.modules.firewall import policy as fw
from app.modules.firewall.input_guard import screen_evidence, screen_user
from app.modules.firewall.output_guard import Context, StreamGuard, canary_for, scaffold_line

log = logging.getLogger("ukos.firewall")


def load_policy(
    open_session: Callable[[], AbstractContextManager[Session]], org_id: uuid.UUID
) -> fw.FirewallPolicy:
    """The org's policy; any failure reads as the default (off), as no policy can be known."""
    try:
        with open_session() as session:
            return fw.load(session, org_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("firewall policy unavailable type=%s", type(exc).__name__)
        return fw.FirewallPolicy()


def check_user(policy: fw.FirewallPolicy, question: str, meta: dict[str, Any]) -> bool:
    """True = the message must be refused (``input_blocked``). Records the decision in ``meta``."""
    d = screen_user(question, policy.input_user, policy.threshold)
    if d.hit:
        meta["input_user"] = {"mode": policy.input_user, "hit": 1, "rules": list(d.rule_ids)}
    return d.hit and policy.input_user == fw.BLOCK


def filter_evidence(
    policy: fw.FirewallPolicy, evidence: Sequence[Any], meta: dict[str, Any]
) -> list[Any]:
    """Evidence minus the hostile chunks (``block``); only counts them in ``flag`` mode."""
    s = screen_evidence(evidence, policy.input_evidence, policy.threshold)
    if s.dropped or s.flagged or s.failed:
        meta["input_evidence"] = {
            "mode": policy.input_evidence,
            "dropped": s.dropped,
            "flagged": s.flagged,
            "rules": list(s.rule_ids),
        }
    return s.kept


def with_canary(policy: fw.FirewallPolicy, org_id: uuid.UUID, system_prompt: str) -> str:
    return (
        system_prompt
        if policy.output == fw.OFF
        else f"{system_prompt}\n\n{scaffold_line(canary_for(org_id))}"
    )


def output_guard(
    policy: fw.FirewallPolicy, org_id: uuid.UUID, scaffold: str, bundle: Any
) -> StreamGuard:
    ctx = Context(
        canary=canary_for(org_id),
        scaffold=scaffold,
        allowed_markers=frozenset(int(n) for n in bundle.citation_map),
        allowed_ids=frozenset(
            str(v).lower()
            for e in bundle.used_evidence
            for v in (e.chunk_id, e.document_id, e.version_id)
        ),
    )
    return StreamGuard(policy.output, ctx)
