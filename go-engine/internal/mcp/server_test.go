package mcp

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/chunker"
	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/ingest"
)

func buildSyntheticChunks(n int) []chunker.Chunk {
	chunks := make([]chunker.Chunk, n)
	for i := 0; i < n; i++ {
		ruleID := fmt.Sprintf("BR-SYN-%04d", i)
		symbol := fmt.Sprintf("FuncSynthetic_%04d", i)
		if i == n-1 {
			ruleID = "BR-POL-002"
			symbol = "AuthorizeSessionCommand"
		}
		chunks[i] = chunker.Chunk{
			ID:                fmt.Sprintf("chunk_%04d", i),
			FilePath:          fmt.Sprintf("legacy/mod_%02d.go", i%20),
			Language:          "go",
			Symbol:            symbol,
			StartLine:         10,
			EndLine:           45,
			RuleIDs:           []string{ruleID},
			SymbolsDefined:    []string{symbol},
			SymbolsReferenced: []string{"AuditTrail"},
			Content:           fmt.Sprintf("// Business Rule %s\nfunc %s() {}", ruleID, symbol),
		}
	}
	return chunks
}

func TestServer_LockFreeRCUHotSwapAndGo122Routing(t *testing.T) {
	tmpDir := t.TempDir()
	indexFile := filepath.Join(tmpDir, "chunks_index.json")

	initialChunks := buildSyntheticChunks(64)
	srv := NewServer(initialChunks, indexFile, "")

	// 1. Verify O(1) Offset Lookup on initial snapshot
	hits := srv.LookupChunks("BR-POL-002")
	if len(hits) != 1 || hits[0].Symbol != "AuthorizeSessionCommand" {
		t.Fatalf("expected 1 hit for BR-POL-002, got %d", len(hits))
	}

	// 2. Write an updated index manifest on disk for Hot-Swap
	updatedChunks := append(buildSyntheticChunks(64), chunker.Chunk{
		ID:             "chunk_new_ha",
		FilePath:       "ha/failover.go",
		Language:       "go",
		Symbol:         "TriggerZeroDowntimeFailover",
		StartLine:      1,
		EndLine:        20,
		RuleIDs:        []string{"BR-HA-999"},
		SymbolsDefined: []string{"TriggerZeroDowntimeFailover"},
		Content:        "func TriggerZeroDowntimeFailover() // enforces BR-HA-999",
	})
	manifest := ingest.IndexManifest{
		Version:          "3.1.0-lockfree-rcu",
		TotalFiles:       21,
		TotalChunks:      len(updatedChunks),
		ManifestChecksum: "sha256_verified_rcu",
		Chunks:           updatedChunks,
	}
	raw, err := json.Marshal(manifest)
	if err != nil {
		t.Fatalf("marshal error: %v", err)
	}
	if err := os.WriteFile(indexFile, raw, 0644); err != nil {
		t.Fatalf("write error: %v", err)
	}

	// 3. Concurrent Wait-Free readers + concurrent HotReloadIndex writers
	var wg sync.WaitGroup
	for r := 0; r < 16; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 200; i++ {
				res := srv.LookupChunks("BR-POL-002")
				if len(res) == 0 {
					t.Errorf("concurrent wait-free reader saw empty result during hot-swap")
				}
			}
		}()
	}
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := srv.HotReloadIndex(); err != nil {
				t.Errorf("HotReloadIndex failed: %v", err)
			}
		}()
	}
	wg.Wait()

	// 4. Verify new rule is immediately searchable via O(1) offset map after swap
	newHits := srv.LookupChunks("BR-HA-999")
	if len(newHits) != 1 || newHits[0].Symbol != "TriggerZeroDowntimeFailover" {
		t.Fatalf("expected newly swapped BR-HA-999 chunk, got %d hits", len(newHits))
	}

	// 5. Verify Go 1.22 method-based ServeMux routing (GET /readyz allowed, GET /reload rejected with 405, POST /reload allowed)
	mux := http.NewServeMux()
	srv.RegisterHAEndpoints(mux)

	reqReady := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	recReady := httptest.NewRecorder()
	mux.ServeHTTP(recReady, reqReady)
	if recReady.Code != http.StatusOK {
		t.Fatalf("GET /readyz expected 200, got %d", recReady.Code)
	}

	reqBadReload := httptest.NewRequest(http.MethodGet, "/reload", nil)
	recBadReload := httptest.NewRecorder()
	mux.ServeHTTP(recBadReload, reqBadReload)
	if recBadReload.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET /reload should be blocked by Go 1.22 method routing with 405, got %d", recBadReload.Code)
	}

	reqPostReload := httptest.NewRequest(http.MethodPost, "/reload", nil)
	recPostReload := httptest.NewRecorder()
	mux.ServeHTTP(recPostReload, reqPostReload)
	if recPostReload.Code != http.StatusOK {
		t.Fatalf("POST /reload expected 200, got %d", recPostReload.Code)
	}
}

// BenchmarkLookup_LegacyRWMutexLinearScan measures the legacy sync.RWMutex + O(N) slice scan under multi-core contention.
func BenchmarkLookup_LegacyRWMutexLinearScan(b *testing.B) {
	chunks := buildSyntheticChunks(1000)
	var mu sync.RWMutex
	query := "BR-POL-002"

	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			mu.RLock()
			snapshot := chunks
			mu.RUnlock()

			var count int
			for _, c := range snapshot {
				if strings.Contains(strings.ToUpper(c.Content), query) || strings.Contains(strings.ToUpper(c.Symbol), query) {
					count++
				}
			}
			if count == 0 {
				b.Fatal("expected match")
			}
		}
	})
}

// BenchmarkLookup_AtomicPointerOffsetIndex measures the new Wait-Free atomic.Pointer[IndexSnapshot] + O(1) int32 offset lookup.
func BenchmarkLookup_AtomicPointerOffsetIndex(b *testing.B) {
	chunks := buildSyntheticChunks(1000)
	srv := NewServer(chunks, "", "")
	query := "BR-POL-002"

	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			matched := srv.LookupChunks(query)
			if len(matched) == 0 {
				b.Fatal("expected match")
			}
		}
	})
}
