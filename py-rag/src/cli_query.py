"""
CLI JSON Bridge invoked by the Go MCP Server (`hybrid_rag_query` tool) or terminal operators.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.retriever import HybridCodeWikiRetriever
from src.verifier import SelfRagVerifier


def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy-Lens-RAG Hybrid GraphRAG CLI Bridge")
    parser.add_argument("--index", required=True, help="Path to chunks_index.json")
    parser.add_argument("--query", required=True, help="Search query in English or Korean")
    parser.add_argument("--top-k", type=int, default=3, help="Number of hits to return")
    args = parser.parse_args()

    retriever = HybridCodeWikiRetriever.from_manifest_file(args.index)
    hits = retriever.search(args.query, top_k=args.top_k)
    answer = SelfRagVerifier.synthesize_grounded_answer(args.query, hits)
    verification = SelfRagVerifier.verify_answer(answer, hits)

    payload = {
        "query": args.query,
        "synthesized_answer": answer,
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
            }
            for h in hits
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
