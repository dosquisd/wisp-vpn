"""Wisp entry point.

Thin wrapper delegating to the CLI parser and command dispatch.
"""

import sys

from wisp.cli.parser import main

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
