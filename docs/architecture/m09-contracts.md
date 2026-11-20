# M09 frontend redesign: migration plan (F0 output)

Scope: refactor `apps/web` to the Intello reference UI. No route renames, no backend contract breaks, no new dependencies unless listed. Capability matrix: `docs/execution/FRONTEND_REDESIGN_STATUS.md`.

## Inputs still missing
The user supplies `design-reference/` (index.html, `assets/intello-prism.svg`, `intello-lockup-light.svg`, `intello-lockup-dark.svg`, docs copy). F1 needs the SVGs (place in `apps/web/public/brand/`) and index.html for pixel decisions (spacing, nav grouping, Home layout). Without them F1 ships token-only styling and a placeholder lockup marked TODO. Do not redraw or recolor the logo.

## Shared contract files (ONLY the shell agent edits, F1, then frozen)
`apps/web/src/app/globals.css`, `app/layout.tsx`, `app/(app)/layout.tsx`, `app/(app)/o/[orgSlug]/layout.tsx`, `features/shell/*`, `features/workspaces/components/workspace-sidebar.tsx` (becomes SidebarNav), `features/organizations/components/org-switcher.tsx`, `features/palette/command-palette.tsx` (chrome), `components/ui/*`, `lib/utils.ts`, `lib/query/*`, `components.json`, `next.config.ts`, `package.json`, `src/proxy.ts`, `features/theme/*`. Tracks needing a change leave a note in AGENT_HANDOFFS.md for the shell owner and never edit these.

## Design-token plan (F1)
- Add CSS variables to `globals.css` from DESIGN_TOKENS.md: indigo `#0B1B4F`, blue `#2563EB`, violet `#7C3AED`, teal `#14B8A6`, canvas `#F8FAFC`, card `#FFF`, text `#182342`, secondary `#748098`, border `#E8ECF4`. Map them onto the existing shadcn variables (`--primary`=indigo, `--ring`=blue, ...) so current `components/ui/*` and features restyle without edits; keep `@theme inline` names stable.
- `#748098` on `#F8FAFC` is about 3.6:1 (fails AA for body text): use a darker secondary (about `#5B6680`, verify >= 4.5:1) for text; keep `#748098` for icons only.
- Light mode is primary. The existing `.dark` block and theme toggle keep working but are not redesigned; the shell owner may hide the toggle if dark is unpolished.
- Font: Inter is not installed and `globals.css` deliberately uses system stacks so offline builds work. Keep the system stack with `Inter` first in the list (used if installed locally). `next/font/google` needs network at build: rejected. `@fontsource-variable/inter` is a new dependency: add only with user approval (question 2); otherwise note the fallback.
- Layout tokens: sidebar 254px (218px medium, drawer <= 720px), topbar 69/61px, content padding 42/28/17px, radius 16 cards / 9-11 controls, 8px grid.
- Motion: 120-180ms hover, 180-260ms panels; all non-essential motion behind `motion-safe:` plus a global `prefers-reduced-motion: reduce` reset.
- New shared primitives (shell, F1) from the inventory: `BrandLockup`, `AppShell`, `SidebarNav`, `Topbar`, `PageHeader`, `Eyebrow`, `SurfaceCard`, `MetricCard`, `StatusBadge`, `ScopeBadge`, `EmptyState`, `ErrorState`, `OfflineBanner`, `Drawer`, `Toast`, `SearchFilterTabs` base, in `components/ui/` and `components/shell/`. Feature components (`CitationChip`, `EvidencePanel`, `GraphInspector`, `MemberRow`, ...) live in the owning track's feature dir.

## Phases and file checklist
**F1 Foundation (shell agent, solo, first).** Edit: `globals.css`, `app/layout.tsx` (title "Intello", metadata, icons), `app/(app)/o/[orgSlug]/layout.tsx` (AppShell composition), `features/shell/app-header.tsx` -> Topbar (breadcrumb, command trigger, avatar menu with sign-out), `workspace-sidebar.tsx` -> SidebarNav (Home, Search, Ask, Library, Graph, Research, Memory; Integrations, Activity, People, Settings only when their routes exist), `components/ui/*` restyle, `features/shell/states.tsx`. New: brand assets, primitives above, `features/shell/nav.ts` (route flags). Keep: `aria-current`, skip link, `RouteFocus`, existing `data-testid`s. Gate: `npm test`, typecheck, lint, existing `tests/e2e/web_*` green.
**F2 Core journey (tracks A and B parallel).** A (Search+Library): `features/search/*`, `features/documents/**`, search and documents routes, `w/[ws]/page.tsx`, `o/[org]/page.tsx` + `workspace-overview.tsx` (Home from existing lists; metric tiles only when the aggregate endpoint exists, no fake numbers). B (Chat+Evidence): `features/chat/*`, `features/evidence/*`, `chat/**` routes; persistent 345px evidence panel driven by citation selection (stacks below on small screens); `use-chat-run`/`sse` logic untouched.
**F3 Secondary views (track D).** New: `features/connectors/` (client + list/sync/revoke UI for `/sources`), `features/members/` (org + workspace members list/role/remove), `features/settings/` (ai-policy form, rename), `features/activity/` (what exists now; full list gated on gap 2). New routes `/o/[org]/w/[ws]/integrations`, `/o/[org]/members`, `/o/[org]/settings`, `/o/[org]/w/[ws]/activity`; nav flags flipped by the shell owner in `nav.ts`. Hide or disable anything without an endpoint.
**F4 Intelligence (track C).** Reskin `features/graph`, `features/research`, `features/memory` and routes onto the primitives; graph keyboard list/detail fallback; "ask about concept" and "follow up in chat" as plain links (chat reading `?q=` is B's file: coordinate by note). No invented API.
**F5 A11y and QA (all).** Focus trap/return/ESC per dialog and drawer (base-ui covers most; verify), reduced motion, contrast, 360/390/768/1280/1440 checks, mobile nav tests; keep SSE reconnect and citation selection tests green.

## Parallel tracks (exclusive directories after F1)
| Track | Owns |
|---|---|
| A Search+Library | `features/search`, `features/documents`, `features/palette/model.ts`, routes `search`, `w/[ws]/documents`, `w/[ws]/page.tsx`, `o/[org]/page.tsx`, `workspace-overview.tsx` |
| B Chat+Evidence | `features/chat`, `features/evidence`, routes `chat/**` |
| C Graph+Research+Memory | `features/graph`, `features/research`, `features/memory`, routes `w/[ws]/graph`, `research/**`, `memory` |
| D Org/Team/Settings/Integrations/Activity | `features/organizations` (except org-switcher), new `features/{connectors,members,settings,activity}` and their new routes |
| Shell | the shared contract files above |
Branches `agent/m09-<track>`; merge order F1, then A/B/D, then C; coordinator runs the full suite once.

## Backend gaps (all "needs backend work"; additive, read-only, no contract breaks)
1. Home metrics: `GET {W}/overview` (document counts by ingestion state, sources, pending memories, research sessions; permission-filtered, never counts of unauthorized docs). Optional: Home ships without tiles.
2. Jobs list: `GET {W}/ingestion/jobs?state=&cursor=` plus counts by state, authoritative from PostgreSQL. Interim: merge per-document `ingestion` summaries from the documents list.
3. Usage summary: `GET {O}/usage` (tokens/requests/storage per period); needs a metering source. If none, Usage stays a documented disabled panel.
4. Invitations: no endpoint (member PUT needs an existing `user_id`); invite dialog blocked. Not small (touches identity): defer unless the user asks.
5. Exist but unwired in the UI: `/sources`, org `/members`, `/ai-policy`, graph merge-candidates.
6. Not planned: chat feedback, notifications, related concepts (derive from graph `/entities`).

## Test plan (lean)
- Component tests with existing vitest + Testing Library: each new/reskinned component gets render, role-aware state, loading/empty/error and keyboard checks. A11y by asserting roles, labels, `aria-current`, dialog focus return; no new axe dependency without approval.
- Shell: nav active state, mobile drawer open/close/Esc/focus return, skip link.
- One HTTP e2e per track in the existing `tests/e2e/web_*` style: A search then open document; B ask, citation, evidence panel; C graph/memory/research load with real API data; D members list + AI-policy save (+ sources list when a fixture exists).
- Tracks run only their own suites; `TEST_DEBT.md` untouched by F0.

## Prototype addendum (design-reference merged; per-view findings in the status doc)
- Brand assets exist: the shell agent copies `design-reference/assets/intello-{prism,lockup-light,lockup-dark,favicon-monochrome}.svg` unchanged to `apps/web/public/brand/` in F1; favicon from the monochrome SVG. This closes the "inputs missing" item; pixel decisions are read from `design-reference/index.html`.
- Prototype tokens match DESIGN_TOKENS.md (`--navy, --ink, --muted, --line, --blue, --violet, --teal, --bg #f8fafc, --r 16px, --sidebar 254px`). It loads Inter from Google Fonts: not acceptable for a local-first build/runtime; keep the system stack (decision pending, question 2).
- Nav is two groups, Workspace (7 views) and Manage (4 views, count badge on Activity); `features/shell/nav.ts` encodes exactly this with per-item enabled flags. Omit or disable, never mock: storage meter, notifications bell, "+ Collection", graph "Evolution", research "Shared", "Sync all", Invite member, Usage/Notifications settings tabs.
- Metric cards on Home/Library/Activity map to backend gaps 1 and 2; "trusted answers" and "connected concepts" have no data source and are dropped until one exists.
- Prototype modals become base-ui dialogs (focus trap, Esc, focus return).

## Risks
Shell markup changes can break e2e selectors and `data-testid`s (restyle via variables first). Spec nav is flat but Library, Graph, Integrations and Activity are workspace-scoped: the shell needs a workspace switcher and a default workspace.
