package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/ingest"
	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/mcp"
)

func main() {
	mode := flag.String("mode", "ingest", "Execution mode: 'ingest' or 'mcp'")
	corpusDir := flag.String("corpus", "../sample-corpus", "Path to legacy source & llm-wiki corpus directory")
	outputFile := flag.String("out", "../artifacts/chunks_index.json", "Path to output JSON chunk index")
	pyCli := flag.String("py-cli", "../py-rag/src/cli_query.py", "Path to Python Hybrid RAG CLI bridge")
	flag.Parse()

	switch *mode {
	case "ingest":
		manifest, err := ingest.RunThreeLanePipeline(*corpusDir, *outputFile)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Ingestion error: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("[Go 3-Lane Ingestor v3.3] Indexed %d files into %d AST/Graph-aware chunks in %d ms -> %s\n",
			manifest.TotalFiles, manifest.TotalChunks, manifest.ElapsedMillis, *outputFile)

	case "mcp":
		raw, err := os.ReadFile(*outputFile)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Failed to read chunk index: %v\n", err)
			os.Exit(1)
		}
		var manifest ingest.IndexManifest
		if err := json.Unmarshal(raw, &manifest); err != nil {
			fmt.Fprintf(os.Stderr, "Invalid chunk index JSON: %v\n", err)
			os.Exit(1)
		}
		srv := mcp.NewServer(manifest.Chunks, *outputFile, *pyCli)
		if err := srv.ServeStdio(os.Stdin, os.Stdout); err != nil {
			fmt.Fprintf(os.Stderr, "MCP stdio error: %v\n", err)
			os.Exit(1)
		}

	default:
		fmt.Fprintf(os.Stderr, "Unknown mode: %s\n", *mode)
		os.Exit(1)
	}
}
