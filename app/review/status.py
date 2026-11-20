"""How a review's state is named on screen.

One vocabulary for the badge, the queue tab, the card banner and the empty state. Without it the
dashboard prints the stored enum — a curator reads ``changes_requested`` and ``publish_failed``,
underscores and all, and the four states the pipeline actually spends its time in have no tab to
reach them from.

The long-form sentences used in the mirror comment live in ``app.review.service._PLAIN_STATUS``
and say the same things in prose; these are the short forms.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from app.models import ReviewStatus


@dataclass(frozen=True)
class StatusPresentation:
    #: Short label for the badge and the queue tab.
    label: str
    #: One sentence naming what state the submission is in and who it is waiting on. Rendered as
    #: the card banner for every status that is not simply "open".
    blurb: str
    #: What an empty queue means *for this filter*. "Nothing is waiting on review" is wrong under
    #: the Published tab and reads as a broken filter.
    empty_heading: str
    empty_body: str
    #: The card's banner tone. None = no banner (an open review needs no explaining).
    tone: str | None = None


STATUS: dict[str, StatusPresentation] = {
    ReviewStatus.OPEN.value: StatusPresentation(
        label="Open",
        blurb="Waiting on a curator.",
        empty_heading="The queue is empty",
        empty_body="Nothing is waiting on review right now.",
    ),
    ReviewStatus.CHANGES_REQUESTED.value: StatusPresentation(
        label="Changes requested",
        blurb="Waiting on the submitter. Re-uploading the GPML puts it back in the queue.",
        empty_heading="Nothing is waiting on a submitter",
        empty_body="No review has been sent back for changes.",
        tone="warn",
    ),
    ReviewStatus.APPROVED.value: StatusPresentation(
        label="Approved",
        # Both kinds, for the docs' table of states; a card uses ``_KIND_BLURB``.
        blurb=(
            "Approved and handed to the repository. It publishes the pathway, assigning the WPID "
            "if it is new, and closes the pull request without merging it."
        ),
        empty_heading="Nothing is waiting to be published",
        empty_body="No approved submission is sitting with the repository right now.",
        tone="info",
    ),
    ReviewStatus.PUBLISHED.value: StatusPresentation(
        label="Published",
        blurb="Published by the repository. A new pathway has the WPID it assigned.",
        empty_heading="Nothing has been published yet",
        empty_body="No submission has come back from the repository with an assigned WPID.",
        tone="ok",
    ),
    ReviewStatus.PUBLISH_FAILED.value: StatusPresentation(
        label="Not published",
        blurb=(
            "Approved, but the repository never published it. This one needs a person: re-run "
            "its publish workflow, then record the assigned WPID here."
        ),
        empty_heading="No publication has failed",
        empty_body="Every approved submission either published or is still in progress.",
        tone="bad",
    ),
    ReviewStatus.REJECTED.value: StatusPresentation(
        label="Rejected",
        blurb="Rejected by a curator.",
        empty_heading="Nothing has been rejected",
        empty_body="No submission has been turned down.",
        tone="bad",
    ),
    ReviewStatus.MERGED.value: StatusPresentation(
        label="Merged",
        blurb="Merged.",
        empty_heading="Nothing has been merged",
        empty_body="No submission has been merged from this portal.",
        tone="ok",
    ),
    ReviewStatus.CLOSED.value: StatusPresentation(
        label="Closed",
        blurb="Closed without merging, outside this portal.",
        empty_heading="Nothing has been closed",
        empty_body="No pull request has been closed out from under a review.",
        tone="muted",
    ),
}

#: Which tabs the queue shows, in order. Mode-dependent because half of these states cannot occur
#: in the other mode: nothing is ever merged where the repository publishes through its own
#: Actions, and nothing is ever published where this app merges the pull request itself. Showing
#: a tab that can only ever be empty teaches a curator to ignore the tab strip.
_PIPELINE_TABS = [
    ReviewStatus.OPEN,
    ReviewStatus.CHANGES_REQUESTED,
    ReviewStatus.APPROVED,
    ReviewStatus.PUBLISHED,
    ReviewStatus.PUBLISH_FAILED,
    ReviewStatus.REJECTED,
    ReviewStatus.CLOSED,
]
_DIRECT_TABS = [
    ReviewStatus.OPEN,
    ReviewStatus.CHANGES_REQUESTED,
    ReviewStatus.MERGED,
    ReviewStatus.REJECTED,
    ReviewStatus.CLOSED,
]

#: States where the submission is still live and can be revised: the pull request is open and a
#: new commit on it means something.
ACTIONABLE = frozenset({ReviewStatus.OPEN.value, ReviewStatus.CHANGES_REQUESTED.value})

#: States a curator's decision still applies to. PUBLISH_FAILED is in here and ACTIONABLE is not:
#: the approval was made and did not take, and re-approving is the documented way to start a
#: fresh publish run (the label is removed and re-applied, so the repository's dispatcher fires
#: again). Rejecting or asking for changes are the other honest answers to a stuck publication.
#: Without this the state every approval lands on today would offer a curator nothing but a form
#: asking for a WPID that does not exist.
DECIDABLE = ACTIONABLE | frozenset({ReviewStatus.PUBLISH_FAILED.value})

#: States where the WPID the repository assigned is the missing piece, so a curator is offered
#: the form to record it by hand.
AWAITING_WPID = frozenset({ReviewStatus.APPROVED.value, ReviewStatus.PUBLISH_FAILED.value})


def queue_tabs(*, pipeline_mode: bool, counts: dict[str, int]) -> list[dict]:
    """The tab strip, with a count per tab.

    A status carrying reviews always gets a tab even when the mode says it should not — a
    deployment that switched publish modes leaves rows behind, and a review with no tab is a
    review nobody can reach.
    """
    tabs = _PIPELINE_TABS if pipeline_mode else _DIRECT_TABS
    values = [s.value for s in tabs]
    extra = [
        s.value
        for s in ReviewStatus
        if s.value not in values and counts.get(s.value, 0) > 0
    ]
    return [
        {
            "status": value,
            "label": STATUS[value].label,
            "count": counts.get(value, 0),
        }
        for value in [*values, *extra]
    ]


#: The banner sentence on a card, by the review's kind, where the generic one hedges for the docs'
#: table. A new pathway gets its WPID from the repository; an update has had its WPID all along,
#: and ``{wpid}`` is filled in from the review.
_KIND_BLURB = {
    "new": {
        ReviewStatus.APPROVED.value: (
            "Approved and handed to the repository. It assigns the WPID, publishes the pathway "
            "and closes the pull request without merging it."
        ),
        ReviewStatus.PUBLISHED.value: "Published by the repository, with the WPID it assigned.",
    },
    "update": {
        ReviewStatus.APPROVED.value: (
            "Approved and handed to the repository. It publishes the edit to {wpid} and closes "
            "the pull request without merging it."
        ),
        ReviewStatus.PUBLISHED.value: "Published to {wpid} by the repository.",
    },
}


def presentation(
    status: str, *, kind: str | None = None, wpid: int | None = None
) -> StatusPresentation:
    """How ``status`` is named on screen. Given the review's ``kind`` (and an update's ``wpid``),
    the banner sentence is the one for that kind rather than the generic one covering both."""
    shown = STATUS.get(status) or StatusPresentation(
        label=status.replace("_", " ").capitalize(),
        blurb="",
        empty_heading="Nothing here",
        empty_body="No review is in this state.",
    )
    blurb = _KIND_BLURB.get(kind or "", {}).get(status)
    if blurb is not None and not (kind == "update" and wpid is None):
        shown = replace(shown, blurb=blurb.format(wpid=f"WP{wpid}"))
    return shown
