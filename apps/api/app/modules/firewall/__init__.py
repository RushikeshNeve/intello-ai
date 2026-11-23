"""M14 AI firewall: input guard (S1), output guard (S3), per-org policy and its API.

Everything is default OFF (no policy row = off). Decisions are recorded as counts and rule ids
only, never text. ``modules.firewall.pii`` belongs to another task and is only reached through a
guarded lazy import (``output_guard.try_redact``).
"""
