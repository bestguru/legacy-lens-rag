package ingest

import (
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"sync"
	"time"

	"github.com/kyungtae-kang/legacy-lens-rag/go-engine/internal/chunker"
)

// IndexManifest represents the deterministic output of the 3-Lane Go Ingestion Pipeline.
type IndexManifest struct {
	Version       string          `json:"version"`
	GeneratedAt   string          `json:"generated_at"`
	TotalFiles    int             `json:"total_files"`
	TotalChunks   int             `json:"total_chunks"`
	ElapsedMillis int64           `json:"elapsed_ms"`
	Chunks        []chunker.Chunk `json:"chunks"`
}

type fileJob struct {
	absPath string
	relPath string
}

// RunThreeLanePipeline mirrors the TACT 3-Lane Dataflow architecture in Go:
// - Lane 1 (Discovery / Control): File-system walk and filtering
// - Lane 2 (Parallel Compute / Parse Pool): NCPU*2 goroutines reading & structure-chunking files
// - Lane 3 (Mutation / Sink): Single serialized aggregator ensuring deterministic ordering (MaxDOP=1)
func RunThreeLanePipeline(corpusRoot string, outputIndexPath string) (*IndexManifest, error) {
	start := time.Now()

	jobChan := make(chan fileJob, 64)
	chunkBatchChan := make(chan []chunker.Chunk, 64)

	// Lane 2: Parallel Compute / Chunking Pool (2x CPU cores for I/O + regex parsing)
	workerCount := runtime.NumCPU() * 2
	if workerCount < 4 {
		workerCount = 4
	}
	var wg sync.WaitGroup
	for i := 0; i < workerCount; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for job := range jobChan {
				data, err := os.ReadFile(job.absPath)
				if err != nil {
					continue
				}
				chunks := chunker.ChunkFile(job.relPath, string(data))
				if len(chunks) > 0 {
					chunkBatchChan <- chunks
				}
			}
		}()
	}

	// Lane 1: Discovery
	go func() {
		defer close(jobChan)
		_ = filepath.WalkDir(corpusRoot, func(path string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil
			}
			rel, err := filepath.Rel(corpusRoot, path)
			if err != nil {
				return nil
			}
			jobChan <- fileJob{absPath: path, relPath: rel}
			return nil
		})
	}()

	go func() {
		wg.Wait()
		close(chunkBatchChan)
	}()

	// Lane 3: Serialized Mutation / Aggregation (MaxDOP = 1)
	var allChunks []chunker.Chunk
	fileSet := make(map[string]bool)
	for batch := range chunkBatchChan {
		for _, c := range batch {
			fileSet[c.FilePath] = true
			allChunks = append(allChunks, c)
		}
	}

	// Deterministic sort by FilePath + StartLine
	sort.Slice(allChunks, func(i, j int) bool {
		if allChunks[i].FilePath == allChunks[j].FilePath {
			return allChunks[i].StartLine < allChunks[j].StartLine
		}
		return allChunks[i].FilePath < allChunks[j].FilePath
	})

	elapsed := time.Since(start).Milliseconds()
	manifest := &IndexManifest{
		Version:       "1.0.0",
		GeneratedAt:   time.Now().UTC().Format(time.RFC3339),
		TotalFiles:    len(fileSet),
		TotalChunks:   len(allChunks),
		ElapsedMillis: elapsed,
		Chunks:        allChunks,
	}

	if outputIndexPath != "" {
		if err := os.MkdirAll(filepath.Dir(outputIndexPath), 0755); err != nil {
			return nil, err
		}
		outBytes, err := json.MarshalIndent(manifest, "", "  ")
		if err != nil {
			return nil, err
		}
		if err := os.WriteFile(outputIndexPath, outBytes, 0644); err != nil {
			return nil, err
		}
	}

	return manifest, nil
}
