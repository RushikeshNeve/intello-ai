# Intello — Full Product UX/UI Specification (Prototype v1)

## Status and intended use
This is a *high-fidelity, clickable front-end experience*, built as a static HTML/CSS/JavaScript prototype for the **authenticated Intello SaaS application**. This is **not** the marketing landing page. It uses fictional data for Northstar Labs and does not call a backend, identity provider, RushDB, AI API, R2 or other service.

Keep the existing Intello prism identity. Reference vector: `assets/intello-prism.svg`. It is an approximation of the approved raster logo, not the original graphic-design master artwork. Avoid introducing alternative icons or unrelated green-only branding.

## Core UX thesis
**Many Sources → Understanding → One Intelligence.** The experience is a calm, unified workspace where users (1) ingest knowledge, (2) search or ask, (3) verify evidence, and (4) explore relationships. Make citations and source provenance first-class; avoid a generic chatbot-with-sidebar aesthetic.

## App information architecture / screens
| Route recommendation | Mock view | Key content | Interactions represented |
|---|---|---|---|
| `/orgs/[orgId]/workspaces/[workspaceId]` | Home | Knowledge metrics, Ask entry, recent content, pulse, ongoing investigations | Quick Ask, upload dialog, navigate library/activity/research |
| `/.../search` | Universal Search | Keyword/semantic results, category tabs, related concepts, scoped search | Search query, result type filters, select result, jump to sourced answer |
| `/.../chat` | Ask Intello | Conversation history, answer claims with inline citations, permanent evidence inspector | Compose, mock answer, evidence selection, new chat, copy/feedback controls |
| `/.../library` | Knowledge Library | Source inventory, metadata, versions, status, file types, upload | Filter source type, search files, inspect source, simulated file selection |
| `/.../graph` | Knowledge Graph | Bounded conceptual graph, related evidence and concepts | Click graph node, inspect edge, ask about a concept |
| `/.../research` | Research Studio | Saved investigations, start brief, evidence-backed reports | Create brief dialog, inspect report, follow-up in chat |
| `/.../memory` | Knowledge Memory | Suggested and approved memories, source/origin, scope | Approve, dismiss, add, remove demo memories |
| `/.../integrations` | Integrations | Connected source catalog, sync status, supported connector types | Connector management/configure UI; all external ops are illustrative |
| `/.../activity` | Activity & Jobs | Job counts, processing stage progress, sync activity | Open job details with stage checkpoints |
| `/.../team` | People & Access | Members, roles, invitations, workspace access | Invite teammate dialog, role controls illustrative |
| `/.../settings` | Settings & Usage | Workspace, model policy, privacy, usage, notifications | Tab navigation and nonpersistent toggles |

## Global shell
- Left: fixed desktop navigation with workspace switcher, app views, management areas, usage state and user identity.
- Top: current org/workspace breadcrumb, command-search trigger, notifications, help, avatar.
- Center: readable max-width, warm-neutral/light surfaces, 8px spacing grid, cards only where they aid scanning.
- Mobile: hamburger drawer replaces desktop nav, single-column content, responsive card stacking, evidence moves below chat.
- Avoid showing info that user is not authorized to access. All data is illustrative here; production API must enforce security independently.

## Signature interactions
### Ask → Citation → Source
A grounded answer cites immutable `document_version_id`/`knowledge_revision_id` + source locator. Selecting a citation should reveal a persistent evidence inspector with source title, page/line/cell/time, text highlight or nearest supported location, explicit version, and restricted access checks. The prototype shows a sample right-hand document panel; production must support PDF, DOCX, code, spreadsheet, image and media viewers.

### Search → Graph → Research
Search produces authorized evidence-backed results across files, repos, concepts, transcripts and sites. A result can become an AI prompt, source preview or graph exploration seed. Graph must be contextual, bounded, permission-aware and keyboard navigable. Saved research runs should retain citations and permissions.

### Sources → Processing → Ready
Upload entry point leads to supported file selection, version registration, scanning, async worker progress, and eventual search publication. Status comes from PostgreSQL. SSE only signals progress; it is not authoritative. UI must handle queued, validating, extracting, normalizing, chunking, embedding, enriching, indexing, verifying, published, failed, cancelled, permission-revoked.

### Memory approval
Suggested long-term memory must not be saved until explicitly approved. Show creator/source, scope (personal/workspace), last reviewed and delete/revoke controls.

## Prototype-only behaviors
- Search result copy and grounded answers are fixture data.
- AI message submission inserts an explicitly illustrative answer and makes zero model calls.
- Sync, sharing, upload, research and budget controls display sample UI but do not persist or send data.
- Graph links represent sample relations only; layout is intentionally fixed.
- No authenticated user/session; sample people and source names are fictional.

## Production interface contracts to preserve
1. Keep FastAPI as the sole business/API integration boundary; no direct browser access to RushDB/Postgres/worker credentials.
2. Derive TS clients from OpenAPI and use TanStack Query for cacheable server state; segregate cache by `orgId`, `workspaceId`, and principal/session.
3. Use SSE with durable run IDs, reconnect/replay or state resync and idempotent client-side event application.
4. Enforce source and graph authorization in FastAPI with PostgreSQL grants, RushDB scoped filters and final citation revalidation.
5. Do not leak an unauthorized document via counts, edges, snippets, notifications, cached results, or AI context.
6. Track actual job status and immutable publication barriers from the backend; never label draft indexed data as ready.
7. Log only sanitized telemetry; honor tenant-approved LLM provider policy.
8. Do not use client-side visual toggles as permission checks.

## Priorities for current frontend replacement
- **P0:** Branded app shell and responsive routing; home; library; search; chat + citation inspector; loading/empty/error states; preserve existing API methods.
- **P1:** Jobs, integrations, graph navigation, workspace/organization selection, member permissions, usage/settings.
- **P2:** Research, user-approved memory, advanced source locators and fine-grained micro-interactions.
- Backend already under development: do not rebuild backend or fabricate APIs. UI ships behind feature flags if backend capability is incomplete.

## Accessibility and responsive quality gates
- WCAG 2.2 AA aspiration, including contrast checks on muted typography, visible keyboard focus, labelled buttons, semantic headers, accessible overlays and dialogs.
- Important: prototype dialogs/menus demonstrate interaction but are **not production accessibility-complete**; a Claude implementation must add focus trapping, return focus, ESC handling and appropriate ARIA roles.
- Support reduced motion, 360px narrow widths, 768px tablets, 1280/1440px desktop, and >1920px displays.
- Mobile side nav is a true drawer, not a scaled desktop layout. Graph gets a focusable list/detail alternative.
- Check screen readers for streamed content; avoid reading every single token as an announcement.

## Visual acceptance
- Use supplied brand SVG with the same blue/violet/teal layers; no infinity symbol, chat bubble, AI sparkles, generic networks.
- Calm, premium, primarily light-mode UI; deep indigo anchors typography, bright tones only for deliberate emphasis.
- Source provenance must be visible, not buried behind a modal on the main AI screen.
- Consistent controls, cards, typography, states and spacing across all screens.
