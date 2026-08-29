# Signal Ledger frontend

A plain Vite + React SPA (no Next.js, no Cloudflare, no ChatGPT auth). It
talks to the Flask JSON API in `live_app/server.py` and is built straight
into `live_app/static/`, which Flask serves as static files -- one service,
one URL, one login.

## Building it

```
cd live_app/frontend
npm install
npm run build
```

That writes `index.html` and hashed `assets/*` into `../static/`, replacing
whatever was there. **Commit `live_app/static/` after building** -- Render's
build step only runs `pip install -r requirements.txt` (see `render.yaml`),
it does not run Node, so the built static files have to already be in the
repo for a deploy to serve the current frontend. `node_modules/` itself is
gitignored; only the source and the build output are committed.

## Local development

```
npm run dev
```

Runs Vite's dev server with hot reload on port 5173, proxying `/api/*` to
`http://127.0.0.1:5000` (see `vite.config.ts`) -- run the Flask app
(`python -m live_app.server`) alongside it for a live backend during
frontend work. `npm run build` is what actually ships, though -- always
rebuild before committing a frontend change.

## Checking types

`npm run build` alone does not type-check (Vite's build only transpiles).
Run `npx tsc --noEmit` to catch TypeScript errors before committing.
