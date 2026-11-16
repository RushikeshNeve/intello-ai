"""M01.01.03 on real PostgreSQL as the app role under RLS: idempotent, race-safe provisioning."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from app.core.db import tenant_session
from app.core.security import Principal, VerifiedClaims
from app.modules.identity.provisioning import USER_PROVISIONED, DbUserProvisioner

pytestmark = pytest.mark.usefixtures("migrated")

ISSUER = "http://127.0.0.1:28381/dex"


def _claims(
    subject: str, *, email: str | None = "alice@ukos.test", name: str | None = "Alice"
) -> VerifiedClaims:
    return VerifiedClaims(
        issuer=ISSUER,
        subject=subject,
        email=email,
        email_verified=True,
        name=name,
        audience=("ukos-api", "ukos-web"),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        raw={"sub": subject},
    )


def _users(admin: Engine, subject: str) -> list[dict[str, object]]:
    with admin.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, issuer, subject, email, display_name, created_at, last_seen_at"
                " FROM users WHERE issuer = :i AND subject = :s"
            ),
            {"i": ISSUER, "s": subject},
        ).mappings()
        return [dict(r) for r in rows]


def _audit(admin: Engine, user_id: uuid.UUID) -> list[dict[str, object]]:
    with admin.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT org_id, actor_user_id, event_type, target_type, target_id, outcome,"
                " metadata FROM audit_events WHERE event_type = :e AND target_id = :t"
            ),
            {"e": USER_PROVISIONED, "t": str(user_id)},
        ).mappings()
        return [dict(r) for r in rows]


def _new_subject() -> str:
    return f"sub-{uuid.uuid4().hex}"


def test_provisioner_runs_as_app_role(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert tuple(row) == (False, False)


def test_first_sign_in_creates_user_and_audit_event(
    app_engine: Engine, admin_engine: Engine
) -> None:
    subject = _new_subject()
    principal = DbUserProvisioner(app_engine).provision(_claims(subject))

    assert isinstance(principal, Principal)
    assert principal.issuer == ISSUER
    assert principal.subject == subject
    assert principal.email == "alice@ukos.test"
    assert principal.display_name == "Alice"
    users = _users(admin_engine, subject)
    assert len(users) == 1
    assert users[0]["id"] == principal.user_id
    assert users[0]["email"] == "alice@ukos.test"
    assert users[0]["display_name"] == "Alice"

    events = _audit(admin_engine, principal.user_id)
    assert events == [
        {
            "org_id": None,
            "actor_user_id": principal.user_id,
            "event_type": "user.provisioned",
            "target_type": "user",
            "target_id": str(principal.user_id),
            "outcome": "success",
            "metadata": {"email_verified": True},
        }
    ]


def test_repeat_sign_in_is_idempotent_and_refreshes_attributes(
    app_engine: Engine, admin_engine: Engine
) -> None:
    provisioner = DbUserProvisioner(app_engine)
    subject = _new_subject()
    first = provisioner.provision(_claims(subject, email="old@ukos.test", name="Old"))
    before = _users(admin_engine, subject)[0]
    time.sleep(0.05)

    second = provisioner.provision(_claims(subject, email="new@ukos.test", name="New"))

    assert second.user_id == first.user_id
    assert (second.email, second.display_name) == ("new@ukos.test", "New")
    users = _users(admin_engine, subject)
    assert len(users) == 1
    after = users[0]
    assert after["email"] == "new@ukos.test"
    assert after["display_name"] == "New"
    assert after["created_at"] == before["created_at"]
    assert after["last_seen_at"] > before["last_seen_at"]  # type: ignore[operator]
    assert len(_audit(admin_engine, first.user_id)) == 1  # no second user.provisioned

    # A token without the profile claims keeps the stored attributes.
    third = provisioner.provision(_claims(subject, email=None, name=None))
    assert (third.user_id, third.email, third.display_name) == (
        first.user_id,
        "new@ukos.test",
        "New",
    )
    assert len(_audit(admin_engine, first.user_id)) == 1


def test_identity_key_is_issuer_and_subject_not_email(
    app_engine: Engine, admin_engine: Engine
) -> None:
    provisioner = DbUserProvisioner(app_engine)
    a = provisioner.provision(_claims(_new_subject(), email="same@ukos.test"))
    b = provisioner.provision(_claims(_new_subject(), email="same@ukos.test"))
    assert a.user_id != b.user_id
    other_issuer = VerifiedClaims(
        issuer="http://127.0.0.1:28382/dex",
        subject=a.subject,
        email=a.email,
        email_verified=True,
        name=None,
        audience=("ukos-api",),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        raw={},
    )
    c = provisioner.provision(other_issuer)
    assert c.user_id not in {a.user_id, b.user_id}


def test_concurrent_first_sign_in_creates_exactly_one_user(
    app_db_url: URL, admin_engine: Engine
) -> None:
    workers = 10
    # A dedicated pool large enough that all ten upserts really run at the same time.
    engine = create_engine(app_db_url, pool_size=workers, max_overflow=0)
    try:
        provisioner = DbUserProvisioner(engine)
        subject = _new_subject()
        barrier = threading.Barrier(workers)

        def sign_in(_: int) -> Principal:
            barrier.wait(timeout=30)
            return provisioner.provision(_claims(subject))

        # Warm the pool so the barrier releases ten ready connections.
        conns = [engine.connect() for _ in range(workers)]
        for conn in conns:
            conn.close()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            principals = list(pool.map(sign_in, range(workers)))
    finally:
        engine.dispose()

    ids = {p.user_id for p in principals}
    assert len(ids) == 1
    users = _users(admin_engine, subject)
    assert len(users) == 1
    assert users[0]["id"] in ids
    assert len(_audit(admin_engine, users[0]["id"])) == 1  # type: ignore[arg-type]


def test_rls_without_identity_context_hides_provisioned_user(app_engine: Engine) -> None:
    """The upsert only works because of the identity context; nothing is visible without it."""
    subject = _new_subject()
    principal = DbUserProvisioner(app_engine).provision(_claims(subject))
    with tenant_session(user_id=None, org_id=None, engine=app_engine) as session:
        count = session.execute(
            text("SELECT count(*) FROM users WHERE id = :id"), {"id": principal.user_id}
        ).scalar_one()
        assert count == 0
        # The app role can't read org-less audit rows back (SELECT is current-org only).
        assert session.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 0
    with tenant_session(user_id=principal.user_id, org_id=None, engine=app_engine) as session:
        count = session.execute(
            text("SELECT count(*) FROM users WHERE id = :id"), {"id": principal.user_id}
        ).scalar_one()
        assert count == 1
