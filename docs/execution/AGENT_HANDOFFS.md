# Agent Handoffs

One entry per task when it reaches REVIEW or DONE. The coordinator adds verification evidence it reran itself on
`main`. Agent self-reports are kept but are not treated as proof.

---

## M00.01.01 — System boundaries and ADR index
- **Owner:** coordinator · **Branch:** main · **Status:** DONE
- **Files:** `docs/architecture/system-boundaries.md`, `tests/docs/test_system_boundaries.py`
- **Acceptance:** "Boundary table and accepted/provisional ADR references are consistent."
- **Evidence:** `uv run --no-project --with pytest pytest -q tests/docs` → `4 passed`. The checks: the register has ADR-001…057 in sequence, every ADR the doc references exists, the doc's index matches the register row-for-row (decision and status), and the core ownership rows are present.
- **Limitations:** ADR rationale documents don't exist (the register is summaries only); boundaries follow MASTER_ARCHITECTURE v1.0.
