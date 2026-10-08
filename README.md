# FTSE 50 Market News Research Agent

An autonomous agentic research system that identifies the **top 50 FTSE companies
by market capitalisation**, finds their latest market-moving news, and produces
a structured **JSON + Markdown digest** — with evidence citations for every finding.

Built **base-first**: a reliable deterministic pipeline first, then the agentic
layer (Pydantic AI) on top. No LangGraph, no Tavily, no DuckDuckGo, no
over-engineered orchestration frameworks.

---

## Architecture

```
FTSE 100 (Wikipedia) ──► yfinance market caps ──► Top 50 by market cap
                                                        │
                                                        ▼
                                              ┌──────────────────┐
                                              │  Pydantic AI     │
                                              │  analysis agent  │
                                              └──────────────────┘
                                                        │ (one batched
                                                        │  call / company)
                                                        ▼
SearXNG ──► insufficient? ──► refined query ──► still insufficient? ──► You.com
(primary)                                                        (fallback, key required)
                                                        │
                                              + yfinance headlines
                                                        ▼
                                              URL dedup (canonicalisation)
                                                        ▼
                                              Source-quality ranking
                                              (deterministic tiers)
                                                        ▼
                                              Crawl4AI extraction
                                              (fallback: httpx + lxml;
                                               then snippet-only)
                                                        ▼
                                              Semantic dedup
                                              (all-MiniLM-L6-v2, cos > 0.85)
                                                        ▼
                                              Ollama LLM: impact / sentiment
                                              (Pydantic-validated output)
                                                        ▼
                                              Top 3 per company + evidence
                                                        ▼
                                              report_YYYY-MM-DD.json
                                              report_YYYY-MM-DD.md
                                                        ▼
                                              Pydantic Evals
```

### Why each technology

| Component | Choice | Why |
|---|---|---|
| Agent framework | **Pydantic AI** | Typed tools, structured/validated outputs, retry handling — Python-first, no graph DSL |
| FTSE list | Wikipedia + `pandas.read_html` | Free, no key; table detected by columns, not fixed index |
| Market caps | **yfinance** (`fast_info`) | Free; cached 7 days |
| Primary search | **SearXNG** (self-hosted) | Free, no per-query cost; `SEARXNG_BASE_URL` configurable |
| Fallback search | **You.com** | Used *only* when SearXNG is insufficient — never both per company |
| Supplementary news | `yfinance.Ticker.news` | Free extra headlines |
| Extraction | **Crawl4AI** | JS-heavy pages → clean Markdown; bounded retries + lightweight fallback |
| Dedup | **SentenceTransformers** `all-MiniLM-L6-v2` | Local, free; threshold configurable (default 0.85) |
| LLM analysis | **Ollama API** (paid key) | Financial-materiality reasoning; one batched call/company |
| Eval | **Pydantic Evals** | Repeatable scoring of relevance, impact, sentiment, dedup, fallbacks |
| Deps/env | **uv** | Fast, lockfile-based (`uv.lock` committed) |

### Design patterns implemented

1. **Parallel search/fetching** — per-company coroutines with bounded semaphores
   (search 4, crawl 3, LLM 2); never overwhelms providers.
2. **Source-quality ranking** — deterministic tiers (official/IR 1.0 → reputable
   0.8 → mainstream 0.6 → other 0.4 → low 0.15); explainable, never LLM-judged.
3. **URL/content deduplication** — canonical URL matching + embedding cosine filter.
4. **Evidence/citation tracking** — every finding keeps title, URL, source,
   publication date and a supporting excerpt.
5. **Caching** — companies, market caps, search results, extractions, LLM
   judgements (file-backed JSON, per-namespace TTLs, `data/cache/`).
6. **Fallback search/extraction** — SearXNG → refined → You.com; Crawl4AI →
   lightweight extractor → snippet-only; LLM failure → recorded, run continues.

The agent never does deterministic work: sorting, filtering, URL normalisation,
scoring and report rendering are plain Python. Pydantic AI is used where
reasoning/tool selection adds value — the financial-materiality analysis — plus
an opt-in fully agentic per-company mode (`--agentic`).

---

## Prerequisites

- Python 3.10+ and [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- A SearXNG instance (see below) for live search
- Ollama paid API key + model for live LLM analysis
- Optional: You.com API key for the fallback path
- Optional: `crawl4ai-setup` (Playwright browsers) for full article extraction —
  the pipeline degrades gracefully to the lightweight extractor without it

---

## Setup

```bash
git clone <this-repo> && cd ftse50-news-agent
uv sync                      # install dependencies (+ dev: uv sync --group dev)
cp .env.example .env          # then fill in your keys (never commit .env)
```

### SearXNG (self-hosted, free)

```bash
docker compose -f docker-compose.searxng.yml up -d
# SEARXNG_BASE_URL=http://localhost:8080 in .env
```

Point `SEARXNG_BASE_URL` at any reachable instance — localhost is not assumed.

### Environment variables

| Variable | Required for | Notes |
|---|---|---|
| `OLLAMA_API_KEY` | live analysis | Paid key; never logged |
| `OLLAMA_BASE_URL` | live analysis | OpenAI-compatible endpoint, e.g. `https://ollama.com/v1` |
| `OLLAMA_MODEL` | live analysis | No hardcoded default — you choose the model |
| `SEARXNG_BASE_URL` | live search | Default `http://localhost:8080` |
| `YOU_API_KEY` | fallback search | Only used when SearXNG is insufficient |

---

## Running

```bash
uv run python main.py --mock                  # offline demo (no keys, no network)
uv run python main.py --limit-companies 5     # cheap live test
uv run python main.py --limit-companies 50    # full run
uv run python main.py --companies AZN.L,SHEL.L  # specific tickers (short EPICs OK)
uv run python main.py --agentic --limit-companies 2  # agent drives tools itself
uv run python main.py --no-cache              # bypass caches
uv run python main.py --evaluate              # Pydantic Evals suite
uv run pytest                                 # unit tests
```

Reports land in `data/output/report_YYYY-MM-DD.{json,md}`.

### Mock mode

`--mock` runs the full pipeline shape on bundled data
(`data/mock/mock_news.json`: 5 companies × 10 articles) with a deterministic
heuristic analyser — zero API cost, zero network. Use it to validate plumbing,
report rendering and the evaluation harness.

---

## Cost-control strategy

- `--limit-companies` caps every run; `--mock` costs nothing.
- Search cascade never calls both providers per company; refined queries only
  when results are thin.
- Date filtering, URL dedup, semantic dedup and source ranking all happen
  **before** the LLM sees anything.
- One batched analysis call per company (not per article); concise prompts,
  short excerpts, structured short outputs.
- Caching at every stage; bounded retries with backoff; bounded concurrency.
- A single company's failure (search/extract/LLM) is recorded — it never aborts
  the 50-company run.

---

## Fallback behaviour

| Stage | Primary | On failure |
|---|---|---|
| Search | SearXNG | refined query → You.com (if key set) → continue with what exists |
| Supplementary | yfinance news | skipped silently, never fatal |
| Extraction | Crawl4AI | bounded retry → lightweight httpx/lxml extractor → snippet-only → marked `failed` |
| LLM | Ollama (Pydantic AI) | exponential backoff, ≤3 retries → company recorded as failed, run continues |

---

## Evaluation

`evaluation.py` + `eval_data/cases.json` implement the Pydantic Evals suite:

- impact classification · sentiment classification · duplicate detection
- source-quality ranking · structured-output validity
- missing/failed extraction handling · search-fallback cascade
- Ollama retry behaviour (transient → success; persistent → bounded give-up)

```bash
uv run python main.py --evaluate
```

Deterministic cases run locally with no keys. LLM-judged cases are skipped
unless `OLLAMA_*` is configured (reported, not faked). The dedup threshold
(0.85) ships with near-duplicate / distinct cases so it can be tuned objectively.

---

## Project structure

```
ftse50-news-agent/
├── main.py                 # CLI + pipeline orchestration
├── config/settings.py      # validated, centralised configuration
├── agent/agent.py          # Pydantic AI agent + tools (pipeline & agentic modes)
├── nodes/
│   ├── ingestion.py        # FTSE 100 -> market caps -> top 50
│   ├── search.py           # SearXNG -> refined -> You.com + yfinance
│   ├── crawl.py            # Crawl4AI extraction + fallback
│   ├── deduplication.py    # URL + semantic dedup
│   ├── analysis.py         # Ollama analysis (Pydantic AI) + mock heuristic
│   └── output.py           # JSON + Markdown reports
├── utils/                  # models, cache, rate limiter, query builder, source quality
├── evaluation.py / eval_data/
├── tests/
├── data/mock/              # offline demo dataset
├── examples/               # sample reports
├── docker-compose.searxng.yml / searxng-settings.yml
└── pyproject.toml / uv.lock
```

---

## Limitations

- Live search quality depends on your SearXNG instance's engines.
- yfinance is rate-limited; market-cap fetch can be slow on first run (cached after).
- Article extraction needs Playwright browsers for Crawl4AI; without them the
  lightweight fallback is used (lower fidelity on JS-heavy pages).
- The paid Ollama key is billed per token — use `--limit-companies` / `--mock`
  while iterating.
- Dedup threshold 0.85 is a starting heuristic — tune with `--evaluate`.

## Future improvements

- Adaptive dedup threshold per sector; learned source-quality weights.
- Persistent vector store for cross-run dedup.
- Richer evals with human-labelled impact cases.
- Scheduled daily runs (cron) + diff-against-yesterday digest.
