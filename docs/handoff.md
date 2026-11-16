# Handoff: where the project stands

Last updated 2026-11-17 at the end of milestone M5. Read this, then
[roadmap.md](roadmap.md), before starting work.

## Done

| Milestone | Pull request | What exists |
| --- | --- | --- |
| M0 Skeleton | direct to `main` | Config, JSON logging, request IDs, `/healthz`, `/readyz`, Compose, CI |
| M1 Database and auth | #1 | Users, organizations, roles, Argon2id, JWT + rotating refresh tokens, rate limits, audit log |
| M2 Document ingestion | #2 | Upload, sandboxed parsing, chunking with offsets, FastEmbed embeddings, worker |
| M3 Hybrid retrieval | #3 | `POST /api/v1/search` (vector, keyword, RRF, rerank), retrieval benchmark in CI |
| M4 Orchestration | #4 | Tasks with a checkpoint per stage, resume, cancel, idempotency, token budget, SSE progress; model layer with retries and accounting |
| M5 Evidence and verification | #5 | Code-verified quotes, cited claims, separate claim verification, abstention, Markdown export |

The core workflow (question to verified, cited report) works end to end. With
`EIA_LLM_PROVIDER=fake` it runs without an API key, giving mechanical answers.

Decision records: `docs/adr/0001` to `0007`. Benchmark reports: `evaluation/reports/`.

## How work is done here

- One branch and one pull request per milestone. Squash-merge only when every CI
  check is green. Each PR description lists what was added, the exit test, bugs
  found, deviations from the design doc and what was not verified.
- Write tests with the code. Run `ruff check`, `ruff format --check`,
  `mypy backend tests evaluation`, the full `pytest` suite and a local
  `gitleaks dir .` before every push. Stop if anything fails.
- Self-review every milestone before the PR and fix what the review finds.
- Never invent results. Benchmark numbers come only from runs; reports are copied
  unedited from CI with a link to the run.
- Commit messages end with the attribution lines the session provides.

## Local environment (Claude's cloud sandbox)

- Postgres 16 runs from `/tmp/pg/data` (user `eia`, password `localtest`, database
  `eia_test`). pgvector comes from apt: `apt-get update && apt-get download
  postgresql-16-pgvector && dpkg -i postgresql-16-pgvector_*.deb`. Start with
  `su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D /tmp/pg/data -l /tmp/pg/log -o '-p 5432 -k /tmp/pg' start"`;
  recreate with `initdb -U eia --auth=scram-sha-256` if the directory is gone.
- Redis: `redis-server --port 6379 --daemonize yes`.
- Test variables: `EIA_DATABASE_URL=postgresql+asyncpg://eia:localtest@127.0.0.1:5432/eia_test`
  and `EIA_REDIS_URL=redis://127.0.0.1:6379/0`.
- Hugging Face is blocked from the sandbox, so the real embedding model and
  reranker cannot run locally. Tests use the hashing embedder; CI jobs
  `model-tests`, `retrieval-benchmark` and `compose-smoke` run the real models.
- Docker cannot run in the sandbox; `compose-smoke` in CI is the full-stack test.
- GitHub GraphQL is blocked: use `gh api` REST calls (`repos/.../pulls`). CI
  artifacts cannot be downloaded; read job logs with
  `gh api repos/{owner}/{repo}/actions/jobs/{job_id}/logs`.

## Next milestones

- **M6 frontend** (React, TypeScript, Vite): login, collections and upload,
  task submission, a live stage timeline from `GET /tasks/{id}/events` (read with
  `fetch`, since the stream needs the `Authorization` header), the report with a
  citation viewer, and task history. Make token refresh single-flight: two
  concurrent refreshes log the user out by design (ADR 0003). Generate the API
  client from `/openapi.json`.
- **M8 tenancy and hardening**: PostgreSQL row-level security on every
  `org_id` table, an app role that cannot bypass it, member invitations, and a
  red-team suite (prompt injection documents, cross-tenant access, upload abuse)
  with results written up, including the cases that succeed.
- **M10 operations**: OpenTelemetry traces, Prometheus metrics, a second model
  provider with fallback, circuit breakers.
- **M12 documentation**: architecture and database diagrams, security doc,
  known limitations.
- **M7, M9 and M11** need the owner (below).

## Blocked on the owner

- **An Anthropic API key** (or another provider's). The Anthropic adapter has
  only been tested against a mocked transport. With a key: set
  `EIA_ANTHROPIC_API_KEY`, run one task, and check the report. Real benchmark runs
  of the workflow (M11) also need it, and cost money.
- **A deployment target** (server or cloud account and credentials) for M7.
- **A larger, openly licensed evaluation corpus.** retrieval-v1 is too easy;
  vector search alone nearly reaches the ceiling.

## Known follow-ups

- Try weighted rank fusion: plain RRF lowered Recall@1 against vector-only at
  400-character chunks.
- Reranking costs about 150 to 230 ms per query on CPU; decide its use against the
  workflow's latency budget.
- Bake the embedding models into the image at build time so production does not
  depend on Hugging Face at start-up.
- Keyword ranking is `ts_rank_cd`, not BM25; a BM25 extension is a candidate.
- Verification is one model call for all claims; per-claim calls are a variant to
  benchmark. Verifier accuracy must be measured against hand labels before any
  claim about it is made.
- PDF export is not implemented (Markdown is).
- A second model provider for fallback is not implemented.
