# ADR-0001: Why Go + Python 3-Way Hybrid GraphRAG for Enterprise Legacy Modernization

- **Status:** Accepted
- **Date:** 2026-10-20
- **Author:** Kyungtae Kang (`bestguru@daum.net`)

## 1. Context & Problem Statement

During large-scale enterprise legacy modernization (such as reverse-engineering 100,000+ lines of undocumented C# / Java / SQL code alongside 1,500+ architecture wiki documents in `llm-wiki`), standard tutorial-grade RAG pipelines fail in production due to four structural bottlenecks:

1. **Fixed-Window Text Splitting Corrupts Syntax & Provenance:** Splitting source code by character/token count detaches method signatures and XML/Javadoc comments from conditional business rule predicates (`BR-*`), invalidating line-number citations.
2. **Dense-Only Embeddings Fail on Exact Code Identifiers:** Operators and coding agents frequently search for exact rule identifiers (`BR-POL-002`), class/method names (`EvaluateCommand`), or numeric thresholds (`0.035`, `MaxDOP = 1`), where lexical matching (Okapi BM25) is strictly superior to dense embeddings alone.
3. **Cross-Lingual Gap in Korean Enterprises:** In Korean financial and telecom enterprises, engineers ask questions in Korean (*"설정 모드 진입 시 필요한 권한 레벨과 우회서버 조건은?"*) while the underlying source code and identifiers are written in English (`configure terminal`, `OperatorRoleLevel >= 3`, `Mode3_KAM_SSH`).
4. **Isolated Chunks Miss Multi-Hop Architectural Context:** A code method (`LegacyTelnetSessionManager.ResolveBastionTopology`) often depends on policies defined in another class (`CommandPolicyEngine`) or an architectural decision record (`ADR-001`).

## 2. Architectural Decision

We architected `legacy-lens-rag` v2.0 as a **polyglot Go + Python 3-Way Hybrid GraphRAG & MCP system**:

1. **Go (`go-engine`) for High-Throughput 3-Lane Ingestion, AST/Graph Extraction & MCP Serving:**
   - Mirrors the TPL Dataflow 3-lane concurrency pattern (Discovery Lane → `2×CPU` Parallel AST/Graph Chunking Lane → `MaxDOP=1` Serialized Deterministic Aggregator).
   - Extracts `SymbolsDefined` and `SymbolsReferenced` per chunk to construct a lightweight **Code & ADR Dependency Graph**.
   - Serves the **Model Context Protocol (MCP) JSON-RPC 2.0** interface over stdio, bridging directly to the Python Hybrid GraphRAG engine via `hybrid_rag_query`.
2. **Python (`py-rag`) for 3-Way Hybrid Retrieval, Cross-Lingual Expansion & Self-RAG Guardrails:**
   - Combines **Okapi BM25** (with CamelCase sub-tokenization and Korean↔English domain lexicon expansion), **Dense Vector Cosine Similarity** (via a pluggable `EmbeddingProvider` supporting both zero-key deterministic local projection and OpenAI/Ollama/vLLM APIs), and **1-Hop GraphRAG Neighbor Boosting** via **Reciprocal Rank Fusion (RRF, $k=60$)**.
   - Enforces a deterministic **Self-RAG Citation Verifier (`SelfRagVerifier`)** that rejects any output missing valid `[file_path:Lstart-Lend]` provenance or citing ungrounded Rule IDs.

## 3. Verification & Consequences

- **Deterministic CI Gate:** Gated by `.github/workflows/ci.yml` running 12 verification gates (`py-rag/harnesses/verify_rag_metrics.py`), achieving **100% Recall@1 (9/9)** across English, Korean cross-lingual, and distractor-heavy queries, **100% hallucination blocking**, and verified 1-hop Code↔ADR graph traversal.
