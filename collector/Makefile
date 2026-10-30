BUILDER ?= $(shell go env GOPATH)/bin/builder

build:
	$(BUILDER) --config builder-config.yaml

keys:
	go run github.com/usewake/wake/log/cmd/wake-keygen -name wake-collector -out keys/collector
	go run github.com/usewake/wake/log/cmd/wake-keygen -name wake-log -out keys/log

run: build
	./build/wake-collector --config config.yaml

test:
	go test ./...

.PHONY: build keys run test
