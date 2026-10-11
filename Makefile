.PHONY: lint test verify tiny tiny-lint

lint:
	python3 -m py_compile toku.py
	bash -n install.sh uninstall.sh
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck install.sh uninstall.sh; else echo "shellcheck not installed — skipped (declared)"; fi

test:
	python3 -m unittest discover -s tests -v

tiny:
	@bash tiny/build.sh

tiny-lint:
	@if command -v go >/dev/null 2>&1; then gofmt -l tiny/main.go && go vet ./tiny/...; else echo "go not installed — tiny lint skipped (declared)"; fi

verify: lint test
