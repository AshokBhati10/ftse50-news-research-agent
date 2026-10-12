#!/usr/bin/env bash
# Start or stop the public research UI.
# Local pieces: SearXNG (if configured on localhost:8080), FastAPI :8000,
# Vite :5173. Public piece: a Cloudflare quick tunnel to Vite only.
# SearXNG, Qdrant, and Ollama are not published.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/data/tunnel"
API_HEALTH="http://127.0.0.1:8000/api/health"
WEB_URL="http://127.0.0.1:5173/"
CF_TARGET="http://127.0.0.1:5173"
CF_RELEASE="https://github.com/cloudflare/cloudflared/releases/download/2026.10.0/cloudflared-linux-amd64"

export PATH="${HOME}/.local/bin:${PATH}"

log() { printf '%s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") <start|stop|status>

  start    SearXNG (local only), API, web UI, and a Cloudflare tunnel
  stop     Stop the tunnel, web UI, and API
  stop --with-searxng
           Also stop the local SearXNG container from docker-compose.searxng.yml
  status   Show what is running and the current public URL

The public URL changes every time the tunnel starts. It is written to
data/tunnel/PUBLIC_URL while the tunnel is up.
EOF
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

env_value() {
  local key=$1 line file="$ROOT/.env"
  [[ -f "$file" ]] || return 0
  line="$(grep -E "^${key}=" "$file" | tail -1 || true)"
  line="${line#*=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  line="${line%"${line##*[![:space:]]}"}"
  printf '%s' "$line"
}

alive() {
  [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null
}

pid_in() {
  local file=$1
  [[ -f "$file" ]] || return 0
  tr -d '[:space:]' <"$file"
}

cmdline() {
  tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null || true
}

port_pids() {
  local port=$1
  ss -ltnp "sport = :${port}" 2>/dev/null \
    | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
    | sort -u
}

descendants() {
  local parent=$1 child
  for child in $(pgrep -P "$parent" 2>/dev/null || true); do
    descendants "$child"
    printf '%s\n' "$child"
  done
}

kill_pid() {
  local pid=$1 child
  alive "$pid" || return 0
  for child in $(descendants "$pid"); do
    kill -TERM "$child" 2>/dev/null || true
  done
  kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  local _i
  for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    alive "$pid" || return 0
    sleep 0.2
  done
  for child in $(descendants "$pid"); do
    kill -KILL "$child" 2>/dev/null || true
  done
  kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

# Start a command in its own session so stop can signal the whole group.
# $! is that process: non-interactive bash does not put background jobs in a
# new process group, so setsid() succeeds without an extra fork.
start_detached() {
  local name=$1 pidfile=$2 logfile=$3 dir=$4
  shift 4
  if alive "$(pid_in "$pidfile")"; then
    log "${name} already running (pid $(pid_in "$pidfile"))"
    return 0
  fi
  mkdir -p "$RUN"
  (
    cd "$dir"
    setsid "$@" >>"$logfile" 2>&1 </dev/null &
    printf '%s\n' "$!" >"$pidfile"
  )
  local pid
  pid="$(pid_in "$pidfile")"
  alive "$pid" || die "failed to start ${name}. See ${logfile}"
  log "started ${name} (pid ${pid})"
}

wait_http() {
  local url=$1 label=$2 tries=${3:-45} i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS -m 3 -o /dev/null "$url" 2>/dev/null; then
      log "${label} is up (${url})"
      return 0
    fi
    sleep 1
  done
  die "${label} did not become ready (${url}). Logs: ${RUN}"
}

cloudflared_bin() {
  local dest="${HOME}/.local/bin/cloudflared"
  if command -v cloudflared >/dev/null 2>&1; then
    command -v cloudflared
    return 0
  fi
  if [[ -x /tmp/cloudflared ]]; then
    mkdir -p "${HOME}/.local/bin"
    cp /tmp/cloudflared "$dest"
    chmod +x "$dest"
    printf '%s\n' "$dest"
    return 0
  fi
  require_cmd curl
  mkdir -p "${HOME}/.local/bin"
  log "downloading cloudflared 2026.10.0"
  curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 -o "$dest" "$CF_RELEASE"
  chmod +x "$dest"
  printf '%s\n' "$dest"
}

latest_url() {
  [[ -f "$RUN/cloudflared.log" ]] || return 0
  grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN/cloudflared.log" | tail -1 || true
}

ensure_searxng() {
  local base
  base="$(env_value SEARXNG_BASE_URL)"
  base="${base:-http://localhost:8080}"
  base="${base%/}"
  case "$base" in
    http://localhost:8080 | http://127.0.0.1:8080)
      if curl -fsS -m 3 -o /dev/null "${base}/" 2>/dev/null; then
        log "SearXNG already up (${base})"
      else
        require_cmd docker
        log "starting SearXNG on ${base}"
        docker compose -f "$ROOT/docker-compose.searxng.yml" up -d
        wait_http "${base}/" "SearXNG" 40
      fi
      ;;
    *)
      log "SEARXNG_BASE_URL is ${base}; leaving Docker SearXNG alone"
      ;;
  esac
}

stop_local_stack() {
  local pid
  for pid in $(pgrep -f 'cloudflared tunnel --url http://127.0.0.1:5173' || true); do
    [[ "$pid" == "$$" ]] && continue
    log "stopping cloudflared (pid ${pid})"
    kill_pid "$pid"
  done
  local port pids cmd cwd
  for port in 5173 8000; do
    pids="$(port_pids "$port" || true)"
    for pid in $pids; do
      cmd="$(cmdline "$pid")"
      cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
      case "$port" in
        5173)
          [[ "$cwd" == "$ROOT/web" || "$cmd" == *"vite"* || "$cmd" == *"ftse50-news-research-agent/web"* ]] || continue
          ;;
        8000)
          [[ "$cmd" == *"api.app:app"* ]] || continue
          ;;
      esac
      log "stopping pid ${pid} on port ${port}"
      local parent pcmd
      parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
      pcmd="$(cmdline "${parent:-0}")"
      if [[ -n "$parent" && "$parent" != "1" && \
        ( "$pcmd" == *"uv run uvicorn"* || "$pcmd" == *"npm run dev"* \
          || "$pcmd" == *"sh -c vite"* ) ]]; then
        kill_pid "$parent"
      else
        kill_pid "$pid"
      fi
    done
  done
  for pid in "$(pid_in "$RUN/cloudflared.pid")" "$(pid_in "$RUN/web.pid")" "$(pid_in "$RUN/api.pid")"; do
    [[ -n "$pid" ]] || continue
    alive "$pid" || continue
    log "stopping leftover pid ${pid}"
    kill_pid "$pid"
  done
  rm -f "$RUN/cloudflared.pid" "$RUN/web.pid" "$RUN/api.pid" "$RUN/PUBLIC_URL"
}

cmd_start() {
  require_cmd curl
  require_cmd setsid
  require_cmd ss
  [[ -f "$ROOT/.env" ]] || die "missing ${ROOT}/.env — copy .env.example and fill it in"
  [[ -d "$ROOT/.venv" ]] || die "missing .venv — from the repo root run: uv sync"
  [[ -d "$ROOT/web/node_modules" ]] || die "missing web/node_modules — from web/ run: npm install"
  local key
  key="$(env_value OLLAMA_API_KEY)"
  if [[ -z "$key" ]]; then
    log "warning: OLLAMA_API_KEY is empty. The UI will load; live research runs will be rejected."
  fi

  mkdir -p "$RUN"
  ensure_searxng

  if curl -fsS -m 3 -o /dev/null "$API_HEALTH" 2>/dev/null; then
    log "API already up (${API_HEALTH})"
  else
    start_detached "API" "$RUN/api.pid" "$RUN/api.log" "$ROOT" \
      uv run uvicorn api.app:app --host 127.0.0.1 --port 8000
    wait_http "$API_HEALTH" "API" 40
  fi

  if curl -fsS -m 3 -o /dev/null "$WEB_URL" 2>/dev/null; then
    log "web UI already up (${WEB_URL})"
  else
    require_cmd npm
    start_detached "web UI" "$RUN/web.pid" "$RUN/web.log" "$ROOT/web" \
      npm run dev -- --host 127.0.0.1 --port 5173
    wait_http "$WEB_URL" "web UI" 40
  fi

  local cf pid url
  cf="$(cloudflared_bin)"
  if pgrep -f 'cloudflared tunnel --url http://127.0.0.1:5173' >/dev/null; then
    log "Cloudflare tunnel already running"
    if [[ -f "$RUN/PUBLIC_URL" ]]; then
      url="$(tr -d '[:space:]' <"$RUN/PUBLIC_URL")"
    else
      url="$(latest_url)"
    fi
    [[ -n "$url" ]] || die "tunnel is already running but its URL is not in ${RUN}/PUBLIC_URL. Run: $0 stop && $0 start"
  else
    : >"$RUN/cloudflared.log"
    start_detached "Cloudflare tunnel" "$RUN/cloudflared.pid" "$RUN/cloudflared.log" "$ROOT" \
      "$cf" tunnel --url "$CF_TARGET" --no-autoupdate
    url=""
    local i
    for ((i = 1; i <= 40; i++)); do
      url="$(latest_url)"
      [[ -n "$url" ]] && break
      pid="$(pid_in "$RUN/cloudflared.pid")"
      alive "$pid" || die "cloudflared exited. See ${RUN}/cloudflared.log"
      sleep 1
    done
    [[ -n "$url" ]] || die "cloudflared did not print a public URL. See ${RUN}/cloudflared.log"
  fi

  printf '%s\n' "$url" >"$RUN/PUBLIC_URL"
  local page health
  page="$(mktemp)"
  local code
  code="$(curl -sS -m 25 -A 'Mozilla/5.0' -o "$page" -w '%{http_code}' "${url}/" || true)"
  if [[ "$code" != "200" ]] || ! grep -q 'FTSE 50 News Research Agent' "$page"; then
    rm -f "$page"
    die "public page check failed (HTTP ${code}). URL was ${url}. See ${RUN}/cloudflared.log"
  fi
  rm -f "$page"
  health="$(curl -fsS -m 20 -A 'Mozilla/5.0' "${url}/api/health")"
  printf '%s' "$health" | grep -q '"status":"ok"' \
    || die "public /api/health did not report status ok: ${health}"

  log ""
  log "Public URL: ${url}"
  log "Local UI:   ${WEB_URL}"
  log "Local API:  ${API_HEALTH}"
  log "Stop with:  $ROOT/scripts/tunnel.sh stop"
}

cmd_stop() {
  local with_searxng=0
  if [[ "${1:-}" == "--with-searxng" ]]; then
    with_searxng=1
  elif [[ -n "${1:-}" ]]; then
    die "unknown stop option: $1"
  fi
  mkdir -p "$RUN"
  stop_local_stack
  if [[ "$with_searxng" -eq 1 ]]; then
    require_cmd docker
    log "stopping SearXNG container"
    docker compose -f "$ROOT/docker-compose.searxng.yml" stop
  else
    log "SearXNG left running. Stop it with: $(basename "$0") stop --with-searxng"
  fi
  log "tunnel, web UI, and API are stopped"
}

cmd_status() {
  local pid
  printf '%-12s %s\n' "API" "$(curl -fsS -m 3 -o /dev/null "$API_HEALTH" 2>/dev/null && echo up || echo down)"
  printf '%-12s %s\n' "web" "$(curl -fsS -m 3 -o /dev/null "$WEB_URL" 2>/dev/null && echo up || echo down)"
  pid="$(pgrep -f 'cloudflared tunnel --url http://127.0.0.1:5173' | head -1 || true)"
  if [[ -n "$pid" ]]; then
    printf '%-12s %s\n' "tunnel" "up (pid ${pid})"
  else
    printf '%-12s %s\n' "tunnel" "down"
  fi
  if [[ -f "$RUN/PUBLIC_URL" ]]; then
    printf '%-12s %s\n' "public" "$(tr -d '[:space:]' <"$RUN/PUBLIC_URL")"
  else
    printf '%-12s %s\n' "public" "(none — start the tunnel)"
  fi
  local base
  base="$(env_value SEARXNG_BASE_URL)"
  base="${base:-http://localhost:8080}"
  base="${base%/}"
  printf '%-12s %s\n' "searxng" "$(curl -fsS -m 3 -o /dev/null "${base}/" 2>/dev/null && echo "up (${base})" || echo "down (${base})")"
}

main() {
  cd "$ROOT"
  case "${1:-}" in
    start) cmd_start ;;
    stop) shift; cmd_stop "${1:-}" ;;
    status) cmd_status ;;
    -h | --help | help) usage ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"
