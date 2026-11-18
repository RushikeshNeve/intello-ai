"""Documents router: composes the per-feature sub-routers (each owned by one M02 task).

Every sub-router uses the prefix ``/v1/orgs/{org_id}/workspaces/{workspace_id}``.
"""

from fastapi import APIRouter

from app.modules.documents import api, deletion, upload, versions

router = APIRouter()
router.include_router(upload.router)
router.include_router(api.router)
router.include_router(versions.router)
router.include_router(deletion.router)
