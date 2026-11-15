# Claude Multi-Agent Execution Playbook

## Rule: leaf-task = one agent deliverable
Select one ID from `TASK_INDEX.csv` and give the agent its owning milestone file and `MASTER_ARCHITECTURE.md`. Work must be independently reviewable in one PR, with tests and acceptance evidence. Avoid creating parallel agents for the same files.

## Coordinator process
1. Validate M00 P0 technology spikes and make blocking architecture corrections before mass parallelization.
2. Identify tasks whose prerequisites are satisfied and whose ownership paths do not overlap.
3. Assign an atomic task to a fresh Git worktree and branch `agent/<task-id>`.
4. Agent produces code/docs, tests, API contracts and PR; never merges without integration review.
5. Coordinator checks contracts, runs integration/security checks, merges one PR at a time and rebases dependent agents.
6. Update `TASK_INDEX.csv`, milestone status and ADR when decisions change.

## Concurrency restrictions
- Never concurrently modify DB migrations, the same OpenAPI schema, the same package manifests, global Compose configuration, global auth middleware or shared interfaces without designated owner.
- Backend and frontend agents may run in parallel only after agreeing on explicit API request/response contracts (schema-first).
- Integration agents must use unique test tenants, data volumes and ports. No agent uses production/customer data.
- An agent cannot grant itself new permissions or alter tenant isolation rules to make a test pass.
- A task that changes a shared contract must obtain coordinator approval before edits.

## Suggested agent roles
- Platform agent: Compose, health, local developer workflow (M00).
- Data/security agent: schemas, authorization and RLS (M01–M03).
- Ingestion agent: processors and checkpoints (M02–M03).
- Retrieval/AI agent: embeddings, search and evaluation (M03–M05).
- Frontend agent: navigation, library, chat and evidence views (M01–M07).
- Quality agent: adversarial tests, backup/recovery and CI (M00/M08).

## Copy-ready worker prompt
```text
You are an implementation agent for Universal Knowledge OS. Read MASTER_ARCHITECTURE.md, AGENT_PLAYBOOK.md, and the milestone Markdown assigned below.
TASK_ID: <TASK_ID>
TASK_TITLE: <TASK_TITLE>
ALLOWED_PATHS: <OWNED_PATHS>
DEPENDENCIES_ALREADY_MERGED: <IDs>
ACCEPTANCE_TEST: <criterion>

Implement ONLY this task in your dedicated worktree. First inspect existing code and contracts. Do not modify files outside ALLOWED_PATHS without explicitly reporting a blocked dependency and requesting coordinator reassignment. Preserve tenancy, authorization, immutable versions, evidence lineage, local-first $0 runtime and idempotency requirements. Add meaningful tests. Run affected lint/typecheck/tests. Return: summary, changed files, test commands/results, risks, PR description and integration follow-ups. Do not claim a task is complete if acceptance checks fail.
```

## Copy-ready coordinator prompt
```text
You are the engineering coordinator. Read MASTER_ARCHITECTURE.md, ADR_REGISTER.md, AGENT_PLAYBOOK.md and TASK_INDEX.csv. Select only ready, non-overlapping atomic tasks. Prepare one independent Claude-agent assignment per task using separate Git worktrees and explicit owned paths. Respect dependency gates and shared-contract freeze points. Review returned diffs, run integration and isolation tests, merge sequentially, update statuses and recommend the next parallel batch. Never claim agents have actually run if no execution tool was invoked.
```

## Merge checklist
- [ ] Scope matches task ID and owned paths
- [ ] Relevant acceptance test passes
- [ ] Authorization and tenant scope preserved
- [ ] No secrets or raw private content in logs
- [ ] Idempotency and retries considered
- [ ] OpenAPI/DB/graph interfaces versioned when changed
- [ ] Docs, ADRs and task tracker updated
