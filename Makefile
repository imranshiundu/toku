.PHONY: lint test verify

lint:
	python3 -m py_compile toku.py
	bash -n install.sh uninstall.sh
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck install.sh uninstall.sh; else echo "shellcheck not installed — skipped (declared)"; fi

test:
	python3 -m unittest discover -s tests -v

verify: lint test
