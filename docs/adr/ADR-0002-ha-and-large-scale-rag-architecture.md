# ADR-0002: High-Availability (HA), Lock-Free RCU & Large-Scale Architecture (v3.1)

- **Status:** Accepted
- **Date:** 2026-10-20
- **Author:** Kyungtae Kang (`bestguru@daum.net`)

## 1. Context & Enterprise Scale Challenges

When scaling `legacy-lens-rag` from departmental repositories to **enterprise-wide monoliths (1,000,000+ LOC / 100,000+ chunks)** and **24/7 mission-critical NOC/Financial operations**, three classes of production risks emerge:

1. **Multi-Core Read Contention & GC Scan Overhead (`sync.RWMutex` Bottleneck):**
   - Even for read-only queries, `sync.RWMutex.RLock()` and `RUnlock()` execute atomic writes (`atomic.AddInt32(&rw.readerCount, ±1)`), causing severe **CPU cache-line bouncing (MESI invalidation)** across multi-core goroutines and P99 latency spikes whenever a writer queues.
   - Duplicating `*Chunk` pointers across in-memory lookup maps forces the Go Garbage Collector (GC) to traverse hundreds of thousands of pointers during every mark cycle.
2. **Large-Scale Ingestion & Retrieval Bottlenecks:**
   - Re-parsing unchanged source files on every commit wastes CPU/I/O.
   - Linear `O(N)` full-corpus vector scanning degrades query P99 latency to seconds once chunks exceed 50,000+.
   - Unbounded file ingestion risks Out-Of-Memory (OOM) spikes on accidental binary/dump files.
3. **High-Availability (HA) & Fault-Isolation Risks:**
   - Overwriting `chunks_index.json` in place during live queries causes partial-read JSON corruption.
   - Remote embedding/LLM API rate-limits (HTTP 429) or network timeouts can bring down the entire RAG pipeline unless isolated by a circuit breaker.

## 2. Architectural Decisions (v3.1)

### A. Lock-Free RCU (`atomic.Pointer[IndexSnapshot]`) & Zero-GC-Scan Offset Indexing (`go-engine/internal/mcp`)
1. **Asymmetric Lock-Free Read-Copy-Update (RCU):**
   - Replaces `sync.RWMutex` on the read path with Go's generic **`sync/atomic.Pointer[IndexSnapshot]`** (`Load()` compiles to a single CPU register load `MOV` with **0 cache-line bouncing** and **0ns reader blocking**).
   - Protects the write path (`HotReloadIndex`) with a coalesced `reloadMu` guard to prevent reload stampedes while building the new immutable `IndexSnapshot` off-lock before a single 8-byte atomic pointer swap (`Store()`).
2. **GC-Friendly `int32` Offset Inverted Index (`RuleOffsets`, `SymbolOffsets map[string][]int32`):**
   - Stores only contiguous `[]int32` slice indices pointing into the single `Chunks []chunker.Chunk` backing array. Because `[]int32` contains zero pointers, Go's runtime allocator places these slices in **`noscan` spans**, reducing GC mark-phase overhead on inverted index postings to zero.
   - **Measured Multi-Core Benchmark (`go test -bench=BenchmarkLookup -benchmem`):**
     - Legacy `sync.RWMutex` + `O(N)` Scan: `526,094 ns/op` (`87,977 B/op`, `1,999 allocs/op`)
     - New `atomic.Pointer[IndexSnapshot]` + `O(1)` `int32` Offset Index: **`489.9 ns/op`** (`196 B/op`, `2 allocs/op`) — **1,073x latency speedup & 99.8% allocation reduction**.
3. **Go 1.22 Method-Aware HTTP Routing:**
   - Uses Go 1.22 enhanced `http.ServeMux` method patterns (`GET /healthz`, `GET /readyz`, `GET /metrics`, `POST /reload`) to prevent accidental index reloads from `GET` health crawlers (`405 Method Not Allowed`).

### B. Large-Scale Engineering
1. **SHA-256 Merkle Incremental Delta Indexing (`go-engine/internal/ingest`):**
   - Computes per-file SHA-256 hashes and compares them against the previous manifest's `file_hashes` map. Unchanged files bypass AST/Regex chunking in `O(1)` time, cutting warm re-indexing work by **>95%**.
2. **Bounded Backpressure & 10 MB File Size Guard:**
   - Uses bounded worker channels (`cap=32`) and skips oversized binary/log dumps (`MaxIngestFileSizeBytes = 10 MB`).
3. **Two-Stage Inverted Index Candidate Pruning (`py-rag/src/retriever.py`):**
   - Stage 1 queries an in-memory **Inverted Index posting list** and Rule/Symbol hash map to prune `N` chunks down to the top `M = 100` candidates (+ 1-hop graph neighbors).
   - Stage 2 computes exact BM25, Dense Cosine, and GraphRAG RRF only on the pruned candidate pool, reducing compute by **97.9%** on a 5,000-chunk benchmark (`~11 ms` query latency) paired with an **LRU Query Cache**.

### C. High-Availability (HA) & Circuit Breaker Fault Isolation
1. **Atomic Blue-Green Index Swap:**
   - Writes new indices to a staging file (`.tmp`), computes a SHA-256 `manifest_checksum`, and performs an atomic OS rename before triggering the wait-free `atomic.Pointer[IndexSnapshot]` swap.
2. **Circuit Breaker with Automatic Local Fallback (`ResilientCircuitBreakerEmbedder`):**
   - Implements a `CLOSED → OPEN → HALF_OPEN` state machine around remote embedding APIs (`OpenAICompatibleEmbedder`).
   - Upon consecutive failures or timeouts, the circuit trips `OPEN` and immediately routes embedding generation to the zero-latency `LocalDeterministicEmbedder`, guaranteeing **100% service availability (Graceful Degradation)**.

## 3. Verification (15-Gate Harness + Go Parallel Benchmarks)

Validated continuously by Gates 01–15 in `py-rag/harnesses/verify_rag_metrics.py`, `go-engine/internal/ingest/pipeline_test.go`, and `go-engine/internal/mcp/server_test.go`.
