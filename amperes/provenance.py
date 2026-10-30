"""Provenance: code digest, dependency versions, platform. Never records environment variables or secrets."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from importlib import metadata
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def code_digest() -> str:
    """SHA-256 over every source file in the amperes package (the repo has no git history to cite)."""
    h = hashlib.sha256()
    for f in sorted(PKG_ROOT.rglob("*")):
        if f.is_file() and f.suffix in (".py", ".html", ".js", ".css") and "__pycache__" not in f.parts:
            h.update(str(f.relative_to(PKG_ROOT)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def git_revision() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PKG_ROOT.parent, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _v(pkg: str) -> str | None:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def environment() -> dict:
    from amperes import __version__

    return {
        "amperes_version": __version__,
        "code_digest": code_digest(),
        "git_revision": git_revision(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "dependencies": {p: _v(p) for p in ("mujoco", "numpy", "pydantic", "fastapi", "matplotlib")},
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
