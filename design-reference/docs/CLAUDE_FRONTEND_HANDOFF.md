# Claude Code — Frontend Redesign Handoff Prompt

> Attach the complete `Intello_Product_UIUX_Mockup.zip` to Claude Code, or extract it inside the existing repository in a `design-reference/` folder. Work in the **existing codebase**; do not initialize a replacement app.

## Prompt to paste into Claude

You are the senior staff frontend engineer, product designer, and integration lead implementing **Intello**, a premium AI Knowledge Intelligence SaaS. We have already built a FastAPI backend and basic Next.js frontend. Your task is to **refactor the existing frontend** to match the attached Intello complete product UI/UX prototype, not to reimplement the backend or create a disconnected fake website.

### Read first
1. `design-reference/index.html` — visual and interactive reference for 11 application areas.
2. `design-reference/docs/PRODUCT_UI_SPEC.md` — precise screens, interaction semantics, requirements and backend boundaries.
3. `design-reference/docs/DESIGN_TOKENS.md` — approved design tokens and component inventory.
4. `design-reference/assets/intello-prism.svg` and the other logo SVG variants — canonical logo direction.
5. Existing frontend and backend source, project `MASTER_ARCHITECTURE.md`, ADRs and existing API contracts.

### Non-negotiable rules
- Preserve existing backend behavior and all functioning API endpoints, auth session semantics, types, routes, migrations and tests.
- Do NOT wipe, rewrite, or replace a working feature merely because a prototype shows mock data.
- Do NOT create a second application or a parallel fake data layer as the primary UI.
- Audit actual endpoints/DTOs before hooking up each screen. Maintain a capability matrix: implemented backend / UI wired / placeholder / blocked.
- Use Next.js App Router + TypeScript, Tailwind CSS, accessible headless UI primitives and TanStack Query as already selected.
- Translate the prototype into composable app components, properly typed hooks, loading/error/empty states, keyboard/focus support and responsive behavior. Do not copy the monolithic HTML architecture into production.
- Retain current functioning features; feature-flag or clearly mark demo-only UI for capabilities the backend does not yet support.
- Preserve approved Intello prism SVG, colors (`#0B1B4F`, `#2563EB`, `#7C3AED`, `#14B8A6`), and no unrelated dark-mode-only redesign.
- UI organization/workspace controls must not bypass server authorization. Search, citations, graph edges, and memory must respect documented permission boundaries.
- Add automated tests where appropriate: components, core flows, mobile navigation, role-aware states, SSE reconnect and citation selection.

### Implementation order
**Phase F0 — Frontend audit & migration plan**: Map current routes, API clients, auth, UI components and working features to the 11 reference views. Identify what remains mock only. Create a file-by-file migration checklist. Avoid disruptive global changes without tests.

**Phase F1 — Design foundation**: Establish CSS variables/design tokens, typography, shared primitives, logo assets, branded shell, sidebar and responsive topbar. Verify no existing navigation regression.

**Phase F2 — Core knowledge journey**: Home, library, upload, search, chat composer, grounded answer rendering, citation/source inspector; integrate current APIs and existing state/query providers.

**Phase F3 — Secondary views**: Job statuses, source connectors, organizations/workspaces, member roles, usage and settings. Wire real endpoints only when available.

**Phase F4 — Intelligence surfaces**: Permission-safe contextual knowledge graph, research studio, approved memory. For missing backend capabilities, construct tested presentational components and honest disabled/mock states without inventing API responses.

**Phase F5 — Accessibility & QA**: Focus traps, keyboard navigation, screen-reader semantics, reduced motion, contrast, 360/390/768/1280/1440 responsive checks, snapshot/screenshots, core E2E and integration tests.

### Parallel-agent strategy
- Divide each phase into small tasks with exclusive file/directory ownership.
- The app-shell/design-tokens agent must finish shared contracts before concurrent feature agents start work; never allow competing edits to globals, `layout.tsx`, base CSS, generated API client or shared config.
- Delegate independently to Search/Library, Chat/Evidence, Graph/Research, and Settings/Org agent tracks once shared contracts are stable.
- Use separate branches/worktrees, explicit test evidence and coordinator-reviewed merges; don't claim parallel work unless agents were actually launched.

### Output and progress
- Maintain `docs/execution/FRONTEND_REDESIGN_STATUS.md`: view, backend status, component owner, implementation status, blocked dependencies, tests, screenshots.
- On each slice: implement → run tests → verify no regression → integrate → update status → pick next ready task.
- Do not stop after producing another design plan. Start with the repository audit and then execute the first safe frontend slice.
- Never claim a feature is integrated, accessible, or production-ready without verification.

### Product experience to retain
The brand story is **Many Sources → Understanding → One Intelligence**. The dashboard invites questions, hybrid search discovers authorized knowledge, AI answers have inspectable citations, and users can traverse linked evidence into contextual knowledge. Make it feel calm, precise, and built for real organizations—not an AI chatbot template.
