# Public tunnel

The research UI is local. This tunnel publishes **only** the web app, so a browser on another network can open it.

Start and shutdown:

```bash
./scripts/tunnel.sh start
./scripts/tunnel.sh status
./scripts/tunnel.sh stop
```

`start` prints the public URL and writes the same address to `data/tunnel/PUBLIC_URL`. A Cloudflare quick tunnel gets a **new** `*.trycloudflare.com` hostname every time it starts. Use the URL from the latest start. The old one stops working.

## What `start` brings up

`./scripts/tunnel.sh start` is safe to run again. If a piece is already answering, it leaves that piece alone.

| Piece | Where it listens | What it is |
|---|---|---|
| SearXNG | `127.0.0.1:8080` | Search. Started from `docker-compose.searxng.yml` only when `SEARXNG_BASE_URL` is `http://localhost:8080` or `http://127.0.0.1:8080` and nothing is answering there. |
| API | `127.0.0.1:8000` | `uv run uvicorn api.app:app`. Reads `.env`. |
| Web UI | `127.0.0.1:5173` | Vite. The browser talks to this origin. `/api` is proxied to the API. |
| Cloudflare tunnel | public HTTPS | `cloudflared tunnel --url http://127.0.0.1:5173`. |

The tunnel forwards to Vite, not to SearXNG and not to the API port. SearXNG stays on the machine. The browser never calls it directly.

```text
browser
  → https://<random>.trycloudflare.com
    → cloudflared on this machine
      → Vite  127.0.0.1:5173
        → /api proxied to FastAPI  127.0.0.1:8000
          → SearXNG  127.0.0.1:8080
          → Ollama API from .env (OLLAMA_BASE_URL)
```

Vite must accept the tunnel hostname. `web/vite.config.ts` sets `server.allowedHosts: true` for that. Without it, the public page is Vite's "host is not allowed" error.

## What `stop` shuts down

```bash
./scripts/tunnel.sh stop
```

This stops, in order:

1. The Cloudflare tunnel whose target is `http://127.0.0.1:5173`
2. The Vite process for this repo on port 5173
3. The FastAPI process for this repo on port 8000 (`api.app:app`)

It removes `data/tunnel/PUBLIC_URL`. It does **not** stop SearXNG. That container is also used as the agent's search backend and is set to `restart: unless-stopped`.

To stop that SearXNG container as well:

```bash
./scripts/tunnel.sh stop --with-searxng
```

`stop` only kills processes it can identify as this stack: the tunnel command above, a listener on 5173 whose working directory is `web/` or whose command line is Vite, and a listener on 8000 whose command line contains `api.app:app`. The separate research-stack containers (`research-app` on `127.0.0.1:8001`, `research-searxng` on `127.0.0.1:8081`, Qdrant, the Docker Ollama, Valkey) are not part of this tunnel and are left running.

## Status

```bash
./scripts/tunnel.sh status
```

Example while everything is up:

```text
API          up
web          up
tunnel       up (pid 12345)
public       https://something.trycloudflare.com
searxng      up (http://localhost:8080)
```

`public` is `(none — start the tunnel)` after a shutdown.

## Before the first start

From the repo root:

```bash
uv sync
cp .env.example .env          # then edit .env; never commit it
cd web && npm install
```

`.env` has to exist. Live research runs need a non-empty `OLLAMA_API_KEY` plus `OLLAMA_BASE_URL` and `OLLAMA_MODEL`. `start` warns when the key is empty and still brings the UI up. `SEARXNG_BASE_URL` defaults to `http://localhost:8080` when it is unset.

Commands the script expects on `PATH`: `curl`, `setsid`, `ss`, `uv`, `npm`, and `docker` when it has to start SearXNG. `cloudflared` is used from `PATH` if present. Otherwise the script copies `/tmp/cloudflared` to `~/.local/bin/cloudflared`, or downloads the 2026.10.0 Linux amd64 binary to that path.

## Logs and pid files

Runtime files live in `data/tunnel/` (gitignored):

| File | Contents |
|---|---|
| `api.log` | Uvicorn |
| `web.log` | Vite |
| `cloudflared.log` | Tunnel, including the public hostname |
| `api.pid`, `web.pid`, `cloudflared.pid` | Process ids from the last start that had to launch that piece |
| `PUBLIC_URL` | Current public URL, one line, only while the tunnel is up |

If start fails, read the matching log. A dead tunnel usually shows up at the end of `cloudflared.log`. An API that exits on import shows up in `api.log`.

## Check that it actually works

`start` already does this before it prints the URL. To repeat it:

```bash
URL="$(cat data/tunnel/PUBLIC_URL)"
curl -fsS -A 'Mozilla/5.0' "$URL/" | grep -o '<title>.*</title>'
curl -fsS -A 'Mozilla/5.0' "$URL/api/health"
```

The HTML title is `FTSE 50 News Research Agent`. Health JSON includes `"status":"ok"`. `searxng.ok` should be true when Docker SearXNG is up. `ollama.ok` is true when `OLLAMA_API_KEY` is set. It does not call the model.

Then open `$URL` in a browser. The app lands on Research Runs. One research run can be active at a time.

## Shutdown and start again

```bash
./scripts/tunnel.sh stop
./scripts/tunnel.sh start
```

The second start prints a different public URL. Send people the new one.

## Limits

- There is no login. Anyone with the current URL can open the UI and start a research run, which spends the Ollama key and uses this machine's SearXNG.
- Quick tunnels have no uptime promise. Cloudflare can drop them. Run `start` again and use the new URL.
- The hostname is random. This setup does not attach a domain you own. A named Cloudflare tunnel would be required for a stable hostname.
- Do not point the tunnel at port 8080 or 8000. Port 8080 would publish SearXNG. Port 8000 would publish the API without the UI, and `/` on that API is not the dashboard.
- ngrok's free hostname shows a warning page before the app. This script uses Cloudflare so the first browser request is the UI.

## If start fails

| What you see | What to do |
|---|---|
| `missing .env` | `cp .env.example .env` and set the Ollama and SearXNG variables. |
| `missing .venv` | `uv sync` in the repo root. |
| `missing web/node_modules` | `npm install` in `web/`. |
| `SearXNG did not become ready` | `docker compose -f docker-compose.searxng.yml logs` and confirm port 8080 is free. |
| `API did not become ready` | Read `data/tunnel/api.log`. |
| `web UI did not become ready` | Read `data/tunnel/web.log`. Port 5173 may already be another app. |
| `host is not allowed` in the browser | `web/vite.config.ts` must have `server.allowedHosts: true`, then stop and start again. |
| Public HTML check failed | Read `data/tunnel/cloudflared.log`. Start again; the hostname is new. |
| Page loads, a run fails immediately | Settings page in the UI. An empty `OLLAMA_API_KEY` is the usual cause. |
| Search returns nothing | `./scripts/tunnel.sh status` should show SearXNG up. The API health field `searxng` is the same check. |
