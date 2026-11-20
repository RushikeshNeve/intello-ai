# Intello — Design Tokens & Component Library

## Brand foundation (approved)
| Token | Value | Use |
|---|---|---|
| `brand.indigo` | `#0B1B4F` | Headlines, primary CTA, dark surfaces |
| `brand.blue` | `#2563EB` | Interaction, source links, active nav |
| `brand.violet` | `#7C3AED` | Synthesis/accent, memory and graph |
| `brand.teal` | `#14B8A6` | Verified understanding, success and knowledge core |
| `surface.canvas` | `#F8FAFC` | App background |
| `surface.card` | `#FFFFFF` | Cards, reading, sheets |
| `text.primary` | `#182342` | Body copy |
| `text.secondary` | `#748098` | Supporting context; verify contrast for AA |
| `border.subtle` | `#E8ECF4` | Separators, surfaces |

**Brand SVG files:** `assets/intello-prism.svg`, `assets/intello-lockup-light.svg`, `assets/intello-lockup-dark.svg`. These are the previously recreated approved-direction vectors. Do not recolor or replace them without approval. For the production system, prefer custom outlined wordmark from a master file if available.

## Typography
- Family: Inter (with system sans fallbacks), variable / 400–800.
- Page title: 26–40px, weight 700, tight tracking ~-.045em.
- Section heading: 16–20px, weight 650–750.
- Card title: 12–15px, weight 650–700.
- Body: 12–14px in this demo; production body text 14–16px where possible for readability.
- Supporting metadata: 10–12px, use sparingly.
- Ellipsis/overflow only where a full title can still be recovered via accessible tooltip or detail screen.

## Layout
- 8px base spacing system (4/8/12/16/24/32/40/48).
- Sidebar: 254px desktop; 218px medium; drawer at 720px and below.
- Top bar: 69px desktop, 61px mobile.
- Content padding: 42px desktop, 28px tablet, 17px mobile.
- Cards: border radius 16px; buttons/input 9–11px.
- Depth: sparse shadows, high reliance on subtle borders and grouped whitespace.
- Chat workspace: flexible main panel + 345px evidence panel; stack on small tablet/mobile.

## Component inventory
- `AppShell`, `BrandLockup`, `OrganizationSwitcher`, `WorkspaceSwitcher`, `SidebarNav`, `Topbar`, `CommandPalette`.
- `PageHeader`, `Eyebrow`, `MetricCard`, `SurfaceCard`, `StatusBadge`, `ScopeBadge`.
- `UniversalSearchInput`, `SearchFilterTabs`, `SearchResultRow`, `ConceptTag`.
- `QuestionComposer`, `MessageBubble`, `GroundedAnswer`, `CitationChip`, `EvidencePanel`, `SourceLocator`.
- `DocumentList`, `FileTypeBadge`, `SourcePreview`, `UploadDropzone`, `VersionHistory`.
- `GraphCanvas`, `GraphNode`, `GraphRelationship`, `GraphInspector`, `GraphListFallback`.
- `ResearchBriefCard`, `ResearchComposer`, `ReportViewer`.
- `MemorySuggestionCard`, `MemoryApprovalActions`, `ApprovedMemoryCard`.
- `IntegrationCard`, `SyncStatus`, `JobProgress`, `PipelineCheckpointTimeline`.
- `MemberRow`, `GrantEditor`, `InviteDialog`, `SettingsGroup`, `PolicyToggle`, `UsageMeter`.
- `Skeleton`, `EmptyState`, `ErrorState`, `OfflineBanner`, `Toast`, `Modal`, `Drawer`.

## Motion language
- Quick hover/focus: 120–180ms; panel transitions: 180–260ms.
- Motion is semantic: relevant knowledge comes together; citations reveal evidence; processing progresses through distinct checkpoints.
- No floating particles, parallax overload, or synthetic AI sparkle indicators.
- All nonessential motion disabled under `prefers-reduced-motion`.

## Data/state boundaries
- Server: TanStack Query scoped by org/workspace/principal; generated OpenAPI types.
- UI: component state or small Zustand store for panel layout/selection only.
- SSE: separate run stream controller with event IDs, replay/merge, and final state refetch.
- Forms: accessible validation and explicit user confirmation for sensitive actions.

## Frontend artifacts
`index.html` contains the screens, overall styling and demo-only interactions. Treat as visual / interaction design reference, NOT a suggested Next.js architecture. Split its concerns into maintainable components when implementing.
