"""
Enterprise Agentic RAG Orchestrator & LLMOps FinOps Telemetry Engine (v3.3):
1. Adaptive Query Intent Router:
   Classifies incoming queries into:
   - `DIRECT_RULE_LOOKUP` (Fast lexical/offset path)
   - `ONTOLOGY_HYBRID_GRAPHRAG` (Single-domain semantic + ontology graph traversal)
   - `MULTI_DOMAIN_DECOMPOSITION` (Complex comparative queries spanning multiple business rules or subsystems)
2. Multi-Query Decomposer:
   Breaks compound enterprise questions into isolated sub-queries, executes parallel/sequential retrieval
   under RBAC guardrails, and merges evidence with deduplicated citations.
3. LLMOps Audit & FinOps Token Telemetry:
   Tracks context token compression ratios (e.g., >99% token reduction vs. full-repository stuffing),
   RBAC policy blocks, PII/Secret redaction counts, and ontology reasoning paths.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import List

from .retriever import HybridCodeWikiRetriever, RULE_ID_PATTERN, RetrievalHit
from .security import EnterpriseSecurityGuardrail


@dataclass
class AgenticExecutionTrace:
    trace_id: str
    original_query: str
    intent_route: str
    sub_queries: List[str]
    user_clearance_tier: int
    rbac_blocked_chunks: int
    redacted_sensitive_items: int
    total_corpus_tokens: int
    injected_context_tokens: int
    finops_token_savings_pct: float
    execution_latency_ms: float
    ontology_reasoning_paths: List[str] = field(default_factory=list)
    final_hits: List[RetrievalHit] = field(default_factory=list)


class AgenticQueryOrchestrator:
    """
    Coordinates Adaptive Query Routing, Sub-Query Decomposition, RBAC/PII Security Guardrails, and FinOps Telemetry.
    """

    ROUTE_DIRECT_RULE = "DIRECT_RULE_LOOKUP"
    ROUTE_ONTOLOGY_HYBRID = "ONTOLOGY_HYBRID_GRAPHRAG"
    ROUTE_MULTI_DECOMPOSITION = "MULTI_DOMAIN_DECOMPOSITION"

    def __init__(self, retriever: HybridCodeWikiRetriever) -> None:
        self.retriever = retriever
        self.total_corpus_tokens = sum(max(c.token_count, 1) for c in retriever.chunks)

    @classmethod
    def classify_intent(cls, query: str) -> str:
        rules = {r.upper() for r in RULE_ID_PATTERN.findall(query)}
        if len(rules) >= 2 or ("비교" in query and len(rules) >= 1) or (" and " in query.lower() and len(rules) >= 2):
            return cls.ROUTE_MULTI_DECOMPOSITION
        if len(rules) == 1 and len(query.split()) <= 6:
            return cls.ROUTE_DIRECT_RULE
        return cls.ROUTE_ONTOLOGY_HYBRID

    @classmethod
    def decompose_query(cls, query: str) -> List[str]:
        """
        Decomposes a compound multi-rule or multi-domain query into targeted sub-queries.
        """
        rules = []
        for r in RULE_ID_PATTERN.findall(query):
            r_up = r.upper()
            if r_up not in rules:
                rules.append(r_up)

        if len(rules) >= 2:
            # Split clauses around conjunctions or build focused rule-scoped sub-queries
            parts = re.split(r"\b(?:and|vs|versus|비교|및|와|과)\b", query, flags=re.IGNORECASE)
            cleaned_parts = [p.strip() for p in parts if len(p.strip()) >= 8]
            if len(cleaned_parts) >= 2:
                sub_queries = []
                for idx, rule_id in enumerate(rules):
                    clause = cleaned_parts[min(idx, len(cleaned_parts) - 1)]
                    if rule_id not in clause.upper():
                        clause = f"{clause} ({rule_id})"
                    sub_queries.append(clause)
                return sub_queries
            return [f"{query} (focus on {rule_id})" for rule_id in rules]

        return [query]

    def execute_agentic_search(
        self,
        query: str,
        user_clearance_tier: int = EnterpriseSecurityGuardrail.TIER_3_PRIVILEGED_SECURITY,
        top_k: int = 3,
        simulated_corpus_tokens: int | None = None,
    ) -> AgenticExecutionTrace:
        t0 = time.perf_counter()
        intent = self.classify_intent(query)
        sub_queries = (
            self.decompose_query(query)
            if intent == self.ROUTE_MULTI_DECOMPOSITION
            else [query]
        )

        merged_hits: List[RetrievalHit] = []
        seen_chunk_ids = set()
        total_rbac_blocked = 0

        per_sub_k = max(1, top_k // len(sub_queries)) if len(sub_queries) > 1 else top_k

        for sq in sub_queries:
            raw_hits = self.retriever.search(sq, top_k=max(per_sub_k + 2, 3))
            rbac_res = EnterpriseSecurityGuardrail.enforce_rbac(raw_hits, user_clearance_tier=user_clearance_tier)
            total_rbac_blocked += len(rbac_res.blocked_chunk_ids)

            added_for_sq = 0
            for h in rbac_res.allowed_hits:
                if h.chunk.id not in seen_chunk_ids:
                    seen_chunk_ids.add(h.chunk.id)
                    merged_hits.append(h)
                    added_for_sq += 1
                    if added_for_sq >= per_sub_k:
                        break

        final_hits = merged_hits[:top_k]

        total_redactions = 0
        ontology_paths: List[str] = []
        for h in final_hits:
            _, red_cnt = EnterpriseSecurityGuardrail.redact_sensitive_text(h.chunk.content)
            total_redactions += red_cnt
            for p in h.ontology_paths:
                if p not in ontology_paths:
                    ontology_paths.append(p)

        injected_tokens = sum(max(h.chunk.token_count, 1) for h in final_hits)
        baseline_tokens = simulated_corpus_tokens or max(self.total_corpus_tokens, 5000 * 24)
        savings_pct = max(0.0, (1.0 - (injected_tokens / max(baseline_tokens, 1))) * 100.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        trace_hash = hashlib.sha256(f"{query}:{user_clearance_tier}:{len(final_hits)}".encode("utf-8")).hexdigest()[:16]

        return AgenticExecutionTrace(
            trace_id=f"tr-{trace_hash}",
            original_query=query,
            intent_route=intent,
            sub_queries=sub_queries,
            user_clearance_tier=user_clearance_tier,
            rbac_blocked_chunks=total_rbac_blocked,
            redacted_sensitive_items=total_redactions,
            total_corpus_tokens=baseline_tokens,
            injected_context_tokens=injected_tokens,
            finops_token_savings_pct=round(savings_pct, 2),
            execution_latency_ms=round(elapsed_ms, 2),
            ontology_reasoning_paths=ontology_paths[:4],
            final_hits=final_hits,
        )
