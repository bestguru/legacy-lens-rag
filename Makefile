.PHONY: all test-go ingest verify query clean

all: test-go ingest verify

test-go:
	cd go-engine && go test ./... -v

ingest:
	cd go-engine && go run ./cmd/legacy-lens -mode=ingest -corpus=../sample-corpus -out=../artifacts/chunks_index.json

verify:
	cd py-rag && python harnesses/verify_rag_metrics.py

query:
	cd py-rag && python src/cli_query.py --index ../artifacts/chunks_index.json --query "운영 장비에서 설정(configure terminal) 모드 진입 시 필요한 권한 레벨과 Bastion 조건은? (BR-POL-002)"
