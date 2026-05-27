# Single-Server Multi-Site Deployment

This document explains how to run multiple isolated madRAG instances behind one host using a reverse proxy (nginx, Traefik, Kubernetes Ingress, …), with **one shared WebUI build** reused by every instance.

> Looking for the basic single-instance Docker setup? See [DockerDeployment.md](./DockerDeployment.md). For frontend build
> mechanics in general, see [FrontendBuildGuide.md](./FrontendBuildGuide.md).

---

## TL;DR

- Set `LIGHTRAG_API_PREFIX` per-instance, on the **backend only**. The WebUI is always mounted at `/webui` (not configurable).
- Build the WebUI **once**. The same artifacts work under any reverse-proxy prefix.
- Point your reverse proxy at each backend, stripping the site prefix before forwarding.

```bash
# One image, two containers, two prefixes — no rebuild.
docker run -e LIGHTRAG_API_PREFIX=/site01 -p 9621:9621 madrag:latest
docker run -e LIGHTRAG_API_PREFIX=/site02 -p 9622:9621 madrag:latest
```

---

## Why "build once, deploy many"

Earlier versions of madRAG baked the site prefix into the JavaScript bundle at build time (via `VITE_API_PREFIX` / `VITE_WEBUI_PREFIX`). Every site that used a different prefix needed its own WebUI build, and reusing a single Docker image across sites required a rebuild step at deploy time. Since the runtime-config-injection refactor:

- **Asset URLs** in `index.html` are emitted as relative paths (`./assets/index-abc.js`). The browser resolves them against the current document URL, so they work under any mount point.
- **API base URL** and **in-app links** read their prefix from `window.__LIGHTRAG_CONFIG__`, which the FastAPI server injects into `index.html` on each response based on its own `LIGHTRAG_API_PREFIX`.

The result: a single `madrag/api/webui/` directory (or Docker image) is reusable across any number of sites with no per-site build artifact.

---

## How runtime prefix injection works

Each request for `index.html` goes through `SmartStaticFiles` in `madrag/api/madrag_server.py`, which:

1. Reads the static `index.html` produced by `bun run build`.
2. Looks for the placeholder comment `<!-- __LIGHTRAG_RUNTIME_CONFIG__ -->`.
3. Replaces it with
   `<script>window.__LIGHTRAG_CONFIG__ = {"apiPrefix":"…","webuiPrefix":"…"}</script>`,
   computed from the configured `LIGHTRAG_API_PREFIX` (the in-app `/webui` mount is hardcoded server-side).

Sequence — browser request to a site-prefixed instance:

```
Browser            nginx                  uvicorn         SmartStaticFiles
  │                  │                       │                    │
  │ GET /site01/webui/                       │                    │
  │─────────────────►│                       │                    │
  │                  │ GET /webui/  (strips /site01)              │
  │                  │──────────────────────►│                    │
  │                  │                       │ get_response("")   │
  │                  │                       │───────────────────►│
  │                  │                       │                    │ inject
  │                  │                       │                    │ window.__LIGHTRAG_CONFIG__
  │                  │                       │                    │ = { apiPrefix: "/site01",
  │                  │                       │                    │ webuiPrefix: "/site01/webui/" }
  │                  │                       │◄───────────────────│
  │                  │◄──────────────────────│                    │
  │◄─────────────────│                       │                    │
  │ index.html with injected runtime config
```

The SPA reads the injected config via `src/lib/runtimeConfig.ts` and uses
it for `axios.baseURL`, `fetch()` template strings, the API-docs iframe,
and in-app links.

---

## One backend variable, that's it

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIGHTRAG_API_PREFIX` | `""` | Reverse-proxy mount prefix. The backend accepts both strip and verbatim forwarding — pick whichever fits your proxy stack. Passed to FastAPI as `root_path`. |

The WebUI is always mounted at `/webui` server-side. `window.__LIGHTRAG_CONFIG__.webuiPrefix` is computed as `LIGHTRAG_API_PREFIX + "/webui/"` and injected for the SPA — you do **not** set it yourself.

There are no longer any frontend `VITE_API_PREFIX` / `VITE_WEBUI_PREFIX` variables. Setting them has no effect (they are ignored by the build).

### Forwarding modes: strip and verbatim both work

After setting `LIGHTRAG_API_PREFIX=/site01`, the backend resolves all routes correctly under either forwarding style:

- **Strip** — proxy removes the prefix, backend sees `/webui/` and `/documents/foo`. The nginx example below uses this style.
- **Verbatim** — proxy forwards the request unchanged, backend sees `/site01/webui/` and `/site01/documents/foo`. The Vite dev flow ([Scenario 2](#scenario-2--simulate-a-site-prefix)) and any non-rewriting proxy use this style.

A small ASGI middleware in `create_app` prepends `root_path` to `scope["path"]` whenever the path does not already include it, so plain Routes and Mount sub-apps (the WebUI's `StaticFiles`) both resolve identically in either mode. You do not need to standardize on one — both coexist on the same backend without configuration toggles.

---

## End-to-end example: two sites behind one nginx

### Instance configuration

`site01.env`:
```bash
HOST=0.0.0.0
PORT=9621
LIGHTRAG_API_PREFIX=/site01
WORKING_DIR=/data/site01/storage
INPUT_DIR=/data/site01/inputs
LIGHTRAG_API_KEY=site01-secret
# … LLM / embedding config …
```

`site02.env`:
```bash
HOST=0.0.0.0
PORT=9621
LIGHTRAG_API_PREFIX=/site02
WORKING_DIR=/data/site02/storage
INPUT_DIR=/data/site02/inputs
LIGHTRAG_API_KEY=site02-secret
# … LLM / embedding config …
```

### docker-compose.yml (one image, two services)

```yaml
services:
  site01:
    image: ghcr.io/hkuds/madrag:latest
    env_file: site01.env
    volumes:
      - ./data/site01:/data/site01
    ports:
      - "127.0.0.1:9621:9621"

  site02:
    image: ghcr.io/hkuds/madrag:latest
    env_file: site02.env
    volumes:
      - ./data/site02:/data/site02
    ports:
      - "127.0.0.1:9622:9621"
```

### nginx config

```nginx
server {
    listen 443 ssl http2;
    server_name host.example.com;

    # site01: strips /site01/ before forwarding
    location /site01/ {
        proxy_pass http://127.0.0.1:9621/;
        proxy_set_header X-Forwarded-Prefix /site01;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    # site02: strips /site02/ before forwarding
    location /site02/ {
        proxy_pass http://127.0.0.1:9622/;
        proxy_set_header X-Forwarded-Prefix /site02;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

Browsing `https://host.example.com/site01/webui/` shows site01's WebUI; `https://host.example.com/site02/webui/` shows site02's. The same Docker image serves both — no per-site build artifact, no rebuild on prefix changes.

### What each layer sees

| Layer | site01 GET /webui/ |
| --- | --- |
| Browser address bar | `https://host.example.com/site01/webui/` |
| nginx receives | `/site01/webui/` |
| nginx forwards | `/webui/` |
| FastAPI `root_path` | `/site01` |
| `app.mount` resolves | `/webui/` |
| Injected `apiPrefix` | `/site01` |
| Injected `webuiPrefix` | `/site01/webui/` |
| Asset URLs in HTML | `./assets/index-abc.js` (resolves to `https://host.example.com/site01/webui/assets/index-abc.js`) |

---

## Single-image Docker recipe

The `Dockerfile` builds the WebUI once, with no prefix:

```dockerfile
FROM oven/bun:1 AS webui-build
WORKDIR /src/madrag_webui
COPY madrag_webui/package.json madrag_webui/bun.lock ./
RUN bun install --frozen-lockfile
COPY madrag_webui/ ./
COPY madrag/api/webui/.gitkeep /src/madrag/api/webui/.gitkeep
RUN bun run build

FROM python:3.11-slim
COPY --from=webui-build /src/madrag/api/webui /app/madrag/api/webui
# … rest of the image …
```

Run any number of containers from the same image, each with its own prefix:

```bash
# Plain single-instance, no prefix.
docker run --rm -p 9621:9621 madrag:latest

# Same image, different prefixes — runtime decides.
docker run --rm -e LIGHTRAG_API_PREFIX=/site01 -p 9621:9621 madrag:latest
docker run --rm -e LIGHTRAG_API_PREFIX=/site02 -p 9622:9621 madrag:latest
```

### Kubernetes Ingress equivalent

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: madrag-multisite
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /$2
spec:
  rules:
  - host: host.example.com
    http:
      paths:
      - path: /site01(/|$)(.*)
        pathType: ImplementationSpecific
        backend:
          service:
            name: madrag-site01
            port: { number: 9621 }
      - path: /site02(/|$)(.*)
        pathType: ImplementationSpecific
        backend:
          service:
            name: madrag-site02
            port: { number: 9621 }
```

Backends still set `LIGHTRAG_API_PREFIX=/site01` / `=/site02`.

---

## Local development with `bun run dev`

> **Always open `http://localhost:5173/` — root path, no `/webui`, no `/site01` — regardless of which scenario below you're in.**
>
> Vite's dev server serves the SPA at its own root (`/`) no matter what prefix you configure. `VITE_DEV_API_PREFIX` only affects how the SPA composes API URLs *after* the page is loaded, and which paths the dev proxy intercepts; it does **not** change the URL you type in the address bar. Trying to access `localhost:5173/site01/webui/` works (Vite's SPA fallback returns the same `index.html`), but it's not the canonical entry point and only differs cosmetically in the address bar.
>
> This is the deliberate consequence of `base: './'` in [`vite.config.ts`](../madrag_webui/vite.config.ts) — the same setting that makes one production build reusable across any number of reverse-proxy mount points. Tying the dev URL to a prefix would force the build to bake the prefix back in.

The dev server mirrors production injection: it serves `index.html` via the same `transformIndexHtml` mechanism the FastAPI server uses at request time, so the SPA reads `window.__LIGHTRAG_CONFIG__` in dev exactly the way it does in prod. Only **two** environment variables matter:

| Variable | Purpose | Where it lives |
| --- | --- | --- |
| `VITE_BACKEND_URL` | Where the dev server forwards proxied API calls. | `madrag_webui/.env*` |
| `VITE_DEV_API_PREFIX` | Prefix to **simulate** (matches the backend LIGHTRAG_API_PREFIX`). Empty → no prefix. | `madrag_webui/.env*` |

`VITE_DEV_API_PREFIX` injects `apiPrefix` into `window.__LIGHTRAG_CONFIG__` in the browser, mirroring the backend behavior. It also serves as a prefix for `VITE_API_ENDPOINTS`, ensuring correct access to backend APIs. The matching `webuiPrefix` is derived as `${VITE_DEV_API_PREFIX}/webui/` automatically — you don't need a separate variable for it.

Three scenarios cover everything you'll hit:

### Scenario 1 — single-instance dev (no prefix, no proxy)

The default. Don't set anything beyond the existing `.env.development`.

```
Browser ──► localhost:5173 (Vite) ──► localhost:9621 (backend, no prefix)
```

```bash
# madrag_webui/.env.development (already in repo as sample)
VITE_BACKEND_URL=http://localhost:9621
VITE_API_PROXY=true
VITE_API_ENDPOINTS=/api,/documents,/graphs,/graph,/health,/query,/docs,/redoc,/openapi.json,/login,/auth-status,/static
# VITE_DEV_API_PREFIX=          ← leave empty
```

Run:
```bash
madrag-server                  # in one terminal, no LIGHTRAG_API_PREFIX
cd madrag_webui && bun run dev # in another; open http://localhost:5173/
```

### Scenario 2 — simulate a site prefix

You want the SPA to run under `/site01` (or whatever production prefix). Set `VITE_DEV_API_PREFIX=/site01`. Vite injects the matching `window.__LIGHTRAG_CONFIG__` and registers prefixed proxy keys; SPA requests like `fetch("/site01/documents/foo")` are forwarded verbatim to whatever `VITE_BACKEND_URL` points at. The upstream — local backend or production nginx — is responsible for understanding the prefix.

```
Browser ──► localhost:5173 (Vite + HMR)
                │
                │  Vite proxy forwards /site01/* verbatim, no rewrite
                ▼
            VITE_BACKEND_URL  ──►  upstream that knows /site01
```

`.env.local` (gitignored — your personal dev config):
```bash
VITE_BACKEND_URL=…                             # see "Where to point VITE_BACKEND_URL" below
VITE_API_PROXY=true
VITE_API_ENDPOINTS=/api,/documents,/graphs,/graph,/health,/query,/docs,/redoc,/openapi.json,/login,/auth-status,/static
VITE_DEV_API_PREFIX=/site01
```

Run `bun run dev` and open **`http://localhost:5173/`**. HMR is purely local — the browser only talks to `localhost:5173` for SPA assets, no WebSocket-upgrade config needed on any upstream.

#### Where to point `VITE_BACKEND_URL`

Two options, picked by where the prefix-aware upstream lives. The Vite-side configuration is identical; only this one variable changes.

**A. Local backend with `LIGHTRAG_API_PREFIX=/site01`** (no nginx anywhere) — the simplest setup, two processes on your laptop. Vite's proxy itself plays the role of the reverse proxy.

```bash
VITE_BACKEND_URL=http://localhost:9621
```
```bash
# Terminal 1
LIGHTRAG_API_PREFIX=/site01 madrag-server
# Terminal 2
cd madrag_webui && bun run dev
```

The backend's FastAPI `root_path=/site01` accepts the prefixed form natively (Starlette's `get_route_path()` strips `root_path` from the request path before matching), so no extra rewriting is needed on either side.

**B. Real (remote) backend reached through its production nginx** — useful when the actual backend has data / configs that are painful to reproduce locally. nginx already strips `/site01/` before forwarding to the backend; the dev frontend benefits without changing anything in production.

```bash
VITE_BACKEND_URL=https://prod.example.com      # or http://10.0.0.5 — the nginx URL
```

The production nginx and backend stay exactly as they are. The flow becomes:

```
SPA fetch /site01/documents/foo
  → Vite forwards to https://prod.example.com/site01/documents/foo
  → nginx matches /site01/, strips it, forwards /documents/foo to backend
  → backend serves it
```

#### Why `VITE_BACKEND_URL` does **not** include `/site01`

Vite forwards the request path **verbatim** (no rewrite). The browser already emits `/site01/documents/foo`, so the URL Vite sends upstream is `${VITE_BACKEND_URL}/site01/documents/foo`. If you set `VITE_BACKEND_URL=https://prod.example.com/site01` you would get `https://prod.example.com/site01/site01/documents/foo` — a duplicated prefix that both nginx and the backend reject. Always point `VITE_BACKEND_URL` at the upstream **root**.

#### Common pitfalls (mostly relevant to option B)

- **HTTPS upstream + self-signed cert**: Vite's proxy rejects by default. Set `proxy: { ..., secure: false }` in `vite.config.ts` to skip cert validation when targeting a staging proxy with a non-public cert.
- **Auth required**: if the upstream requires `LIGHTRAG_API_KEY`, log in via the dev SPA exactly as you would in prod — the auth token flows through the proxy unchanged.
- **CORS errors**: shouldn't happen because the browser sees same-origin requests to `localhost:5173`. If they appear, check that `changeOrigin: true` is in effect (it is, by default in `vite.config.ts`).

### Quick decision matrix

| Scenario | `VITE_BACKEND_URL` | `VITE_DEV_API_PREFIX` | Upstream the dev proxy talks to | Open in browser |
| --- | --- | --- | --- | --- |
| 1. Default single-instance dev | `http://localhost:9621` | unset | local backend, no prefix | `http://localhost:5173/` |
| 2A. Simulate a prefix locally (no nginx) | `http://localhost:9621` | `/site01` | local backend with `LIGHTRAG_API_PREFIX=/site01` | `http://localhost:5173/` |
| 2B. Hit a real backend through its production nginx | `https://prod.example.com` | `/site01` | remote nginx that already strips `/site01/` | `http://localhost:5173/` |

Rows 2A and 2B share **everything except `VITE_BACKEND_URL`** — the choice is purely "is the prefix-aware upstream on my laptop or in production?".

**The "Open in browser" column is always `http://localhost:5173/` — that is the entry point in every dev scenario.** What changes between rows is where the API traffic ultimately lands; the SPA itself is always served from the dev server's root.

---

## Troubleshooting

### Asset URLs 404 when accessing the WebUI

The base URL must end with `/`. Accessing `/site01/webui` (no trailing slash) makes the browser resolve `./assets/foo.js` against `/site01/`, which 404s. The server already redirects the no-slash form to the
slash form; verify the redirect is reaching nginx (check `X-Forwarded-Prefix` and that nginx uses `proxy_pass http://…/` with the trailing slash).

### `apiPrefix` is empty in `window.__LIGHTRAG_CONFIG__` after deploy

View the page source. If you see the literal placeholder `<!-- __LIGHTRAG_RUNTIME_CONFIG__ -->` instead of an injected `<script>` tag, the request did not go through `SmartStaticFiles` — double-check that `madrag/api/webui/index.html` exists in the running container and that the WebUI mount succeeded (the server logs `WebUI assets mounted at <path>` at startup).

### `bun run dev` proxy returns 404 with `VITE_DEV_API_PREFIX` set

Confirm the backend is also running with the matching `LIGHTRAG_API_PREFIX`. The dev proxy forwards prefixed paths verbatim; if the backend has no prefix configured, it does not register routes under that path.

### I want to disable the WebUI entirely

Don't build the frontend — `madrag/api/webui/index.html` will not exist and the server will skip the WebUI mount, redirecting `/` and the WebUI path to `/docs` instead. The runtime-config injection is purely opt-in via the existence of the build artifact.
