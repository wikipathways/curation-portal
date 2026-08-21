"""Read a pull request the portal did not open, and decide what review it describes.

The portal's own submissions arrive with their identity already known: the app chose the WPID,
wrote the path and opened the branch. A pull request from the PathVisio plugin, or from anyone
with push access, arrives with none of that — only a file list. This module turns that file list
into the three facts a ``Review`` row cannot be built without: **which pathway, new or an edit,
and whose**.

Deliberately pure. ``derive`` performs no I/O and imports nothing from ``app.main``, so every
shape below is a unit test rather than a fixture with a fake GitHub behind it.

**The classification is the target repository's, not ours.** ``1_on_pull_request.yml`` decides new
versus edit by *filename*, and this agrees with it character for character by reusing
``app.pipeline.drafts._PIPELINE_EDIT_RE``. Where GitHub's own ``status`` field disagrees — a
``WP1234`` directory reported as ``added``, say — the disagreement is recorded and the filename
still wins. Predicting something the repository will not do is how the dashboard ends up reporting
an app-side mis-prediction against a submitter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.github import PrFile, PullRequestDetail
from app.pipeline.drafts import _PIPELINE_EDIT_RE
from app.submit.gpml import PLACEHOLDER_WPID_STR

#: ``pathways/<dir>/<file>`` — the only layout the content repository has.
_PATHWAY_PATH_RE = re.compile(r"^pathways/([^/]+)/([^/]+)$")

#: A directory that names a real pathway: ``WP`` plus an id with no leading zero. Same grammar as
#: the pipeline's filename test, applied to the directory, because the two always agree in the
#: repository's own layout and the directory is what identifies the pathway.
_PATHWAY_DIR_RE = re.compile(r"^WP[1-9][0-9]{0,4}$")


@dataclass(frozen=True)
class Adoption:
    """What a foreign pull request turns out to be."""

    pr_number: int
    submitter: str
    kind: str  # "new" | "update"
    wpid: int | None
    #: The GPML the review is *about*: the preview, the quality report and the checklist all
    #: describe this one file, because ``PreviewService.render_local`` renders one GPML.
    primary_path: str
    #: Every pathway GPML the pull request touches, sorted. More than one is not an error here —
    #: it is recorded, surfaced on the card, and blocked at the approval gate by the
    #: ``one_pathway_per_pr`` checklist item, which is the surface that already knows how to
    #: explain itself to a curator.
    paths: list[str]
    head_branch: str
    head_repo: str | None
    head_sha: str
    title: str
    #: Set when GitHub's ``status`` disagrees with the filename classification. Recorded, never
    #: acted on.
    note: str | None = None

    @property
    def multi_pathway(self) -> bool:
        return len(self.paths) > 1


@dataclass(frozen=True)
class Skipped:
    """Why a pull request is not a pathway submission. Carried so the log can say."""

    pr_number: int
    reason: str


def _pathway_gpml(files: list[PrFile]) -> list[PrFile]:
    """The pathway GPML files a pull request adds or changes.

    Deletions are dropped rather than adopted. A removal leaves nothing to render and a checklist
    of failures blaming the author for a file they deliberately deleted.
    """
    out = []
    for entry in files:
        if entry.status == "removed":
            continue
        match = _PATHWAY_PATH_RE.match(entry.filename)
        if match and match.group(2).lower().endswith(".gpml"):
            out.append(entry)
    return out


def derive(detail: PullRequestDetail, files: list[PrFile]) -> Adoption | Skipped:
    """Classify a pull request. Returns ``Skipped`` — never None — so the caller can log why."""
    gpml = _pathway_gpml(files)
    if not gpml:
        touches_pathways = any(f.filename.startswith("pathways/") for f in files)
        removed_gpml = any(
            f.status == "removed" and f.filename.endswith(".gpml") for f in files
        )
        if removed_gpml:
            reason = "only deletes pathway files"
        elif touches_pathways:
            reason = "touches pathways/ but changes no GPML"
        else:
            reason = "changes no pathway files"
        return Skipped(pr_number=detail.number, reason=reason)

    paths = sorted(f.filename for f in gpml)
    primary = paths[0]
    by_path = {f.filename: f for f in gpml}
    directory = _PATHWAY_PATH_RE.match(primary).group(1)  # matched in _pathway_gpml
    filename = primary.rsplit("/", 1)[-1]

    note: str | None = None
    if _PATHWAY_DIR_RE.match(directory) and _PIPELINE_EDIT_RE.fullmatch(filename):
        kind, wpid = "update", int(directory[2:])
        if by_path[primary].status == "added":
            note = (
                f"`{primary}` is added rather than modified, so this pathway is not on the "
                f"base branch yet. Classified as an edit anyway, because the repository "
                f"classifies by filename."
            )
    else:
        # Either the placeholder (`WP0001`, whose leading zero keeps it out of the edit grammar
        # on purpose) or a title-derived directory, which is what the PathVisio plugin writes for
        # a new pathway — `pathways/testing_new_pathway/testing_new_pathway.gpml`. The repository
        # files both as new submissions under `WP0`, so neither is malformed.
        kind, wpid = "new", None
        if directory != PLACEHOLDER_WPID_STR and by_path[primary].status == "modified":
            note = (
                f"`{primary}` is modified rather than added, but its directory does not name a "
                f"pathway id. Classified as a new submission, because the repository classifies "
                f"by filename."
            )

    return Adoption(
        pr_number=detail.number,
        submitter=detail.author,
        kind=kind,
        wpid=wpid,
        primary_path=primary,
        paths=paths,
        head_branch=detail.head_branch,
        head_repo=detail.head_repo,
        head_sha=detail.head_sha,
        title=detail.title,
        note=note,
    )
