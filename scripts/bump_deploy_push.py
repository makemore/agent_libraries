#!/usr/bin/env python3
"""
Bump version, commit, push, build and publish packages.

Usage:
    python bump_deploy_push.py [--dry-run] [--skip-publish] [--bump patch|minor|major]
    
Options:
    --dry-run       Show what would be done without making changes
    --skip-publish  Skip the build and publish step
    --bump          Version bump type (default: patch)
"""

import subprocess
import sys
import re
import json
import argparse
from pathlib import Path

# Private PyPI repository name (must match [agents] section in ~/.pypirc)
# See PACKAGE_REGISTRY.md for GCP Artifact Registry setup
DEVPI_REPO = "agents"

# Package configurations (in dependency order, with correct paths)
PACKAGES = [
    {
        "name": "agent-frontend",
        "path": "clients/agent-frontend",
        "type": "npm",
        "version_file": "package.json",
    },
    {
        "name": "agent_runtime_core",
        "path": "agent/agent_runtime_core",
        "type": "pypi",
        "version_file": "pyproject.toml",
        "import_module": "agent_runtime_core",
    },
    {
        "name": "django_agent_runtime",
        "path": "agent/django_agent_runtime",
        "type": "pypi",
        "version_file": "pyproject.toml",
        "import_module": "django_agent_runtime",
        "django_app": True,
    },
    {
        "name": "parrot_django",
        "path": "parrot/parrot-django",
        "type": "pypi",
        "version_file": "pyproject.toml",
    },
    {
        "name": "django_agent_studio",
        "path": "agent/django_agent_studio",
        "type": "pypi",
        "version_file": "pyproject.toml",
    },
    {
        "name": "chisel",
        "path": "chisel",
        "type": "pypi",
        "version_file": "pyproject.toml",
    },
    {
        "name": "django_chisel",
        "path": "django_chisel",
        "type": "pypi",
        "version_file": "pyproject.toml",
    },
]

def run(cmd: str, cwd: Path = None, dry_run: bool = False, capture: bool = False) -> str:
    """Run a shell command."""
    print(f"  $ {cmd}")
    if dry_run and not capture:
        return ""
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=capture, text=True)
    if capture:
        return result.stdout.strip()
    if result.returncode != 0:
        print(f"  ❌ Command failed with code {result.returncode}")
        sys.exit(1)
    return ""

def get_git_status(pkg_path: Path) -> str:
    """Get git status for a package directory (which is its own git repo)."""
    result = subprocess.run(
        "git status --porcelain",
        shell=True, cwd=pkg_path, capture_output=True, text=True
    )
    return result.stdout.strip()

def bump_version(version: str, bump_type: str) -> str:
    """Bump a semver version string."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Invalid version: {version}")
    major, minor, patch = map(int, match.groups())
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    else:  # patch
        return f"{major}.{minor}.{patch + 1}"

def get_version_npm(pkg_path: Path) -> str:
    """Get version from package.json."""
    with open(pkg_path / "package.json") as f:
        return json.load(f)["version"]

def set_version_npm(pkg_path: Path, version: str, dry_run: bool):
    """Set version in package.json."""
    pkg_file = pkg_path / "package.json"
    with open(pkg_file) as f:
        data = json.load(f)
    data["version"] = version
    if not dry_run:
        with open(pkg_file, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    print(f"  Updated package.json to {version}")

def get_version_pypi(pkg_path: Path) -> str:
    """Get version from pyproject.toml."""
    content = (pkg_path / "pyproject.toml").read_text()
    match = re.search(r'version\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else "0.0.0"

def set_version_pypi(pkg_path: Path, version: str, dry_run: bool):
    """Set version in pyproject.toml."""
    pyproject = pkg_path / "pyproject.toml"
    content = pyproject.read_text()
    new_content = re.sub(r'(version\s*=\s*)"[^"]+"', f'\\1"{version}"', content)
    if not dry_run:
        pyproject.write_text(new_content)
    print(f"  Updated pyproject.toml to {version}")

def smoke_test_wheel(pkg: dict, pkg_path: Path, dry_run: bool):
    """Pre-publish gate: install the freshly-built wheel into a CLEAN venv
    (pulling deps, incl. private ones, from the registry) and import it. Catches
    packaging breaks an in-tree test run cannot — a subpackage missing from the
    build's package list, or a dependency version that isn't published yet
    (both shipped broken in django_agent_runtime 0.17.0). Hard-fails the run.
    """
    import glob
    import tempfile

    import_module = pkg.get("import_module")
    if not import_module:
        print("  ⚠️  no 'import_module' configured — skipping smoke gate")
        return
    if dry_run:
        print("  (dry-run) skipping smoke gate")
        return

    wheels = sorted(glob.glob(str(pkg_path / "dist" / "*.whl")))
    if not wheels:
        print("  ❌ smoke gate: no wheel found in dist/")
        sys.exit(1)
    wheel = wheels[-1]
    registry = "https://europe-west2-python.pkg.dev/devpi-mmd/agents/simple/"
    venv = Path(tempfile.mkdtemp()) / "venv"
    pip = f"{venv}/bin/pip"
    py = f"{venv}/bin/python"

    print(f"\n🔬 Smoke gate: install {Path(wheel).name} in a clean venv and import")
    run(f"python3 -m venv {venv}")
    run(f"{pip} install -q --upgrade pip keyrings.google-artifactregistry-auth")
    # --keyring-provider=import makes pip use the in-process keyring (where
    # the gauth plugin's get_password actually works via gcloud ADC). The
    # default 'auto' provider sometimes routes to a subprocess-based lookup
    # that returns None for the registry host, which causes pip to prompt
    # for credentials and fail under non-interactive runs.
    # --no-input prevents the prompt; combined with the import keyring the
    # resolver pulls deps from the private registry cleanly.
    run(
        f"{pip} install -q --no-input --keyring-provider=import "
        f"'{wheel}' --extra-index-url {registry}"
    )
    run(f'{py} -c "import {import_module}"')  # run() exits(1) on failure

    if pkg.get("django_app"):
        work = Path(tempfile.mkdtemp())
        (work / "smoke_settings.py").write_text(
            'SECRET_KEY = "smoke"\n'
            'INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth",\n'
            '                  "rest_framework", "django_agent_runtime"]\n'
            'DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}\n'
            'DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"\n'
        )
        (work / "smoke_check.py").write_text(
            "import django; django.setup()\n"
            "from django.core.management import call_command\n"
            "call_command('check')\n"
        )
        run(f"DJANGO_SETTINGS_MODULE=smoke_settings {py} smoke_check.py", cwd=work)

    print("  ✅ smoke gate passed")


def process_package(pkg: dict, bump_type: str, dry_run: bool, skip_publish: bool):
    """Process a single package."""
    print(f"\n{'='*60}")
    print(f"📦 {pkg['name']}")
    print(f"{'='*60}")
    
    pkg_path = Path(pkg["path"])
    if not pkg_path.exists():
        print(f"  ⚠️  Path {pkg_path} does not exist, skipping")
        return

    # 1. Check git status
    print("\n📋 Git status:")
    status = get_git_status(pkg_path)
    if status:
        print(f"  {status.replace(chr(10), chr(10) + '  ')}")
    else:
        print("  No changes detected")
        return  # Skip if no changes
    
    # 2. Get and bump version
    print("\n🔢 Version bump:")
    if pkg["type"] == "npm":
        old_version = get_version_npm(pkg_path)
    else:
        old_version = get_version_pypi(pkg_path)
    
    new_version = bump_version(old_version, bump_type)
    print(f"  {old_version} → {new_version} ({bump_type})")
    
    if pkg["type"] == "npm":
        set_version_npm(pkg_path, new_version, dry_run)
    else:
        set_version_pypi(pkg_path, new_version, dry_run)
    
    # 3. Git add, commit, push (run from inside the package's git repo)
    print("\n📤 Git commit and push:")
    run("git add .", cwd=pkg_path, dry_run=dry_run)
    run(f'git commit -m "bump to v{new_version}"', cwd=pkg_path, dry_run=dry_run)
    run("git push", cwd=pkg_path, dry_run=dry_run)

    # 4. Build and publish
    if skip_publish:
        print("\n⏭️  Skipping publish (--skip-publish)")
        return

    print("\n🚀 Build and publish:")
    if pkg["type"] == "npm":
        run("npm publish", cwd=pkg_path, dry_run=dry_run)
    else:
        # Clean old builds
        dist_path = pkg_path / "dist"
        if dist_path.exists() and not dry_run:
            import shutil
            shutil.rmtree(dist_path)
        # Build, then SMOKE-GATE the built wheel before uploading. A wheel that
        # can't be imported in a clean install never reaches the registry.
        run("uv build", cwd=pkg_path, dry_run=dry_run)
        smoke_test_wheel(pkg, pkg_path, dry_run)
        run(f"twine upload --disable-progress-bar -r {DEVPI_REPO} dist/*", cwd=pkg_path, dry_run=dry_run)

    print(f"\n✅ {pkg['name']} v{new_version} published!")


def main():
    parser = argparse.ArgumentParser(description="Bump, commit, push and publish packages")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--skip-publish", action="store_true", help="Skip build and publish")
    parser.add_argument("--bump", choices=["patch", "minor", "major"], default="patch",
                        help="Version bump type (default: patch)")
    parser.add_argument("--package", "-p", help="Process only this package")
    args = parser.parse_args()

    if args.dry_run:
        print("🔍 DRY RUN MODE - no changes will be made\n")

    packages = PACKAGES
    if args.package:
        packages = [p for p in PACKAGES if p["name"] == args.package or p["path"] == args.package]
        if not packages:
            print(f"❌ Package '{args.package}' not found")
            sys.exit(1)

    for pkg in packages:
        process_package(pkg, args.bump, args.dry_run, args.skip_publish)

    print("\n" + "="*60)
    print("🎉 All done!")
    print("="*60)


if __name__ == "__main__":
    main()

