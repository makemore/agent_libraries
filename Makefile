.PHONY: help checkout install clean

help:
	@echo "Targets:"
	@echo "  make checkout   Clone every sub-repo (idempotent)"
	@echo "  make install    Editable-install the Python packages"
	@echo "  make clean      Remove untracked files in sub-repo dirs (careful)"

checkout:
	@bash scripts/checkout.sh

install: checkout
	@bash scripts/install.sh

clean:
	@echo "Removing untracked files under sub-repo dirs..."
	@for r in agent chisel clients django_chisel docs parrot test-harness; do \
	  if [ -d "$$r" ]; then \
	    git -C "$$r" clean -fdx 2>/dev/null || true; \
	  fi; \
	done
