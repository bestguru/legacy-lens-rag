package mcp

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
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

// Server exposes the indexed chunks with RWMutex Zero-Downtime Hot-Swap and HA observability endpoints.
type Server struct {
	mu               sync.RWMutex
	Chunks           []chunker.Chunk
	ManifestChecksum string
	IndexPath        string
	PythonCliPath    string
	TotalRequests    atomic.Int64
	HotSwapCount     atomic.Int64
	StartedAt        time.Time
}

func NewServer(chunks []chunker.Chunk, indexPath string, pythonCliPath string) *Server {
	return &Server{
		Chunks:        chunks,
		IndexPath:     indexPath,
		PythonCliPath: pythonCliPath,
		StartedAt:     time.Now(),
	}
}

// HotReloadIndex atomically swaps the in-memory chunk slice under RWMutex write lock
// after reading and validating the newly swapped index manifest on disk (Zero-Downtime Hot-Swap).
func (s *Server) HotReloadIndex() error {
	raw, err := os.ReadFile(s.IndexPath)
	if err != nil {
		return err
	}
	var manifest ingest.IndexManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return err
	}
	s.mu.Lock()
	s.Chunks = manifest.Chunks
	s.ManifestChecksum = manifest.ManifestChecksum
	s.mu.Unlock()
	s.HotSwapCount.Add(1)
	return nil
}

// RegisterHAEndpoints attaches Kubernetes/Enterprise HA probes (/healthz, /readyz, /metrics, /reload).
func (s *Server) RegisterHAEndpoints(mux *http.ServeMux) {
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"healthy","version":"3.0.0"}`))
	})

	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		s.mu.RLock()
		count := len(s.Chunks)
		checksum := s.ManifestChecksum
		s.mu.RUnlock()
		w.Header().Set("Content-Type", "application/json")
		if count == 0 {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"ready":false,"reason":"empty_index"}`))
			return
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"ready":true,"indexed_chunks":%d,"manifest_checksum":"%s"}`, count, checksum)
	})

	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		s.mu.RLock()
		count := len(s.Chunks)
		s.mu.RUnlock()
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"total_requests":%d,"hot_swap_count":%d,"indexed_chunks":%d,"uptime_sec":%d}`,
			s.TotalRequests.Load(), s.HotSwapCount.Load(), count, int(time.Since(s.StartedAt).Seconds()))
	})

	mux.HandleFunc("/reload", func(w http.ResponseWriter, r *http.Request) {
		if err := s.HotReloadIndex(); err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			fmt.Fprintf(w, `{"reloaded":false,"error":"%v"}`, err)
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
					"version": "3.0.0",
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
						"description": "Thread-safe RWMutex lexical + GraphRAG lookup of extracted legacy business rules (BR-*) and ADRs.",
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
						"description": "Performs a zero-downtime RWMutex pointer swap to reload the latest chunks_index.json.",
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

		query := strings.ToUpper(strings.TrimSpace(callParams.Arguments["rule_id"]))
		if query == "" {
			query = strings.ToUpper(strings.TrimSpace(callParams.Arguments["query"]))
		}

		s.mu.RLock()
		snapshot := s.Chunks
		s.mu.RUnlock()

		var matched []chunker.Chunk
		for _, c := range snapshot {
			if strings.Contains(strings.ToUpper(c.Content), query) || strings.Contains(strings.ToUpper(c.Symbol), query) {
				matched = append(matched, c)
			}
		}
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
