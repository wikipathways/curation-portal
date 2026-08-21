"""What a pull request the portal did not open turns out to be.

Every case here is a **real pull request** on `wikipathways/sandbox-wp-db`, measured on
2026-08-21 and named by its number. That is deliberate, and it is the same reasoning as
`tests/fixtures/published/`: hand-invented shapes encode what their author already thought to
include, and this project has been bitten five times by exactly that. The PathVisio plugin writes
a *title-derived* directory for a new pathway, which nobody would have guessed.
"""
from __future__ import annotations

import pytest

from app.github import PrFile, PullRequestDetail
from app.review.adopt import Adoption, Skipped, derive


def _detail(number: int, *, author: str = "traybug23", title: str = "") -> PullRequestDetail:
    return PullRequestDetail(
        number=number,
        html_url=f"https://github.com/wikipathways/sandbox-wp-db/pull/{number}",
        head_branch=f"WP0001_{author}_20260820-053517",
        head_sha="e47f6026bcfa42d4f4991296a728eb0babb23f41",
        state="open",
        merged=False,
        title=title,
        body="",
        labels=[],
        author=author,
        head_repo=f"{author}/sandbox-wp-db",
    )


def _f(path: str, status: str = "modified") -> PrFile:
    return PrFile(filename=path, status=status)


# --- the three path shapes, all measured -------------------------------------------------


def test_pr73_wpid_directory_is_an_update():
    """#73 `Contribution: Update WP3894` — the ordinary plugin edit."""
    got = derive(_detail(73), [_f("pathways/WP3894/WP3894.gpml")])
    assert isinstance(got, Adoption)
    assert (got.kind, got.wpid) == ("update", 3894)
    assert got.primary_path == "pathways/WP3894/WP3894.gpml"
    assert got.submitter == "traybug23"
    assert not got.multi_pathway
    assert got.note is None


def test_pr63_title_derived_directory_is_a_new_pathway():
    """#63 adds `pathways/TESTGLYCOLYSIS/TESTGLYCOLYSIS.gpml`.

    The plugin sanitises the pathway *title* into the path for a new submission, so a new
    pathway does not arrive at a WPID path at all. The repository files anything not matching
    its edit grammar as new under `WP0`, so this is a supported shape and must not be refused.
    """
    got = derive(_detail(63), [_f("pathways/TESTGLYCOLYSIS/TESTGLYCOLYSIS.gpml", "added")])
    assert isinstance(got, Adoption)
    assert (got.kind, got.wpid) == ("new", None)
    assert got.primary_path == "pathways/TESTGLYCOLYSIS/TESTGLYCOLYSIS.gpml"


def test_placeholder_directory_is_new_and_never_coerced_to_wp1():
    """`WP0001` is a placeholder, not an address.

    Its leading zero keeps it out of the pipeline's edit grammar on purpose, and `WP0001`
    reaching published GPML is a defect this project has already had to repair. Reading it as
    WP1 would be the same mistake with a different sign.
    """
    got = derive(_detail(74), [_f("pathways/WP0001/WP0001.gpml", "added")])
    assert isinstance(got, Adoption)
    assert (got.kind, got.wpid) == ("new", None)


# --- more than one pathway, which is not hypothetical -------------------------------------


def test_pr74_two_directories_is_adopted_and_flagged():
    """#74 adds the placeholder *and* a title-derived directory in one pull request."""
    got = derive(
        _detail(74),
        [
            _f("pathways/testing_new_pathway/testing_new_pathway.gpml", "added"),
            _f("pathways/WP0001/WP0001.gpml", "added"),
        ],
    )
    assert isinstance(got, Adoption)
    assert got.multi_pathway
    assert len(got.paths) == 2
    # Sorted, so the primary is deterministic and explainable rather than "the biggest diff".
    assert got.primary_path == "pathways/WP0001/WP0001.gpml"


def test_pr58_two_wpid_directories_takes_the_first_in_sort_order():
    """#58 modifies WP1072 and WP179."""
    got = derive(
        _detail(58),
        [_f("pathways/WP1072/WP1072.gpml"), _f("pathways/WP179/WP179.gpml")],
    )
    assert isinstance(got, Adoption)
    assert (got.kind, got.wpid) == ("update", 1072)
    assert got.paths == ["pathways/WP1072/WP1072.gpml", "pathways/WP179/WP179.gpml"]


# --- what must stay out of the queue ------------------------------------------------------


def test_pr67_workflow_only_is_skipped():
    """#67 changes `.github/workflows/*` and nothing else — it is not a submission."""
    got = derive(_detail(67, author="marvinm2"), [_f(".github/workflows/1_on_pull_request.yml")])
    assert isinstance(got, Skipped)
    assert got.reason == "changes no pathway files"


def test_a_deletion_is_not_adopted():
    """An empty after-render and a checklist blaming the author for deleting a file on purpose."""
    got = derive(_detail(90), [_f("pathways/WP1/WP1.gpml", "removed")])
    assert isinstance(got, Skipped)
    assert got.reason == "only deletes pathway files"


def test_pathway_metadata_without_gpml_is_not_adopted():
    got = derive(_detail(91), [_f("pathways/WP1/WP1.md")])
    assert isinstance(got, Skipped)
    assert got.reason == "touches pathways/ but changes no GPML"


# --- status is a cross-check, never the authority -----------------------------------------


@pytest.mark.parametrize(
    ("path", "status", "kind", "wpid"),
    [
        ("pathways/WP5432/WP5432.gpml", "added", "update", 5432),
        ("pathways/some_title/some_title.gpml", "modified", "new", None),
    ],
)
def test_the_filename_wins_over_githubs_status_and_the_disagreement_is_recorded(
    path, status, kind, wpid
):
    """The repository classifies by filename, so the app does too — and says when they differ.

    Acting on `status` instead would make the app predict a slug the pipeline never writes, and
    the dashboard would then read artifacts that do not exist and report it against the submitter.
    """
    got = derive(_detail(92), [PrFile(filename=path, status=status)])
    assert isinstance(got, Adoption)
    assert (got.kind, got.wpid) == (kind, wpid)
    assert got.note is not None and path in got.note
