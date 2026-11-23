# M14 contracts

## M14.01/03 Input guard and output guard
- Table `firewall_policies` (migration 0023, RLS by org, app CRUD, worker SELECT): modes `input_user`, `input_evidence` in off|flag|block, `output` in off|flag|block|redact, `threshold` 0.05-1.0. No row = all off. API `GET/PUT /v1/orgs/{org}/firewall-policy` (members read, owner/admin write, other org 404); audit `org.firewall_policy.updated` (modes only). UI: AI firewall card in settings.
- `firewall/input_guard.py`: layered rules (injection phrases, prompt exfiltration, role play, role tokens, fence forgery, bidi/invisible/tag Unicode, encoded markers) on normalized text plus pluggable `set_classifier(fn(text)->score)` (no-op default). Hit = score >= threshold.
- Hooks (`firewall/hooks.py`, called from `ai/chat.py`, `research/runtime.py`): user hit in block -> run fails `input_blocked`, model never called; hostile evidence chunk dropped (counted), run continues; flag only records.
- `firewall/output_guard.py`: per-org canary appended to the system scaffold only while output mode != off (prompt text and `PROMPT_VERSION` unchanged), 6-word scaffold shingle echo, secret patterns, citation markers/ids outside the offered evidence. `StreamGuard` holds back 96 chars so blocked text is never flushed; block -> run fails `output_blocked`; redact replaces secrets (and `firewall.pii.redact` when that module exists; guarded import), unredactable findings block.
- Fail-safe: guard error in block mode blocks (user: refuse, chunk: drop, output: withhold); in flag/off it passes. Policy load failure reads as off.
- Metadata: `chat_runs.model.firewall` holds counts and rule ids only, never text.
