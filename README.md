# Enterprise Intelligence Agent (EIA)

A research agent that answers questions over a document collection and returns a
report in which every claim cites a verbatim quote, and every claim has been checked
against its evidence.

> **Status: milestone M2 (document ingestion).** Accounts, roles and the audit log
> exist, and uploaded documents are parsed, chunked, embedded and indexed. Search
> endpoints and the research workflow do not exist yet. The [roadmap](docs/roadmap.md) says what each milestone adds.
> This README describes only what is built; nothing below is aspirational.

## What works today

| Capability | How to see it |
| --- | --- |
| API process with typed, fail-fast configuration | `uv run uvicorn backend.main:app --factory` |
| `GET /healthz` liveness check (touches no dependency) | returns `{"status": "ok"}` |
| `GET /readyz` readiness check of PostgreSQL and Redis, each under a timeout | 200 when both respond, 503 naming the failed check |
| JSON logs with a per-request ID, echoed in the `X-Request-ID` header | any request |
| Accounts: register, log in, refresh, log out (Argon2id passwords, 15-minute JWTs, rotating refresh tokens with reuse detection) | `/api/v1/auth/*`, interactive docs at `/docs` |
| Organizations and roles (owner, admin, analyst, viewer) with per-route permission checks | `GET /api/v1/me`, `GET /api/v1/admin/members` |
| Rate limits on failed logins and on registrations | 429 with `Retry-After` |
| Audit log of registrations, logins, logouts and token reuse | `audit_events` table |
| Error responses as RFC 9457 problem details with the request ID | any error |
| Collections and document upload (PDF, DOCX, UTF-8 text or Markdown, 25 MB max), type detected from content | `POST /api/v1/collections/{id}/documents` |
| Background worker: sandboxed parsing, page-aware chunking with exact offsets, local embeddings (BAAI/bge-small-en-v1.5 via FastEmbed) | `python -m backend.worker` |
| Vector (HNSW) and full-text (GIN) indexes on every chunk | `document_chunks` table |
| Database migrations (Alembic), checked against the models in CI | `uv run alembic upgrade head` |
| Docker Compose stack: migrations, API, worker, PostgreSQL with pgvector, Redis | `docker compose up` |
| CI: lint, format, strict type check, unit tests, integration tests on real services, full-stack smoke test | `.github/workflows/ci.yml` |

## Quick start

Requirements: Docker with Compose, or Python 3.12+ with [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # then replace every "change-me" value
docker compose up --build     # runs migrations, then starts the API and worker
curl -i localhost:8000/readyz

curl -X POST localhost:8000/api/v1/auth/register -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"a long passphrase","organization_name":"My Org"}'
```

Without Docker for the app itself:

```bash
uv sync
docker compose up -d postgres redis
export EIA_DATABASE_URL=postgresql+asyncpg://eia:<password>@localhost:5432/eia
uv run alembic upgrade head
uv run uvicorn backend.main:app --factory --reload
uv run python -m backend.worker          # in a second terminal
```

## Tests

```bash
uv run pytest tests/unit                  # no services needed
docker compose up -d postgres redis
docker compose exec postgres createdb -U eia eia_test
EIA_DATABASE_URL=postgresql+asyncpg://eia:<password>@localhost:5432/eia_test \
EIA_REDIS_URL=redis://localhost:6379/0 \
uv run pytest tests/integration
```

The integration suite drops and recreates every table, so it refuses to run unless
the database name ends in `_test`.

Lint and types: `uv run ruff check . && uv run ruff format --check . && uv run mypy backend tests`.

## Design

- [Phase 1 design document](https://claude.ai/artifact/FfhXNQN6GJPNBWaMKRu3LP): specification, architecture, schema, API, threat model, evaluation plan.
- [Decision records](docs/adr/), including [authentication](docs/adr/0003-authentication.md) and [document ingestion](docs/adr/0004-document-ingestion.md)
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
