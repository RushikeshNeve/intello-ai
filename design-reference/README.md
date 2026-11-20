# Intello — Complete SaaS Product UI/UX Mockup

This is the **authenticated product application**, not Intello's marketing landing page. It covers 11 view areas with representative sample interactions, deliberately separated from the live implementation being developed by Claude.

## Open
Open `index.html` in a browser, or serve this directory as a static site. All demo UI runs locally without a backend. Font loading may require an internet connection; system sans-serif fallback is included.

## What's included
- `index.html` — desktop/mobile clickable demo including home, search, chat/citations, library, graph, research, memory, integrations, processing, people/access, settings.
- `assets/*.svg` — approved-direction Intello prism and light/dark logos; prior vector reconstruction.
- `docs/PRODUCT_UI_SPEC.md` — screen spec, states, interactions, backend/data constraints.
- `docs/DESIGN_TOKENS.md` — visual language and component inventory.
- `docs/CLAUDE_FRONTEND_HANDOFF.md` — copy-paste Claude Code prompt for integrating into existing Next.js app.

## Important
Everything displayed in this prototype is fictional design data. The prototype is not connected to FastAPI, PostgreSQL, RushDB, AI inference, authentication, external connectors or file uploads. It should not be shipped as-is. Production implementation requires backend integration, accessibility improvement, actual API/error/permission state coverage and browser-based visual QA.

## Quick try
1. Click **Ask Intello** to see a sourced answer and source-evidence panel.
2. Click citations `1`/`2`/`3` to change the supporting evidence.
3. Click **Knowledge Graph**, then a node to inspect evidence-linked relationships.
4. Open **Knowledge Library**, filter source types and inspect source previews.
5. Open **Knowledge Memory** and approve or dismiss suggestions.
6. Use **Ctrl/Cmd + K** for the command palette.
7. At mobile width, open the menu button to navigate.

See `docs/CLAUDE_FRONTEND_HANDOFF.md` for a phased, low-regression implementation strategy.
