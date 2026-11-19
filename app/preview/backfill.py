"""Re-render cached previews drawn by an older renderer (stream A fills this in).

Called from the background tick. Never raises: a preview is cosmetic.
"""
from __future__ import annotations


def refresh_stale_renders(
    preview, session_factory, github, *, content_repo: str, default_branch: str, budget: int = 5
) -> int:
    """Re-render up to ``budget`` live reviews whose render is stale; return how many were."""
    return 0
