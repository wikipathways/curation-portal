from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import make_engine, make_session_factory
from app.locks import PathwayLockRegistry
from app.models import Base


@pytest.fixture
def session_factory(tmp_path) -> sessionmaker:
    # File-based SQLite (not :memory:) so multiple threads/connections share one database —
    # required to exercise the concurrent-allocation race meaningfully.
    engine = make_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def locks(session_factory) -> PathwayLockRegistry:
    """A lock registry with no open-PR scanner — for tests about what happens to a lock the app
    already holds, rather than about refusing to take one."""
    return PathwayLockRegistry(session_factory)


class RecordingPreviews:
    """Stands in for PreviewService: records which PRs were freed, and what a sweep was told.

    Shared because the two halves of issue #18 are exercised from opposite ends — the per-
    transition free from the curation tests, the sweep from the pipeline ones — and a second copy
    would be a second thing to keep in step.
    """

    def __init__(self) -> None:
        self.discarded: list[int] = []
        self.swept: list[set[int]] = []

    def discard(self, pr_number: int) -> bool:
        self.discarded.append(pr_number)
        return True

    def sweep(self, keep, *, force: bool = False) -> int:
        self.swept.append(set(keep))
        return 0


class RecordingDrafts:
    """Stands in for DraftsReader, for the one thing the curation service asks of it."""

    def __init__(self) -> None:
        self.sweeps = 0

    def sweep(self, *, force: bool = False) -> int:
        self.sweeps += 1
        return 0


@pytest.fixture(autouse=True)
def _draft_pages_are_built(request, monkeypatch):
    """Every pipeline draft page counts as built unless a test says otherwise.

    ``CurationService.approve`` refuses in pipeline mode when the draft page workflow 3A needs is
    missing, and the real check is a HEAD request to raw.githubusercontent.com. Nearly every
    approval in the suite is about something else, and none of them should touch the network.
    Mark a test ``@pytest.mark.real_draft_check`` to exercise the real method.
    """
    if request.node.get_closest_marker("real_draft_check"):
        return
    from app.pipeline.drafts import DraftsReader

    monkeypatch.setattr(DraftsReader, "draft_page_exists", lambda self, slug: True)
