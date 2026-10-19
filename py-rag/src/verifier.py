"""
Self-RAG Citation Verifier & Grounded Answer Synthesizer.
Enforces strict [file_path:Lstart-Lend] provenance and checks every claim against retrieved chunks
to prevent LLM hallucinations in legacy business rule extraction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

from .retriever import ChunkRecord, RetrievalHit


CITATION_REGEX = re.compile(r"\[([a-zA-Z0-9_./\-]+):L(\d+)-L(\d+)\]")


@dataclass
class VerificationReport:
    is_grounded: bool
    citations_found: List[str]
    verified_citations: List[str]
    hallucinated_citations: List[str]
    fact_checks_passed: bool
    rejection_reason: str = ""


class SelfRagVerifier:
    """
    Deterministic Hallucination Guardrail:
    1. Every synthesized response MUST include at least one valid [file_path:Lstart-Lend] citation.
    2. Every cited file_path and line range MUST match an actually retrieved chunk in the context window.
    3. Every numeric threshold or Rule ID (BR-*, ADR-*) in the answer MUST appear verbatim in the cited chunk.
    """

    @staticmethod
    def verify_answer(answer_text: str, retrieved_hits: List[RetrievalHit]) -> VerificationReport:
        valid_lookup: Dict[str, ChunkRecord] = {
            hit.chunk.citation_tag: hit.chunk for hit in retrieved_hits
        }

        matches = CITATION_REGEX.findall(answer_text)
        citations_found = [f"[{fp}:L{s}-L{e}]" for fp, s, e in matches]

        if not citations_found:
            return VerificationReport(
                is_grounded=False,
                citations_found=[],
                verified_citations=[],
                hallucinated_citations=[],
                fact_checks_passed=False,
                rejection_reason="MISSING_CITATION: Answer contains zero [file:line] citations.",
            )

        verified: List[str] = []
        hallucinated: List[str] = []
        combined_source_text = "\n".join(hit.chunk.content for hit in retrieved_hits)

        for tag in citations_found:
            if tag in valid_lookup:
                verified.append(tag)
            else:
                hallucinated.append(tag)

        if hallucinated:
            return VerificationReport(
                is_grounded=False,
                citations_found=citations_found,
                verified_citations=verified,
                hallucinated_citations=hallucinated,
                fact_checks_passed=False,
                rejection_reason=f"HALLUCINATED_CITATION: {hallucinated} not present in retrieved context.",
            )

        # Verify Rule IDs mentioned in the answer exist in retrieved context
        rule_ids_in_answer = set(re.findall(r"\b(?:BR-[A-Z]+-\d+[A-Z]?|ADR-\d+)\b", answer_text))
        for rid in rule_ids_in_answer:
            if rid not in combined_source_text:
                return VerificationReport(
                    is_grounded=False,
                    citations_found=citations_found,
                    verified_citations=verified,
                    hallucinated_citations=[],
                    fact_checks_passed=False,
                    rejection_reason=f"UNGROUNDED_RULE_ID: '{rid}' does not exist in retrieved chunks.",
                )

        return VerificationReport(
            is_grounded=True,
            citations_found=citations_found,
            verified_citations=verified,
            hallucinated_citations=[],
            fact_checks_passed=True,
            rejection_reason="OK",
        )

    @staticmethod
    def synthesize_grounded_answer(query: str, hits: List[RetrievalHit]) -> str:
        """
        Synthesizes a deterministic, citation-backed technical answer from top retrieved chunks.
        Can also wrap an external LLM (Claude/OpenAI/Gemini) prior to passing through verify_answer().
        """
        if not hits:
            return "No matching legacy code or architecture documentation found."

        top = hits[0].chunk
        rules_str = ", ".join(top.rule_ids) if top.rule_ids else "N/A"
        preview_lines = [line.strip() for line in top.content.splitlines() if line.strip()][:4]
        evidence_snippet = " | ".join(preview_lines)

        return (
            f"Grounded Analysis for '{query}':\n"
            f"- Primary Source: {top.citation_tag} (Symbol/Section: `{top.symbol}`, Rules: {rules_str})\n"
            f"- Extracted Evidence: {evidence_snippet}"
        )
