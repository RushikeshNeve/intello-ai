"""Upload sessions and finalize (M02.01.03, M02.01.04). Owned by the upload agents."""

from fastapi import APIRouter

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["documents"])
