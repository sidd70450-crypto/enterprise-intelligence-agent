# ADR 0006: Workflow orchestration and the model layer

- Status: accepted
- Date: 2026-11-17

## Decision

**A fixed pipeline of stages, run by a small engine of our own.** A workflow is
an ordered list of stages. Each stage gets the question and the outputs of the
stages before it, and returns JSON. The engine claims a task the way the
ingestion worker claims a document (`FOR UPDATE SKIP LOCKED`, lock token,
heartbeat). After each stage it writes the output to `workflow_steps`, the
task's token count and a heartbeat in one transaction. That row is the
checkpoint: a worker that resumes a task loads finished stages instead of running
them again, so a crash costs at most the stage that was running, and the model
is not paid twice for finished work. LangGraph was not used for M4: seven fixed
stages need a loop and a table, about 300 lines that can be read in full. It
stays a candidate when branching plans arrive (v0.3).

**One client for every model call.** `LLMClient` adds a per-attempt timeout,
bounded retries with exponential backoff for retryable errors only, a token
budget per task (default 50,000) that refuses further calls once spent, and a
`model_calls` row for every attempt. Errors carry a category (`timeout`,
`rate_limited`, `unavailable`, `invalid_response`, `bad_request`, `auth`,
`configuration`); the engine retries the task only for transient ones.

**No invented prices.** Cost is computed only when
`EIA_LLM_INPUT_PRICE_PER_MTOK` and `EIA_LLM_OUTPUT_PRICE_PER_MTOK` are set.
Otherwise it is recorded as unknown, not guessed.

**Providers.** An Anthropic adapter uses structured output through a forced tool
call. It is tested against a mocked HTTP transport only; **it has not been run
against the real API**, because no key was available. `llm_provider = "fake"`
selects a keyless stand-in that gives mechanical answers so the pipeline runs end
to end in demos and CI; it does no reasoning. Without a configured model the
worker still ingests documents, and tasks fail with category `configuration`.

**Progress events.** The engine publishes stage and task events to Redis.
`GET /tasks/{id}/events` streams them as server-sent events, starting with a
snapshot from the database so a late client misses nothing. The database is the
record; events are only a live feed.

## Consequences

- Stages must be deterministic functions of their inputs and checkpoints, or a
  resumed task could disagree with itself.
- Cancellation is checked between stages, so a cancel waits for the current stage
  to finish.
- A second provider adapter for fallback is still to do.
