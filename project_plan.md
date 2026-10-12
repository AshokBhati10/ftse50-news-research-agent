# Claude Code Task — Align ftse50-news-research-agent with the SearXNG Research Agent Guide

## What you are doing

I have a working `ftse50-news-research-agent` project. My professor gave me a
`SearXNG_Research_Agent_Guide.md` that defines the required architecture, tooling,
config, and module layout. My build works but does not follow the guide's structure.

Align the repo with the guide **without breaking any existing functionality**.
Read the guide in full before writing any code — it is the source of truth for
every decision.

---

## What needs to be added / changed

### 1. Folder structure — add `research-stack/`
The guide specifies a `research-stack/` layout at the repo root. Create it
alongside the existing `agent/`, `nodes/`, `api/`, `web/` folders (do not
touch those). The layout is in the guide's "Suggested repository layout" block.

### 2. Docker Compose — unified stack
The existing `docker-compose.searxng.yml` only covers SearXNG + Valkey.
Create `research-stack/docker-compose.yml` that runs SearXNG, Valkey, and the
app together as the guide describes in section 2 and section 11.

### 3. SearXNG config — move and fix
`searxng-settings.yml` at the repo root needs to move to
`research-stack/searxng/settings.yml` and be updated to match the guide's
`settings.yml` from section 2.2 exactly — especially `use_default_settings: true`,
the `json` format in `search.formats`, the correct `valkey.url`, the engines list,
and the `outgoing` block. Also create the `limiter.toml` placeholder the guide
mentions.

### 4. `research-stack/tools/engine_health.py`
Implement the canary health-check script from guide section 12.1. It queries
each engine with a test query and prints OK/FAIL with result count, then dumps
`engine_health.json`.

### 5. `research-stack/tools/bulk_sources.py`
Create the stub for batch ingestion from OpenAlex / PubMed / arXiv / RSS into
Qdrant, as mentioned in guide section 13 (Week 7+). Stub only — mark TODOs.

### 6. `research-stack/app/research_agent/` package
The guide's module layout lists these files inside `research_agent/`:
`config.py`, `models.py`, `urls.py`, `cache.py`, `searx_client.py`,
`extract.py`, `chunking.py`, `models_ml.py`, `store.py`, `llm.py`,
`prompts.py`, `citations.py`, `agent.py`, `main.py`, `cli.py`.

Create each one. Where the logic already exists in the top-level codebase
(`agent/`, `nodes/`, `utils/`), re-export or wrap it — do not duplicate.
Where it does not exist, implement it per the guide:
- `chunking.py` — ~300-word chunker (guide section 5)
- `models_ml.py` — lazy-load bge-m3 embedder + bge-reranker-v2-m3 cross-encoder (guide section 6)
- `store.py` — Qdrant collection helpers (guide section 6)
- `citations.py` — citation validator that strips invented citation numbers (guide section 8)
- `urls.py` — URL normalisation (guide section 4)
- `prompts.py` — system prompt strings for planner and synthesiser LLM calls
- `searx_client.py` — SearXNG JSON API client wrapping the existing search code

### 7. `research-stack/app/Dockerfile` and `requirements.txt`
Dockerfile for the Python app container. `requirements.txt` derived from
`pyproject.toml` plus any guide-required libs not yet present
(trafilatura, pymupdf, qdrant-client, sentence-transformers, valkey/redis).

### 8. `.env.example` — append missing variables
Add the guide-required env vars that are not already in `.env.example`:
`SEARXNG_SECRET`, `SEARXNG_LIMITER`, `VALKEY_URL`, `QDRANT_URL`,
`QDRANT_COLLECTION`, and the optional embedding/reranker model names.

### 9. `README.md` — add a research-stack section
Add a `## Research Stack (guide-aligned)` section before the existing
`## Architecture` heading. Cover: what `research-stack/` is, the folder tree,
and the quick-start (`docker compose up`, `engine_health.py`).

### 10. Tests — add `tests/test_engine_health.py`
Unit tests for the `check()` function in `engine_health.py` using `respx` to
mock SearXNG HTTP responses. Test both the OK path and the failure path.
Add `respx` and `pytest-asyncio` to `pyproject.toml` dev dependencies if missing.

---

## What NOT to change

- `agent/`, `nodes/`, `utils/`, `api/`, `web/`, `main.py`, `evaluation.py`,
  existing tests — leave untouched.
- `docker-compose.searxng.yml` and `searxng-settings.yml` at the repo root —
  keep for backward compatibility.
- Existing env variable names already used in the codebase.

---

## Verification

After changes, confirm:
1. `uv run pytest tests/ -q` — all existing tests pass.
2. `uv run pytest tests/test_engine_health.py -v` — new test passes.
3. All new `research_agent/` modules import without error.
4. `docker compose -f research-stack/docker-compose.yml config` — valid.
