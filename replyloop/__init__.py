"""ReplyLoop package foundation."""

from __future__ import annotations

__all__ = ["__version__", "main"]
__version__ = "0.1.0"


def main() -> int:
    """Console script entry point."""
    from .cli import main as cli_main

    return cli_main()
