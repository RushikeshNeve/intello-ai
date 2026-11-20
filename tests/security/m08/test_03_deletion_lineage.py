"""M08.01.03: deleting a document denies it EVERYWHERE, and no in-process cache keeps its content.

One scenario (run on the in-memory index and once on a REAL RushDB): a document is searched, cited
by a chat message, used as evidence by an approved memory, a research report and a graph claim.
After ``DELETE`` + the worker's purge:

* search, the evidence chunk, the document itself deny; the old chat message's citations are
  re-resolved to nothing; graph neighborhood edges are empty and the claim is a 404;
* the memory is invalidated; the research report's statements are dropped at read time;
* the index holds no chunks of the revision and the artifacts are gone;
* in-process caches (rate limiter buckets, ``lru_cache`` singletons, policy) hold no content.

Live-check checklist (running stack): delete a document in the UI, then (1) search for its words,
(2) open an old chat citation, (3) open the graph neighborhood/claim, (4) the memory shows
``invalidated``, (5) the research report drops its statements, (6) ``rushdb`` raw query for the
document id returns nothing.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from catalog_support import World
from chat_support import HERON, HERON_ANSWER, HERON_Q, ask_ok, new_thread
from ret_support import Published, Rig
from sqlalchemy import Engine

from app.core.config import Settings
from app.modules.ai.chat import ChatDeps
from app.modules.ai.models.fake import FakeChatModel
from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.models.types import ChatMessage
from app.modules.graph.fake_read import FakeGraphRead
from app.modules.graph.ports_read import GClaim, GEntity, GEvidence
from app.modules.ingestion.artifacts import artifact_key
from app.modules.knowledge.deletion import purge_document
from app.modules.memory import invalidation
from app.workers.chat_tasks import run_chat
from app.workers.research_tasks import run_research
from app.workers.worker_db import session_factory_for

APP_DIR = Path(__file__).resolve().parents[3] / "apps" / "api" / "app"
STATEMENT = "Herons feed on fish and frogs in shallow water."
# Every lru_cache in the app: settings getters and per-process singletons. None is keyed by or
# stores tenant content; a NEW cache must be reviewed and added here on purpose.
ALLOWED_CACHES = {
    "get_settings", "get_url_capture_settings", "get_graph_read_settings",
    "_default_graph_repository", "default_graph_read_repository", "get_format_settings",
    "default_repository", "default_uploads_store", "default_artifact_store",
    "get_search_settings", "_embedder", "_limiter", "_build_object_store", "get_worker_settings",
}  # fmt: skip


def _plan(messages: Sequence[ChatMessage]) -> str:
    if messages[0]["content"].startswith("You break"):
        return json.dumps({"subqueries": ["what do herons feed on"]})
    return HERON_ANSWER


def _scenario(rig: Rig, world: World, worker: Engine, base: Settings, gholder: Any) -> None:
    api, org, ws = world.api, world.org_id, world.ws
    viewer = world.viewer
    doc = rig.seed_world(world, title="Heron guide")
    pub: Published = rig.publish(org, ws, doc, [HERON])
    base_search = f"/v1/orgs/{org}/search"

    # graph claim whose only evidence is the document's chunk
    a, b = (GEntity(uuid.uuid4(), org, ws, "concept", n, "active") for n in ("Heron", "Fish"))
    claim = GClaim(
        uuid.uuid4(), org, ws, a.id, "depends_on", b.id, None, 0.9, "verified",
        pub.revision_id, True,
    )  # fmt: skip
    evidence = GEvidence(
        uuid.uuid4(), claim.id, pub.chunk_ids[0], pub.revision_id, doc.id, pub.version_id, "x"
    )
    gholder.graph = FakeGraphRead([a, b], [claim], [evidence])
    graph = f"/v1/orgs/{org}/workspaces/{ws}/graph"

    # an approved memory citing the chunk
    sugg = api.post(
        f"/v1/orgs/{org}/memory/suggestions",
        viewer,
        {
            "statement": STATEMENT,
            "kind": "fact",
            "evidence": [
                {
                    "chunk_id": str(pub.chunk_ids[0]),
                    "document_id": str(doc.id),
                    "version_id": str(pub.version_id),
                }
            ],
        },
    )
    assert sugg.status == 201, sugg.body
    memory_id = sugg.body["id"]
    assert api.post(f"/v1/orgs/{org}/memory/{memory_id}/approve", viewer, None).status == 200

    # an old chat message with a citation
    thread = new_thread(api, org, viewer)
    run = ask_ok(api, org, thread, viewer, HERON_Q)
    deps = ChatDeps(
        repo=rig.repo,
        embedder=rig.embedder,
        chat_settings=ChatSettings(),
        model_factory=lambda _ps, _org, _cs: FakeChatModel([HERON_ANSWER]),
    )
    assert run_chat(run, engine=worker, settings=base, deps_provider=lambda: deps) == "completed"
    thread_path = f"/v1/orgs/{org}/chat/threads/{thread}"

    def assistant_citations() -> list[Any]:
        msgs = api.get(thread_path, viewer).body["messages"]
        return [c for m in msgs if m["role"] == "assistant" for c in m["citations"]]

    # a research report citing it
    started = api.post(f"/v1/orgs/{org}/research", viewer, {"question": "What do herons eat?"})
    assert started.status == 202, started.body
    session = started.body["id"]
    rdeps = ChatDeps(
        repo=rig.repo,
        embedder=rig.embedder,
        chat_settings=ChatSettings(),
        model_factory=lambda _ps, _org, _cs: FakeChatModel(reply_fn=_plan),
    )
    assert (
        run_research(uuid.UUID(session), engine=worker, settings=base, deps_provider=lambda: rdeps)
        == "completed"
    )

    def report_statements() -> list[Any]:
        report = api.get(f"/v1/orgs/{org}/research/{session}", viewer).body["report"]
        return [st for sec in report["sections"] for st in sec["statements"]]

    scope = rig.org_scope(org)
    chunk_key = artifact_key(org, pub.revision_id, "chunk")
    chunk_path = world.doc_path(doc.id, f"/versions/{pub.version_id}/chunks/{pub.chunk_ids[0]}")

    # ---- before: everything works
    assert api.post(base_search, viewer, {"query": "herons fish frogs"}).body["results"]
    assert api.get(chunk_path, viewer).status == 200
    assert assistant_citations() and report_statements()
    assert api.get(f"{graph}/neighborhood?entity_id={a.id}", viewer).body["edges"]
    assert api.get(f"{graph}/claims/{claim.id}", viewer).status == 200
    assert api.get(f"/v1/orgs/{org}/memory/{memory_id}", viewer).body["state"] == "approved"
    assert rig.repo.count_chunks(scope, pub.revision_id) == 1
    assert rig.artifact_objects.head_object(chunk_key) is not None

    # ---- delete, then the worker's purge and memory invalidation (as `tasks.delete_document`)
    assert api.delete(world.doc_path(doc.id), world.ws_admin).status in (200, 202, 204)
    sessions = session_factory_for(worker)
    purge_document(sessions, rig.repo, rig.artifacts, org, doc.id)
    invalidation.invalidate_document(worker, org, doc.id)

    # ---- after: everything denies
    found = api.post(base_search, viewer, {"query": "herons fish frogs"})
    assert found.status == 200 and found.body["results"] == []
    assert "Heron guide" not in str(found.body)
    assert api.get(chunk_path, viewer).status == 404
    assert api.get(world.doc_path(doc.id), viewer).status == 404
    assert assistant_citations() == []  # old messages keep their text, lose the citation
    assert api.get(f"{graph}/neighborhood?entity_id={a.id}", viewer).body["edges"] == []
    assert api.get(f"{graph}/claims/{claim.id}", viewer).status == 404
    mem = api.get(f"/v1/orgs/{org}/memory/{memory_id}", viewer).body
    assert (mem["state"], mem["invalidated_reason"]) == ("invalidated", "evidence_deleted")
    assert report_statements() == []
    assert rig.repo.count_chunks(scope, pub.revision_id) == 0
    assert rig.artifact_objects.head_object(chunk_key) is None
    assert (
        rig.q("SELECT count(*) FROM documents WHERE id = :d AND deleted_at IS NOT NULL", d=doc.id)[
            0
        ][0]
        == 1
    )

    # ---- in-process caches hold nothing of the document
    _assert_caches_clean(rig, str(doc.id), "Heron guide", HERON)


def _assert_caches_clean(rig: Rig, *needles: str) -> None:
    keys = " ".join(rig.limiter._buckets)
    for needle in needles:
        assert needle not in keys  # buckets are keyed by user id only
    assert all(re.fullmatch(r"[0-9a-f-]{36}", k) for k in rig.limiter._buckets)
    found: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.strip() in ("@lru_cache", "@cache") or line.strip().startswith("@lru_cache("):
                found.add(re.sub(r"def (\w+).*", r"\1", lines[i + 1].strip()))
    assert found == ALLOWED_CACHES, found ^ ALLOWED_CACHES
    # the AI policy is read from PostgreSQL per call: it is not cached in-process
    assert not [
        p for p in APP_DIR.joinpath("modules/ai").rglob("*.py") if "lru_cache" in p.read_text()
    ]


def test_deletion_denies_everywhere_in_memory_index(
    rig: Rig, world: World, worker_engine: Engine, sholder: Any, gholder: Any
) -> None:
    _scenario(rig, world, worker_engine, sholder.current, gholder)


def test_deletion_denies_everywhere_on_real_rushdb(
    real_rig: Rig, world: World, worker_engine: Engine, sholder: Any, gholder: Any
) -> None:
    _scenario(real_rig, world, worker_engine, sholder.current, gholder)
