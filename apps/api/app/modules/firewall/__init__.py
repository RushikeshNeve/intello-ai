"""M14 AI firewall and abuse protection.

Input guard (S1), output guard (S3), per-org policy and its API, abuse protection and canary
documents (S8), PII and secret detection (``firewall.pii``, S4). Everything is default OFF (no policy row = off). Decisions are recorded as counts
and rule ids only, never text. ``modules.firewall.pii`` is reached through a guarded lazy import
(``output_guard.try_redact``).
"""
