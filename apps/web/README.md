# ukos-web

Next.js (App Router, TypeScript strict, Tailwind CSS v4, shadcn/ui) starter for Universal Knowledge OS
(M00.02.05).

```sh
npm ci
npm run lint && npm run typecheck && npm test && npm run build
npm run dev            # http://localhost:3000
```

- Health probe: `GET /api/health` → `{"status":"ok"}`.
- UI components: `npx shadcn@latest add <component>` (config in `components.json`).
- Container image: `Dockerfile` (standalone output, runs `node server.js` on port 3000).
