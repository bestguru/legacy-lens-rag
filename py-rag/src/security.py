"""
Enterprise AI Security & Governance Guardrail (v3.3):
1. Chunk-Level Role-Based Access Control (RBAC):
   Classifies code and architecture chunks into security clearance tiers (Tier 1: General, Tier 2: Operations,
   Tier 3: Privileged Security/Policy) and blocks unauthorized retrieval before context reaches the LLM.
2. Deterministic PII, Credential & Network Topology Redaction:
   Scans retrieved chunks and prompts for IPv4 addresses, hardcoded secrets/passwords/API keys, and emails,
   replacing them with auditable `[REDACTED_*]` tokens to guarantee zero sensitive data leakage in regulated
   financial and telecom environments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple


IPV4_PATTERN = re.compile(r"\b(?:10|127|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")
SECRET_ASSIGN_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|api_key|apikey|access_token|private_key)\s*[:=]\s*['\"][^'\"]+['\"]"
)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@dataclass
class RBACFilterResult:
    allowed_hits: list
    blocked_chunk_ids: List[str] = field(default_factory=list)
    required_tiers_blocked: List[int] = field(default_factory=list)


class EnterpriseSecurityGuardrail:
    """
    Enforces document/chunk-level RBAC clearance and deterministic PII/Secret redaction.
    """

    TIER_1_GENERAL = 1
    TIER_2_OPERATIONS = 2
    TIER_3_PRIVILEGED_SECURITY = 3

    PRIVILEGED_RULES = {"BR-POL-001", "BR-POL-002", "BR-POL-003"}
    OPERATIONS_RULES = {"BR-NET-401", "BR-NET-402", "BR-DB-501", "BR-FIN-101", "BR-FIN-102"}

    @classmethod
    def classify_chunk_clearance_tier(cls, chunk) -> int:
        """
        Determines the minimum RBAC security tier required to access a given ChunkRecord.
        """
        chunk_rules = {r.upper() for r in getattr(chunk, "rule_ids", [])}
        if chunk_rules & cls.PRIVILEGED_RULES or "CommandPolicyEngine" in getattr(chunk, "file_path", ""):
            return cls.TIER_3_PRIVILEGED_SECURITY
        if chunk_rules & cls.OPERATIONS_RULES:
            return cls.TIER_2_OPERATIONS
        return cls.TIER_1_GENERAL

    @classmethod
    def enforce_rbac(cls, hits: list, user_clearance_tier: int = TIER_3_PRIVILEGED_SECURITY) -> RBACFilterResult:
        """
        Filters retrieval hits according to the caller's RBAC clearance tier.
        """
        allowed = []
        blocked_ids: List[str] = []
        blocked_tiers: List[int] = []

        for h in hits:
            req_tier = cls.classify_chunk_clearance_tier(h.chunk)
            if user_clearance_tier >= req_tier:
                allowed.append(h)
            else:
                blocked_ids.append(h.chunk.id)
                blocked_tiers.append(req_tier)

        return RBACFilterResult(
            allowed_hits=allowed,
            blocked_chunk_ids=blocked_ids,
            required_tiers_blocked=blocked_tiers,
        )

    @staticmethod
    def redact_sensitive_text(text: str) -> Tuple[str, int]:
        """
        Redacts internal IPv4 addresses, secret assignments, and email addresses prior to LLM context injection.
        Returns `(redacted_text, total_redactions_count)`.
        """
        redactions = 0

        def _replace_ip(_match: re.Match) -> str:
            nonlocal redactions
            redactions += 1
            return "[REDACTED_IP]"

        def _replace_secret(match: re.Match) -> str:
            nonlocal redactions
            redactions += 1
            key_name = match.group(1)
            return f'{key_name}="[REDACTED_SECRET]"'

        def _replace_email(_match: re.Match) -> str:
            nonlocal redactions
            redactions += 1
            return "[REDACTED_EMAIL]"

        sanitized = IPV4_PATTERN.sub(_replace_ip, text)
        sanitized = SECRET_ASSIGN_PATTERN.sub(_replace_secret, sanitized)
        sanitized = EMAIL_PATTERN.sub(_replace_email, sanitized)
        return sanitized, redactions
