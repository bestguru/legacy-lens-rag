package mcp

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os/exec"
	"strings"

	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/chunker"
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

// Server exposes the indexed chunks and bridges directly to the Python Hybrid RRF + GraphRAG engine.
type Server struct {
	Chunks        []chunker.Chunk
	IndexPath     string
	PythonCliPath string
}

func NewServer(chunks []chunker.Chunk, indexPath string, pythonCliPath string) *Server {
	return &Server{
		Chunks:        chunks,
		IndexPath:     indexPath,
		PythonCliPath: pythonCliPath,
	}
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
	switch req.Method {
	case "initialize":
		return JSONRPCResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Result: map[string]interface{}{
				"protocolVersion": "2024-11-05",
				"serverInfo": map[string]string{
					"name":    "legacy-lens-rag-mcp",
					"version": "2.0.0",
				},
				"capabilities": map[string]interface{}{
					"tools": map[string]bool{"listChanged": false},
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
						"description": "Fast lexical + GraphRAG lookup of extracted legacy business rules (BR-*) and ADRs.",
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
						"description": "Executes the Python 3-Way Hybrid GraphRAG Retriever (BM25 + Dense + 1-Hop Graph) and Self-RAG Citation Verifier.",
						"inputSchema": map[string]interface{}{
							"type": "object",
							"properties": map[string]interface{}{
								"query": map[string]string{"type": "string", "description": "Natural language question in English or Korean"},
							},
							"required": []string{"query"},
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
		var matched []chunker.Chunk
		for _, c := range s.Chunks {
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
