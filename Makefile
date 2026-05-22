.PHONY: protogen
protogen:
	@echo "Generating protobuf files..."
	buf generate proto
	@echo "Protobuf generation complete."

.PHONY: protofmt
protofmt:
	buf format -w proto
	@echo "Protobuf formatting complete."

.PHONY: test
test:
	go test ./cmd/record-release ./cmd/generate-manifest ./cmd/merge-manifests

.PHONY: workflow-validate
workflow-validate:
	yq '.' .github/workflows/release.yaml >/dev/null

.PHONY: verify
verify: protogen test workflow-validate

.PHONY: docs
docs:
	@echo "Generating documentation diagrams..."
	dot -Tpng docs/diagrams/release-workflow.dot -o docs/diagrams/release-workflow.png
	@echo "Documentation generation complete."
