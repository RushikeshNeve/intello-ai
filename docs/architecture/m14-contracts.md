# M14 contracts

## M14.07
- Tables (migration `0025_abuse_canaries`, FORCE RLS by org): `security_events` (kind `abuse_anomaly|canary_hit`, severity, `counts` jsonb, run_id, document_id; ids and counts only, never text), `canary_documents` (flag kept out of `documents`), `abuse_settings` (per-org action `off|flag|throttle|block` + thresholds).
- `firewall/abuse.py`: `guard(sessions, org, user, kind, document_ids=)` counts search/chat/evidence_read/export and distinct documents in a sliding window (Redis zsets, in-process fallback; fails open). Hooked after the membership check in search, chunk evidence, chat run creation and research export.
- Crossing a threshold records ONE event per user per window (Redis/memory gate plus DB check) and an audit event; `throttle` answers 429 `abuse_throttled` while over the threshold, `block` for `block_seconds`. Defaults: flag, 300 s window, 120 search, 60 chat, 200 reads, 20 exports, 150 distinct docs.
- Canaries: an admin marks a document; its audience is nobody, so ANY retrieval, evidence read or chat citation raises a `high` `canary_hit` (deduped 60 s per user and document). `abuse.canary_document_ids(session, org)` MUST be used to exclude canaries from eval and analytics counts.
- Admin API `/v1/orgs/{org}/security/{events, events/{id}/acknowledge, abuse-settings, canaries}`: owner/admin only (member 403, outsider 404). Web: `AbusePanel` in settings.
- Runbook: place a canary in a locked workspace nobody is granted, with a believable title; treat any event as an incident (security-incident.md section 5).
