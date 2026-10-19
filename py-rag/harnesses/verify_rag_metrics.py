"""
Deterministic RAG Verification Harness v2.0 (TACT 15-Harness Engineering Discipline applied to Hybrid GraphRAG).
Evaluates 12 rigorous verification gates:
- 9 Golden Retrieval & Grounding Queries (English + Korean Cross-Lingual + Distractor Disambiguation + Multi-Hop GraphRAG)
- 3 Adversarial Hallucination Guardrail & Graph Connectivity Gates
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.retriever import HybridCodeWikiRetriever
from src.verifier import SelfRagVerifier


GOLDEN_BENCHMARK_CASES = [
    {
        "id": "Q1 [EN-Policy]",
        "query": "What are the requirements for privileged configure terminal mode on production equipment in BR-POL-002?",
        "expected_file": "code/CommandPolicyEngine.cs",
        "expected_rule": "BR-POL-002",
    },
    {
        "id": "Q2 [EN-Blocklist]",
        "query": "Which CLI commands are globally prohibited on production network equipment (BR-POL-001)?",
        "expected_file": "code/CommandPolicyEngine.cs",
        "expected_rule": "BR-POL-001",
    },
    {
        "id": "Q3 [EN-Finance]",
        "query": "When does the Robo-Advisor portfolio drift threshold trigger rebalancing and how does VIX 28.5 circuit breaker work?",
        "expected_file": "code/RoboAdvisorRebalanceService.java",
        "expected_rule": "BR-FIN-101",
    },
    {
        "id": "Q4 [EN-ADR001]",
        "query": "Why does the TPL Dataflow Mutation Lane enforce MaxDOP = 1 while Query Lane uses 2x CPU cores in ADR-001?",
        "expected_file": "llm-wiki/ADR-001-Three-Lane-Dataflow-Pipeline.md",
        "expected_rule": "ADR-001",
    },
    {
        "id": "Q5 [EN-ADR002]",
        "query": "How did the Headless VT100 engine eliminate synchronous Control.Refresh() repaints and achieve 4,858 ms in ADR-002?",
        "expected_file": "llm-wiki/ADR-002-Headless-VT-Engine-Architecture.md",
        "expected_rule": "ADR-002",
    },
    {
        "id": "Q6 [EN-Bastion]",
        "query": "How many reconnect attempts are allowed on Mode3_KAM_SSH bastion before failing over to Legacy_Bastion_V2 (BR-NET-402)?",
        "expected_file": "code/LegacyTelnetSessionManager.cs",
        "expected_rule": "BR-NET-402",
    },
    {
        "id": "Q7 [EN-DapperDB]",
        "query": "Why is Legacy_OleDb_Pool prohibited in AuditLogBatchWriter and what is the max batch size in BR-DB-501?",
        "expected_file": "code/AuditLogBatchWriter.cs",
        "expected_rule": "BR-DB-501",
    },
    {
        "id": "Q8 [KO-CrossLingual]",
        "query": "운영 장비에서 설정(configure terminal) 모드 진입 시 필요한 운영자 권한 레벨과 우회서버(Bastion) 조건은? (BR-POL-002)",
        "expected_file": "code/CommandPolicyEngine.cs",
        "expected_rule": "BR-POL-002",
    },
    {
        "id": "Q9 [KO-CrossLingual]",
        "query": "로보어드바이저 포트폴리오 리밸런싱 허용 편차(0.035)와 VIX 변동성 서킷브레이커 기준(BR-FIN-102)은 무엇인가?",
        "expected_file": "code/RoboAdvisorRebalanceService.java",
        "expected_rule": "BR-FIN-102",
    },
]


def run_verification_harness(manifest_path: Path) -> int:
    print("=" * 88)
    print(" LEGACY-LENS-RAG v2.0 : 3-WAY HYBRID GRAPHRAG & SELF-RAG VERIFICATION HARNESS")
    print("=" * 88)

    retriever = HybridCodeWikiRetriever.from_manifest_file(manifest_path)
    print(f"[Loaded Index] {len(retriever.chunks)} AST/Graph-aware chunks from {manifest_path}")

    hits_at_1 = 0
    hits_at_3 = 0
    reciprocal_ranks = []
    grounded_passes = 0

    for case in GOLDEN_BENCHMARK_CASES:
        results = retriever.search(case["query"], top_k=3)
        rank_found = None
        for rank_idx, hit in enumerate(results, start=1):
            if hit.chunk.file_path == case["expected_file"] and (
                case["expected_rule"] in hit.chunk.rule_ids or case["expected_rule"] in hit.chunk.content
            ):
                rank_found = rank_idx
                break

        if rank_found == 1:
            hits_at_1 += 1
        if rank_found is not None and rank_found <= 3:
            hits_at_3 += 1
            reciprocal_ranks.append(1.0 / rank_found)
        else:
            reciprocal_ranks.append(0.0)

        synthesized = SelfRagVerifier.synthesize_grounded_answer(case["query"], results)
        report = SelfRagVerifier.verify_answer(synthesized, results)
        if report.is_grounded:
            grounded_passes += 1

        status = "PASS" if (rank_found == 1 and report.is_grounded) else "FAIL"
        top_hit = results[0]
        print(
            f"  [{status}] {case['id']:<20} | Rank={rank_found} | RRF={top_hit.rrf_score:.4f} "
            f"(BM25#{top_hit.bm25_rank}, Dense#{top_hit.dense_rank}, Graph+{top_hit.graph_boost:.3f}) "
            f"-> {top_hit.chunk.citation_tag}"
        )

    # Gate 10 & 11: Adversarial Hallucination Guardrail Tests (Model Triangulation / Negative Gate)
    sample_hits = retriever.search(GOLDEN_BENCHMARK_CASES[0]["query"], top_k=2)
    fake_citation_answer = "Privileged mode is allowed for all users [code/NonExistentFile.cs:L1-L50]."
    fake_rule_answer = f"This is governed by BR-FAKE-999 {sample_hits[0].chunk.citation_tag}."

    neg_report_1 = SelfRagVerifier.verify_answer(fake_citation_answer, sample_hits)
    neg_report_2 = SelfRagVerifier.verify_answer(fake_rule_answer, sample_hits)

    # Gate 12: Multi-Hop Code <-> ADR GraphRAG Connectivity Verification
    # Verify that LegacyTelnetSessionManager or AuditLogBatchWriter links to ADR-001 / CommandPolicyEngine
    bastion_hits = retriever.search(GOLDEN_BENCHMARK_CASES[5]["query"], top_k=1)
    graph_neighbors = bastion_hits[0].linked_neighbors if bastion_hits else []
    graph_connectivity_ok = any("CommandPolicyEngine.cs" in nb or "ADR-001" in nb for nb in graph_neighbors)

    adversarial_blocked = (not neg_report_1.is_grounded) and (not neg_report_2.is_grounded)
    print("-" * 88)
    print(f"  [GATE 10: GUARDRAIL] Fake File Citation Blocked : {not neg_report_1.is_grounded} ({neg_report_1.rejection_reason})")
    print(f"  [GATE 11: GUARDRAIL] Fake Rule ID Blocked       : {not neg_report_2.is_grounded} ({neg_report_2.rejection_reason})")
    print(f"  [GATE 12: GRAPHRAG ] 1-Hop Code<->ADR Neighbors : {graph_connectivity_ok} (Linked: {graph_neighbors[:3]})")

    total = len(GOLDEN_BENCHMARK_CASES)
    recall_1 = hits_at_1 / total
    recall_3 = hits_at_3 / total
    mrr = sum(reciprocal_ranks) / total
    faithfulness = grounded_passes / total

    print("-" * 88)
    print(f"  METRICS SUMMARY (12 VERIFICATION GATES):")
    print(f"    - Recall@1 (Top-1 Exact Hit)    : {recall_1 * 100:.1f}% ({hits_at_1}/{total})")
    print(f"    - Recall@3                      : {recall_3 * 100:.1f}% ({hits_at_3}/{total})")
    print(f"    - Mean Reciprocal Rank (MRR)    : {mrr:.4f}")
    print(f"    - Citation Faithfulness Rate    : {faithfulness * 100:.1f}% ({grounded_passes}/{total})")
    print(f"    - Hallucination Guardrail Block : {'100.0% (2/2 Blocked)' if adversarial_blocked else 'FAILED'}")
    print(f"    - 1-Hop GraphRAG Edge Link      : {'PASS' if graph_connectivity_ok else 'FAILED'}")
    print("=" * 88)

    metrics_out = Path(manifest_path).parent / "rag_verification_report.json"
    metrics_out.write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "total_queries": total,
                "recall_at_1": recall_1,
                "recall_at_3": recall_3,
                "mrr": mrr,
                "citation_faithfulness": faithfulness,
                "hallucination_guardrail_passed": adversarial_blocked,
                "graph_rag_connectivity_passed": graph_connectivity_ok,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if recall_1 == 1.0 and faithfulness == 1.0 and adversarial_blocked and graph_connectivity_ok:
        print("  RESULT: ALL 12/12 RAG VERIFICATION HARNESS GATES PASSED.")
        return 0
    return 1


if __name__ == "__main__":
    default_manifest = Path(__file__).resolve().parents[2] / "artifacts" / "chunks_index.json"
    sys.exit(run_verification_harness(default_manifest))
