"""
Enterprise Domain Ontology Engine (v3.2):
Constructs a typed RDF-style Knowledge Graph (`Subject -[Predicate]-> Object`) over AST code chunks,
Business Rules (`BR-*`), Architecture Decision Records (`ADR-*`), and Canonical Ubiquitous Language Concepts.

Provides:
1. Automated Triple Extraction from ingested code/wiki chunks (`ENFORCES_RULE`, `GOVERNS_RULE`, `GOVERNS_COMPONENT`, `DEPENDS_ON_SYMBOL`, `REQUIRES_CONCEPT`).
2. Strict Ontology Schema Validation (verifying 100% Domain -> Range predicate type compliance).
3. Multi-Hop Ontology Reasoning Path Traversal (`CodeArtifact --[ENFORCES_RULE]--> BusinessRule <--[GOVERNS_RULE]-- ArchitectureDecision`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


@dataclass(frozen=True)
class OntologyTriple:
    subject_id: str
    subject_type: str
    predicate: str
    object_id: str
    object_type: str
    weight: float
    source_chunk_id: str

    def format_edge(self) -> str:
        return f"({self.subject_type}:{self.subject_id}) --[{self.predicate}]--> ({self.object_type}:{self.object_id})"


class DomainOntologyEngine:
    """
    Governs typed semantic relationships and multi-hop reasoning across Code, Business Rules, ADRs, and Domain Concepts.
    """

    def __init__(self, schema_path: Optional[Path] = None) -> None:
        if schema_path is None:
            schema_path = Path(__file__).resolve().parents[2] / "sample-corpus" / "ontology" / "domain_ontology.json"
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.entity_classes: Dict[str, dict] = self.schema["entity_classes"]
        self.predicates: Dict[str, dict] = self.schema["predicates"]
        self.canonical_concepts: List[dict] = self.schema.get("canonical_concepts", [])

        self.triples: List[OntologyTriple] = []
        self.outgoing_by_subject: Dict[str, List[OntologyTriple]] = {}
        self.incoming_by_object: Dict[str, List[OntologyTriple]] = {}
        self.rule_to_concepts: Dict[str, List[str]] = {}

        self._index_canonical_concepts()

    def _index_canonical_concepts(self) -> None:
        pred_weight = self.predicates["REQUIRES_CONCEPT"]["weight"]
        for concept in self.canonical_concepts:
            cid = concept["id"]
            for rule_id in concept.get("bound_rules", []):
                r_upper = rule_id.upper()
                self.rule_to_concepts.setdefault(r_upper, []).append(cid)
                triple = OntologyTriple(
                    subject_id=r_upper,
                    subject_type="BusinessRule",
                    predicate="REQUIRES_CONCEPT",
                    object_id=cid,
                    object_type="DomainConcept",
                    weight=pred_weight,
                    source_chunk_id="ontology_schema",
                )
                self._add_triple(triple)

    def _add_triple(self, triple: OntologyTriple) -> None:
        self.triples.append(triple)
        self.outgoing_by_subject.setdefault(triple.subject_id, []).append(triple)
        self.incoming_by_object.setdefault(triple.object_id, []).append(triple)

    def build_knowledge_graph_from_chunks(self, chunks: list) -> int:
        """
        Extracts typed OntologyTriples from ChunkRecord objects according to the formal ontology schema.
        """
        defined_symbol_owners: Dict[str, str] = {}
        for c in chunks:
            is_adr = c.file_path.startswith("llm-wiki/") or c.language == "markdown"
            if not is_adr:
                for sym in c.symbols_defined:
                    defined_symbol_owners[sym.upper()] = c.symbol

        for c in chunks:
            is_adr = c.file_path.startswith("llm-wiki/") or c.language == "markdown"
            subj_type = "ArchitectureDecision" if is_adr else "CodeArtifact"
            subj_id = c.symbol

            for raw_rule in c.rule_ids:
                rule_id = raw_rule.upper()
                if rule_id.startswith("ADR-") and is_adr:
                    subj_id = rule_id
                    continue

                predicate = "GOVERNS_RULE" if is_adr else "ENFORCES_RULE"
                weight = self.predicates[predicate]["weight"]
                self._add_triple(
                    OntologyTriple(
                        subject_id=subj_id,
                        subject_type=subj_type,
                        predicate=predicate,
                        object_id=rule_id,
                        object_type="BusinessRule",
                        weight=weight,
                        source_chunk_id=c.id,
                    )
                )

            for ref_sym in c.symbols_referenced:
                target_owner = defined_symbol_owners.get(ref_sym.upper())
                if not target_owner or target_owner == subj_id:
                    continue
                predicate = "GOVERNS_COMPONENT" if is_adr else "DEPENDS_ON_SYMBOL"
                weight = self.predicates[predicate]["weight"]
                self._add_triple(
                    OntologyTriple(
                        subject_id=subj_id,
                        subject_type=subj_type,
                        predicate=predicate,
                        object_id=target_owner,
                        object_type="CodeArtifact",
                        weight=weight,
                        source_chunk_id=c.id,
                    )
                )

        return len(self.triples)

    def validate_schema_compliance(self) -> Tuple[bool, int, List[str]]:
        """
        Verifies that every OntologyTriple strictly obeys the Domain -> Predicate -> Range type constraints.
        """
        violations: List[str] = []
        for t in self.triples:
            if t.predicate not in self.predicates:
                violations.append(f"Unknown predicate: {t.predicate}")
                continue
            spec = self.predicates[t.predicate]
            if t.subject_type != spec["domain"]:
                violations.append(
                    f"Domain violation on {t.format_edge()}: expected {spec['domain']}, got {t.subject_type}"
                )
            if t.object_type != spec["range"]:
                violations.append(
                    f"Range violation on {t.format_edge()}: expected {spec['range']}, got {t.object_type}"
                )
            if t.subject_type not in self.entity_classes or t.object_type not in self.entity_classes:
                violations.append(f"Unregistered entity class in {t.format_edge()}")

        return len(violations) == 0, len(self.triples), violations

    def resolve_query_concepts(self, query: str) -> Tuple[List[str], Set[str]]:
        """
        Maps a Korean or English natural-language query to canonical DomainConcept IDs and bound BusinessRules.
        """
        q_lower = query.lower()
        matched_concepts: List[str] = []
        inferred_rules: Set[str] = set()

        for concept in self.canonical_concepts:
            labels = concept.get("en_labels", []) + concept.get("ko_labels", [])
            if any(lbl.lower() in q_lower for lbl in labels):
                matched_concepts.append(concept["id"])
                for r in concept.get("bound_rules", []):
                    inferred_rules.add(r.upper())

        return matched_concepts, inferred_rules

    def trace_reasoning_paths_for_chunk(self, chunk) -> List[str]:
        """
        Generates human-auditable multi-hop Ontology Reasoning Paths explaining how a chunk connects
        across CodeArtifact -> BusinessRule -> DomainConcept / ArchitectureDecision.
        """
        paths: List[str] = []
        is_adr = chunk.file_path.startswith("llm-wiki/") or chunk.language == "markdown"
        node_type = "ArchitectureDecision" if is_adr else "CodeArtifact"
        node_id = chunk.symbol

        for raw_rule in chunk.rule_ids:
            r_id = raw_rule.upper()
            pred = "GOVERNS_RULE" if is_adr else "ENFORCES_RULE"
            base_edge = f"({node_type}:{node_id}) --[{pred}]--> (BusinessRule:{r_id})"

            concepts = self.rule_to_concepts.get(r_id, [])
            if concepts:
                paths.append(f"{base_edge} --[REQUIRES_CONCEPT]--> (DomainConcept:{concepts[0]})")
            else:
                paths.append(base_edge)

            # Check if an ADR governs this rule or a CodeArtifact enforces it (2-Hop Bridge)
            for inc in self.incoming_by_object.get(r_id, []):
                if inc.subject_id != node_id:
                    paths.append(
                        f"{base_edge} <--[{inc.predicate}]-- ({inc.subject_type}:{inc.subject_id})"
                    )

        return paths[:4]
