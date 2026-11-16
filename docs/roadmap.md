# Roadmap

Each milestone ends with an exit test that must pass before the next one starts.
Status is updated in the same commit that completes a milestone.

| # | Milestone | Exit test | Status |
| --- | --- | --- | --- |
| M0 | Skeleton: layout, config, logging, health checks, Compose, CI | `/readyz` reports ready against real PostgreSQL and Redis; CI green | Done (2026-11-16) |
| M1 | Database and auth | API tests cover login, expiry, refresh rotation, forbidden route | In review |
| M2 | Document ingestion | A 50-page PDF becomes searchable; a corrupt file fails with a reason | Not started |
| M3 | Hybrid retrieval and first benchmark | Recall@10 reported for vector, keyword and hybrid on the same questions | Not started |
| M4 | Workflow orchestration | A worker killed mid-task resumes without repeating finished stages | Not started |
| M5 | Evidence, report, verification | Abstains on unanswerable questions; every citation resolves to a real quote | Not started |
| M6 | Frontend | A full task can be run and inspected in the browser | Not started |
| M7 | Deploy v0.1 | A public demo runs the benchmark corpus | Not started |
| M8 | Tenancy and hardening (v0.2) | Cross-tenant tests pass on every endpoint; red-team results published | Not started |
| M9 | Tools and planning (v0.3) | Planned variant benchmarked against the fixed pipeline | Not started |
| M10 | Memory and operations (v0.4) | With one provider blocked, tasks complete on the other | Not started |
| M11 | Benchmark and multi-agent (v0.5) | Benchmark report reproduces from a clean clone | Not started |
| M12 | Documentation | A new reader runs the project from the README alone | Not started |
