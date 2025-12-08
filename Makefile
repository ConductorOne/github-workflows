.PHONY: protogen
protogen:
	@echo "Generating protobuf files..."
	buf generate proto
	@echo "Protobuf generation complete."

.PHONY: protofmt
protofmt:
	buf format -w proto
	@echo "Protobuf formatting complete."
