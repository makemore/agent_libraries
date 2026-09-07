#!/usr/bin/env python3
"""Run the actual iOS headless tests on macOS without downloading UI dependencies."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    client = root / "clients" / "agent-ios"
    if sys.platform != "darwin" or not shutil.which("swift"):
        raise SystemExit("Headless iOS tests require macOS and the Swift/Xcode toolchain.")
    for directory in ("Sources/AgentClient", "Tests/AgentClientTests"):
        if not (client / directory).is_dir():
            raise SystemExit("Missing iOS sources. Run make checkout first.")
    with tempfile.TemporaryDirectory(prefix="agent-libraries-ios-") as directory:
        package = Path(directory)
        shutil.copyfile(root / "test-harness/ios-headless/Package.swift", package / "Package.swift")
        for name in ("Sources", "Tests"):
            (package / name).symlink_to(client / name, target_is_directory=True)
        # Source-audit tests locate the client by its usual meta-repo path.
        (package / "clients").mkdir()
        (package / "clients/agent-ios").symlink_to(client, target_is_directory=True)
        (package / "test-harness").symlink_to(root / "test-harness", target_is_directory=True)
        # Retain package-local fixtures as fallbacks for standalone-only scenarios.
        if (client / "test-fixtures").is_dir():
            (package / "test-fixtures").symlink_to(client / "test-fixtures", target_is_directory=True)
        process = subprocess.Popen(
            ["swift", "test", *sys.argv[1:]], cwd=package, start_new_session=True,
        )
        try:
            return process.wait(timeout=300)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # Stop only this test process group before removing its temporary build.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            print("iOS tests interrupted or exceeded the five-minute limit.", file=sys.stderr)
            return 124


if __name__ == "__main__":
    raise SystemExit(main())