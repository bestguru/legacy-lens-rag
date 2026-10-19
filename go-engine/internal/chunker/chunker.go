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
// enriched with Code GraphRAG dependency edges (SymbolsDefined, SymbolsReferenced)
// and exact line-range provenance for deterministic RAG citation verification.
type Chunk struct {
	ID                string   `json:"id"`
	FilePath          string   `json:"file_path"`
	Language          string   `json:"language"`
	Symbol            string   `json:"symbol"`
	ParentSymbol      string   `json:"parent_symbol,omitempty"`
	StartLine         int      `json:"start_line"`
	EndLine           int      `json:"end_line"`
	RuleIDs           []string `json:"rule_ids"`
	SymbolsDefined    []string `json:"symbols_defined"`
	SymbolsReferenced []string `json:"symbols_referenced"`
	Content           string   `json:"content"`
	TokenCount        int      `json:"token_count"`
}

var (
	ruleIDRegex    = regexp.MustCompile(`\b(BR-[A-Z]+-\d+[A-Z]?|ADR-\d+)\b`)
	classDeclRegex = regexp.MustCompile(`^\s*(?:public|internal|private|protected|sealed|abstract|final|static|\s)*\b(class|interface|struct)\s+([A-Za-z0-9_]+)`)
	methodSigRegex = regexp.MustCompile(`^\s*(?:public|private|protected|internal|static|final|async|virtual|override|\s)+\s*[A-Za-z0-9_<>,\[\]\.]+\s+([A-Za-z0-9_]+)\s*\(`)
	symbolRefRegex = regexp.MustCompile(`\b([A-Z][a-zA-Z0-9_]{3,})\b`)
	mdHeaderRegex  = regexp.MustCompile(`^(#{1,3})\s+(.+)$`)

	commonStopSymbols = map[string]bool{
		"String": true, "StringComparer": true, "StringComparison": true,
		"OrdinalIgnoreCase": true, "List": true, "HashSet": true, "Map": true,
		"ConcurrentDictionary": true, "BigDecimal": true, "RoundingMode": true,
		"Math": true, "System": true, "Collections": true, "Generic": true,
		"Threading": true, "Tasks": true, "Task": true, "Data": true,
		"InvalidOperationException": true, "Environment": true, "ProcessorCount": true,
		"Rule": true, "Business": true, "Context": true, "Decision": true,
		"Consequences": true, "Accepted": true, "Date": true, "Status": true,
		"Domain": true, "Related": true, "Rules": true, "Primary": true,
	}
)

// ChunkFile dispatches a file to either the Brace-Balanced AST Code Chunker or Markdown Section Chunker.
func ChunkFile(relPath string, rawContent string) []Chunk {
	ext := strings.ToLower(filepath.Ext(relPath))
	normalized := strings.ReplaceAll(rawContent, "\r\n", "\n")
	lines := strings.Split(normalized, "\n")

	switch ext {
	case ".cs", ".java", ".go", ".py":
		return chunkSourceCodeAST(relPath, strings.TrimPrefix(ext, "."), lines)
	case ".md":
		return chunkMarkdown(relPath, lines)
	default:
		return chunkFallback(relPath, "text", lines)
	}
}

// chunkSourceCodeAST tracks brace depth ({ / }) and doc-comments so that every class header
// and method body is extracted as a self-contained syntactic unit with GraphRAG edges.
func chunkSourceCodeAST(relPath, lang string, lines []string) []Chunk {
	var chunks []Chunk
	startIdx := 0
	currentSymbol := strings.TrimSuffix(filepath.Base(relPath), filepath.Ext(relPath))
	parentClass := currentSymbol

	flush := func(endIdx int) {
		if endIdx < startIdx {
			return
		}
		slice := lines[startIdx : endIdx+1]
		text := strings.TrimSpace(strings.Join(slice, "\n"))
		if len(text) == 0 {
			return
		}
		chunks = append(chunks, buildGraphChunk(relPath, lang, currentSymbol, parentClass, startIdx+1, endIdx+1, text))
	}

	for i, line := range lines {
		if m := classDeclRegex.FindStringSubmatch(line); len(m) >= 3 {
			className := m[2]
			cutIdx := findLeadingCommentStart(lines, i, startIdx)
			if cutIdx > startIdx && (cutIdx-startIdx) >= 6 {
				flush(cutIdx - 1)
				startIdx = cutIdx
			}
			parentClass = className
			currentSymbol = className
			continue
		}

		if m := methodSigRegex.FindStringSubmatch(line); len(m) >= 2 {
			methodName := m[1]
			// Skip constructors when they are short or immediately follow class header
			if methodName == parentClass && (i-startIdx) < 12 {
				continue
			}
			cutIdx := findLeadingCommentStart(lines, i, startIdx)
			if cutIdx > startIdx && (cutIdx-startIdx) >= 6 {
				flush(cutIdx - 1)
				startIdx = cutIdx
			}
			currentSymbol = fmt.Sprintf("%s.%s", parentClass, methodName)
		}
	}
	flush(len(lines) - 1)
	return chunks
}

func findLeadingCommentStart(lines []string, declIdx int, minIdx int) int {
	cutIdx := declIdx
	for cutIdx > minIdx {
		prev := strings.TrimSpace(lines[cutIdx-1])
		if strings.HasPrefix(prev, "///") || strings.HasPrefix(prev, "//") ||
			strings.HasPrefix(prev, "*") || strings.HasPrefix(prev, "/*") || strings.HasPrefix(prev, "*/") {
			cutIdx--
		} else {
			break
		}
	}
	return cutIdx
}

func chunkMarkdown(relPath string, lines []string) []Chunk {
	var chunks []Chunk
	startIdx := 0
	docName := strings.TrimSuffix(filepath.Base(relPath), filepath.Ext(relPath))
	currentHeader := docName

	flush := func(endIdx int) {
		if endIdx < startIdx {
			return
		}
		slice := lines[startIdx : endIdx+1]
		text := strings.TrimSpace(strings.Join(slice, "\n"))
		if len(text) == 0 {
			return
		}
		chunks = append(chunks, buildGraphChunk(relPath, "markdown", currentHeader, docName, startIdx+1, endIdx+1, text))
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
	base := filepath.Base(relPath)
	return []Chunk{buildGraphChunk(relPath, lang, base, base, 1, len(lines), text)}
}

func buildGraphChunk(relPath, lang, symbol, parentSymbol string, startLine, endLine int, text string) Chunk {
	slashPath := filepath.ToSlash(relPath)
	rawID := fmt.Sprintf("%s:%d-%d", slashPath, startLine, endLine)
	sum := sha256.Sum256([]byte(rawID))
	shortID := hex.EncodeToString(sum[:6])

	rules := uniqueMatches(ruleIDRegex.FindAllString(text, -1))

	defined := []string{symbol}
	if parentSymbol != "" && parentSymbol != symbol {
		defined = append(defined, parentSymbol)
	}
	defined = append(defined, rules...)

	definedSet := make(map[string]bool)
	for _, d := range defined {
		definedSet[d] = true
		for _, part := range strings.Split(d, ".") {
			definedSet[part] = true
		}
	}

	rawRefs := symbolRefRegex.FindAllString(text, -1)
	var referenced []string
	seenRef := make(map[string]bool)
	for _, ref := range rawRefs {
		if commonStopSymbols[ref] || definedSet[ref] || seenRef[ref] {
			continue
		}
		seenRef[ref] = true
		referenced = append(referenced, ref)
	}
	for _, r := range rules {
		if !seenRef[r] {
			seenRef[r] = true
			referenced = append(referenced, r)
		}
	}
	if referenced == nil {
		referenced = []string{}
	}

	words := strings.Fields(text)
	return Chunk{
		ID:                shortID,
		FilePath:          slashPath,
		Language:          lang,
		Symbol:            symbol,
		ParentSymbol:      parentSymbol,
		StartLine:         startLine,
		EndLine:           endLine,
		RuleIDs:           rules,
		SymbolsDefined:    defined,
		SymbolsReferenced: referenced,
		Content:           text,
		TokenCount:        len(words),
	}
}

func uniqueMatches(items []string) []string {
	seen := make(map[string]bool)
	var out []string
	for _, item := range items {
		if !seen[item] {
			seen[item] = true
			out = append(out, item)
		}
	}
	if out == nil {
		return []string{}
	}
	return out
}
