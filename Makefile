.PHONY: check test lint dist
check: lint test

lint:
	python3 -m compileall -q ai_monitor scripts tests
	sh -n install.sh uninstall.sh status.sh
	glib-compile-schemas --strict --dry-run extension/schemas
	@for file in extension/*.js tests/*.mjs; do node --check "$$file" || exit; done

test:
	python3 -m unittest discover -s tests -v
	node tests/logic.test.mjs
	node --experimental-vm-modules tests/extension.test.mjs

dist:
	python3 scripts/package.py
