package chunker

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
)

// Chunk represents a structure-aware segment of source code or architecture markdown
// with exact line-range provenance for deterministic RAG citation verification.
type Chunk struct {
	ID         string   `json:"id"`
	FilePath   string   `json:"file_path"`
	Language   string   `json:"language"`
	Symbol     string   `json:"symbol"`
	StartLine  int      `json:"start_line"`
	EndLine    int      `json:"end_line"`
	RuleIDs    []string `json:"rule_ids"`
	Content    string   `json:"content"`
	TokenCount int      `json:"token_count"`
}

var (
	ruleIDRegex   = regexp.MustCompile(`\b(BR-[A-Z]+-\d+[A-Z]?|ADR-\d+)\b`)
	codeDeclRegex = regexp.MustCompile(`^\s*(public|private|protected|internal)\s+.*(class|interface|struct|void|Task|PolicyDecision|RebalanceDecision)\s+([A-Za-z0-9_]+)`)
	mdHeaderRegex = regexp.MustCompile(`^(#{1,3})\s+(.+)$`)
)

// ChunkFile dispatches a file to either the Code AST-like boundary chunker or Markdown section chunker.
func ChunkFile(relPath string, rawContent string) []Chunk {
	ext := strings.ToLower(filepath.Ext(relPath))
	normalized := strings.ReplaceAll(rawContent, "\r\n", "\n")
	lines := strings.Split(normalized, "\n")

	switch ext {
	case ".cs", ".java", ".go", ".py":
		return chunkSourceCode(relPath, strings.TrimPrefix(ext, "."), lines)
	case ".md":
		return chunkMarkdown(relPath, lines)
	default:
		return chunkFallback(relPath, "text", lines)
	}
}

func chunkSourceCode(relPath, lang string, lines []string) []Chunk {
	var chunks []Chunk
	startIdx := 0
	currentSymbol := filepath.Base(relPath)

	flush := func(endIdx int) {
		if endIdx < startIdx {
			return
		}
		slice := lines[startIdx : endIdx+1]
		text := strings.TrimSpace(strings.Join(slice, "\n"))
		if len(text) == 0 {
			return
		}
		chunks = append(chunks, buildChunk(relPath, lang, currentSymbol, startIdx+1, endIdx+1, text))
	}

	for i, line := range lines {
		if matches := codeDeclRegex.FindStringSubmatch(line); len(matches) >= 4 {
			// Include preceding doc-comments with the new declaration
			cutIdx := i
			for cutIdx > startIdx {
				prev := strings.TrimSpace(lines[cutIdx-1])
				if strings.HasPrefix(prev, "///") || strings.HasPrefix(prev, "//") || strings.HasPrefix(prev, "*") || strings.HasPrefix(prev, "/*") {
					cutIdx--
				} else {
					break
				}
			}
			if cutIdx > startIdx && (cutIdx-startIdx) >= 8 {
				flush(cutIdx - 1)
				startIdx = cutIdx
			}
			currentSymbol = matches[3]
		}
	}
	flush(len(lines) - 1)
	return chunks
}

func chunkMarkdown(relPath string, lines []string) []Chunk {
	var chunks []Chunk
	startIdx := 0
	currentHeader := filepath.Base(relPath)

	flush := func(endIdx int) {
		if endIdx < startIdx {
			return
		}
		slice := lines[startIdx : endIdx+1]
		text := strings.TrimSpace(strings.Join(slice, "\n"))
		if len(text) == 0 {
			return
		}
		chunks = append(chunks, buildChunk(relPath, "markdown", currentHeader, startIdx+1, endIdx+1, text))
	}

	for i, line := range lines {
		if matches := mdHeaderRegex.FindStringSubmatch(line); len(matches) >= 3 {
			if i > startIdx && (i-startIdx) >= 5 {
				flush(i - 1)
				startIdx = i
			}
			currentHeader = strings.TrimSpace(matches[2])
		}
	}
	flush(len(lines) - 1)
	return chunks
}

func chunkFallback(relPath, lang string, lines []string) []Chunk {
	text := strings.TrimSpace(strings.Join(lines, "\n"))
	if text == "" {
		return nil
	}
	return []Chunk{buildChunk(relPath, lang, filepath.Base(relPath), 1, len(lines), text)}
}

func buildChunk(relPath, lang, symbol string, startLine, endLine int, text string) Chunk {
	slashPath := filepath.ToSlash(relPath)
	rawID := fmt.Sprintf("%s:%d-%d", slashPath, startLine, endLine)
	sum := sha256.Sum256([]byte(rawID))
	shortID := hex.EncodeToString(sum[:6])

	matches := ruleIDRegex.FindAllString(text, -1)
	seen := make(map[string]bool)
	var rules []string
	for _, m := range matches {
		if !seen[m] {
			seen[m] = true
			rules = append(rules, m)
		}
	}
	if rules == nil {
		rules = []string{}
	}

	words := strings.Fields(text)
	return Chunk{
		ID:         shortID,
		FilePath:   slashPath,
		Language:   lang,
		Symbol:     symbol,
		StartLine:  startLine,
		EndLine:    endLine,
		RuleIDs:    rules,
		Content:    text,
		TokenCount: len(words),
	}
}
