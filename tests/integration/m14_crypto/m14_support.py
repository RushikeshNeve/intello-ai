"""Constants of the M14.08 tests (test keys only; never real key material)."""

from __future__ import annotations

import base64

KEK1 = base64.b64encode(bytes(range(200, 232))).decode()
KEK2 = base64.b64encode(bytes(range(50, 82))).decode()
CANARY = "ghp_CANARY_m14_token_9f8e7d6c5b4a"
