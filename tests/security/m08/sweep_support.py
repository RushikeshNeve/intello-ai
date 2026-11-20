"""Route enumeration and path filling for the M08.01 tenant sweep."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import FastAPI

WRITE = {"POST", "PUT", "PATCH", "DELETE"}


def routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every (METHOD, path template) of the live OpenAPI schema."""
    out: list[tuple[str, str]] = []
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            out.append((method.upper(), path))
    return sorted(out, key=lambda r: (r[1], r[0]))


def tenant_routes(app: FastAPI) -> list[tuple[str, str]]:
    return [r for r in routes(app) if "{org_id}" in r[1]]


def fill(path: str, ids: dict[str, Any]) -> str:
    """Substitute ``{name}`` params from ``ids``; unknown params get a random UUID."""

    def sub(m: re.Match[str]) -> str:
        return str(ids.get(m.group(1)) or uuid.uuid4())

    return re.sub(r"\{(\w+)\}", sub, path)
