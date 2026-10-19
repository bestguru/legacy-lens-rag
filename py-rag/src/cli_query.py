"""
CLI JSON Bridge invoked by the Go MCP Server (`hybrid_rag_query` tool) or terminal operators (v3.3).
Integrates:
- AgenticQueryOrchestrator (Intent Routing, Sub-Query Decomposition, FinOps Token Telemetry)
- EnterpriseSecurityGuardrail (Chunk-Level RBAC Tier 1~3 & PII/Secret Redaction)
- DomainOntologyEngine (Multi-Hop Ontology Reasoning Paths)
- SelfRagVerifier ([File:Line] Citation & Rule-ID Hallucination Guardrail)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentic_router import AgenticQueryOrchestrator
from src.retriever import HybridCodeWikiRetriever
from src.security import EnterpriseSecurityGuardrail
from src.verifier import SelfRagVerifier


def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy-Lens-RAG v3.3 Enterprise AI CLI Bridge")
    parser.add_argument("--index", required=True, help="Path to chunks_index.json")
    parser.add_argument("--query", required=True, help="Search query in English or Korean")
    parser.add_argument("--top-k", type=int, default=3, help="Number of hits to return")
    parser.add_argument(
        "--role-tier",
        type=int,
        default=EnterpriseSecurityGuardrail.TIER_3_PRIVILEGED_SECURITY,
        help="Caller RBAC clearance tier (1=General, 2=Operations, 3=Privileged Security)",
    )
    args = parser.parse_args()

    retriever = HybridCodeWikiRetriever.from_manifest_file(args.index)
    orchestrator = AgenticQueryOrchestrator(retriever)
    trace = orchestrator.execute_agentic_search(
        args.query, user_clearance_tier=args.role_tier, top_k=args.top_k
    )
    hits = trace.final_hits
    answer = SelfRagVerifier.synthesize_grounded_answer(args.query, hits)
    verification = SelfRagVerifier.verify_answer(answer, hits)

    payload = {
        "query": args.query,
        "synthesized_answer": answer,
        "agentic_trace": {
            "trace_id": trace.trace_id,
            "intent_route": trace.intent_route,
            "sub_queries": trace.sub_queries,
            "user_clearance_tier": trace.user_clearance_tier,
            "rbac_blocked_chunks": trace.rbac_blocked_chunks,
            "redacted_sensitive_items": trace.redacted_sensitive_items,
            "finops_token_savings_pct": trace.finops_token_savings_pct,
            "execution_latency_ms": trace.execution_latency_ms,
            "ontology_reasoning_paths": trace.ontology_reasoning_paths,
        },
        "verification": {
            "is_grounded": verification.is_grounded,
            "verified_citations": verification.verified_citations,
            "rejection_reason": verification.rejection_reason,
        },
        "hits": [
            {
                "citation": h.chunk.citation_tag,
                "symbol": h.chunk.symbol,
                "rule_ids": h.chunk.rule_ids,
                "rrf_score": h.rrf_score,
                "bm25_rank": h.bm25_rank,
                "dense_rank": h.dense_rank,
                "linked_graph_neighbors": h.linked_neighbors,
                "ontology_paths": h.ontology_paths,
            }
            for h in hits
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
