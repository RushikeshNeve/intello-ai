"""Document deletion (M02.02.04). Owned by its batch-B agent."""

from fastapi import APIRouter

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["documents"])
