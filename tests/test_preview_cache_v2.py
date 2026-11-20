"""The render cache since renderer 2: kept sources, graph sidecar, render.json, redraws, backfill,
the diff keyed by id, and the routes that serve them."""
from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.github import FakeGitHubClient
from app.models import Review, ReviewStatus
from app.preview import PreviewService
from app.preview.backfill import refresh_stale_renders
from app.preview.diff import diff_nodes
from app.preview.metadata import parse_curation_metadata
from app.preview.render import RENDERER_VERSION
from tests.test_api import _authed_app, _login, _settings

FIXTURES = Path(__file__).parent / "fixtures" / "gpml"
NEW = (FIXTURES / "1_new_AhR_dioxin.gpml").read_bytes()
REV = (FIXTURES / "2_revision_AhR_dioxin.gpml").read_bytes()
ZOO21 = (FIXTURES / "zoo_2021.gpml").read_bytes()


def _svc(tmp_path) -> PreviewService:
    return PreviewService(cache_dir=tmp_path / "cache")


# -- what render_local writes ------------------------------------------------------------------

def test_render_local_keeps_source_graph_and_stamp(tmp_path):
    svc = _svc(tmp_path)
    state = svc.render_local(
        5, 5429, after_gpml=REV, before_gpml=NEW, submitter_note=" added AHRR ", head_sha="abc123"
    )
    assert state.status == "ready" and state.has_before and state.has_after
    d = tmp_path / "cache" / "5"
    assert (d / "after.gpml").read_bytes() == REV and (d / "before.gpml").read_bytes() == NEW
    assert svc.source_path(5, "after") == d / "after.gpml"

    stamp = svc.stamp(5)
    assert stamp["renderer"] == RENDERER_VERSION
    assert stamp["head_sha"] == "abc123" and stamp["target_wpid"] == 5429
    assert stamp["submitter_note"] == "added AHRR" and stamp["origin"] == "portal"
    assert stamp["dialect"] == "2013a" and stamp["counts"]["after"] > 0
    assert len(stamp["rev"]) == 12 and stamp["rendered_at"]
    assert not svc.needs_rerender(5)

    graph = svc.graph(5, "after")
    assert graph["version"] == 2 and graph["stats"]["nodes"] == 8
    # nodes.json keeps its shape and gains the id, in the graph's order (K2).
    nodes = svc.nodes(5, "after")
    assert [n["id"] for n in nodes] == [n["id"] for n in graph["nodes"]]
    assert svc.graph(5, "sideways") is None and svc.graph(6, "after") is None


def test_rev_changes_with_the_drawing_and_only_then(tmp_path):
    svc = _svc(tmp_path)
    svc.render_local(5, 5429, after_gpml=NEW)
    first = svc.stamp(5)["rev"]
    svc.render_local(5, 5429, after_gpml=NEW)
    assert svc.stamp(5)["rev"] == first
    svc.render_local(5, 5429, after_gpml=REV)
    assert svc.stamp(5)["rev"] != first


def test_rev_changes_when_only_the_graph_does(tmp_path):
    # A re-annotated Xref leaves the picture alone but changes the graph the viewer reads the
    # identifier and link from. The graph is served immutable under ?rev=, so the rev must move.
    svc = _svc(tmp_path)
    svc.render_local(5, 5429, after_gpml=NEW, before_gpml=NEW)
    svg = svc.svg_path(5, "after").read_bytes()
    graph, rev = svc.graph(5, "after"), svc.stamp(5)["rev"]
    reannotated = NEW.replace(b'ID="ENSG00000106546"', b'ID="ENSG00000999999"', 1)
    assert reannotated != NEW
    svc.render_local(5, 5429, after_gpml=reannotated)
    assert svc.svg_path(5, "after").read_bytes() == svg  # same picture...
    assert svc.graph(5, "after") != graph  # ...different graph...
    assert svc.stamp(5)["rev"] != rev  # ...so a different URL
    # An edited comment, likewise.
    rev = svc.stamp(5)["rev"]
    svc.render_local(5, 5429, after_gpml=reannotated.replace(
        b"Ligand-activated basic", b"Ligand-activated, basic", 1))
    assert svc.stamp(5)["rev"] != rev


def test_concurrent_renders_of_one_pull_request_stay_consistent(tmp_path):
    # The backfill thread and a request thread can render the same PR at once. Neither may fail
    # a side it drew, and the stamp's rev must describe the files left on disk.
    import threading

    from app.preview.service import PreviewService

    svc = _svc(tmp_path)
    errors: list[BaseException] = []
    states = []

    def worker(gpml: bytes) -> None:
        try:
            for _ in range(5):
                states.append(svc.render_local(40, 5429, after_gpml=gpml, before_gpml=NEW))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(g,)) for g in (NEW, REV, NEW, REV)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(s.status == "ready" and s.has_after for s in states)
    out = tmp_path / "cache" / "40"
    assert svc.stamp(40)["rev"] == PreviewService._rev(out)
    assert not list(out.glob(".*.tmp"))


def test_atomic_writes_to_one_path_from_two_threads_never_collide(tmp_path):
    import threading

    from app.preview.service import _write_atomic

    target = tmp_path / "x.svg"
    errors: list[BaseException] = []

    def worker(data: bytes) -> None:
        try:
            for _ in range(300):
                _write_atomic(target, data)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(b,)) for b in (b"a" * 4096, b"b" * 4096)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert target.read_bytes() in (b"a" * 4096, b"b" * 4096)
    assert [p.name for p in tmp_path.iterdir()] == ["x.svg"]


def test_a_2021_file_draws_instead_of_rendering_blank(tmp_path):
    svc = _svc(tmp_path)
    assert svc.render_local(8, 0, after_gpml=ZOO21).status == "ready"
    assert svc.stamp(8)["dialect"] == "2021"
    assert svc.graph(8, "after")["stats"]["nodes"] == 10
    meta = svc.metadata(8)
    assert meta["name"] == "Renderer feature zoo" and meta["organism"] == "Homo sapiens"
    assert {n["label"] for n in meta["data_nodes"]} >= {"AAA", "DDD"}


def test_content_that_cannot_be_placed_is_a_failed_render_not_a_blank_one(tmp_path):
    svc = _svc(tmp_path)
    svc.render_local(9, 0, after_gpml=NEW)
    assert svc.status(9) == "ready"
    # Every element has Graphics, none has coordinates this renderer understands.
    odd = (b'<Pathway xmlns="http://pathvisio.org/GPML/2099" Name="p">'
           b'<DataNode TextLabel="A"><Graphics posX="1" posY="2"/></DataNode></Pathway>')
    assert svc.render_local(9, 0, after_gpml=odd).status == "failed"
    assert svc.status(9) == "failed"
    # ...and the previous upload's picture is not left behind under the new file's name.
    assert svc.svg_path(9, "after") is None and svc.graph(9, "after") is None


def test_an_empty_pathway_is_still_ready(tmp_path):
    svc = _svc(tmp_path)
    empty = b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="p" Organism="x"/>'
    assert svc.render_local(10, 0, after_gpml=empty).status == "ready"


def test_a_revision_of_an_update_keeps_its_base(tmp_path):
    # The revise route passes no "before"; the update's base is still the comparison.
    svc = _svc(tmp_path)
    svc.render_local(11, 5429, after_gpml=NEW, before_gpml=NEW)
    svc.render_local(11, 5429, after_gpml=REV)
    assert svc.diff(11)["summary"]["added"] == 1  # AHRR
    # A new pathway (no WPID) never inherits one.
    svc.render_local(12, 0, after_gpml=NEW)
    svc.render_local(12, 0, after_gpml=REV)
    assert svc.diff(12) is None


def test_a_reused_pull_request_number_does_not_inherit_another_pathways_base(tmp_path):
    svc = _svc(tmp_path)
    svc.render_local(13, 1111, after_gpml=NEW, before_gpml=NEW)
    svc.render_local(13, 2222, after_gpml=REV)
    assert svc.diff(13) is None


def test_needs_rerender_and_redraw_from_source(tmp_path):
    svc = _svc(tmp_path)
    assert svc.needs_rerender(20)  # nothing on file
    svc.render_local(20, 5429, after_gpml=REV, before_gpml=NEW, submitter_note="n",
                     origin="adopted", head_sha="h1")
    d = tmp_path / "cache" / "20"
    stamp = json.loads((d / "render.json").read_text())
    stamp["renderer"] = 1
    (d / "render.json").write_text(json.dumps(stamp))
    (d / "after-graph.json").unlink()
    assert svc.needs_rerender(20)

    state = svc.rerender_from_source(20)
    assert state is not None and state.status == "ready" and state.has_before
    new = svc.stamp(20)
    assert new["renderer"] == RENDERER_VERSION
    assert (new["origin"], new["head_sha"], new["submitter_note"], new["target_wpid"]) == (
        "adopted", "h1", "n", 5429)
    assert svc.graph(20, "after") is not None
    assert svc.rerender_from_source(21) is None  # no source on disk


# -- diff v2 -----------------------------------------------------------------------------------

def test_diff_carries_pairs_and_ids(tmp_path):
    svc = _svc(tmp_path)
    svc.render_local(30, 5429, after_gpml=REV, before_gpml=NEW)
    d = svc.diff(30)
    assert d["version"] == 2
    assert d["ids"]["after"]["n-d008"] == {"change": "added"}
    assert d["ids"]["before"]["n-d001"] == "unchanged"
    assert {"before": "n-d001", "after": "n-d001", "change": "unchanged"} in d["pairs"]
    # The v1 index arrays are still there, index-aligned with nodes.json.
    assert len(d["after"]) == len(svc.nodes(30, "after"))


def test_diff_ids_carry_what_a_node_was():
    b = [{"id": "n-a", "graph_id": "a", "label": "AKT", "type": "GeneProduct",
          "database": "Ensembl", "identifier": "E1", "cx": 1.0, "cy": 1.0}]
    a = [dict(b[0], identifier="E2")]
    d = diff_nodes(b, a).as_dict()
    assert d["ids"]["after"]["n-a"]["change"] == "reannotated"
    assert d["ids"]["after"]["n-a"]["was"]["identifier"] == "E1"
    assert d["pairs"] == [{"before": "n-a", "after": "n-a", "change": "reannotated"}]
    # Nodes from a cache older than renderer 2 have no ids: no pairs, no crash.
    old = diff_nodes([{"label": "X"}], [{"label": "X"}]).as_dict()
    assert old["pairs"] == [] and old["ids"] == {"before": {}, "after": {}}


def test_metadata_reads_2021_spellings():
    meta = parse_curation_metadata(ZOO21)
    assert meta.name == "Renderer feature zoo"
    node = next(n for n in meta.data_nodes if n.label == "AAA")
    assert (node.type, node.database, node.identifier) == ("GeneProduct", "Ensembl",
                                                           "ENSG00000000001")


# -- get_blob on the fake ----------------------------------------------------------------------

def test_fake_get_blob_answers_seeded_and_existing_blobs():
    fake = FakeGitHubClient(
        existing_files={"o/r#pathways/WP1/WP1.gpml": "blob1"},
        existing_contents={"o/r#pathways/WP1/WP1.gpml": "<Pathway/>"},
    )
    assert fake.get_blob("o/r", "blob1") == b"<Pathway/>"
    fake.seed_blob("o/r", "old", "<Pathway Name='old'/>")
    assert fake.get_blob("o/r", "old") == b"<Pathway Name='old'/>"
    assert fake.get_blob("o/r", "nope") is None
    assert fake.get_blob("other/repo", "blob1") is None


def test_http_get_blob_asks_for_the_raw_media_type():
    import httpx

    from app.github.client import HttpGitHubClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["accept"] = request.headers.get("accept")
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, content=b"<Pathway/>")

    client = HttpGitHubClient(token="t", transport=httpx.MockTransport(handler))
    assert client.get_blob("o/r", "abc") == b"<Pathway/>"
    assert seen == {"path": "/repos/o/r/git/blobs/abc", "accept": "application/vnd.github.raw"}
    assert client.get_blob("o/r", "missing") is None


# -- backfill ----------------------------------------------------------------------------------

def _session_factory(tmp_path):
    from app.db import make_engine, make_session_factory
    from app.models import Base

    engine = make_engine(f"sqlite:///{tmp_path / 'b.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _add(sf, **kw):
    defaults = dict(submitter="alice", kind="update", status=ReviewStatus.OPEN)
    defaults.update(kw)
    with sf() as s:
        s.add(Review(**defaults))
        s.commit()


def test_backfill_redraws_from_disk_then_from_github_within_budget(tmp_path):
    sf = _session_factory(tmp_path)
    repo = "o/content"
    path = "pathways/WP5429/WP5429.gpml"
    fake = FakeGitHubClient()
    svc = _svc(tmp_path)

    # 1: rendered by renderer 2 but stamped older -> redrawn from disk, no GitHub.
    _add(sf, pr_number=1, wpid=5429, head_sha="h1", pathway_paths=[path])
    svc.render_local(1, 5429, after_gpml=REV, before_gpml=NEW, head_sha="h1")
    st = svc.stamp(1)
    st["renderer"] = 1
    (tmp_path / "cache" / "1" / "render.json").write_text(json.dumps(st))
    # 2: an update cached by renderer 1 (pictures only) -> after at head, before from base_blob.
    _add(sf, pr_number=2, wpid=5429, head_sha="h2", pathway_paths=[path], base_blob="baseblob",
         submitter_note="from the row")
    (tmp_path / "cache" / "2").mkdir(parents=True)
    (tmp_path / "cache" / "2" / "after.svg").write_bytes(b"<svg/>")
    fake.commit_contents[(repo, "h2", path)] = REV.decode()
    fake.seed_blob(repo, "baseblob", NEW.decode())
    # 3: a new pathway with nothing cached at all, path from the row.
    _add(sf, pr_number=3, wpid=None, kind="new", head_sha="h3",
         pathway_paths=["pathways/WP0001/WP0001.gpml"])
    fake.commit_contents[(repo, "h3", "pathways/WP0001/WP0001.gpml")] = NEW.decode()
    # 4: terminal -> never touched.
    _add(sf, pr_number=4, wpid=5429, head_sha="h4", status=ReviewStatus.PUBLISHED)
    # 5: current already -> skipped.
    _add(sf, pr_number=5, wpid=5429, head_sha="h5")
    svc.render_local(5, 5429, after_gpml=NEW)

    done = refresh_stale_renders(svc, sf, fake, content_repo=repo, default_branch="main",
                                 budget=1)
    # #1 from disk (free) and one GitHub redraw; the other waits for the next tick.
    assert done == 2
    assert not svc.needs_rerender(1)
    assert svc.stamp(4) is None
    fetched = [pr for pr in (2, 3) if not svc.needs_rerender(pr)]
    assert len(fetched) == 1

    done = refresh_stale_renders(svc, sf, fake, content_repo=repo, default_branch="main")
    assert done == 1
    assert not svc.needs_rerender(2) and not svc.needs_rerender(3)
    # The update was compared against the blob it was made on, not today's default branch.
    assert svc.diff(2)["summary"]["added"] == 1
    assert svc.stamp(2)["head_sha"] == "h2" and svc.stamp(2)["submitter_note"] == "from the row"
    assert svc.diff(3) is None and svc.stamp(3)["target_wpid"] is None
    # Nothing left to do, and nothing was posted anywhere.
    assert refresh_stale_renders(svc, sf, fake, content_repo=repo, default_branch="main") == 0
    assert fake.comments == {} and fake.issue_comments == {}


def test_backfill_backs_off_a_review_it_cannot_fetch_and_never_raises(tmp_path):
    sf = _session_factory(tmp_path)
    svc = _svc(tmp_path)
    _add(sf, pr_number=7, wpid=5429, head_sha="gone", pathway_paths=["pathways/x.gpml"])
    fake = FakeGitHubClient()
    assert refresh_stale_renders(svc, sf, fake, content_repo="o/r", default_branch="main") == 0
    # Backed off: a second tick does not ask again.
    fake.fail_on = {"get_file_content"}
    assert refresh_stale_renders(svc, sf, fake, content_repo="o/r", default_branch="main") == 0
    # A broken session factory is logged, not raised.
    def broken():
        raise RuntimeError("db down")

    assert refresh_stale_renders(svc, broken, fake, content_repo="o/r",
                                 default_branch="main") == 0
    # No GitHub client: only disk redraws happen.
    assert refresh_stale_renders(svc, sf, None, content_repo="o/r", default_branch="main") == 0


def test_backfill_backs_off_a_disk_redraw_that_does_not_take(tmp_path):
    # A redraw from disk that fails, or leaves the stamp old, must wait like a failed fetch
    # rather than run (and log) again on every tick.
    sf = _session_factory(tmp_path)
    svc = _svc(tmp_path)
    _add(sf, pr_number=8, wpid=5429, head_sha="h8")
    svc.render_local(8, 5429, after_gpml=REV)
    st = svc.stamp(8)
    st["renderer"] = 1
    (tmp_path / "cache" / "8" / "render.json").write_text(json.dumps(st))

    calls = []

    def broken(pr):
        calls.append(pr)
        raise RecursionError("deep")

    svc.rerender_from_source = broken
    for _ in range(3):
        assert refresh_stale_renders(svc, sf, None, content_repo="o/r",
                                     default_branch="main") == 0
    assert calls == [8]

    # A redraw that returns but leaves no current stamp counts as failed too.
    calls.clear()
    svc._backfill_backoff.clear()
    svc.rerender_from_source = lambda pr: calls.append(pr) or object()
    for _ in range(3):
        assert refresh_stale_renders(svc, sf, None, content_repo="o/r",
                                     default_branch="main") == 0
    assert calls == [8]


# -- routes ------------------------------------------------------------------------------------

def _submit(c, current, gpml: bytes) -> int:
    current["user"] = "bob"
    return c.post(
        "/api/submit", files={"file": ("u.gpml", io.BytesIO(gpml), "application/xml")}
    ).json()["pr_number"]


def _good(gpml: bytes) -> bytes:
    # The submit path validates the embedded Version; give the fixture one.
    return gpml.replace(b'Organism="Homo sapiens"',
                        b'Organism="Homo sapiens" Version="WP5636_r20260520113005"', 1)


def test_graph_route_and_rev_caching(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        pr = _submit(c, current, _good(NEW))
        stamp = app.state.preview.stamp(pr)
        rev = stamp["rev"]
        assert stamp["head_sha"]  # the render call sites pass the commit

        r = c.get(f"/previews/{pr}/after-graph.json?rev={rev}")
        assert r.status_code == 200 and r.json()["version"] == 2
        assert "immutable" in r.headers["cache-control"]
        assert c.get(f"/previews/{pr}/after-graph.json").headers["cache-control"] == "no-cache"
        assert c.get(f"/previews/{pr}/before-graph.json").status_code == 404
        assert c.get(f"/previews/{pr}/sideways-graph.json").status_code == 404
        assert c.get("/previews/999999/after-graph.json").status_code == 404

        svg = c.get(f"/previews/{pr}/after.svg?rev={rev}")
        assert svg.status_code == 200 and svg.content.startswith(b"<svg")
        assert svg.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "sandbox" in svg.headers["content-security-policy"]
        stale = c.get(f"/previews/{pr}/after.svg?rev=000000000000")
        assert stale.headers["cache-control"] == "no-cache"

        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
        assert f"/previews/{pr}/after.svg?rev={rev}" in page


def test_review_view_preview_keys(tmp_path):
    # K4: the keys the viewer reads, for an update (two sides and a diff) and a new pathway.
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    path = "pathways/WP5636/WP5636.gpml"
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#{path}": "oldsha"},
        existing_contents={f"{repo}#{path}": _good(NEW).decode()},
    )
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    captured = {}
    with TestClient(app) as c:
        current["user"] = "alice"
        upd = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(_good(REV)), "application/xml")},
        ).json()["pr_number"]
        new = _submit(c, current, _good(NEW))

        # ``_review_view`` is a closure inside build_app; the template context is where its
        # output can be seen without depending on how stream B's markup uses it.
        from app.main import templates

        original = templates.TemplateResponse

        def spy(request, name, context, *a, **kw):
            if "review" in context:
                captured[context["review"]["pr_number"]] = context["review"]["preview"]
            return original(request, name, context, *a, **kw)

        templates.TemplateResponse = spy
        try:
            _login(c, "curator")
            assert c.get(f"/dashboard/{upd}").status_code == 200
            assert c.get(f"/dashboard/{new}").status_code == 200
        finally:
            templates.TemplateResponse = original

    p = captured[upd]
    rev = app.state.preview.stamp(upd)["rev"]
    assert p["rev"] == rev and p["renderer"] == RENDERER_VERSION
    assert p["after_svg_url"] == f"/previews/{upd}/after.svg?rev={rev}"
    assert p["before_graph_url"] == f"/previews/{upd}/before-graph.json?rev={rev}"
    assert p["after_graph_url"] == f"/previews/{upd}/after-graph.json?rev={rev}"
    assert p["diff_url"] == f"/previews/{upd}/diff.json?rev={rev}"
    assert p["diff"]["counts"]["added"] == 1

    q = captured[new]
    assert q["before_graph_url"] is None and q["diff_url"] is None
    assert q["after_graph_url"].startswith(f"/previews/{new}/after-graph.json?rev=")
