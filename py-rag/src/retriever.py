"""
Hybrid Retriever combining:
1. Okapi BM25 Sparse Keyword Scoring (critical for exact Rule IDs, method names, and error codes)
2. Dense Semantic Subword N-Gram / Term-Projection Vector Cosine Similarity
3. Reciprocal Rank Fusion (RRF, k=60) + Rule-ID Exact Boost Reranking
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_.\-]+|[가-힣]+")
RULE_ID_PATTERN = re.compile(r"\b(?:BR-[A-Z]+-\d+[A-Z]?|ADR-\d+)\b", re.IGNORECASE)


def split_camel_and_tokenize(text: str) -> List[str]:
    """Tokenizes source code and prose, splitting CamelCase identifiers into semantic subtokens."""
    raw_tokens = TOKEN_PATTERN.findall(text)
    expanded: List[str] = []
    for tok in raw_tokens:
        lower_tok = tok.lower()
        expanded.append(lower_tok)
        # Split CamelCase: e.g., CommandPolicyEngine -> command, policy, engine
        camel_parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok).lower().split()
        if len(camel_parts) > 1:
            expanded.extend(camel_parts)
    return expanded


@dataclass
class ChunkRecord:
    id: str
    file_path: str
    language: str
    symbol: str
    start_line: int
    end_line: int
    rule_ids: List[str]
    content: str
    token_count: int

    @property
    def citation_tag(self) -> str:
        return f"[{self.file_path}:L{self.start_line}-L{self.end_line}]"


@dataclass
class RetrievalHit:
    chunk: ChunkRecord
    rrf_score: float
    bm25_rank: int
    dense_rank: int
    bm25_score: float
    dense_score: float


class HybridCodeWikiRetriever:
    """
    Enterprise Hybrid Retriever implementing BM25 + Dense Vector Projection + RRF Fusion.
    Designed per Karpathy guidelines: zero bloated framework wrappers, transparent math, 100% deterministic.
    """

    def __init__(self, chunks: List[ChunkRecord], vector_dim: int = 256, rrf_k: int = 60) -> None:
        self.chunks = chunks
        self.vector_dim = vector_dim
        self.rrf_k = rrf_k

        self.doc_tokens: List[List[str]] = []
        self.doc_freqs: Dict[str, int] = {}
        self.doc_lengths: List[int] = []
        self.avg_dl: float = 1.0

        self._build_bm25_index()
        self.dense_vectors: List[List[float]] = [
            self._compute_dense_embedding(c.content + " " + c.symbol + " " + " ".join(c.rule_ids))
            for c in self.chunks
        ]

    @classmethod
    def from_manifest_file(cls, manifest_path: str | Path) -> "HybridCodeWikiRetriever":
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        records = [
            ChunkRecord(
                id=item["id"],
                file_path=item["file_path"],
                language=item["language"],
                symbol=item["symbol"],
                start_line=item["start_line"],
                end_line=item["end_line"],
                rule_ids=item.get("rule_ids", []),
                content=item["content"],
                token_count=item["token_count"],
            )
            for item in data["chunks"]
        ]
        return cls(records)

    def _build_bm25_index(self) -> None:
        total_len = 0
        for c in self.chunks:
            augmented = f"{c.file_path} {c.symbol} {' '.join(c.rule_ids)} {c.content}"
            tokens = split_camel_and_tokenize(augmented)
            self.doc_tokens.append(tokens)
            self.doc_lengths.append(len(tokens))
            total_len += len(tokens)
            for unique_tok in set(tokens):
                self.doc_freqs[unique_tok] = self.doc_freqs.get(unique_tok, 0) + 1
        self.avg_dl = total_len / max(len(self.chunks), 1)

    def _compute_dense_embedding(self, text: str) -> List[float]:
        """
        Feature-hashed character 3/4-gram + semantic token random projection embedding (L2-normalized).
        Provides deterministic semantic similarity resilient to morphological variations without external API keys.
        """
        vec = [0.0] * self.vector_dim
        tokens = split_camel_and_tokenize(text)
        features: List[Tuple[str, float]] = []
        for tok in tokens:
            idf_weight = self._idf(tok)
            features.append((f"tok:{tok}", idf_weight))
            if len(tok) >= 4:
                for i in range(len(tok) - 2):
                    features.append((f"ng3:{tok[i:i+3]}", 0.4 * idf_weight))

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

    def _idf(self, term: str) -> float:
        n_docs = len(self.chunks)
        df = self.doc_freqs.get(term, 0)
        return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))

    def _score_bm25(self, query_tokens: List[str], doc_idx: int, k1: float = 1.5, b: float = 0.75) -> float:
        tokens = self.doc_tokens[doc_idx]
        dl = self.doc_lengths[doc_idx]
        tf_map: Dict[str, int] = {}
        for t in tokens:
            tf_map[t] = tf_map.get(t, 0) + 1

        score = 0.0
        for qt in query_tokens:
            tf = tf_map.get(qt, 0)
            if tf == 0:
                continue
            idf = self._idf(qt)
            numerator = tf * (k1 + 1.0)
            denominator = tf + k1 * (1.0 - b + b * (dl / max(self.avg_dl, 1.0)))
            score += idf * (numerator / denominator)
        return score

    @staticmethod
    def _cosine_sim(v1: List[float], v2: List[float]) -> float:
        return sum(a * b for a, b in zip(v1, v2))

    def search(self, query: str, top_k: int = 3) -> List[RetrievalHit]:
        query_tokens = split_camel_and_tokenize(query)
        query_vec = self._compute_dense_embedding(query)
        mentioned_rules = {r.upper() for r in RULE_ID_PATTERN.findall(query)}

        bm25_scores = [self._score_bm25(query_tokens, i) for i in range(len(self.chunks))]
        dense_scores = [self._cosine_sim(query_vec, self.dense_vectors[i]) for i in range(len(self.chunks))]

        bm25_order = sorted(range(len(self.chunks)), key=lambda i: bm25_scores[i], reverse=True)
        dense_order = sorted(range(len(self.chunks)), key=lambda i: dense_scores[i], reverse=True)

        bm25_ranks = {doc_idx: rank + 1 for rank, doc_idx in enumerate(bm25_order)}
        dense_ranks = {doc_idx: rank + 1 for rank, doc_idx in enumerate(dense_order)}

        hits: List[RetrievalHit] = []
        for idx, chunk in enumerate(self.chunks):
            r_bm25 = bm25_ranks[idx]
            r_dense = dense_ranks[idx]
            rrf = (1.0 / (self.rrf_k + r_bm25)) + (1.0 / (self.rrf_k + r_dense))

            # Exact Business Rule ID / ADR ID match boost (Domain-aware reranking)
            chunk_rules = {r.upper() for r in chunk.rule_ids}
            if mentioned_rules and (mentioned_rules & chunk_rules):
                rrf += 0.05

            hits.append(
                RetrievalHit(
                    chunk=chunk,
                    rrf_score=round(rrf, 6),
                    bm25_rank=r_bm25,
                    dense_rank=r_dense,
                    bm25_score=round(bm25_scores[idx], 4),
                    dense_score=round(dense_scores[idx], 4),
                )
            )

        hits.sort(key=lambda h: (h.rrf_score, h.bm25_score), reverse=True)
        return hits[:top_k]
