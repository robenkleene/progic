"""Command-line entry point."""

from __future__ import annotations

import argparse

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="progic", description="Create and edit Logic Pro projects.")
    parser.add_argument("--version", action="version", version=f"progic {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
