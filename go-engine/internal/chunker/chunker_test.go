package chunker_test

import (
	"testing"

	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/chunker"
)

func TestChunkSourceCodeExtractsRulesAndLines(t *testing.T) {
	sample := `using System;
namespace Demo {
    /// <summary>Rule BR-POL-002 check</summary>
    public class PolicyEngine {
        public void Validate() {
            // line 6
        }
    }
}`
	chunks := chunker.ChunkFile("code/PolicyEngine.cs", sample)
	if len(chunks) == 0 {
		t.Fatalf("expected at least 1 chunk, got 0")
	}
	foundRule := false
	for _, c := range chunks {
		for _, r := range c.RuleIDs {
			if r == "BR-POL-002" {
				foundRule = true
			}
		}
	}
	if !foundRule {
		t.Errorf("expected RuleID BR-POL-002 to be extracted")
	}
}
