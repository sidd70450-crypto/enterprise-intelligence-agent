# Handoff: where the project stands

Last updated 2026-11-16 at the end of milestone M3. Read this, then
[roadmap.md](roadmap.md), before starting work.

## Done

| Milestone | Pull request | What exists |
| --- | --- | --- |
| M0 Skeleton | direct to `main` | Config, JSON logging, request IDs, `/healthz`, `/readyz`, Compose, CI |
| M1 Database and auth | #1 | Users, organizations, roles, Argon2id, JWT + rotating refresh tokens, rate limits, audit log |
| M2 Document ingestion | #2 | Upload, sandboxed parsing, chunking with offsets, FastEmbed embeddings, worker |
| M3 Hybrid retrieval | #3 | `POST /api/v1/search` (vector, keyword, RRF, rerank), retrieval benchmark in CI |

Decision records: `docs/adr/0001` to `0005`. Benchmark reports: `evaluation/reports/`.

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

## Next: M4 workflow orchestration

Exit test: a worker killed mid-task resumes without repeating finished stages.

1. `backend/llm/`: a `ChatModel` interface (structured output as Pydantic models,
   token and cost accounting, timeout, bounded retries, typed error categories), a
   deterministic `FakeChatModel` for tests, and adapters for real providers. The
   real adapters can be written but **not verified without API keys**; mark them
   unverified in the PR.
2. Migration `0003`: `conversations`, `tasks`, `workflow_steps` (unique on task,
   stage and attempt), `model_calls`, following design doc section 4.
3. `backend/orchestration/`: the fixed seven-stage state machine from design doc
   section 3, with a checkpoint per stage and the same claim, lock-token and
   heartbeat pattern as `backend/ingestion/pipeline.py`. Tasks run in the existing
   worker process.
4. API: `POST /tasks` (202, `Idempotency-Key`), `GET /tasks`, `GET /tasks/{id}`,
   `POST /tasks/{id}/cancel`, `GET /tasks/{id}/events` (server-sent events).

Then M5 (evidence, report, verification), M6 (React frontend), M8 (row-level
security and red-team suite), M10 (OpenTelemetry, Prometheus, circuit breakers),
M12 (documentation), as in the roadmap.

## Blocked on the owner

- **LLM API keys** for the real provider adapters (M4, M5) and for real benchmark
  runs of the research workflow (M11).
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
