"""Who changed a review, and when: an append-only trail written from the session's change tracking.

A review's state is changed from a dozen places — the portal's buttons, label and close webhooks,
the reconcile pass, a re-upload, adoption — and the row only ever held the latest ``decided_by``.
Asking each of those paths to write an event would be a dozen chances to forget one. Instead a
``before_flush`` listener compares each changed ``Review`` with what was loaded and records:

- ``opened``: a new row (the submitter, or the pull request author for an adopted one);
- ``status``: any status change, with the note that explains it where there is one;
- ``assigned``: the assigned curator changing;
- ``checklist``: a person's answer to a checklist item. The automated hints beside the items are
  not recorded — they are rewritten on every upload and pipeline run and would bury the human
  decisions the trail exists for — and neither are the states the portal filled in on rows written
  before it stopped doing so.

The actor is whoever the service method was acting for (``acting_as``), else ``portal``. Bulk
UPDATEs on the table (the reconcile claim) bypass the ORM and are deliberately not recorded.
"""
from __future__ import annotations

import functools
import inspect as pyinspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Review, ReviewEvent, ReviewStatus

_actor: ContextVar[str | None] = ContextVar("review_audit_actor", default=None)


@contextmanager
def acting_as(actor: str | None) -> Iterator[None]:
    token = _actor.set(actor or _actor.get())
    try:
        yield
    finally:
        _actor.reset(token)


def as_actor(param: str) -> Callable:
    """Run a service method acting as the login in its ``param`` argument."""

    def decorate(fn: Callable) -> Callable:
        signature = pyinspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            actor = signature.bind_partial(*args, **kwargs).arguments.get(param)
            with acting_as(actor if isinstance(actor, str) and actor else None):
                return fn(*args, **kwargs)

        return wrapper

    return decorate


def default_actor(name: str) -> Callable:
    """Act as ``name`` unless a caller already said who is acting.

    For the handlers that react to GitHub — a close, a new head commit — which are reached both
    from a webhook (nobody named) and from a curator's own action (who should keep the credit).
    """

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with acting_as(_actor.get() or name):
                return fn(*args, **kwargs)

        return wrapper

    return decorate


def current_actor() -> str:
    return _actor.get() or "portal"


def _value(v):
    return v.value if isinstance(v, ReviewStatus) else v


def _change(obj, attr: str):
    hist = inspect(obj).attrs[attr].history
    if not hist.has_changes():
        return None
    old = hist.deleted[0] if hist.deleted else None
    new = hist.added[0] if hist.added else None
    return (old, new) if old != new else None


def _checklist_events(old: list | None, new: list | None) -> list[dict]:
    before = {i.get("key"): i for i in (old or [])}
    out = []
    for item in new or []:
        if item.get("auto"):
            continue
        prior = before.get(item.get("key")) or {}
        if prior.get("state") == item.get("state") and not prior.get("auto"):
            continue
        if prior.get("auto") and item.get("state") == "pending":
            # An app-filled state on an old row giving way to a fresh, unanswered item: nobody
            # decided anything.
            continue
        out.append(
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "from": prior.get("state"),
                "to": item.get("state"),
                "note": item.get("note") or None,
            }
        )
    return out


def _before_flush(session: Session, _ctx, _instances) -> None:
    actor = current_actor()
    events: list[ReviewEvent] = []
    for obj in list(session.new):
        if isinstance(obj, Review):
            events.append(
                ReviewEvent(
                    pr_number=obj.pr_number,
                    actor=_actor.get() or obj.submitter,
                    kind="opened",
                    detail={"origin": obj.origin or "portal", "kind": obj.kind},
                )
            )
    for obj in list(session.dirty):
        if not isinstance(obj, Review):
            continue
        status = _change(obj, "status")
        if status:
            detail = {"from": _value(status[0]), "to": _value(status[1])}
            # Only a note written with this change explains it. A note left over from an earlier
            # decision would put a curator's old words against a transition they did not make.
            note = _change(obj, "decision_note")
            if note and note[1]:
                detail["note"] = note[1]
            events.append(
                ReviewEvent(pr_number=obj.pr_number, actor=actor, kind="status", detail=detail)
            )
        assigned = _change(obj, "assigned_curator")
        if assigned:
            events.append(
                ReviewEvent(
                    pr_number=obj.pr_number,
                    actor=actor,
                    kind="assigned",
                    detail={"from": assigned[0], "to": assigned[1]},
                )
            )
        checklist = _change(obj, "checklist")
        if checklist:
            for item in _checklist_events(*checklist):
                events.append(
                    ReviewEvent(
                        pr_number=obj.pr_number, actor=actor, kind="checklist", detail=item
                    )
                )
    session.add_all(events)


def install(factory: sessionmaker) -> None:
    """Attach the listener to ``factory`` once.

    Marked on the factory itself rather than asked of ``event.contains``: SQLAlchemy's listener
    registry is keyed by ``id()``, so a new factory that reuses the address of a collected one is
    reported as already listened to, and then records nothing. That happened to about one fresh
    factory in thirty in the test suite (a flaky history test, 2026-11-12).
    """
    if getattr(factory, "_review_audit_installed", False):
        return
    event.listen(factory, "before_flush", _before_flush)
    factory._review_audit_installed = True


def history(factory: sessionmaker, pr_number: int, *, limit: int = 200) -> list[ReviewEvent]:
    """A review's events, newest first."""
    with factory() as s:
        return list(
            s.execute(
                select(ReviewEvent)
                .where(ReviewEvent.pr_number == pr_number)
                .order_by(ReviewEvent.at.desc(), ReviewEvent.id.desc())
                .limit(limit)
            ).scalars()
        )
