"""Helpers for telling the user how to run Wisp's privileged commands.

The privileged half of Wisp lives behind ``sudo`` (Linux) or an elevated shell
(Windows), and the way a user reaches that half depends on how the CLI was
installed. Helpers here keep the copy-pasteable hints accurate instead of
printing a command that only works in the shell that already has ``PATH`` set.

This module is deliberately free of ``wisp.daemon`` imports: the unprivileged
CLI and TUI use it, and nothing here needs the privileged package (or the
handler modules behind it) to build a hint string.
"""

import shutil
import sys
from pathlib import Path


def root_invocation(command: str) -> str:
    """Return a copy-pasteable ``sudo`` invocation for a Wisp command.

    ``sudo`` resets ``PATH`` to its ``secure_path`` (``/sbin:/bin:/usr/sbin:
    /usr/bin`` on most distros), which does not include ``~/.local/bin`` — where
    ``uv tool install`` puts the console script. Printing ``sudo wisp ...`` in
    that situation tells the user to run a command their shell cannot find,
    while ``wisp ...`` works fine without ``sudo``. The absolute path of the
    running script is known here, so the hint uses it.

    ``PYTHONDONTWRITEBYTECODE=1`` is deliberate: the console script imports
    ``wisp`` from the user's tool venv before handing off to the root-owned
    daemon venv, and running that import as root would leave root-owned
    ``__pycache__`` entries in a user-owned venv, breaking the next
    ``uv tool install --force``.

    Args:
        command (str): Subcommand and arguments, e.g. ``"daemon install"``.

    Returns:
        str: One shell line, e.g.
        ``sudo env PYTHONDONTWRITEBYTECODE=1 "/path/to/wisp" daemon install``.
    """
    executable = shutil.which("wisp")
    if executable is None:
        candidate = Path(sys.argv[0])
        executable = str(candidate.resolve()) if candidate.exists() else "wisp"
    return f'sudo env PYTHONDONTWRITEBYTECODE=1 "{executable}" {command}'
