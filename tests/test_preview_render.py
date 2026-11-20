"""In-app GPML -> SVG preview (issue #11): the renderer and the cache it serves from."""
from __future__ import annotations

import pytest

from app.github import FakeGitHubClient
from app.preview import PreviewService
from app.preview.render import RenderError, render_gpml, render_gpml_with_nodes

_GPML = """<?xml version="1.0" encoding="UTF-8"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Demo" Organism="Homo sapiens">
  <Graphics BoardWidth="400.0" BoardHeight="300.0" />
  <DataNode TextLabel="TP53" Type="GeneProduct">
    <Graphics CenterX="100.0" CenterY="100.0" Width="80.0" Height="24.0" />
    <Xref Database="Ensembl" ID="ENSG00000141510" />
  </DataNode>
  <DataNode TextLabel="MDM2" Type="GeneProduct">
    <Graphics CenterX="300.0" CenterY="100.0" Width="80.0" Height="24.0" />
    <Xref Database="Ensembl" ID="ENSG00000135679" />
  </DataNode>
  <Interaction>
    <Graphics>
      <Point X="140.0" Y="100.0" />
      <Point X="260.0" Y="100.0" ArrowHead="mim-stimulation" />
    </Graphics>
  </Interaction>
</Pathway>
"""


def test_render_produces_valid_svg_with_nodes_edges_labels():
    svg = render_gpml(_GPML.encode()).decode()
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert 'viewBox="0.0 0.0 400.0 300.0"' in svg  # honours the declared board
    assert svg.count("<rect") == 3  # board background + 2 data nodes
    assert "TP53" in svg and "MDM2" in svg  # labels
    # The interaction is a path with an arrowhead marker (renderer 2 draws paths, not polylines).
    assert 'class="wp-edge__line"' in svg and 'marker-end="url(#wp-m-mim-stimulation)"' in svg


def test_render_infers_viewbox_when_board_missing():
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="x" Organism="Homo sapiens">'
        '<DataNode TextLabel="A"><Graphics CenterX="50" CenterY="50" Width="20" Height="20"/>'
        "</DataNode></Pathway>"
    )
    svg = render_gpml(gpml).decode()
    # bbox is 40..60 in both axes, padded by 15 → origin 25,25 size 50x50
    assert 'viewBox="25.0 25.0 50.0 50.0"' in svg


def test_render_escapes_label_text():
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="x" Organism="Homo sapiens">'
        '<DataNode TextLabel="A &amp; B &lt;x&gt;"><Graphics CenterX="10" CenterY="10" '
        'Width="10" Height="10"/></DataNode></Pathway>'
    )
    svg = render_gpml(gpml).decode()
    assert "A &amp; B &lt;x&gt;" in svg  # not raw & or <


@pytest.mark.parametrize("bad", [b"", b"<not-gpml/>", b"garbage", b"<Pathway>unterminated"])
def test_render_raises_on_non_gpml(bad):
    with pytest.raises(RenderError):
        render_gpml(bad)


def _svc(tmp_path):
    return PreviewService(cache_dir=tmp_path / "cache")


def test_render_local_serves_both_sides(tmp_path):
    svc = _svc(tmp_path)
    state = svc.render_local(42, 554, after_gpml=_GPML.encode(), before_gpml=_GPML.encode())
    assert state.status == "ready" and state.has_before and state.has_after
    assert svc.status(42) == "ready"
    assert svc.svg_path(42, "after") is not None
    assert svc.svg_path(42, "before") is not None
    assert b"<svg" in (tmp_path / "cache" / "42" / "after.svg").read_bytes()


def test_render_local_new_pathway_has_no_before(tmp_path):
    svc = _svc(tmp_path)
    state = svc.render_local(7, 5640, after_gpml=_GPML.encode())  # no before_gpml
    assert state.status == "ready" and state.has_after and not state.has_before
    assert svc.svg_path(7, "after") is not None
    assert svc.svg_path(7, "before") is None  # absent side → placeholder


def test_render_local_bad_gpml_fails_gracefully(tmp_path):
    svc = _svc(tmp_path)
    state = svc.render_local(9, 1, after_gpml=b"not gpml at all")
    assert state.status == "failed" and not state.has_after
    # ...and the queue says so, instead of spinning on "generating" forever.
    assert svc.status(9) == "failed"


def test_re_render_clears_a_previous_failure(tmp_path):
    svc = _svc(tmp_path)
    svc.render_local(9, 1, after_gpml=b"not gpml at all")
    assert svc.status(9) == "failed"
    svc.render_local(9, 1, after_gpml=_GPML.encode())  # revised upload draws fine
    assert svc.status(9) == "ready"


def test_status_pending_before_anything_is_rendered(tmp_path):
    assert _svc(tmp_path).status(123) == "pending"


def test_render_local_caches_metadata_and_note(tmp_path):
    svc = _svc(tmp_path)
    svc.render_local(
        42, 554, after_gpml=_GPML.encode(), submitter_note="  please check the arrow  "
    )
    meta = svc.metadata(42)
    assert meta is not None
    assert [n["label"] for n in meta["data_nodes"]] == ["TP53", "MDM2"]
    assert meta["submitter_note"] == "please check the arrow"  # trimmed
    assert svc.metadata(999) is None  # nothing rendered for this PR


def test_get_file_content_roundtrip():
    fake = FakeGitHubClient(existing_contents={"owner/repo#pathways/WP554/WP554.gpml": _GPML})
    got = fake.get_file_content("owner/repo", "main", "pathways/WP554/WP554.gpml")
    assert got is not None and b"<Pathway" in got
    assert fake.get_file_content("owner/repo", "main", "pathways/WP999/WP999.gpml") is None


def test_hotspots_carry_geometry_and_properties():
    # Issue #14: the overlay is positioned in percentages of the viewBox, because the drawing is
    # served into an <img> whose own markup is inert and whose size the viewport changes.
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="P" Organism="Homo sapiens">'
        '<DataNode TextLabel="INS" Type="GeneProduct">'
        '<Comment>a note</Comment>'
        '<Graphics CenterX="100" CenterY="50" Width="80" Height="20"/>'
        '<Xref Database="Ensembl" ID="ENSG00000254647"/>'
        "</DataNode>"
        '<Graphics BoardWidth="400" BoardHeight="200"/>'
        "</Pathway>"
    )
    svg, hotspots = render_gpml_with_nodes(gpml)
    assert b"<svg" in svg
    assert len(hotspots) == 1
    h = hotspots[0]
    # x = 100 - 80/2 = 60 of 400 = 15%; y = 50 - 20/2 = 40 of 200 = 20%
    assert h.left == pytest.approx(15.0)
    assert h.top == pytest.approx(20.0)
    assert h.width == pytest.approx(20.0)
    assert h.height == pytest.approx(10.0)
    assert h.label == "INS"
    assert h.type == "GeneProduct"
    assert h.database == "Ensembl"
    assert h.identifier == "ENSG00000254647"
    assert h.url == "https://identifiers.org/ensembl:ENSG00000254647"
    assert h.comment == "a note"


def test_unannotated_node_still_gets_a_hotspot():
    # The node with no identifier is the one a curator most wants to click.
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="P">'
        '<DataNode TextLabel="IRS1" Type="GeneProduct">'
        '<Graphics CenterX="50" CenterY="50" Width="20" Height="10"/>'
        "</DataNode>"
        '<Graphics BoardWidth="100" BoardHeight="100"/>'
        "</Pathway>"
    )
    _, hotspots = render_gpml_with_nodes(gpml)
    assert len(hotspots) == 1
    assert hotspots[0].identifier == ""
    assert hotspots[0].url is None


def test_geometry_and_properties_cannot_drift_apart():
    # A DataNode without <Graphics> is skipped by the renderer but kept by the metadata parser,
    # so joining the two by list index would attach the wrong identifier to the wrong box. Both
    # come from the same element in one pass; this pins that.
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="P">'
        '<DataNode TextLabel="NoGraphics" Type="GeneProduct">'
        '<Xref Database="Ensembl" ID="ENSG-MISSING"/>'
        "</DataNode>"
        '<DataNode TextLabel="Drawn" Type="Metabolite">'
        '<Graphics CenterX="50" CenterY="50" Width="20" Height="10"/>'
        '<Xref Database="ChEBI" ID="CHEBI:15377"/>'
        "</DataNode>"
        '<Graphics BoardWidth="100" BoardHeight="100"/>'
        "</Pathway>"
    )
    _, hotspots = render_gpml_with_nodes(gpml)
    assert [h.label for h in hotspots] == ["Drawn"]
    assert hotspots[0].identifier == "CHEBI:15377"


def test_nodes_outside_a_declared_board_are_drawn():
    # GPML can place a node beyond its own BoardWidth/Height. Renderer 1 cropped to the board and
    # dropped the node's hotspot; renderer 2 grows the viewBox to take it in, because a node the
    # curator cannot see is a node nobody reviews.
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="P">'
        '<DataNode TextLabel="OffBoard" Type="GeneProduct">'
        '<Graphics CenterX="900" CenterY="900" Width="20" Height="10"/>'
        "</DataNode>"
        '<Graphics BoardWidth="100" BoardHeight="100"/>'
        "</Pathway>"
    )
    svg, hotspots = render_gpml_with_nodes(gpml)
    # board 0..100 joined with the node 890..910 x 895..905 padded by 15
    assert b'viewBox="0.0 0.0 925.0 920.0"' in svg
    assert [h.label for h in hotspots] == ["OffBoard"]
    h = hotspots[0]
    assert 0 < h.left < 100 and 0 < h.top < 100


def test_render_gpml_still_returns_bytes():
    # The one-argument form is what every existing caller uses.
    out = render_gpml('<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="P"/>')
    assert isinstance(out, bytes) and out.startswith(b"<svg")


def test_discard_frees_a_pull_requests_cache(tmp_path):
    # Issue #18: nothing ever deleted a cached render, so the GlusterFS volume the whole cluster
    # shares grew by one directory per submission, forever.
    svc = PreviewService(cache_dir=tmp_path / "cache")
    svc.render_local(11, 5637, after_gpml=_GPML.encode())
    assert svc.status(11) == "ready"
    assert svc.svg_path(11, "after") is not None

    assert svc.discard(11) is True
    assert svc.status(11) == "pending"
    assert svc.svg_path(11, "after") is None
    assert svc.nodes(11, "after") is None
    # Idempotent: a second terminal transition, or a webhook redelivery, must not raise.
    assert svc.discard(11) is False


def test_discard_leaves_other_pull_requests_alone(tmp_path):
    svc = PreviewService(cache_dir=tmp_path / "cache")
    svc.render_local(11, 5637, after_gpml=_GPML.encode())
    svc.render_local(12, 5638, after_gpml=_GPML.encode())
    svc.discard(11)
    assert svc.status(12) == "ready"


def _sweeper(tmp_path, *, min_age=0.0, interval=0.0) -> PreviewService:
    """A PreviewService whose sweep is due immediately and takes everything collectable.

    Both guards are timing, so a test that left them at their real values would have to wait an
    hour to observe anything. Each is pinned by its own test below instead.
    """
    return PreviewService(
        cache_dir=tmp_path / "cache",
        sweep_interval_seconds=interval,
        sweep_min_age_seconds=min_age,
    )


def test_sweep_collects_caches_no_live_review_claims(tmp_path):
    # Issue #18's backstop. #11 is a live review, #12 terminalised (or never had a row at all --
    # a submission that died between rendering and registering looks identical from here).
    svc = _sweeper(tmp_path)
    svc.render_local(11, 5637, after_gpml=_GPML.encode())
    svc.render_local(12, 5638, after_gpml=_GPML.encode())

    assert svc.sweep({11}) == 1
    assert svc.status(11) == "ready"
    assert svc.status(12) == "pending"


def test_sweep_spares_a_cache_too_young_to_judge(tmp_path):
    # The race the age guard exists for: a render is written before the review row, so for a
    # moment every new submission is indistinguishable from an orphan. Sweeping it there would
    # delete the preview of a pull request that is about to open.
    svc = _sweeper(tmp_path, min_age=3600.0)
    svc.render_local(11, 5637, after_gpml=_GPML.encode())

    assert svc.sweep(set()) == 0
    assert svc.status(11) == "ready"


def test_sweep_is_throttled(tmp_path):
    # The caller is the dashboard load, so without this it walks and stats the cache on every
    # page view -- and collects nothing, because nothing terminalises that fast.
    svc = _sweeper(tmp_path, interval=3600.0)
    svc.render_local(11, 5637, after_gpml=_GPML.encode())
    svc.render_local(12, 5638, after_gpml=_GPML.encode())

    assert svc.sweep({11}) == 1  # first call runs
    svc.render_local(13, 5639, after_gpml=_GPML.encode())
    assert svc.sweep({11}) == 0  # second is inside the interval
    assert svc.status(13) == "ready"
    assert svc.sweep({11}, force=True) == 1  # ...and force overrides it
    assert svc.status(13) == "pending"


def test_sweep_leaves_directories_it_did_not_write(tmp_path):
    # The cache lives on the volume every service on the cluster shares. A sweep that deleted
    # whatever it found there would be a much worse bug than the leak it is fixing.
    svc = _sweeper(tmp_path)
    svc.render_local(11, 5637, after_gpml=_GPML.encode())
    stranger = tmp_path / "cache" / "not-a-pull-request"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("mine", encoding="utf-8")
    loose = tmp_path / "cache" / "77"  # a file, not a directory
    loose.write_text("", encoding="utf-8")

    assert svc.sweep(set()) == 1
    assert list(svc.cached_pull_requests()) == []
    assert (stranger / "keep.txt").is_file()
    assert loose.is_file()


def test_sweep_on_an_absent_cache_directory_is_not_an_error(tmp_path):
    # A deployment that has never rendered anything still loads the dashboard.
    assert _sweeper(tmp_path).sweep({1}) == 0
