"""
3-Way Hybrid GraphRAG Retriever v3.0 (Large-Scale & High-Availability Edition):
1. Two-Stage Candidate Pruning via Inverted Index Posting Lists (`O(1)` lookup -> Top-M candidates)
   eliminating `O(N)` full-corpus vector scans on large 100K+ chunk repositories.
2. Okapi BM25 + Dense Vector Cosine + 1-Hop Code/ADR Dependency Graph Expansion
3. Reciprocal Rank Fusion (RRF, k=60) + Domain Rule-ID Exact Boost
4. Bounded LRU Query Result Cache for sub-millisecond repeat query serving.
"""
from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .providers import EmbeddingProvider, LocalDeterministicEmbedder, expand_and_tokenize


RULE_ID_PATTERN = re.compile(r"\b(?:BR-[A-Z]+-\d+[A-Z]?|ADR-\d+)\b", re.IGNORECASE)


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
    parent_symbol: str = ""
    symbols_defined: List[str] = field(default_factory=list)
    symbols_referenced: List[str] = field(default_factory=list)

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
    graph_boost: float = 0.0
    linked_neighbors: List[str] = field(default_factory=list)
    candidates_evaluated: int = 0


class HybridCodeWikiRetriever:
    """
    Enterprise 3-Way Hybrid GraphRAG Retriever with Two-Stage Inverted Index Pruning & LRU Cache.
    """

    def __init__(
        self,
        chunks: List[ChunkRecord],
        embedder: Optional[EmbeddingProvider] = None,
        rrf_k: int = 60,
        candidate_pool_cap: int = 120,
        lru_cache_capacity: int = 256,
    ) -> None:
        self.chunks = chunks
        self.rrf_k = rrf_k
        self.candidate_pool_cap = candidate_pool_cap
        self.lru_cache_capacity = lru_cache_capacity
        self._query_cache: OrderedDict[Tuple[str, int], List[RetrievalHit]] = OrderedDict()
        self.cache_hits: int = 0

        self.doc_term_freqs: List[Dict[str, int]] = []
        self.doc_freqs: Dict[str, int] = {}
        self.inverted_index: Dict[str, List[int]] = {}
        self.doc_lengths: List[int] = []
        self.avg_dl: float = 1.0

        # Graph adjacency index: symbol/rule -> list of chunk indices defining or referencing it
        self.symbol_to_defining_chunks: Dict[str, List[int]] = {}
        self.symbol_to_referencing_chunks: Dict[str, List[int]] = {}

        self._build_inverted_and_graph_index()

        self.embedder: EmbeddingProvider = embedder or LocalDeterministicEmbedder(
            doc_freqs=self.doc_freqs, total_docs=len(self.chunks)
        )
        self.dense_vectors: List[List[float]] = [
            self.embedder.embed_text(
                f"{c.symbol} {' '.join(c.rule_ids)} {' '.join(c.symbols_referenced)} {c.content}"
            )
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
                parent_symbol=item.get("parent_symbol", ""),
                start_line=item["start_line"],
                end_line=item["end_line"],
                rule_ids=item.get("rule_ids", []),
                symbols_defined=item.get("symbols_defined", []),
                symbols_referenced=item.get("symbols_referenced", []),
                content=item["content"],
                token_count=item["token_count"],
            )
            for item in data["chunks"]
        ]
        return cls(records)

    def _build_inverted_and_graph_index(self) -> None:
        total_len = 0
        for idx, c in enumerate(self.chunks):
            augmented = (
                f"{c.file_path} {c.symbol} {' '.join(c.rule_ids)} "
                f"{' '.join(c.symbols_referenced)} {c.content}"
            )
            tokens = expand_and_tokenize(augmented)
            tf_map: Dict[str, int] = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1
            self.doc_term_freqs.append(tf_map)
            self.doc_lengths.append(len(tokens))
            total_len += len(tokens)

            for unique_tok in tf_map:
                self.doc_freqs[unique_tok] = self.doc_freqs.get(unique_tok, 0) + 1
                self.inverted_index.setdefault(unique_tok, []).append(idx)

            for sym in set(c.symbols_defined + c.rule_ids):
                key = sym.upper()
                self.symbol_to_defining_chunks.setdefault(key, []).append(idx)
                for part in sym.split("."):
                    self.symbol_to_defining_chunks.setdefault(part.upper(), []).append(idx)

            for ref in set(c.symbols_referenced):
                key = ref.upper()
                self.symbol_to_referencing_chunks.setdefault(key, []).append(idx)

        self.avg_dl = total_len / max(len(self.chunks), 1)

    def _idf(self, term: str) -> float:
        n_docs = len(self.chunks)
        df = self.doc_freqs.get(term, 0)
        return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))

    def _score_bm25_fast(self, query_tokens: List[str], doc_idx: int, k1: float = 1.5, b: float = 0.75) -> float:
        tf_map = self.doc_term_freqs[doc_idx]
        dl = self.doc_lengths[doc_idx]
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

    def _prune_candidate_indices(self, query_tokens: List[str], mentioned_rules: Set[str]) -> List[int]:
        """
        Stage 1 (Candidate Pruning): Uses the Inverted Index posting lists and Rule/Symbol hash maps
        to select at most `candidate_pool_cap` promising chunk indices in O(K) instead of scanning all N chunks.
        """
        total_chunks = len(self.chunks)
        if total_chunks <= self.candidate_pool_cap:
            return list(range(total_chunks))

        candidate_weights: Dict[int, float] = {}
        for r in mentioned_rules:
            for idx in self.symbol_to_defining_chunks.get(r, []):
                candidate_weights[idx] = candidate_weights.get(idx, 0.0) + 15.0

        for qt in set(query_tokens):
            posting = self.inverted_index.get(qt)
            if not posting:
                continue
            idf = self._idf(qt)
            for idx in posting:
                candidate_weights[idx] = candidate_weights.get(idx, 0.0) + idf

        if not candidate_weights:
            return list(range(min(total_chunks, self.candidate_pool_cap)))

        sorted_candidates = sorted(
            candidate_weights.keys(), key=lambda i: candidate_weights[i], reverse=True
        )[: self.candidate_pool_cap]

        # Expand 1-hop graph neighbors of top 5 lexical seeds into the candidate pool
        expanded_set = set(sorted_candidates)
        for seed_idx in sorted_candidates[:5]:
            s_chunk = self.chunks[seed_idx]
            for ref in s_chunk.symbols_referenced + s_chunk.rule_ids:
                for target_idx in self.symbol_to_defining_chunks.get(ref.upper(), []):
                    expanded_set.add(target_idx)

        return sorted(expanded_set)

    def get_one_hop_neighbors(self, chunk_idx: int) -> List[str]:
        """Returns citation tags of 1-hop connected chunks via shared symbols or ADR/Rule references."""
        chunk = self.chunks[chunk_idx]
        neighbor_indices: Set[int] = set()

        for ref in chunk.symbols_referenced + chunk.rule_ids:
            key = ref.upper()
            for target_idx in self.symbol_to_defining_chunks.get(key, []):
                if target_idx != chunk_idx:
                    neighbor_indices.add(target_idx)

        for defined in chunk.symbols_defined + chunk.rule_ids:
            key = defined.upper()
            for ref_idx in self.symbol_to_referencing_chunks.get(key, []):
                if ref_idx != chunk_idx:
                    neighbor_indices.add(ref_idx)

        return [self.chunks[i].citation_tag for i in sorted(neighbor_indices)]

    def search(self, query: str, top_k: int = 3) -> List[RetrievalHit]:
        cache_key = (query.strip(), top_k)
        if cache_key in self._query_cache:
            self.cache_hits += 1
            self._query_cache.move_to_end(cache_key)
            return self._query_cache[cache_key]

        query_tokens = expand_and_tokenize(query)
        query_vec = self.embedder.embed_text(query)
        mentioned_rules = {r.upper() for r in RULE_ID_PATTERN.findall(query)}

        # Stage 1: Inverted Index Candidate Pruning
        candidate_indices = self._prune_candidate_indices(query_tokens, mentioned_rules)
        num_evaluated = len(candidate_indices)

        # Stage 2: Exact BM25 + Dense Vector + 1-Hop GraphRAG Scoring on Candidate Pool
        bm25_scores = {i: self._score_bm25_fast(query_tokens, i) for i in candidate_indices}
        dense_scores = {i: self._cosine_sim(query_vec, self.dense_vectors[i]) for i in candidate_indices}

        bm25_order = sorted(candidate_indices, key=lambda i: bm25_scores[i], reverse=True)
        dense_order = sorted(candidate_indices, key=lambda i: dense_scores[i], reverse=True)

        bm25_ranks = {doc_idx: rank + 1 for rank, doc_idx in enumerate(bm25_order)}
        dense_ranks = {doc_idx: rank + 1 for rank, doc_idx in enumerate(dense_order)}

        seed_indices = {bm25_order[0], dense_order[0]}
        graph_connected_to_seed: Set[int] = set()
        for s_idx in seed_indices:
            s_chunk = self.chunks[s_idx]
            for ref in s_chunk.symbols_referenced + s_chunk.rule_ids:
                for target_idx in self.symbol_to_defining_chunks.get(ref.upper(), []):
                    if target_idx != s_idx:
                        graph_connected_to_seed.add(target_idx)

        hits: List[RetrievalHit] = []
        for idx in candidate_indices:
            chunk = self.chunks[idx]
            r_bm25 = bm25_ranks[idx]
            r_dense = dense_ranks[idx]
            rrf = (1.0 / (self.rrf_k + r_bm25)) + (1.0 / (self.rrf_k + r_dense))

            g_boost = 0.0
            chunk_rules = {r.upper() for r in chunk.rule_ids}
            if mentioned_rules and (mentioned_rules & chunk_rules):
                g_boost += 0.05

            if idx in graph_connected_to_seed:
                g_boost += 0.008

            total_rrf = rrf + g_boost
            hits.append(
                RetrievalHit(
                    chunk=chunk,
                    rrf_score=round(total_rrf, 6),
                    bm25_rank=r_bm25,
                    dense_rank=r_dense,
                    bm25_score=round(bm25_scores[idx], 4),
                    dense_score=round(dense_scores[idx], 4),
                    graph_boost=round(g_boost, 4),
                    linked_neighbors=self.get_one_hop_neighbors(idx),
                    candidates_evaluated=num_evaluated,
                )
            )

        hits.sort(key=lambda h: (h.rrf_score, h.bm25_score), reverse=True)
        top_results = hits[:top_k]

        self._query_cache[cache_key] = top_results
        if len(self._query_cache) > self.lru_cache_capacity:
            self._query_cache.popitem(last=False)

        return top_results
