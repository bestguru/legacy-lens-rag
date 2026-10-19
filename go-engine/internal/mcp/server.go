package mcp

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/chunker"
	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/ingest"
)

// JSONRPCRequest represents a Model Context Protocol (MCP) JSON-RPC 2.0 request.
type JSONRPCRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      interface{}     `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// JSONRPCResponse represents a Model Context Protocol (MCP) JSON-RPC 2.0 response.
type JSONRPCResponse struct {
	JSONRPC string      `json:"jsonrpc"`
	ID      interface{} `json:"id"`
	Result  interface{} `json:"result,omitempty"`
	Error   interface{} `json:"error,omitempty"`
}

// IndexSnapshot is an immutable, pre-indexed snapshot swapped atomically via sync/atomic.Pointer[IndexSnapshot]
// (Lock-Free Read-Copy-Update / RCU pattern).
//
// GC Optimization: RuleOffsets and SymbolOffsets store pointer-free []int32 slice offsets into Chunks
// rather than duplicating Chunk structs or pointers. Go's runtime allocator places []int32 backing arrays
// in `noscan` spans, reducing Garbage Collector mark-phase traversal overhead to near zero.
type IndexSnapshot struct {
	Chunks           []chunker.Chunk
	ManifestChecksum string
	RuleOffsets      map[string][]int32
	SymbolOffsets    map[string][]int32
	LoadedAt         time.Time
}

// BuildIndexSnapshot constructs an immutable IndexSnapshot with O(1) int32 offset inverted maps.
func BuildIndexSnapshot(chunks []chunker.Chunk, checksum string) *IndexSnapshot {
	ruleOffsets := make(map[string][]int32, len(chunks)*2)
	symbolOffsets := make(map[string][]int32, len(chunks)*2)

	appendUnique := func(m map[string][]int32, key string, idx int32) {
		k := strings.ToUpper(strings.TrimSpace(key))
		if k == "" {
			return
		}
		existing := m[k]
		if !slices.Contains(existing, idx) {
			m[k] = append(existing, idx)
		}
	}

	for i, c := range chunks {
		idx := int32(i)
		for _, r := range c.RuleIDs {
			appendUnique(ruleOffsets, r, idx)
		}
		appendUnique(symbolOffsets, c.Symbol, idx)
		for _, sym := range c.SymbolsDefined {
			appendUnique(symbolOffsets, sym, idx)
		}
		for _, sym := range c.SymbolsReferenced {
			appendUnique(symbolOffsets, sym, idx)
		}
	}

	return &IndexSnapshot{
		Chunks:           chunks,
		ManifestChecksum: checksum,
		RuleOffsets:      ruleOffsets,
		SymbolOffsets:    symbolOffsets,
		LoadedAt:         time.Now(),
	}
}

// Server exposes indexed chunks using an Asymmetric Lock-Free RCU architecture:
//   - Read Path: 100% Wait-Free atomic.Pointer[IndexSnapshot].Load() (zero RWMutex readerCount cache-line bouncing).
//   - Write Path: Coalesced reloadMu guard preventing reload stampedes while building the new immutable snapshot off-lock.
type Server struct {
	snapshot      atomic.Pointer[IndexSnapshot]
	reloadMu      sync.Mutex
	IndexPath     string
	PythonCliPath string
	TotalRequests atomic.Int64
	HotSwapCount  atomic.Int64
	StartedAt     time.Time
}

func NewServer(chunks []chunker.Chunk, indexPath string, pythonCliPath string) *Server {
	s := &Server{
		IndexPath:     indexPath,
		PythonCliPath: pythonCliPath,
		StartedAt:     time.Now(),
	}
	s.snapshot.Store(BuildIndexSnapshot(chunks, "initial"))
	return s
}

// Snapshot returns the current immutable IndexSnapshot via a single wait-free atomic pointer load.
func (s *Server) Snapshot() *IndexSnapshot {
	snap := s.snapshot.Load()
	if snap == nil {
		return BuildIndexSnapshot(nil, "")
	}
	return snap
}

// HotReloadIndex builds a fresh immutable IndexSnapshot off the read path and atomically swaps
// the 8-byte snapshot pointer via atomic.Pointer.Store (0ns reader blocking, zero-downtime RCU).
func (s *Server) HotReloadIndex() error {
	s.reloadMu.Lock()
	defer s.reloadMu.Unlock()

	raw, err := os.ReadFile(s.IndexPath)
	if err != nil {
		return err
	}
	var manifest ingest.IndexManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return err
	}

	newSnap := BuildIndexSnapshot(manifest.Chunks, manifest.ManifestChecksum)
	s.snapshot.Store(newSnap)
	s.HotSwapCount.Add(1)
	return nil
}

// LookupChunks performs a Wait-Free RCU read with O(1) offset map lookup, falling back to substring scan only on miss.
func (s *Server) LookupChunks(query string) []chunker.Chunk {
	snap := s.Snapshot()
	q := strings.ToUpper(strings.TrimSpace(query))
	if q == "" || len(snap.Chunks) == 0 {
		return nil
	}

	// Stage 1: O(1) Lock-Free Offset Map Lookup (RuleOffsets & SymbolOffsets)
	ruleHits := snap.RuleOffsets[q]
	symHits := snap.SymbolOffsets[q]
	if len(ruleHits) > 0 || len(symHits) > 0 {
		matched := make([]chunker.Chunk, 0, len(ruleHits)+len(symHits))
		seen := make([]int32, 0, len(ruleHits)+len(symHits))
		for _, idx := range ruleHits {
			seen = append(seen, idx)
			matched = append(matched, snap.Chunks[idx])
		}
		for _, idx := range symHits {
			if !slices.Contains(seen, idx) {
				seen = append(seen, idx)
				matched = append(matched, snap.Chunks[idx])
			}
		}
		return matched
	}

	// Stage 2: Fallback substring scan over immutable snapshot
	var matched []chunker.Chunk
	for _, c := range snap.Chunks {
		if strings.Contains(strings.ToUpper(c.Content), q) || strings.Contains(strings.ToUpper(c.Symbol), q) {
			matched = append(matched, c)
		}
	}
	return matched
}

// RegisterHAEndpoints attaches Go 1.22 method-aware HTTP probes (GET /healthz, GET /readyz, GET /metrics, POST /reload).
func (s *Server) RegisterHAEndpoints(mux *http.ServeMux) {
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"healthy","version":"3.3.0","concurrency_model":"lock_free_rcu_atomic_pointer"}`))
	})

	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		snap := s.Snapshot()
		count := len(snap.Chunks)
		w.Header().Set("Content-Type", "application/json")
		if count == 0 {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"ready":false,"reason":"empty_index"}`))
			return
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"ready":true,"indexed_chunks":%d,"indexed_rules":%d,"manifest_checksum":"%s"}`,
			count, len(snap.RuleOffsets), snap.ManifestChecksum)
	})

	mux.HandleFunc("GET /metrics", func(w http.ResponseWriter, r *http.Request) {
		snap := s.Snapshot()
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"total_requests":%d,"hot_swap_count":%d,"indexed_chunks":%d,"indexed_rules":%d,"indexed_symbols":%d,"uptime_sec":%d}`,
			s.TotalRequests.Load(), s.HotSwapCount.Load(), len(snap.Chunks), len(snap.RuleOffsets), len(snap.SymbolOffsets), int(time.Since(s.StartedAt).Seconds()))
	})

	mux.HandleFunc("POST /reload", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if err := s.HotReloadIndex(); err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(map[string]any{"reloaded": false, "error": err.Error()})
			return
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"reloaded":true,"hot_swap_count":%d}`, s.HotSwapCount.Load())
	})
}

func (s *Server) ServeStdio(in io.Reader, out io.Writer) error {
	scanner := bufio.NewScanner(in)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var req JSONRPCRequest
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			continue
		}
		resp := s.HandleRequest(req)
		b, _ := json.Marshal(resp)
		fmt.Fprintln(out, string(b))
	}
	return scanner.Err()
}

func (s *Server) HandleRequest(req JSONRPCRequest) JSONRPCResponse {
	s.TotalRequests.Add(1)

	switch req.Method {
	case "initialize":
		return JSONRPCResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Result: map[string]interface{}{
				"protocolVersion": "2024-11-05",
				"serverInfo": map[string]string{
					"name":    "legacy-lens-rag-mcp-ha",
					"version": "3.3.0",
				},
				"capabilities": map[string]interface{}{
					"tools": map[string]bool{"listChanged": true},
				},
			},
		}
	case "tools/list":
		return JSONRPCResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Result: map[string]interface{}{
				"tools": []map[string]interface{}{
					{
						"name":        "lookup_business_rule",
						"description": "Wait-Free RCU (atomic.Pointer[IndexSnapshot]) + O(1) int32 offset lookup of legacy business rules (BR-*) and ADRs.",
						"inputSchema": map[string]interface{}{
							"type": "object",
							"properties": map[string]interface{}{
								"rule_id": map[string]string{"type": "string", "description": "Business rule ID or symbol (e.g. BR-POL-002, ADR-001)"},
							},
							"required": []string{"rule_id"},
						},
					},
					{
						"name":        "hybrid_rag_query",
						"description": "Executes the Python 3-Way Hybrid GraphRAG Retriever (2-Stage Inverted Index Pruning + Circuit Breaker Fallback) and Self-RAG Verifier.",
						"inputSchema": map[string]interface{}{
							"type": "object",
							"properties": map[string]interface{}{
								"query": map[string]string{"type": "string", "description": "Natural language question in English or Korean"},
							},
							"required": []string{"query"},
						},
					},
					{
						"name":        "hot_reload_index",
						"description": "Performs a wait-free atomic.Pointer[IndexSnapshot] RCU swap to reload the latest chunks_index.json with zero downtime.",
						"inputSchema": map[string]interface{}{
							"type":       "object",
							"properties": map[string]interface{}{},
						},
					},
				},
			},
		}
	case "tools/call":
		var callParams struct {
			Name      string            `json:"name"`
			Arguments map[string]string `json:"arguments"`
		}
		_ = json.Unmarshal(req.Params, &callParams)

		if callParams.Name == "hot_reload_index" {
			err := s.HotReloadIndex()
			return JSONRPCResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Result: map[string]interface{}{
					"reloaded":       err == nil,
					"hot_swap_count": s.HotSwapCount.Load(),
				},
			}
		}

		if callParams.Name == "hybrid_rag_query" && s.PythonCliPath != "" {
			q := strings.TrimSpace(callParams.Arguments["query"])
			cmd := exec.Command("python", s.PythonCliPath, "--index", s.IndexPath, "--query", q)
			outBytes, err := cmd.Output()
			if err == nil {
				var pyResult map[string]interface{}
				if json.Unmarshal(outBytes, &pyResult) == nil {
					return JSONRPCResponse{
						JSONRPC: "2.0",
						ID:      req.ID,
						Result:  pyResult,
					}
				}
			}
		}

		query := callParams.Arguments["rule_id"]
		if query == "" {
			query = callParams.Arguments["query"]
		}

		matched := s.LookupChunks(query)
		return JSONRPCResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Result: map[string]interface{}{
				"matches": matched,
				"count":   len(matched),
			},
		}
	default:
		return JSONRPCResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Error:   map[string]interface{}{"code": -32601, "message": "Method not found"},
		}
	}
}
