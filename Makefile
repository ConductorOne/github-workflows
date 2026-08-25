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
test: test-go test-scripts

.PHONY: test-go
test-go:
	go test ./cmd/extract-images ./cmd/record-release ./cmd/generate-manifest ./cmd/merge-manifests ./cmd/publish-public-ecr-release-tags

.PHONY: test-scripts
test-scripts:
	bash scripts/test-derive-iam-role-name.sh
	bash scripts/test-normalize-release-options.sh
	python3 scripts/test-release-config-templates.py
	bash scripts/test-release-workflow-tag-pin.sh
	bash scripts/test-s3-release-uploads.sh
	if command -v pwsh >/dev/null 2>&1; then pwsh -NoProfile -File scripts/test-s3-release-uploads.ps1; else echo "pwsh not found; skipping PowerShell S3 release upload tests"; fi

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
