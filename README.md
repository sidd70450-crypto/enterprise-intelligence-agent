# Enterprise Intelligence Agent (EIA)

A research agent that answers questions over a document collection and returns a
report in which every claim cites a verbatim quote, and every claim has been checked
against its evidence.

> **Status: milestone M0 (project skeleton).** The API process, configuration,
> structured logging, health checks, containers and CI exist. No research
> functionality exists yet. The [roadmap](docs/roadmap.md) says what each milestone adds.
> This README describes only what is built; nothing below is aspirational.

## What works today

| Capability | How to see it |
| --- | --- |
| API process with typed, fail-fast configuration | `uv run uvicorn backend.main:app --factory` |
| `GET /healthz` liveness check (touches no dependency) | returns `{"status": "ok"}` |
| `GET /readyz` readiness check of PostgreSQL and Redis, each under a timeout | 200 when both respond, 503 naming the failed check |
| JSON logs with a per-request ID, echoed in the `X-Request-ID` header | any request |
| Docker Compose stack: API, PostgreSQL with pgvector, Redis | `docker compose up` |
| CI: lint, format, strict type check, unit tests, integration tests on real services | `.github/workflows/ci.yml` |

## Quick start

Requirements: Docker with Compose, or Python 3.12+ with [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # then replace every "change-me" value
docker compose up --build
curl -i localhost:8000/readyz
```

Without Docker for the app itself:

```bash
uv sync
docker compose up -d postgres redis
uv run uvicorn backend.main:app --factory --reload
```

## Tests

```bash
uv run pytest tests/unit                  # no services needed
docker compose up -d postgres redis
EIA_DATABASE_URL=postgresql+asyncpg://eia:<password>@localhost:5432/eia \
EIA_REDIS_URL=redis://localhost:6379/0 \
uv run pytest tests/integration
```

Lint and types: `uv run ruff check . && uv run ruff format --check . && uv run mypy backend tests`.

## Design

- [Phase 1 design document](https://claude.ai/artifact/FfhXNQN6GJPNBWaMKRu3LP): specification, architecture, schema, API, threat model, evaluation plan.
- [Decision records](docs/adr/)
- [Roadmap](docs/roadmap.md)

## Repository layout

```text
backend/        application code; one package per responsibility (most are empty until their milestone)
evaluation/     benchmark datasets, runners, metrics and generated reports
tests/          unit, integration and security tests
frontend/       React dashboard (M6)
infrastructure/ deployment configuration
docs/           roadmap, decision records, security notes
```

## Licence

MIT. See [LICENSE](LICENSE).
