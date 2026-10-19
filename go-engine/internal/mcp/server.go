package mcp

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
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

// Server exposes the indexed chunks as an MCP tool server over stdio for Claude Code / AI Agents.
type Server struct {
	Chunks []chunker.Chunk
}

func NewServer(chunks []chunker.Chunk) *Server {
	return &Server{Chunks: chunks}
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
					"version": "1.0.0",
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
						"description": "Search extracted legacy business rules (BR-*) and ADRs with exact file and line provenance.",
						"inputSchema": map[string]interface{}{
							"type": "object",
							"properties": map[string]interface{}{
								"rule_id": map[string]string{"type": "string", "description": "Business rule ID or keyword (e.g. BR-POL-002, BR-FIN-101, ADR-001)"},
							},
							"required": []string{"rule_id"},
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
		query := strings.ToUpper(strings.TrimSpace(callParams.Arguments["rule_id"]))
		var matched []chunker.Chunk
		for _, c := range s.Chunks {
			if strings.Contains(strings.ToUpper(c.Content), query) {
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
