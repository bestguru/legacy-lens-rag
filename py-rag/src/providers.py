"""
Pluggable Embedding Providers, Circuit Breaker Fault Isolation & Cross-Lingual Domain Lexicon (v3.0 HA/Scale).
Supports:
1. LocalDeterministicEmbedder (zero external API keys, sub-millisecond execution)
2. OpenAICompatibleEmbedder (production adapter for OpenAI / Azure / Ollama / vLLM)
3. ResilientCircuitBreakerEmbedder (HA wrapper: Closed -> Open -> Half-Open state machine that
   automatically falls back to LocalDeterministicEmbedder on external API timeout or 5xx/429 failure)
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.request
from typing import Dict, List, Protocol, Tuple


TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_.\-]+|[가-힣]+")

# Enterprise Legacy Modernization Cross-Lingual Lexicon (Korean <-> English technical terms)
KOREAN_ENGLISH_DOMAIN_LEXICON: Dict[str, List[str]] = {
    "설정": ["configure", "terminal", "config", "system-view"],
    "권한": ["privileged", "role", "operator", "level", "whitelist"],
    "차단": ["blocked", "prohibited", "deny", "blocklist"],
    "명령어": ["command", "cli", "policy", "evaluate"],
    "리밸런싱": ["rebalance", "drift", "portfolio", "equity"],
    "변동성": ["volatility", "vix", "circuit", "breaker"],
    "동시성": ["concurrency", "dataflow", "lane", "parallelism", "maxdop"],
    "변이": ["mutation", "sequential", "serialized", "maxdop"],
    "쿼리": ["query", "dapper", "io"],
    "터미널": ["terminal", "headless", "vt100", "ansi", "refresh"],
    "우회서버": ["bastion", "kam", "ssh", "topology"],
    "배치": ["batch", "audit", "flush", "oledb", "dapper"],
    "커넥션": ["connection", "pool", "oledb", "dapper", "leak"],
}


def expand_and_tokenize(text: str) -> List[str]:
    """
    Tokenizes text, splits CamelCase identifiers, and expands Korean domain terms
    into their English code equivalents for cross-lingual code retrieval.
    """
    raw_tokens = TOKEN_PATTERN.findall(text)
    expanded: List[str] = []
    for tok in raw_tokens:
        lower_tok = tok.lower()
        expanded.append(lower_tok)

        camel_parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok).lower().split()
        if len(camel_parts) > 1:
            expanded.extend(camel_parts)

        for ko_key, en_terms in KOREAN_ENGLISH_DOMAIN_LEXICON.items():
            if ko_key in lower_tok:
                expanded.extend(en_terms)

    return expanded


class EmbeddingProvider(Protocol):
    """Protocol for pluggable vector embedding backends."""

    def embed_text(self, text: str) -> List[float]:
        ...


class LocalDeterministicEmbedder:
    """
    Feature-hashed character N-gram + IDF-weighted semantic token random projection embedder.
    100% deterministic, zero network dependency, acts as both CI embedder and HA fallback engine.
    """

    def __init__(self, doc_freqs: Dict[str, int], total_docs: int, vector_dim: int = 256) -> None:
        self.doc_freqs = doc_freqs
        self.total_docs = max(total_docs, 1)
        self.vector_dim = vector_dim

    def _idf(self, term: str) -> float:
        df = self.doc_freqs.get(term, 0)
        return math.log(1.0 + (self.total_docs - df + 0.5) / (df + 0.5))

    def embed_text(self, text: str) -> List[float]:
        vec = [0.0] * self.vector_dim
        tokens = expand_and_tokenize(text)
        features: List[Tuple[str, float]] = []
        for tok in tokens:
            w = self._idf(tok)
            features.append((f"tok:{tok}", w))
            if len(tok) >= 4:
                for i in range(len(tok) - 2):
                    features.append((f"ng3:{tok[i:i+3]}", 0.35 * w))

        for feat, weight in features:
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            h_int = int.from_bytes(digest, "little")
            idx = h_int % self.vector_dim
            sign = 1.0 if ((h_int >> 32) & 1) == 0 else -1.0
            vec[idx] += sign * weight

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 1e-9:
            vec = [v / norm for v in vec]
        return vec


class OpenAICompatibleEmbedder:
    """
    Production adapter for OpenAI / Azure OpenAI / Ollama / vLLM `/v1/embeddings` endpoints.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1/embeddings",
        model: str = "text-embedding-3-small",
        timeout_sec: float = 2.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = os.environ.get("OPENAI_EMBED_BASE_URL", base_url)
        self.model = model
        self.timeout_sec = timeout_sec

    def embed_text(self, text: str) -> List[float]:
        if not self.api_key and "openai.com" in self.base_url:
            raise ConnectionError("OPENAI_API_KEY not configured for remote OpenAI endpoint.")
        payload = json.dumps({"input": text, "model": self.model}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["data"][0]["embedding"]


class ResilientCircuitBreakerEmbedder:
    """
    High-Availability Circuit Breaker Wrapper (Closed -> Open -> Half-Open) for Embedding Providers.
    Guarantees 100% query availability even when external LLM/Embedding APIs suffer outages or rate-limits:
    - Tracks consecutive failures against `failure_threshold`.
    - Trips to OPEN state on threshold breach, bypassing dead network calls and immediately routing to
      `fallback_provider` (LocalDeterministicEmbedder) with 0ms network wait.
    - Automatically probes recovery in HALF_OPEN state after `recovery_timeout_sec`.
    """

    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        primary_provider: EmbeddingProvider,
        fallback_provider: EmbeddingProvider,
        failure_threshold: int = 2,
        recovery_timeout_sec: float = 30.0,
    ) -> None:
        self.primary = primary_provider
        self.fallback = fallback_provider
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec

        self.state: str = self.STATE_CLOSED
        self.failure_count: int = 0
        self.fallback_invocations: int = 0
        self.last_failure_time: float = 0.0

    def embed_text(self, text: str) -> List[float]:
        now = time.monotonic()

        if self.state == self.STATE_OPEN:
            if (now - self.last_failure_time) >= self.recovery_timeout_sec:
                self.state = self.STATE_HALF_OPEN
            else:
                self.fallback_invocations += 1
                return self.fallback.embed_text(text)

        try:
            vec = self.primary.embed_text(text)
            if self.state == self.STATE_HALF_OPEN:
                self.state = self.STATE_CLOSED
            self.failure_count = 0
            return vec
        except Exception:
            self.failure_count += 1
            self.last_failure_time = now
            self.fallback_invocations += 1
            if self.failure_count >= self.failure_threshold:
                self.state = self.STATE_OPEN
            return self.fallback.embed_text(text)
