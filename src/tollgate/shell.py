"""Connecting and disconnecting without knowing what an environment variable is.

`tollgate run` is the safe default and needs none of this: it sets the
variables for one command and they vanish when it exits. This module is for
people who want their app pointed at Tollgate permanently, and it exists mostly
so that *undoing* that is one command rather than a text-editor task.

Everything is written between two markers, so `tollgate disconnect` removes
exactly what `tollgate connect` added and nothing else — including if the user
has edited around it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

START = "# >>> tollgate >>>"
END = "# <<< tollgate <<<"


def profile_path(shell: str | None = None, home: Path | None = None) -> Path:
    """The startup file for the user's shell.

    Guesses from $SHELL and falls back to the most common one. Being wrong is
    recoverable — `tollgate status` reports where it looked.
    """
    home = home or Path.home()
    shell = shell or os.environ.get("SHELL", "")
    name = os.path.basename(shell)
    if name == "fish":
        return home / ".config" / "fish" / "config.fish"
    if name == "bash":
        # macOS logs in through bash_profile; Linux through bashrc. Prefer one
        # that already exists so we extend rather than shadow.
        for candidate in (home / ".bash_profile", home / ".bashrc"):
            if candidate.exists():
                return candidate
        return home / ".bashrc"
    return home / ".zshrc"


def render_block(base_url: str, shell: str | None = None) -> str:
    name = os.path.basename(shell or os.environ.get("SHELL", ""))
    if name == "fish":
        lines = [
            f"set -gx ANTHROPIC_BASE_URL {base_url}",
            f"set -gx OPENAI_BASE_URL {base_url}/v1",
        ]
    else:
        lines = [
            f"export ANTHROPIC_BASE_URL={base_url}",
            f"export OPENAI_BASE_URL={base_url}/v1",
        ]
    body = "\n".join(lines)
    return (
        f"{START}\n"
        "# Sends this machine's AI traffic through Tollgate so it can be measured.\n"
        "# Remove with: tollgate disconnect\n"
        f"{body}\n"
        f"{END}\n"
    )


def _strip_block(text: str) -> str:
    """Remove an existing block, tolerating a missing end marker."""
    if START not in text:
        return text
    head, _, rest = text.partition(START)
    if END in rest:
        _, _, tail = rest.partition(END)
        tail = tail.lstrip("\n")
    else:
        tail = ""
    return head.rstrip("\n") + ("\n" + tail if tail else "\n")


def is_connected(path: Path) -> bool:
    return path.exists() and START in path.read_text()


def connect(path: Path, base_url: str, shell: str | None = None) -> bool:
    """Point this machine's AI traffic at Tollgate. Returns False if unchanged.

    Idempotent: running it twice replaces the block rather than stacking two.
    """
    existing = path.read_text() if path.exists() else ""
    block = render_block(base_url, shell)
    updated = _strip_block(existing)
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated = f"{updated}\n{block}" if updated.strip() else block
    if updated == existing:
        return False
    _write(path, updated)
    return True


def disconnect(path: Path) -> bool:
    """Undo `connect`. Returns False if there was nothing to remove."""
    if not path.exists():
        return False
    existing = path.read_text()
    if START not in existing:
        return False
    _write(path, _strip_block(existing))
    return True


def _write(path: Path, text: str) -> None:
    """Write the profile, keeping a copy of what was there before.

    This is someone's shell startup file. A backup costs nothing and means a
    mistake is never unrecoverable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".tollgate-backup"))
    path.write_text(text)
