# ADR-0002: Enterprise Domain Ontology, Lock-Free RCU, HA & Large-Scale Architecture (v3.2)

- **Status:** Accepted
- **Date:** 2026-10-20
- **Author:** Kyungtae Kang (`bestguru@daum.net`)

## 1. Context & Enterprise Scale Challenges

When scaling `legacy-lens-rag` from departmental repositories to **enterprise-wide monoliths (1,000,000+ LOC / 100,000+ chunks)** and **24/7 mission-critical NOC/Financial operations**, four classes of production risks emerge:

1. **Untyped Graph Drift (Lack of Formal Domain Ontology):**
   - Naive GraphRAG systems connect chunks via untyped term co-occurrence, causing multi-hop traversal to drift into unrelated modules as the graph grows.
2. **Multi-Core Read Contention & GC Scan Overhead (`sync.RWMutex` Bottleneck):**
   - Even for read-only queries, `sync.RWMutex.RLock()` and `RUnlock()` execute atomic writes (`atomic.AddInt32(&rw.readerCount, ±1)`), causing severe **CPU cache-line bouncing (MESI invalidation)** across multi-core goroutines and P99 latency spikes whenever a writer queues.
3. **Large-Scale Ingestion & Retrieval Bottlenecks:**
   - Re-parsing unchanged source files on every commit wastes CPU/I/O, and linear `O(N)` full-corpus vector scanning degrades query P99 latency once chunks exceed 50,000+.
4. **High-Availability (HA) & Fault-Isolation Risks:**
   - Overwriting `chunks_index.json` in place during live queries causes partial-read JSON corruption, and remote embedding API timeouts can halt retrieval unless isolated by a circuit breaker.

## 2. Architectural Decisions (v3.2)

### A. Formal Enterprise Domain Ontology & Typed Knowledge Graph (`sample-corpus/ontology/domain_ontology.json`, `py-rag/src/ontology.py`)
1. **4-Class Ontology Metamodel & Domain/Range Constraints:**
   - Defines formal entity classes (`CodeArtifact`, `BusinessRule`, `ArchitectureDecision`, `DomainConcept`) and directional predicates (`ENFORCES_RULE`, `GOVERNS_RULE`, `GOVERNS_COMPONENT`, `DEPENDS_ON_SYMBOL`, `REQUIRES_CONCEPT`).
   - Enforces strict schema validation (`validate_schema_compliance()`) ensuring 0 domain/range type violations across all extracted triples.
2. **Ontology-Guided Multi-Hop Reasoning Paths:**
   - Produces deterministic reasoning chains (`CodeArtifact --[ENFORCES_RULE]--> BusinessRule <--[GOVERNS_RULE]-- ArchitectureDecision`) and maps Korean/English operational terms to canonical `DomainConcept` nodes.

### B. Lock-Free RCU (`atomic.Pointer[IndexSnapshot]`) & Zero-GC-Scan Offset Indexing (`go-engine/internal/mcp`)
1. **Asymmetric Lock-Free Read-Copy-Update (RCU):**
   - Replaces `sync.RWMutex` on the read path with Go's generic **`sync/atomic.Pointer[IndexSnapshot]`** (`Load()` compiles to a single CPU register load `MOV` with **0 cache-line bouncing** and **0ns reader blocking**).
2. **GC-Friendly `int32` Offset Inverted Index (`RuleOffsets`, `SymbolOffsets map[string][]int32`):**
   - Stores only contiguous `[]int32` slice indices (`noscan` memory spans), reducing GC mark-phase traversal overhead to zero.
   - **Measured Multi-Core Benchmark (`go test -bench=BenchmarkLookup -benchmem`):**
     - Legacy `sync.RWMutex` + `O(N)` Scan: `526,094 ns/op` (`87,977 B/op`, `1,999 allocs/op`)
     - New `atomic.Pointer[IndexSnapshot]` + `O(1)` `int32` Offset Index: **`489.9 ns/op`** (`196 B/op`, `2 allocs/op`) — **1,073x latency speedup & 99.8% allocation reduction**.

### C. Large-Scale Engineering & High-Availability (HA)
1. **SHA-256 Merkle Incremental Delta Indexing & 10 MB Guard (`go-engine/internal/ingest`):** Reuses unchanged chunks in `O(1)` (`>95%` warm re-index savings).
2. **Two-Stage Inverted Index Candidate Pruning (`py-rag/src/retriever.py`):** Prunes 5,000 chunks down to ~103 candidates (**97.9% compute reduction**, `~11 ms` latency).
3. **Atomic Blue-Green Index Swap & Circuit Breaker Auto-Fallback (`ResilientCircuitBreakerEmbedder`):** Guarantees 0-downtime index reloads and 0ms automatic fallback to `LocalDeterministicEmbedder` during remote API outages.

## 3. Verification (16-Gate Harness + Go Parallel Benchmarks)

Validated continuously by Gates 01–16 in `py-rag/harnesses/verify_rag_metrics.py`, `go-engine/internal/ingest/pipeline_test.go`, and `go-engine/internal/mcp/server_test.go`.
