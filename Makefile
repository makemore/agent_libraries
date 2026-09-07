PYTHON ?= python3

.PHONY: help checkout install test test-harness test-web test-ios test-android clean

help:
	@echo "Targets:"
	@echo "  make checkout   Clone current sub-repos (idempotent)"
	@echo "  make install    Editable-install the Python packages"
	@echo "  make test       Run offline stub-server and TypeScript client tests"
	@echo "  make test-ios   Run headless iOS tests on macOS (no UI dependencies)"
	@echo "  make test-android  Run Android JVM unit tests (requires JDK/SDK)"
	@echo "  make clean      Preview ignored files only; deletes nothing"

checkout:
	@bash scripts/checkout.sh

install: checkout
	@bash scripts/install.sh

test: test-harness test-web

test-harness:
	$(PYTHON) -m unittest discover -s test-harness/stub-server -p 'test_*.py' -v

test-web:
	cd clients/agent-frontend && npm run typecheck:client
	cd clients/agent-frontend && npm test --workspace @makemore/agent-client

test-ios:
	$(PYTHON) scripts/test_ios_headless.py $(IOS_TEST_ARGS)

test-android:
	cd clients/agent-android && ./gradlew --offline :testDebugUnitTest :agent-client:testDebugUnitTest

clean:
	@echo "Dry run only. Review each repository before removing any files."
	@for r in agent/agent_runtime_core agent/django_agent_runtime agent/django_agent_studio \
	  agent/agent_studio chisel django_chisel parrot docs clients/agent-frontend \
	  clients/agent-ios clients/agent-android clients/agent-unity clients/agent-client; do \
	  if [ -e "$$r/.git" ]; then \
	    echo "--- $$r"; git -C "$$r" clean -ndX; \
	  fi; \
	done
