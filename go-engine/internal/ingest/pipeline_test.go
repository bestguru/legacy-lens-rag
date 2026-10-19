package ingest_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/ingest"
)

func TestIncrementalDeltaAndAtomicSwap(t *testing.T) {
	tmpDir := t.TempDir()
	corpusDir := filepath.Join(tmpDir, "corpus")
	if err := os.MkdirAll(corpusDir, 0755); err != nil {
		t.Fatal(err)
	}

	fileA := filepath.Join(corpusDir, "ServiceA.cs")
	fileB := filepath.Join(corpusDir, "ServiceB.cs")
	ontologyDir := filepath.Join(corpusDir, "ontology")
	_ = os.MkdirAll(ontologyDir, 0755)
	_ = os.WriteFile(filepath.Join(ontologyDir, "domain_ontology.json"), []byte(`{"version":"3.3.0","rules":["BR-TEST-001"]}`), 0644)
	_ = os.WriteFile(fileA, []byte("public class ServiceA { // BR-TEST-001\n public void Run() {}\n}"), 0644)
	_ = os.WriteFile(fileB, []byte("public class ServiceB { // BR-TEST-002\n public void Exec() {}\n}"), 0644)

	outPath := filepath.Join(tmpDir, "index.json")

	// First full run: 2 files reparsed, 0 reused
	m1, err := ingest.RunThreeLanePipeline(corpusDir, outPath)
	if err != nil {
		t.Fatalf("first run failed: %v", err)
	}
	if m1.ReparsedDeltaFiles != 2 || m1.ReusedCachedFiles != 0 {
		t.Fatalf("expected 2 reparsed and 0 reused on cold start, got reparsed=%d reused=%d", m1.ReparsedDeltaFiles, m1.ReusedCachedFiles)
	}

	// Second run with no changes: 0 reparsed, 2 reused from SHA-256 Merkle cache
	m2, err := ingest.RunThreeLanePipeline(corpusDir, outPath)
	if err != nil {
		t.Fatalf("second run failed: %v", err)
	}
	if m2.ReparsedDeltaFiles != 0 || m2.ReusedCachedFiles != 2 {
		t.Fatalf("expected 0 reparsed and 2 reused on warm run, got reparsed=%d reused=%d", m2.ReparsedDeltaFiles, m2.ReusedCachedFiles)
	}
	if m1.ManifestChecksum != m2.ManifestChecksum {
		t.Fatalf("expected identical checksum across unchanged runs")
	}

	// Modify only fileB: 1 reparsed, 1 reused
	_ = os.WriteFile(fileB, []byte("public class ServiceB { // BR-TEST-002 updated\n public void ExecV2() {}\n}"), 0644)
	m3, err := ingest.RunThreeLanePipeline(corpusDir, outPath)
	if err != nil {
		t.Fatalf("third delta run failed: %v", err)
	}
	if m3.ReparsedDeltaFiles != 1 || m3.ReusedCachedFiles != 1 {
		t.Fatalf("expected 1 reparsed and 1 reused on single-file delta, got reparsed=%d reused=%d", m3.ReparsedDeltaFiles, m3.ReusedCachedFiles)
	}
}
