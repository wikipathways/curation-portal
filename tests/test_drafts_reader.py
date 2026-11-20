"""Reading the target repo pipeline's derived artifacts off its site repo.

Driven by an injected `httpx.MockTransport`, the same seam `app/github/client.py` uses, so no
test touches the network. The happy-path fixtures under `tests/fixtures/drafts/` are the real
files the pipeline produced for `WP0__PR10`, downloaded from `wikipathways/sandbox-wp.gh.io` —
column names and quoting included, since guessing those is exactly what this module must not do.
`WP554__PR11-bibliography.tsv` is there for the shortfall case: it is the real bibliography of a
draft whose GPML declares 8 distinct references and whose bibliography resolved 7.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from app.pipeline import (
    DraftsReader,
    datanode_check,
    info_checks,
    reference_check,
)
from app.review.checklist import ChecklistState
from app.submit.gpml import PLACEHOLDER_WPID_STR

FIXTURES = Path(__file__).parent / "fixtures" / "drafts"

REPO = "wikipathways/sandbox-wp.gh.io"
BRANCH = "main"
SITE = "https://sandbox.wikipathways.org"

SLUG = "WP0__PR10"

DATANODES_PATH = f"/_data/drafts/{SLUG}-datanodes.tsv"
BIBLIOGRAPHY_PATH = f"/_data/drafts/{SLUG}-bibliography.tsv"
INFO_PATH = f"/draft_assets/{SLUG}/{SLUG}-info.json"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_reader(handler, tmp_path, **kwargs) -> DraftsReader:
    return DraftsReader(
        repo=REPO,
        branch=BRANCH,
        site_base_url=SITE,
        cache_dir=tmp_path / "drafts-cache",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def serve(files: dict[str, bytes], seen: list[str] | None = None):
    """A handler serving `files` keyed by the path under `<repo>/<branch>/`; 404 otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/{REPO}/{BRANCH}")
        if seen is not None:
            seen.append(path)
        body = files.get(path)
        if body is None:
            return httpx.Response(404, text="404: Not Found")
        return httpx.Response(200, content=body)

    return handler


def real_files() -> dict[str, bytes]:
    return {
        DATANODES_PATH: fixture(f"{SLUG}-datanodes.tsv"),
        BIBLIOGRAPHY_PATH: fixture(f"{SLUG}-bibliography.tsv"),
        INFO_PATH: fixture(f"{SLUG}-info.json"),
    }


def tsv(rows: list[list[str]]) -> bytes:
    return ("\n".join("\t".join(cells) for cells in rows) + "\n").encode()


# ---- slug derivation ----------------------------------------------------------------------


def test_slug_for_new_submission_uses_the_zero_placeholder():
    assert DraftsReader.slug_for(kind="new", wpid=None, pr_number=54) == "WP0__PR54"
    # A new submission stays WP0 even if the app happens to know a WPID: workflow 1 classifies
    # from the filename, and a new pathway's file is not named WP<id>.gpml.
    assert DraftsReader.slug_for(kind="new", wpid=1234, pr_number=54) == "WP0__PR54"


def test_slug_for_update_keeps_the_existing_wpid():
    assert DraftsReader.slug_for(kind="update", wpid=133, pr_number=45) == "WP133__PR45"
    assert DraftsReader.slug_for(kind="update", wpid=None, pr_number=45) == "WP0__PR45"


def test_the_new_pathway_placeholder_is_not_pipeline_edit_shaped():
    """The WP0 prediction only holds while the placeholder keeps its leading zero.

    `app/submit/gpml.py` commits a new pathway as WP0001.gpml, which fails the pipeline's
    `^WP[1-9][0-9]{0,4}\\.gpml$` test and is therefore filed as new. Renaming the placeholder to
    something in range would silently make every new submission predict the wrong slug.
    """
    assert DraftsReader.slug_for_filename(f"{PLACEHOLDER_WPID_STR}.gpml", 54) == "WP0__PR54"


@pytest.mark.parametrize(
    ("basename", "expected"),
    [
        # In range for the pipeline's pattern: WP1 to WP99999.
        ("WP1.gpml", "WP1__PR7"),
        ("WP99999.gpml", "WP99999__PR7"),
        # Out of range, so the pipeline calls it new. Predicting WP0 here is the point: the app
        # must not send the dashboard after a WP0001__PR7 or WP123456__PR7 nobody ever writes.
        ("WP0.gpml", "WP0__PR7"),
        ("WP0001.gpml", "WP0__PR7"),
        ("WP123456.gpml", "WP0__PR7"),
        ("ketogenic in epilepstogenesis.gpml", "WP0__PR7"),
        # A basename already carrying a slug is collapsed to its prefix first, so a resubmitted
        # draft is classified as an edit of the pathway in that prefix. Live: the site repo has
        # WP1001 drafts under PR60, 61, 62 and 64.
        ("WP1001__PR60.gpml", "WP1001__PR7"),
        ("WP0__PR60.gpml", "WP0__PR7"),
        # A path, not a bare name: the workflow takes the basename first.
        ("pathways/WP554/WP554.gpml", "WP554__PR7"),
    ],
)
def test_slug_for_filename_follows_the_pipelines_own_classification(basename, expected):
    assert DraftsReader.slug_for_filename(basename, 7) == expected


def test_slug_for_an_out_of_range_wpid_predicts_what_the_pipeline_files():
    # 0 and anything six digits or longer fail the pipeline's pattern, so it files them as new.
    assert DraftsReader.slug_for(kind="update", wpid=0, pr_number=45) == "WP0__PR45"
    assert DraftsReader.slug_for(kind="update", wpid=123456, pr_number=45) == "WP0__PR45"


# ---- the happy path, against the real artifacts -------------------------------------------


def test_fetch_reads_the_three_artifacts_and_builds_the_urls(tmp_path):
    reader = make_reader(serve(real_files()), tmp_path)
    art = reader.fetch(SLUG)

    assert art.available is True
    assert len(art.datanodes) == 17
    assert art.datanodes[0]["Label"] == "ATBC"
    assert art.datanodes[0]["Identifier"] == "wikidata:Q306135"
    assert len(art.bibliography) == 3
    assert art.bibliography[0]["Database"] == "DOI"
    assert art.info["title"] == "Vitamin A1 and A5/X pathways"
    assert art.draft_url == f"{SITE}/drafts/{SLUG}"
    assert art.svg_url == (
        f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/draft_assets/{SLUG}/{SLUG}.svg"
    )
    assert art.thumb_url.endswith(f"draft_assets/{SLUG}/{SLUG}-thumb.png")


def test_checks_on_the_real_artifacts(tmp_path):
    art = make_reader(serve(real_files()), tmp_path).fetch(SLUG)

    # Pending, not pass: the table lists the 17 nodes the pipeline annotated, and the GPML it
    # came from has 21 data nodes. It cannot answer "are the data nodes annotated?".
    state, note = datanode_check(art)
    assert state == ChecklistState.PENDING.value
    assert "17" in note

    # The fixture's bibliography holds exactly the three references its GPML declares.
    assert reference_check(art, gpml_reference_count=3)[0] == ChecklistState.PASS.value

    checks = info_checks(art)
    assert checks["naming_ok"][0] == ChecklistState.PASS.value
    assert "assigned on approval" in checks["naming_ok"][1]
    assert checks["ontology_tags"] == (ChecklistState.PASS.value, "3 ontology tags.")
    # Both title and description are present, so the "meaningful?" judgement stays with the
    # curator rather than being auto-passed.
    assert checks["description_ok"][0] == ChecklistState.PENDING.value


def test_a_slug_disagreement_is_pending_rather_than_a_fail_on_a_required_item(tmp_path):
    """`naming_ok` is required, and the app is at least as likely to be wrong as the submitter."""
    files = real_files()
    files[INFO_PATH] = b'{"wpid": "WP99__PR10", "title": "T", "description": "D"}'
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    state, note = info_checks(art)["naming_ok"]
    assert state == ChecklistState.PENDING.value
    assert "WP99__PR10" in note and SLUG in note


def test_description_check_fails_on_the_empty_description_the_pipeline_often_writes(tmp_path):
    files = real_files()
    # Verbatim shape of a real draft (WP1001__PR60): title extracted, description empty.
    files[INFO_PATH] = b'{"wpid": "WP0__PR10", "title": "Peptide GPCRs", "description": ""}'
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    checks = info_checks(art)
    assert checks["description_ok"] == (
        ChecklistState.FAIL.value,
        "The pipeline extracted no description.",
    )
    # No ontology-ids key at all must read as the optional "none", not as a crash.
    assert checks["ontology_tags"][0] == ChecklistState.NA.value


# ---- the pipeline produced nothing --------------------------------------------------------


def test_missing_draft_is_unavailable_and_the_checks_say_nothing(tmp_path):
    art = make_reader(serve({}), tmp_path).fetch(SLUG)

    assert art.available is False
    assert (art.datanodes, art.bibliography, art.info) == (None, None, None)
    assert (art.draft_url, art.svg_url, art.thumb_url) == (None, None, None)
    assert datanode_check(art) is None
    assert reference_check(art, gpml_reference_count=3) is None
    assert info_checks(art) == {}


def test_fetch_never_raises_when_the_transport_errors(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("site repo unreachable", request=request)

    art = make_reader(handler, tmp_path).fetch(SLUG)
    assert art.available is False


def test_a_transport_error_is_not_cached(tmp_path):
    """A blip must not freeze a healthy draft as missing for the whole TTL.

    It does hold off the next attempt for two minutes, so a site that just refused is not asked
    again on every background pass, but nothing is written that would outlive that.
    """
    failing = {"first": True}
    clock = {"t": 1000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        if failing["first"]:
            raise httpx.ConnectError("blip", request=request)
        return serve(real_files())(request)

    reader = make_reader(handler, tmp_path, clock=lambda: clock["t"])
    assert reader.fetch(SLUG).available is False
    failing["first"] = False
    assert reader.fetch(SLUG).available is False  # inside the back-off: not asked yet
    clock["t"] += 121
    assert reader.fetch(SLUG).available is True


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "../../etc/passwd",
        "WP0__PR10/../x",
        "not-a-slug",
        # `$` matches before a trailing newline, so an anchored `match` would have let this
        # through into a URL and a cache filename.
        "WP0__PR10\n",
        "WP0__PR10\n../../etc/passwd",
    ],
)
def test_a_slug_that_is_not_pipeline_shaped_is_refused_without_a_request(tmp_path, slug):
    seen: list[str] = []
    art = make_reader(serve(real_files(), seen), tmp_path).fetch(slug)
    assert art.available is False
    assert seen == []


# ---- data-node judgement ------------------------------------------------------------------


def test_unrecognised_datanode_columns_are_pending_never_fail(tmp_path):
    files = real_files()
    files[DATANODES_PATH] = tsv(
        [["Node", "Kind", "Xref"], ["a", "GeneProduct", "ncbigene:1"], ["b", "Metabolite", ""]]
    )
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    state, note = datanode_check(art)
    assert state == ChecklistState.PENDING.value
    assert "2 data nodes" in note
    assert "Node, Kind, Xref" in note


def test_unmapped_identifiers_fail_with_the_counts_and_labels(tmp_path):
    files = real_files()
    files[DATANODES_PATH] = tsv(
        [
            ["Label", "Type", "Identifier"],
            ["CYP1A1", "GeneProduct", "ncbigene:1543"],
            ["mystery protein", "GeneProduct", ""],
            ["another one", "Metabolite", "NA"],
            ["third", "Metabolite", "null"],
        ]
    )
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    state, note = datanode_check(art)
    assert state == ChecklistState.FAIL.value
    assert "3 of 4" in note
    assert "mystery protein" in note


def test_a_full_datanode_table_stops_at_pending_because_it_lists_only_annotated_nodes(tmp_path):
    files = real_files()
    files[DATANODES_PATH] = tsv(
        [["Label", "Type", "Identifier"], ["CYP1A1", "GeneProduct", "ncbigene:1543"]]
    )
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    state, note = datanode_check(art)
    assert state == ChecklistState.PENDING.value
    assert "1 data nodes" in note


def test_an_empty_datanode_table_is_pending_and_says_why_it_is_not_reassuring(tmp_path):
    """Empty is the alarming reading, not the benign one, and it must not land as `na`.

    The generator lists only the nodes it could annotate — measured over the eight live drafts,
    no row anywhere has an empty Identifier and WP9__PR34 has 14 rows for 35 GPML data nodes —
    so an empty table means nothing resolved at least as plausibly as it means there is nothing
    there. `na` would also be written onto a required item and stall approval.
    """
    files = real_files()
    files[DATANODES_PATH] = tsv([["Label", "Type", "Identifier"]])
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    state, note = datanode_check(art)
    assert state == ChecklistState.PENDING.value
    assert "only the nodes it could annotate" in note


# ---- reference judgement ------------------------------------------------------------------


def test_reference_check_fails_on_a_real_shortfall_and_names_both_numbers(tmp_path):
    """WP554__PR11 as the pipeline actually filed it: 8 references in the GPML, 7 resolved."""
    files = real_files()
    files[BIBLIOGRAPHY_PATH] = fixture("WP554__PR11-bibliography.tsv")
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    assert len(art.bibliography) == 7
    state, note = reference_check(art, gpml_reference_count=8)
    assert state == ChecklistState.FAIL.value
    assert "7 of the 8" in note


def test_reference_check_without_references_is_pending_not_na(tmp_path):
    """`references_valid` is required, so an `na` here would be an item approval cannot accept."""
    art = make_reader(serve(real_files()), tmp_path).fetch(SLUG)
    state, note = reference_check(art, gpml_reference_count=0)
    assert state == ChecklistState.PENDING.value
    assert "no literature references" in note
    assert reference_check(art, gpml_reference_count=None) is None


def test_a_header_only_bibliography_reads_as_zero_resolved(tmp_path):
    files = real_files()
    # A real shape: the pipeline writes the header even when it resolved nothing. WP133__PR45 is
    # such a file, though there it is honest — that GPML declares no references at all.
    files[BIBLIOGRAPHY_PATH] = b"ID\tDatabase\tCitation\n"
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    assert art.bibliography == []
    state, note = reference_check(art, gpml_reference_count=2)
    assert state == ChecklistState.FAIL.value
    assert "0 of the 2" in note


# ---- caching ------------------------------------------------------------------------------


def test_a_second_fetch_is_served_from_the_cache(tmp_path):
    seen: list[str] = []
    reader = make_reader(serve(real_files(), seen), tmp_path)

    first = reader.fetch(SLUG)
    assert len(seen) == 3
    second = reader.fetch(SLUG)
    assert len(seen) == 3  # nothing new went over the wire
    assert second == first


def test_a_second_reader_shares_the_cache_on_disk(tmp_path):
    seen: list[str] = []
    make_reader(serve(real_files(), seen), tmp_path).fetch(SLUG)
    make_reader(serve(real_files(), seen), tmp_path).fetch(SLUG)
    assert len(seen) == 3


def test_an_expired_cache_entry_is_refetched(tmp_path):
    seen: list[str] = []
    reader = make_reader(serve(real_files(), seen), tmp_path, ttl_seconds=0)
    reader.fetch(SLUG)
    reader.fetch(SLUG)
    assert len(seen) == 6


def test_a_404_draft_is_cached_because_the_answer_was_definite(tmp_path):
    """Most of the pipeline's runs produce nothing; re-asking on every render is the load to
    avoid. A 404 is an answer about the file, so it is safe to keep for the TTL."""
    seen: list[str] = []
    reader = make_reader(serve({}, seen), tmp_path)
    assert reader.fetch(SLUG).available is False
    assert len(seen) == 3
    assert reader.fetch(SLUG).available is False
    assert len(seen) == 3


def test_a_rate_limit_is_not_cached_as_a_missing_draft(tmp_path):
    """raw.githubusercontent.com rate-limits anonymous callers, and the dashboard asks for three
    files per queued review on every render, so a 429 is expected. Caching it would freeze a
    healthy draft as missing for the whole TTL."""
    limited = {"on": True}
    clock = {"t": 1000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        if limited["on"]:
            return httpx.Response(429, text="rate limited")
        return serve(real_files())(request)

    reader = make_reader(handler, tmp_path, clock=lambda: clock["t"])
    assert reader.fetch(SLUG).available is False
    assert list((tmp_path / "drafts-cache").glob("*.json")) == []

    limited["on"] = False
    clock["t"] += 121  # past the back-off
    assert reader.fetch(SLUG).available is True


def test_one_failing_file_does_not_cache_the_other_two(tmp_path):
    """A partial answer is still not an answer: it would cache a draft as missing its info.json."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("-info.json"):
            return httpx.Response(503, text="unavailable")
        return serve(real_files())(request)

    reader = make_reader(handler, tmp_path)
    assert reader.fetch(SLUG).available is False
    assert list((tmp_path / "drafts-cache").glob("*.json")) == []


def test_a_corrupt_cache_file_is_ignored_rather_than_fatal(tmp_path):
    reader = make_reader(serve(real_files()), tmp_path)
    reader.fetch(SLUG)
    cached = next(iter((tmp_path / "drafts-cache").glob("*.json")))
    cached.write_text("{not json", encoding="utf-8")
    assert reader.fetch(SLUG).available is True


def test_readers_on_different_branches_do_not_share_a_cache_entry(tmp_path):
    """One cache_dir serves every reader, so the key has to carry the repo and the branch."""
    seen: list[str] = []
    make_reader(serve(real_files(), seen), tmp_path).fetch(SLUG)
    assert len(seen) == 3

    other = DraftsReader(
        repo=REPO,
        branch="gh-pages",
        site_base_url=SITE,
        cache_dir=tmp_path / "drafts-cache",
        transport=httpx.MockTransport(serve({}, seen)),
    )
    assert other.fetch(SLUG).available is False  # not the main-branch payload
    assert len(seen) == 6


def _cached_files(tmp_path) -> list[Path]:
    return sorted((tmp_path / "drafts-cache").glob("*.json"))


def _age(tmp_path, seconds: float) -> None:
    """Backdate every cache file, since the sweep is a question about elapsed time.

    Faking it with a tiny TTL does not work: the cutoff has a floor under it, so that a reader
    configured to cache for no time at all still does not delete a file it wrote a moment ago.
    """
    for path in _cached_files(tmp_path):
        stamp = path.stat().st_mtime - seconds
        os.utime(path, (stamp, stamp))


def test_sweep_takes_entries_too_old_to_serve(tmp_path):
    # Issue #18's other half. Entries expired by the TTL but the file stayed, so the disk kept one
    # per submission per scope forever -- including a whole dead scope on the live deployment,
    # from before its drafts repo was repointed at the fork.
    reader = make_reader(serve(real_files()), tmp_path, ttl_seconds=300)
    reader.fetch(SLUG)
    assert len(_cached_files(tmp_path)) == 1
    _age(tmp_path, 86400)

    assert reader.sweep() == 1
    assert _cached_files(tmp_path) == []


def test_sweep_spares_an_entry_still_inside_its_ttl(tmp_path):
    reader = make_reader(serve(real_files()), tmp_path, ttl_seconds=300)
    reader.fetch(SLUG)

    assert reader.sweep() == 0
    assert len(_cached_files(tmp_path)) == 1


def test_sweep_spares_an_entry_only_just_expired(tmp_path):
    # The margin over the TTL. Nothing breaks without it -- an expired entry is refetched either
    # way -- but mtime on the GlusterFS replica is not worth trusting to the second.
    reader = make_reader(serve(real_files()), tmp_path, ttl_seconds=300)
    reader.fetch(SLUG)
    _age(tmp_path, 400)

    assert reader.sweep() == 0
    assert len(_cached_files(tmp_path)) == 1


def test_sweeping_an_entry_costs_a_refetch_and_nothing_else(tmp_path):
    """The sweep is behaviour-neutral by construction: it only takes what `_read_cache` would
    already have refused, so the worst it can do is what the TTL was going to do anyway."""
    seen: list[str] = []
    reader = make_reader(serve(real_files(), seen), tmp_path, ttl_seconds=300)
    first = reader.fetch(SLUG)
    _age(tmp_path, 86400)
    reader.sweep()

    assert reader.fetch(SLUG) == first
    assert len(seen) == 6


def test_sweep_is_throttled(tmp_path):
    # The caller is the dashboard load, and this globs and stats the whole directory.
    reader = make_reader(serve(real_files()), tmp_path, ttl_seconds=300)
    reader.fetch(SLUG)
    _age(tmp_path, 86400)
    assert reader.sweep() == 1

    reader.fetch(SLUG)
    _age(tmp_path, 86400)
    assert reader.sweep() == 0
    assert len(_cached_files(tmp_path)) == 1
    assert reader.sweep(force=True) == 1


def test_sweep_on_a_cache_that_was_never_written_is_not_an_error(tmp_path):
    assert make_reader(serve({}), tmp_path).sweep() == 0


def test_unparseable_json_degrades_to_no_info_without_losing_the_tables(tmp_path):
    files = real_files()
    files[INFO_PATH] = b"<!DOCTYPE html> not json at all"
    art = make_reader(serve(files), tmp_path).fetch(SLUG)

    assert art.available is True
    assert art.info is None
    assert info_checks(art) == {}
    assert datanode_check(art)[0] == ChecklistState.PENDING.value


# ---- the published page -------------------------------------------------------------------


def test_published_url_is_built_from_the_assigned_id(tmp_path):
    reader = make_reader(serve(real_files()), tmp_path)
    assert reader.published_url(5423) == f"{SITE}/pathways/WP5423"


def test_published_url_does_not_depend_on_the_drafts_still_existing(tmp_path):
    """Publication *moves* the drafts, so this has to work when `fetch` finds nothing.

    The regression this guards: deriving the published link from `DraftArtifacts` looks natural
    and is exactly backwards — by the time it is the right link to show, every draft has been
    moved out from under it and `available` is False. A published review would then offer no
    link to the finished page at all, which is the one page a submitter wants most.
    """
    reader = make_reader(serve({}), tmp_path)  # nothing on the site at all
    art = reader.fetch(SLUG)

    assert art.available is False
    assert art.draft_url is None
    assert reader.published_url(5423) == f"{SITE}/pathways/WP5423"
