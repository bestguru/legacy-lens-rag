package chunker_test

import (
	"testing"

	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/chunker"
)

func TestChunkSourceCodeExtractsRulesAndGraphEdges(t *testing.T) {
	sample := `using System;
namespace Demo {
    /// <summary>Rule BR-POL-002 check calling CommandPolicyEngine</summary>
    public class SessionGateway {
        public void ValidateSession() {
            // Invokes CommandPolicyEngine and references ADR-001
            var engine = new CommandPolicyEngine();
        }
    }
}`
	chunks := chunker.ChunkFile("code/SessionGateway.cs", sample)
	if len(chunks) == 0 {
		t.Fatalf("expected at least 1 chunk, got 0")
	}

	foundRule := false
	foundRef := false
	for _, c := range chunks {
		for _, r := range c.RuleIDs {
			if r == "BR-POL-002" {
				foundRule = true
			}
		}
		for _, ref := range c.SymbolsReferenced {
			if ref == "CommandPolicyEngine" || ref == "ADR-001" {
				foundRef = true
			}
		}
	}
	if !foundRule {
		t.Errorf("expected RuleID BR-POL-002 to be extracted")
	}
	if !foundRef {
		t.Errorf("expected GraphRAG SymbolsReferenced to include CommandPolicyEngine or ADR-001")
	}
}
