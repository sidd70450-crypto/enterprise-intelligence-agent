# ADR 0001: Modular monolith with one worker

- Status: accepted
- Date: 2026-11-16

## Context

The system needs an HTTP API, a long-running research workflow, document ingestion
and evaluation. It is built and run by one developer, and the first release serves a
small number of users.

## Decision

One Python codebase, deployed as two processes from the same image: the API
(`uvicorn backend.main:app --factory`) and a background worker (added in M4).
Responsibilities are separated by package inside `backend/`, not by service.
PostgreSQL is the single store for relational data, vectors and keyword search;
Redis carries the job queue, rate-limit counters and progress events.

## Consequences

- One deployable, one database, one place to enforce tenant isolation.
- Package boundaries must be kept by discipline and an import-linting rule in CI,
  since nothing at runtime stops one package reaching into another.
- A single worker is a throughput limit and a single point of failure. Workflow
  stages are designed to be idempotent so more workers can be added without code
  changes.
