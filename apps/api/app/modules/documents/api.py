"""Document list, get and rename (M02.02.02). Owned by its batch-B agent."""

from fastapi import APIRouter

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["documents"])
