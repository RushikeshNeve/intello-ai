"""Graph READ settings: own settings class over the ``GRAPH_*`` env names (contract m05 §7)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hard server-side ceilings: no request parameter or setting can exceed them.
HARD_MAX_DEPTH = 2
HARD_MAX_NODES = 100
HARD_MAX_EDGES = 200


class GraphReadSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    GRAPH_ENABLED: bool = False
    GRAPH_ROUTER_MODE: Literal["auto", "off", "force"] = "auto"
    GRAPH_TRAVERSAL_MAX_DEPTH: int = Field(default=2, ge=1, le=HARD_MAX_DEPTH)
    GRAPH_MAX_NODES: int = Field(default=50, ge=1, le=HARD_MAX_NODES)
    GRAPH_MAX_EVIDENCE_EXTRA: int = Field(default=6, ge=0, le=20)


@lru_cache(maxsize=1)
def get_graph_read_settings() -> GraphReadSettings:
    return GraphReadSettings()
