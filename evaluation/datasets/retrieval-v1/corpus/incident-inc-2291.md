# Postmortem INC-2291: route planning outage

## Summary

On 14 October 2025 the route planning service was unavailable for 3 hours and 12 minutes. Drivers in Northern Europe received no new routes and dispatchers fell back to paper manifests.

## Root cause

A schema migration added an index to the shipments table without the concurrent option. The migration took an exclusive lock on the table, every planning query queued behind it, and the connection pool was exhausted within four minutes.

## What went well

The on-call engineer identified the blocking lock within 20 minutes using the lock monitoring dashboard.

## Follow-up actions

1. Every migration that creates an index must use the concurrent option; CI now rejects any migration that does not.
2. Planning queries get a statement timeout of five seconds so they fail fast instead of piling up.
3. Dispatch receives a cached copy of the last good routes, valid for six hours, for future outages.
