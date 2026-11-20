"""M09 backend gaps 1 and 2: ``GET {ws}/ingestion/jobs`` and ``GET {ws}/overview``."""

from __future__ import annotations

from catalog_support import World
from status_support import seed_job
from tenancy_support import assert_denied

ZERO = {
    "queued": 0,
    "processing": 0,
    "ready": 0,
    "failed": 0,
    "unsupported": 0,
    "no_text": 0,
    "cancelled": 0,
}
JOB_KEYS = {
    "document_id",
    "title",
    "version_id",
    "state",
    "stage",
    "error_code",
    "message",
    "attempts",
    "updated_at",
}


def _seed_mix(world: World) -> dict[str, object]:
    ready = world.seed(title="Ready doc")
    seed_job(world, ready, state="succeeded", revision_state="published")
    failed = world.seed(title="Failed doc")
    seed_job(world, failed, state="dead", error_code="pdf_unreadable", attempts=5)
    running = world.seed(title="Running doc")
    seed_job(world, running, state="running", stage="embed", revision_state="building")
    world.seed(title="No job yet")  # never counted
    return {"ready": ready, "failed": failed, "running": running}


def test_jobs_list_and_counts(world: World) -> None:
    _seed_mix(world)
    res = world.api.get(world.path("/ingestion/jobs"), world.viewer)
    assert res.status == 200, res.body
    assert res.body["counts"] == {**ZERO, "ready": 1, "failed": 1, "processing": 1}
    assert {i["title"] for i in res.body["items"]} == {"Ready doc", "Failed doc", "Running doc"}
    assert all(set(i) == JOB_KEYS for i in res.body["items"])
    failed = next(i for i in res.body["items"] if i["state"] == "failed")
    assert failed["error_code"] == "pdf_unreadable"
    assert failed["attempts"] == 5
    assert failed["message"]

    only = world.api.get(world.path("/ingestion/jobs?state=failed"), world.viewer)
    assert [i["title"] for i in only.body["items"]] == ["Failed doc"]
    assert only.body["counts"]["ready"] == 1  # counts ignore the state filter

    first = world.api.get(world.path("/ingestion/jobs?limit=2"), world.viewer)
    assert len(first.body["items"]) == 2
    assert first.body["next_cursor"]
    second = world.api.get(
        world.path(f"/ingestion/jobs?limit=2&cursor={first.body['next_cursor']}"), world.viewer
    )
    assert len(second.body["items"]) == 1
    assert second.body["next_cursor"] is None
    bad = world.api.get(world.path("/ingestion/jobs?cursor=garbage"), world.viewer)
    assert bad.status == 422


def test_overview_counts(world: World) -> None:
    _seed_mix(world)
    res = world.api.get(world.path("/overview"), world.viewer)
    assert res.status == 200, res.body
    body = res.body
    assert body["documents_total"] == 4  # includes the document that has no job
    assert body["documents"] == {**ZERO, "ready": 1, "failed": 1, "processing": 1}
    assert body["sources"] == 1
    assert body["pending_memories"] == 0
    assert body["research"] == {"by_state": {}, "total": 0}
    assert body["recent_chat_threads"] == 0


def test_outsiders_and_foreign_org_get_404(world: World) -> None:
    _seed_mix(world)
    for sub in ("/ingestion/jobs", "/overview"):
        res = world.api.get(world.path(sub), world.t.outsider)
        assert_denied(world.api, res, 404, action="document.read", reason="not_org_member")
        # a foreign org member asking through our org and workspace ids
        assert world.api.get(world.path(sub), world.foreign.owner).status == 404
        # our workspace id under the foreign org
        path = f"/v1/orgs/{world.foreign.org_id}/workspaces/{world.ws}{sub}"
        assert world.api.get(path, world.foreign.owner).status == 404
        assert world.api.get(world.path(sub), None).status == 401


def test_grant_only_user_sees_counts_of_granted_documents_only(world: World) -> None:
    docs = _seed_mix(world)
    # No role and no grant: concealed 404, nothing counted.
    for sub in ("/ingestion/jobs", "/overview"):
        res = world.api.get(world.path(sub), world.plain)
        assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
    world.grant(docs["failed"].id, world.plain, "read")  # type: ignore[attr-defined]
    jobs = world.api.get(world.path("/ingestion/jobs"), world.plain)
    assert jobs.status == 200, jobs.body
    assert jobs.body["counts"] == {**ZERO, "failed": 1}
    assert [i["title"] for i in jobs.body["items"]] == ["Failed doc"]
    overview = world.api.get(world.path("/overview"), world.plain)
    assert overview.body["documents_total"] == 1
    assert overview.body["documents"] == {**ZERO, "failed": 1}
