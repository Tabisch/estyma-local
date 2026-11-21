"""The running software version, shown in the web UI footer and /api/health.

Docker images get it from the build (``ESTYMA_VERSION`` / ``ESTYMA_REVISION``,
set by the Dockerfile from build args the CI workflow passes: the tag name
such as ``v1.0.0`` or the branch name, and the commit SHA). A run from a git
checkout uses ``git describe``; anything else shows ``dev``.
"""

from __future__ import annotations

import html
import os
import re
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHA = re.compile(r"[0-9a-f]{7,40}")


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", _ROOT, *args], capture_output=True,
                             text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def detect(env=None) -> tuple[str, str]:
    """(version, short commit) of the running code; commit may be ''."""
    env = os.environ if env is None else env
    version = env.get("ESTYMA_VERSION", "").strip()
    revision = env.get("ESTYMA_REVISION", "").strip().lower()
    if not version and os.path.isdir(os.path.join(_ROOT, ".git")):
        version = _git("describe", "--tags", "--always", "--dirty")
        revision = revision or _git("rev-parse", "HEAD")
    revision = revision[:7] if _SHA.fullmatch(revision) else ""
    return version or "dev", revision


VERSION, REVISION = detect()


def label(version: str = VERSION, revision: str = REVISION) -> str:
    """Human readable, e.g. 'v1.0.0 (3f2a9c1)' or 'dev'."""
    if revision and revision not in version:
        return f"{version} ({revision})"
    return version


def footer_html(version: str = VERSION, revision: str = REVISION) -> str:
    return ('<footer class="version" style="margin:2rem 0 0;padding-top:.6rem;'
            'border-top:1px solid rgba(127,127,127,.25);font-size:.8rem;opacity:.7">'
            f'estyma-local <span data-version>{html.escape(label(version, revision))}</span>'
            '</footer>')


def add_footer(page: bytes, footer: str | None = None) -> bytes:
    """Put the version footer at the end of the page's <main>, else before </body>."""
    tag = (footer or footer_html()).encode()
    for end in (b"</main>", b"</body>"):
        i = page.rfind(end)
        if i != -1:
            return page[:i] + tag + b"\n" + page[i:]
    return page + tag
