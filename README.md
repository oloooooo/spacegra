# aegra-api

Aegra API - Self-hosted Agent Protocol server.

Aegra is an open-source, self-hosted alternative to LangSmith Deployments. This package provides the core API server that implements the Agent Protocol, allowing you to run AI agents on your own infrastructure without vendor lock-in.

## Features

- **Agent Protocol Compliant**: Works with Agent Chat UI, LangGraph Studio, CopilotKit
- **Drop-in Replacement**: Compatible with the LangGraph SDK
- **Self-Hosted**: Run on your own PostgreSQL database
- **Streaming Support**: Real-time streaming of agent responses
- **Human-in-the-Loop**: Built-in support for human approval workflows
- **Async Subagent Ready**: Serves as the Agent Protocol backend for Deep Agents async subagents
- **Scheduled Cron Jobs**: Trigger runs on a schedule with timezone support and multi-instance safe claim
- **Vector Store**: Semantic search capabilities with PostgreSQL

## Installation

```bash
pip install aegra-api
```

## Quick Start

The easiest way to get started is with the `aegra-cli` package:

```bash
# Install the CLI
pip install aegra-cli

# Initialize a new project (interactive)
aegra init
cd <your-project>

# Configure environment
cp .env_example .env
# Add your OPENAI_API_KEY to .env

# Install dependencies and start developing
uv sync
uv run aegra dev
```

### Manual Setup

If you prefer manual setup:

```bash
# Install dependencies
pip install aegra-api

# Set environment variables
export POSTGRES_USER=aegra
export POSTGRES_PASSWORD=aegra_secret
export POSTGRES_HOST=localhost
export POSTGRES_DB=aegra

# Run migrations
alembic upgrade head

# Start server
uvicorn aegra_api.main:app --port 2026 --reload
```

## Tutorial: running from this repo

A complete walkthrough for running the server locally and wiring a Deep Agents
supervisor to it (the exact steps used to verify the async-subagent contract).

### 0. Repo layout

```
custom/      HTTP surface   app.py (aegra.json http.app) + middleware/ api/ service/ db/
graphs/      graph surface  supervisor.py / space_agent.py (aegra.json graphs)
                            + middleware/ access/ tools/ prompts/
shared/      logic both sides use: config token outbox session/ knowledge/
knowledge/   space content (data, not code)
tests/       pytest; tests/unit/test_layout.py guards the dependency rules
```

Dependency direction is one-way: `custom -> shared`, `graphs -> shared`, and the two
packages never import each other. Roots of `custom/` and `graphs/` hold **only the
entrypoints aegra.json points at**. Layer rules: `CONTEXT.md` § 代码布局.

### 1. Install and configure

```bash
cd aegra-api
uv sync                      # creates .venv, installs aegra-api in editable mode
cp .env_example .env         # then fill in real values
```

`.env` is loaded automatically at startup (`settings.py` calls `load_dotenv()`;
real environment variables win over the file). Minimum contents:

```bash
# PostgreSQL — either a single URL ...
# DATABASE_URL=postgresql://postgres:password@localhost:5432/aegra
# ...or the individual vars (used when DATABASE_URL is unset)
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=aegra

# OpenAI-compatible LLM API (read by langchain-openai inside your graphs)
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1   # or any OpenAI-compatible endpoint
OPENAI_MODEL=gpt-4o-mini                     # picked up by graph code via os.environ
```

### 2. Register your graphs in `aegra.json`

```json
{
  "graphs": {
    "my_agent": "./graphs/my_agent.py:graph",
    "my_subagent": "./graphs/my_subagent.py:graph"
  }
}
```

Each value is `path/to/module.py:attr` exporting a compiled LangGraph
`graph`. Do **not** pass a checkpointer when compiling — the server injects one
per request. Graph name doubles as the `assistant_id` clients send; no manual
assistant registration is needed.

> **Windows**: run uvicorn with the selector event loop —
> `--loop "asyncio:SelectorEventLoop"` (psycopg/asyncpg are incompatible with
> the default Proactor loop).

### 3. Migrate and start

```bash
uv run alembic upgrade head

# Dev entry point — the selector loop Windows needs is already wired up
uv run python src/aegra_api/main.py

# Local mode (single process, in-memory broker)
uv run uvicorn aegra_api.main:app --port 2026 --reload --loop "asyncio:SelectorEventLoop"

# Worker mode (Redis job queue + crash recovery) — requires a running Redis
REDIS_BROKER_ENABLED=true uv run uvicorn aegra_api.main:app --port 2026 --loop "asyncio:SelectorEventLoop"
```

Verify: `curl http://localhost:2026/health` →
`{"status":"healthy","database":"connected",...}`.

### 4. Smoke-test the Agent Protocol surface

```bash
# Create a thread (body may be {})
TID=$(curl -s -X POST localhost:2026/threads -H 'Content-Type: application/json' -d '{}' | python -c 'import sys,json;print(json.load(sys.stdin)["thread_id"])')

# Start a run — assistant_id is the graph name from aegra.json
RUN_ID=$(curl -s -X POST "localhost:2026/threads/$TID/runs" -H 'Content-Type: application/json' \
  -d '{"assistant_id":"supervisor","input":{"messages":[{"role":"user","content":"hello"}]}}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')

# Poll the run, then read the result from the thread's latest values
curl -s "localhost:2026/threads/$TID/runs/$RUN_ID"   # -> status (success/error), error, interrupted_by
curl -s "localhost:2026/threads/$TID"                # -> values.messages[-1].content
```

### 5. Point a Deep Agents supervisor at it

```python
# graphs/my_supervisor.py
import os
from deepagents import AsyncSubAgent, create_deep_agent

graph = create_deep_agent(
    model=f"openai:{os.environ['OPENAI_MODEL']}",
    subagents=[
        AsyncSubAgent(
            name="my_subagent",
            description="Research agent for information gathering",
            graph_id="my_subagent",                             # aegra.json key
            url=os.environ.get("AEGRA_SERVER_URL", "http://localhost:2026"),
        )
    ],
)
```

Run the supervisor the same way as any other graph. Its async subagent tasks
start, poll, update and cancel over this server's `/threads` + `/runs`
endpoints; each subagent's memory lives in its own thread in PostgreSQL.

### 6. Run the tests

```bash
uv run pytest tests/unit tests/integration -q          # no external deps

# e2e hits a LIVE server (SERVER_URL, default http://localhost:2026)
# with graphs registered in aegra.json — start the server first, then:
uv run pytest tests/e2e -q                              # all e2e
uv run pytest tests/e2e/test_runs/test_supersede_e2e.py -q   # one case
```

`@pytest.mark.prod_only` tests need worker mode (`REDIS_BROKER_ENABLED=true`
with a reachable Redis); they exercise lease/crash-recovery paths that do not
exist in local mode.

## Configuration

Two files with two different jobs:

| | `aegra.json` | `.env` / environment variables |
|---|---|---|
| **What goes in it** | What this deployment *serves*: graphs, custom HTTP app, store/auth/checkpointer policy | How it *runs and connects*: host/port, PostgreSQL, Redis, workers, cron, logging, OpenTelemetry, LLM keys |
| **Read by** | `src/aegra_api/config.py` | `src/aegra_api/settings.py` (`load_dotenv()` at import; real env vars win over the file) |
| **Format** | JSON | `KEY=VALUE` in the project-root `.env` |
| **Secrets** | never | yes |

Rule of thumb: machine/deployment specific values (credentials, hostnames, ports, capacity, feature switches) are environment variables; anything that describes *what* this server hosts (which graphs, which auth handler, retention policy) belongs in `aegra.json`.

### `aegra.json`

Path resolution: `AEGRA_CONFIG` env var → `./aegra.json` → `./langgraph.json` (compatibility fallback). Relative paths inside the file resolve against the config file's directory. **It is read once at startup — restart after editing it.** A full example lives in `aegra_example.json`.

```json
{
  "dependencies": ["./examples"],
  "graphs": {
    "my_agent": "./graphs/my_agent.py:graph"
  },
  "http": {
    "app": "./custom_routes.py:app",
    "enable_custom_route_auth": true,
    "cors": { "allow_origins": ["https://app.example.com"] }
  },
  "store": {
    "scopes": { "orgs": ["org_id"] },
    "index": { "dims": 1536, "embed": "openai:text-embedding-3-small", "fields": ["$"] }
  },
  "checkpointer": {
    "durability": "async",
    "ttl": { "strategy": "delete", "default_ttl": 10080, "sweep_interval_minutes": 30 }
  },
  "auth": {
    "path": "./auth.py:auth"
  }
}
```

| Key | Purpose |
|-----|---------|
| `graphs` | Graph name → `path/to/module.py:attr`. The name doubles as the `assistant_id` clients send: every key gets an assistant (`uuid5(namespace, key)`) pre-created at startup, so removing a key breaks every client pointing at it. |
| `dependencies` | Extra directories added to `sys.path` before graphs are imported (shared code living outside the project root); the first entry wins. |
| `http.app` | Custom FastAPI app — it **replaces** the app, with the core routers added onto it. See `examples/custom_routes_example.py`. |
| `http.enable_custom_route_auth` | Apply the Aegra auth dependency to the custom app's routes. |
| `http.cors` | CORS overrides: `allow_origins`, `allow_origin_regex`, `allow_credentials`, `allow_methods`, `allow_headers`, `expose_headers`, `max_age`. |
| `store.scopes` | Namespace prefix → list of user attributes allowed to scope it. |
| `store.index` | Turns on pgvector semantic search: `dims`, `embed` (`provider:model`), `fields` (JSON paths to embed, default `["$"]`). |
| `checkpointer.durability` | Default checkpoint durability for runs that don't set one: `sync`, `async` or `exit`. |
| `checkpointer.ttl` | Thread retention: `strategy` (`delete` or `keep_latest`), `default_ttl` (minutes), `sweep_interval_minutes`, `sweep_limit`. |
| `auth.path` | Auth handler import path (`./auth.py:auth` or `mypackage.auth:auth`). |

Two of these keys have an env override — env wins over `aegra.json`, and a per-run value wins over both:
`AEGRA_THREAD_TTL` replaces `checkpointer.ttl`, `AEGRA_CHECKPOINT_DURABILITY` replaces `checkpointer.durability`.

Every key in detail, the startup-time error messages, the graph-key / `assistant_id` rules (and what breaks when they mismatch), every environment variable, and logging — including the rotating `logs/aegra.log` file handler: **[docs/configuration.md](docs/configuration.md)** (Chinese).

### Environment Variables (`.env`)

Loaded once at import; real environment variables take precedence over the file, so the same `.env` works in Docker/K8s with a few keys overridden. Configuration comes from three places: aegra's own settings in `src/aegra_api/settings.py` (env only), this project's **non-secret** knobs in [`config.yaml`](config.yaml) (read by `shared/config.py`, env override allowed), and **secrets** in `.env` (JWT_SECRET, PASSWORD_PEPPER, SESSION_SCOPE_SECRET, S3 credentials, OPENAI_API_KEY). A commented starting point is [`.env_example`](.env_example) (A native / B secrets / C third-party); the full reference (server, PostgreSQL, pools, Redis/workers, cron, retention, logging, OpenTelemetry, metrics) is in [docs/configuration.md](docs/configuration.md).

```bash
# --- Server ---
HOST=0.0.0.0
PORT=2026
AEGRA_CONFIG=aegra.json       # path to the aegra.json described above
AUTH_TYPE=noop                # noop | custom
RUN_MIGRATIONS_ON_STARTUP=true   # false for multi-pod: run `aegra db upgrade` out-of-band

# --- PostgreSQL (DATABASE_URL wins when both are set) ---
# DATABASE_URL=postgresql://postgres:postgres@localhost:5432/aegra
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=aegra

# --- Logging (stdout always; the file handler is on by default) ---
LOG_LEVEL=INFO
LOG_FILE=logs/aegra.log       # "" = stdout only
LOG_FILE_MAX_MB=10            # rotate at this size
LOG_FILE_RETENTION_DAYS=7     # delete rotated files older than this

# --- LLM (read by langchain-openai inside your graphs) ---
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1   # only for OpenAI-compatible endpoints
```

`AEGRA_THREAD_TTL` takes either a bare number (default TTL in minutes) or a JSON object with `strategy` / `default_ttl` / `sweep_interval_minutes` / `sweep_limit`; `LANGGRAPH_THREAD_TTL` is accepted as an alias.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/assistants` | POST | Create assistant from graph_id |
| `/assistants/search` | POST | Search assistants |
| `/assistants/{assistant_id}` | GET | Get assistant details |
| `/threads` | POST | Create conversation thread |
| `/threads/{thread_id}/state` | GET | Get thread state |
| `/threads/{thread_id}/runs` | POST | Execute graph (background) |
| `/threads/{thread_id}/runs/stream` | POST | Execute graph (streaming) |
| `/threads/{thread_id}/runs/{run_id}/cancel` | POST | Cancel a run |
| `/runs/crons` | POST | Create a stateless cron job |
| `/threads/{thread_id}/runs/crons` | POST | Create a thread-bound cron job |
| `/runs/crons/{cron_id}` | PATCH/DELETE | Update or delete a cron job |
| `/runs/crons/search` | POST | Search cron jobs |
| `/runs/crons/count` | POST | Count cron jobs |
| `/store/items` | PUT | Save to vector store |
| `/store/items/search` | POST | Semantic search |
| `/store/namespaces` | POST | List store namespaces |
| `/health` | GET | Health check |

## Creating Graphs

Agents are Python modules exporting a compiled `graph` variable:

```python
# graphs/my_agent/graph.py
from typing import TypedDict
from langgraph.graph import StateGraph, START, END

class State(TypedDict):
    messages: list[str]

def process_node(state: State) -> State:
    messages = state.get("messages", [])
    messages.append("Processed!")
    return {"messages": messages}

# Build the graph
builder = StateGraph(State)
builder.add_node("process", process_node)
builder.add_edge(START, "process")
builder.add_edge("process", END)

# Export as 'graph'
graph = builder.compile()
```

## Async Subagents (Deep Agents)

aegra-api doubles as the Agent Protocol server for [Deep Agents](https://github.com/langchain-ai/deepagents)
**async subagents**: a supervisor launches background runs over HTTP, polls them, sends follow-up
instructions, and cancels them, while each subagent's memory lives in your PostgreSQL.

Register the subagent graph in `aegra.json` like any other graph:

```json
{
  "graphs": {
    "my_agent": "./graphs/my_agent.py:graph"
  }
}
```

```python
# graphs/my_agent.py — compiled without a checkpointer; aegra injects one per request
from deepagents import create_deep_agent

graph = create_deep_agent(model="openai:gpt-5.5", system_prompt="You are a thorough research agent.")
```

Point the supervisor at this server (`graph_id` is the `aegra.json` key):

```python
from deepagents import AsyncSubAgent, create_deep_agent

supervisor = create_deep_agent(
    model="openai:gpt-5.5",
    subagents=[AsyncSubAgent(name="my_subagent", description="...", graph_id="my_subagent", url="http://localhost:2026")],
)
```

See [docs/design.md](docs/design.md) for the exact contract, the status vocabulary, and known limits
(HITL subagents cannot be resumed by deepagents).

## Architecture

```
+---------------------------------------------------------+
|  FastAPI HTTP Layer (Agent Protocol API)                |
|  - /assistants, /threads, /runs, /store endpoints       |
+---------------------------------------------------------+
|  Middleware Stack                                        |
|  - Auth, CORS, Structured Logging, Correlation ID       |
+---------------------------------------------------------+
|  Service Layer (Business Logic)                          |
|  - LangGraphService, AssistantService, StreamingService |
+---------------------------------------------------------+
|  LangGraph Runtime                                       |
|  - Graph execution, state management, tool execution    |
+---------------------------------------------------------+
|  Database Layer (PostgreSQL)                             |
|  - AsyncPostgresSaver (checkpoints), AsyncPostgresStore |
+---------------------------------------------------------+
```

## Package Structure

```
aegra-api/
├── src/aegra_api/        # aegra core (Agent Protocol server, do not fork)
│   ├── api/              # /assistants, /threads, /runs, /store endpoints
│   ├── services/         # business logic (LangGraphService, StreamingService, ...)
│   ├── core/             # database, auth, orm, app loader
│   ├── models/           # Pydantic request/response schemas
│   ├── middleware/       # ASGI middleware
│   ├── observability/    # OpenTelemetry tracing
│   ├── main.py           # server entry point
│   ├── config.py         # aegra.json loading
│   └── settings.py       # env settings
├── custom/               # HTTP surface for this deployment
│   ├── app.py            # FastAPI app (aegra.json http.app)
│   ├── middleware/       # auth handler
│   ├── api/              # one file per URL domain (account/spaces/graphs/tasks/delivery)
│   ├── service/          # business logic (account, quota, outbox dispatcher)
│   └── db/               # ORM models + repositories
├── graphs/               # graph surface (runs only inside a run)
│   ├── supervisor.py     # main agent graph (aegra.json graphs.supervisor)
│   ├── space_agent.py    # per-space subagent graph (aegra.json graphs.domain_agent)
│   ├── middleware/       # AsyncTaskGuard
│   ├── access/           # space allow-list, ACL filesystem backend
│   ├── tools/            # xlsx (info/query/search/profile) + ask_user_question
│   └── prompts/
├── shared/               # used by both sides; imports no framework
│   ├── config.py         # config.yaml singleton
│   ├── token.py          # JWT minting (user + service identity)
│   ├── outbox.py         # delivery message format
│   ├── session/          # LangGraph Store namespace + S3 blobs
│   └── knowledge/        # knowledge root (local|s3) + AGENT.md parsing
├── knowledge/            # space content (data, not code)
├── tests/                # pytest (unit / integration / e2e)
├── web/                  # Vue 3 frontend
├── alembic/              # Database migrations
├── aegra.json            # graph / http / auth entrypoints
└── config.yaml           # deployment config (non-secret)
```

## Related Packages

- **aegra-cli**: Command-line interface for project management

## Documentation

- **启动指南 / Getting started (中文)**: [README_zh.md](README_zh.md) — 环境依赖、`.env`、建表、初始化账号与知识空间、前后端启动、排错
- Configuration reference (aegra.json, config.yaml, environment variables, logging): [docs/configuration.md](docs/configuration.md) (Chinese)
- Async subagent (ACP) design: [docs/design.md](docs/design.md)
- Streaming reference (stream modes, sub-agent stream proxy): [docs/streaming.md](docs/streaming.md) (Chinese)
- Frontend API contract (custom endpoints, SSE frames, interrupt/resume): [docs/frontend-api.md](docs/frontend-api.md) (Chinese)
- Glossary: [CONTEXT.md](CONTEXT.md)

## Web UI

`web/` contains a Vue 3 + Vite frontend (chat + sub-agent live progress + knowledge-space browser + cron jobs):

```bash
cd web && npm install && npm run dev     # http://localhost:5273 (backend on :2026)
node scripts/smoke.mjs                   # contract smoke test against a running backend
```

See [web/README.md](web/README.md).
