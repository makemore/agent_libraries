"""Standalone REST client. Importing this package never loads Django."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as run

    raise SystemExit(run())
