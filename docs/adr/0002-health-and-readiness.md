# ADR 0002: Separate liveness and readiness checks

- Status: accepted
- Date: 2026-11-16

## Decision

- `GET /healthz` returns 200 if the process is running. It touches no dependency, so
  a database outage does not make an orchestrator restart healthy API processes.
- `GET /readyz` checks PostgreSQL (`SELECT 1`) and Redis (`PING`) concurrently, each
  under `EIA_READINESS_TIMEOUT_SECONDS` (default 2 s). It returns 503 if any check
  fails or times out.
- The response names each check as `ok` or `failed` but never includes the error
  text, which can contain hostnames or credentials. The error type is logged.
- Both endpoints are unauthenticated. They reveal only whether the service and its
  dependencies are up.

## Consequences

A hanging dependency cannot hang the readiness probe. Detailed failure reasons are
available to operators through logs, matched by request ID, and not to the public.
