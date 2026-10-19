# ADR-001: Re-Architecting Server Request Pipeline onto 3-Lane TPL Dataflow

- **Status:** Accepted
- **Date:** 2026-05-14
- **Domain:** TACT Server Concurrency & Request Routing
- **Related Rules:** `BR-SRV-301`, `BR-SRV-302`, `BR-SRV-303`

## Context

The legacy `ClientResponseProcess` processed all incoming client socket packets through a single serialized thread and global lock (`lock(_syncRoot)`). When long-running database queries or equipment configuration scripts executed, real-time control heartbeats and operator UI queries stalled, causing cascading session timeouts across NOC terminals.

## Decision

We replaced the single serialized queue with a **3-Lane TPL Dataflow (`ActionBlock<RequestEnvelope>`) Pipeline**, routing each request based on its operational semantics:

1. **Control Lane (`BR-SRV-301`):**
   - **Target:** Session authentication, KeepAlive heartbeats, emergency disconnect signals.
   - **Concurrency (`MaxDegreeOfParallelism`):** `Environment.ProcessorCount` (1x CPU cores).
   - **Rationale:** Control signals are CPU-light and latency-critical; they must never queue behind DB queries.

2. **Query Lane (`BR-SRV-302`):**
   - **Target:** Read-only device topology lookups, group tree queries, command history search via Dapper.
   - **Concurrency (`MaxDegreeOfParallelism`):** `Environment.ProcessorCount * 2` (2x CPU cores).
   - **Rationale:** I/O-bound SQL Server operations spend most time awaiting network I/O; 2x core parallelism maximizes throughput without exhausting the Dapper connection pool.

3. **Mutation Lane (`BR-SRV-303`):**
   - **Target:** Device state changes, policy table updates, credential rotation, audit log commits.
   - **Concurrency (`MaxDegreeOfParallelism`):** `1` (`MaxDOP = 1`, strictly sequential).
   - **Rationale:** State mutations require strict causal ordering and transactional consistency. Parallelizing mutations risks race conditions on device session locks.

## Consequences

- **Throughput & Latency:** P99 control packet latency dropped by 84% under heavy query load; global lock contention was eliminated using `ConcurrentDictionary`.
- **Verification:** Validated by `RACTServerConcurrencyVerify` harness across 10,000 synthetic concurrent packets with zero ordering violations.
