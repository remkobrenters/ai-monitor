.PHONY: check test lint dist
UNAME := $(shell uname -s)
check: lint test

lint:
	python3 -m compileall -q ai_monitor scripts tests
	sh -n install.sh uninstall.sh status.sh
ifeq ($(UNAME),Darwin)
	swiftc -parse-as-library -typecheck macos/Logic.swift macos/App.swift
else
	glib-compile-schemas --strict --dry-run extension/schemas
	@for file in extension/*.js tests/*.mjs; do node --check "$$file" || exit; done
endif

test:
	python3 -m unittest discover -s tests -v
ifeq ($(UNAME),Darwin)
	@mkdir -p build
	swiftc macos/Logic.swift macos/tests/main.swift -o build/logic-tests
	build/logic-tests
else
	node tests/logic.test.mjs
	node --experimental-vm-modules tests/extension.test.mjs
endif

dist:
	python3 scripts/package.py
