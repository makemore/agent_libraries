#!/usr/bin/env python3
"""Small, non-destructive coordinator; .gitmodules is the only repo inventory."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def repositories(root: Path) -> list[str]:
    entries = git(root, "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    paths = [line.split(maxsplit=1)[1] for line in entries.splitlines()]
    for path in paths:
        if Path(path).is_absolute() or ".." in Path(path).parts or path == ".":
            raise ValueError(f"Unsafe repository path: {path}")
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate repository paths")
    return paths


def checkout(root: Path) -> None:
    for path in repositories(root):
        target = root / path
        if (target / ".git").exists():
            print(f"keep  {path} (existing branch/worktree unchanged)", flush=True)
            continue
        if target.exists() and any(target.iterdir()):
            raise RuntimeError(f"Refusing to overwrite non-repository directory: {path}")
        subprocess.run(["git", "-C", str(root), "submodule", "update", "--init", "--", path], check=True)


def status(root: Path) -> None:
    for path in [".", *repositories(root)]:
        target = root / path
        if not (target / ".git").exists():
            print(f"{path}: NOT INITIALIZED")
            continue
        branch = git(target, "branch", "--show-current") or "DETACHED (pinned)"
        dirty = bool(git(target, "status", "--porcelain"))
        upstream = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "@{upstream}"],
            text=True, capture_output=True,
        )
        tracking = "no upstream"
        if upstream.returncode == 0:
            behind, ahead = git(target, "rev-list", "--left-right", "--count",
                                upstream.stdout.strip() + "...HEAD").split()
            tracking = f"behind={behind} ahead={ahead}"
        print(f"{path}: {branch}; {'DIRTY' if dirty else 'clean'}; {tracking}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["checkout", "status", "clean-preview"])
    args = parser.parse_args()
    if args.command == "checkout":
        checkout(ROOT)
    elif args.command == "status":
        status(ROOT)
    else:
        for path in repositories(ROOT):
            if (ROOT / path / ".git").exists():
                print(f"--- {path}", flush=True)
                subprocess.run(["git", "-C", str(ROOT / path), "clean", "-ndX"], check=True)


if __name__ == "__main__":
    main()