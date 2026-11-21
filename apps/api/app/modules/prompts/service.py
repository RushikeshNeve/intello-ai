# ruff: noqa: S608 - the SQL is built only from the COLUMNS constant (no caller data).
"""Admin operations on ``prompt_versions`` (all take an RLS-scoped session of the context org)."""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.prompts.registry import sha256_of

COLUMNS: Final = (
    "id, org_id, kind, version, body, sha256, change_note, created_by, created_at, state,"
    " activated_at, eval_run_id, eval_attestation"
)


class VersionConflict(Exception):
    """Concurrent writer took the version number or the active slot; retry."""


def list_versions(session: Session, org_id: uuid.UUID, kind: str | None) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            f"SELECT {COLUMNS} FROM prompt_versions"
            " WHERE (org_id = :o OR org_id IS NULL) AND (CAST(:k AS text) IS NULL OR kind = :k)"
            " ORDER BY kind, org_id NULLS FIRST, version DESC"
        ),
        {"o": org_id, "k": kind},
    ).mappings()
    return [dict(r) for r in rows]


def create_draft(
    session: Session, org_id: uuid.UUID, user_id: uuid.UUID, kind: str, body: str, note: str | None
) -> dict[str, Any]:
    new_id = uuid.uuid4()
    try:
        with session.begin_nested():
            row = (
                session.execute(
                    text(
                        "INSERT INTO prompt_versions"
                        " (id, org_id, kind, version, body, sha256, change_note, created_by, state)"
                        " VALUES (:id, :o, :k, COALESCE((SELECT max(version) FROM prompt_versions"
                        " WHERE org_id = :o AND kind = :k), 0) + 1, :b, :h, :n, :u, 'draft')"
                        f" RETURNING {COLUMNS}"
                    ),
                    {
                        "id": new_id,
                        "o": org_id,
                        "k": kind,
                        "b": body,
                        "h": sha256_of(body),
                        "n": note,
                        "u": user_id,
                    },
                )
                .mappings()
                .one()
            )
    except IntegrityError:
        raise VersionConflict from None
    return dict(row)


def get_own(session: Session, org_id: uuid.UUID, version_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(f"SELECT {COLUMNS} FROM prompt_versions WHERE id = :i AND org_id = :o"),
            {"i": version_id, "o": org_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else dict(row)


def _retire_active(session: Session, org_id: uuid.UUID, kind: str) -> None:
    session.execute(
        text(
            "UPDATE prompt_versions SET state = 'retired'"
            " WHERE org_id = :o AND kind = :k AND state = 'active'"
        ),
        {"o": org_id, "k": kind},
    )


def activate(
    session: Session,
    org_id: uuid.UUID,
    version: dict[str, Any],
    evidence: tuple[uuid.UUID | None, str | None] = (None, None),
) -> dict[str, Any]:
    if version["state"] == "active":
        return version
    try:
        with session.begin_nested():
            _retire_active(session, org_id, version["kind"])
            row = (
                session.execute(
                    text(
                        "UPDATE prompt_versions SET state = 'active', activated_at = now(),"
                        " eval_run_id = COALESCE(:er, eval_run_id),"
                        " eval_attestation = COALESCE(:ea, eval_attestation)"
                        f" WHERE id = :i AND org_id = :o RETURNING {COLUMNS}"
                    ),
                    {"i": version["id"], "o": org_id, "er": evidence[0], "ea": evidence[1]},
                )
                .mappings()
                .one()
            )
    except IntegrityError:
        raise VersionConflict from None
    return dict(row)


def rollback(session: Session, org_id: uuid.UUID, kind: str) -> tuple[bool, dict[str, Any] | None]:
    """Re-activate the highest earlier version that was ever active; with none, retire the current
    one so the org falls back to the global default. Returns ``(had_active, new_active)``."""
    current = (
        session.execute(
            text(
                f"SELECT {COLUMNS} FROM prompt_versions"
                " WHERE org_id = :o AND kind = :k AND state = 'active'"
            ),
            {"o": org_id, "k": kind},
        )
        .mappings()
        .one_or_none()
    )
    if current is None:
        return False, None
    previous = (
        session.execute(
            text(
                f"SELECT {COLUMNS} FROM prompt_versions WHERE org_id = :o AND kind = :k"
                " AND version < :v AND activated_at IS NOT NULL ORDER BY version DESC LIMIT 1"
            ),
            {"o": org_id, "k": kind, "v": current["version"]},
        )
        .mappings()
        .one_or_none()
    )
    if previous is None:
        _retire_active(session, org_id, kind)
        return True, None
    return True, activate(session, org_id, dict(previous))
