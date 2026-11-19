"""Chat API (M04.02.04; contract m04 §14 "Chat API (as built)").

* ``POST   /v1/orgs/{o}/chat/threads``                      create a thread (own, private)
* ``GET    /v1/orgs/{o}/chat/threads``                      own threads, newest first, keyset
* ``GET    /v1/orgs/{o}/chat/threads/{t}``                  messages (+ the active run)
* ``PATCH  /v1/orgs/{o}/chat/threads/{t}``                  rename
* ``DELETE /v1/orgs/{o}/chat/threads/{t}``                  soft delete (cancels active runs)
* ``POST   /v1/orgs/{o}/chat/threads/{t}/runs``             ask (idempotent), 202
* ``GET    /v1/orgs/{o}/chat/runs/{r}``                     run state (+ the answer when done)
* ``POST   /v1/orgs/{o}/chat/runs/{r}/cancel``              cancel (idempotent), 202
* ``GET    /v1/orgs/{o}/chat/runs/{r}/events``              SSE, replayable with ``Last-Event-ID``

Another member's thread or run, and anything in an org the caller is not in, is a concealed 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import KeysetCursor, Tenant, api_error, not_found
from app.core.config import Settings, get_settings
from app.modules.ai import chat_repo, chat_service
from app.modules.ai import chat_stream as sse
from app.modules.ai.chat_schemas import (
    CancelOut,
    RunAccepted,
    RunCreate,
    RunOut,
    ThreadCreate,
    ThreadDetail,
    ThreadList,
    ThreadOut,
    ThreadRename,
)

router = APIRouter(prefix="/v1/orgs/{org_id}/chat", tags=["chat"])

_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
_CONCEALED: dict[int | str, dict[str, Any]] = {
    404: {"description": "Not found, or not yours (concealed)."}
}


@router.post(
    "/threads", status_code=status.HTTP_201_CREATED, response_model=ThreadOut, responses=_CONCEALED
)
def create_thread(tenant: Tenant, org_id: uuid.UUID, body: ThreadCreate) -> ThreadOut:
    with tenant.transaction(org_id) as session:
        row = chat_service.create_thread(
            session, tenant, org_id, workspace_id=body.workspace_id, title=body.title
        )
        return chat_service.thread_out(row)


@router.get("/threads", response_model=ThreadList, responses=_CONCEALED)
def list_threads(
    tenant: Tenant,
    org_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after: Annotated[str | None, Query(max_length=512)] = None,
) -> ThreadList:
    cursor: KeysetCursor | None = None
    if after:
        try:
            cursor = KeysetCursor.decode(after)
        except ValueError:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_cursor") from None
    with tenant.transaction(org_id) as session:
        chat_service.require_member(session, tenant, org_id)
        rows, next_cursor = chat_repo.list_threads(
            session, org_id=org_id, user_id=tenant.user_id, limit=limit, after=cursor
        )
        return ThreadList(
            items=[chat_service.thread_out(r) for r in rows],
            next_cursor=None if next_cursor is None else next_cursor.encode(),
        )


@router.get("/threads/{thread_id}", response_model=ThreadDetail, responses=_CONCEALED)
def get_thread(tenant: Tenant, org_id: uuid.UUID, thread_id: uuid.UUID) -> ThreadDetail:
    with tenant.transaction(org_id) as session:
        return chat_service.thread_detail(session, tenant, org_id, thread_id)


@router.patch("/threads/{thread_id}", response_model=ThreadOut, responses=_CONCEALED)
def rename_thread(
    tenant: Tenant, org_id: uuid.UUID, thread_id: uuid.UUID, body: ThreadRename
) -> ThreadOut:
    with tenant.transaction(org_id) as session:
        chat_service.require_member(session, tenant, org_id)
        if not chat_repo.set_thread_title(
            session, org_id=org_id, user_id=tenant.user_id, thread_id=thread_id, title=body.title
        ):
            raise not_found()
        row = chat_repo.get_thread(
            session, org_id=org_id, user_id=tenant.user_id, thread_id=thread_id
        )
        if row is None:  # pragma: no cover - renamed an instant ago in this transaction
            raise not_found()
        return chat_service.thread_out(row)


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_CONCEALED)
def delete_thread(tenant: Tenant, org_id: uuid.UUID, thread_id: uuid.UUID) -> Response:
    with tenant.transaction(org_id) as session:
        chat_service.delete_thread(session, tenant, org_id, thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/threads/{thread_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunAccepted,
    responses={
        200: {"description": "The idempotency key already had a run: same ids."},
        404: _CONCEALED[404],
        409: {"description": "run_in_progress (this thread is answering) or too_many_runs."},
        422: {"description": "Invalid request."},
        429: {"description": "rate_limited; see Retry-After."},
    },
)
def create_run(
    tenant: Tenant,
    org_id: uuid.UUID,
    thread_id: uuid.UUID,
    body: RunCreate,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RunAccepted:
    with tenant.transaction(org_id) as session:
        accepted = chat_service.create_run(
            session,
            tenant,
            org_id,
            thread_id,
            content=body.content,
            idempotency_key=body.idempotency_key,
            workspace_ids=body.workspace_ids,
            settings=settings,
        )
    if not accepted.created:
        response.status_code = status.HTTP_200_OK
    return RunAccepted(run_id=accepted.run_id, message_id=accepted.message_id, state=accepted.state)


@router.get("/runs/{run_id}", response_model=RunOut, responses=_CONCEALED)
def get_run(tenant: Tenant, org_id: uuid.UUID, run_id: uuid.UUID) -> RunOut:
    with tenant.transaction(org_id) as session:
        return chat_service.run_detail(session, tenant, org_id, run_id)


@router.post(
    "/runs/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CancelOut,
    responses=_CONCEALED,
)
def cancel_run(tenant: Tenant, org_id: uuid.UUID, run_id: uuid.UUID) -> CancelOut:
    with tenant.transaction(org_id) as session:
        return CancelOut(state=chat_service.cancel_run(session, tenant, org_id, run_id))


@router.get(
    "/runs/{run_id}/events",
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "Server-sent events."},
        404: _CONCEALED[404],
    },
)
async def run_events(
    request: Request,
    tenant: Tenant,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=32)] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id", max_length=32)] = None,
) -> StreamingResponse:
    """SSE of one run (protocol in ``chat_stream``): creator only, replayable, re-authorized."""
    scope = sse.Scope(tenant, org_id, run_id)
    after = sse.parse_last_event_id(last_event_id, last_event_id_query)
    # The first read runs before the 200 is sent: a denied caller gets a real concealed 404.
    first = await run_in_threadpool(sse.read, scope, after)
    return StreamingResponse(
        sse.stream(scope, first), media_type="text/event-stream", headers=_SSE_HEADERS
    )
