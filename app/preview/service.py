"""Pathway preview service (issue #11) — the before/after render the dashboard shows.

The app draws both sides itself (``app/preview/render.py``) when the PR is created and caches the
SVGs on disk, so a curator sees the diagram immediately with no CI wait.

There is no second source. CI used to render with PinPath and upload the SVGs as a run artifact,
and this service downloaded them; that path was retired on 2026-07-27 along with the render step
itself (``mvp1/pr-preview.yml`` now converts to pvjson only — a PR comment cannot show an image
anyway). What CI produces is validation, not pictures, so nothing here talks to GitHub.

Cached per PR under ``<cache_dir>/<pr_number>/``: ``before.svg`` / ``after.svg``, the GPML each
was drawn from (``before.gpml`` / ``after.gpml``), the viewer's ``<side>-graph.json`` and the older
``<side>-nodes.json`` hotspots, the ``metadata.json`` sidecar the dashboard panel reads,
``render.json`` (which renderer drew it, and from what), and a ``render-failed`` marker when a
GPML could not be drawn (so the queue can say so instead of spinning on "generating" forever).

The source is kept so a renderer upgrade can redraw a live review from disk
(``rerender_from_source``) rather than fetching it from GitHub again; ``needs_rerender`` compares
``render.json`` against ``RENDERER_VERSION`` to find the ones that need it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Container, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.preview.diff import diff_metadata, diff_nodes
from app.preview.metadata import parse_curation_metadata
from app.preview.render import RENDERER_VERSION, RenderError, render_gpml_full

_SIDES = ("before", "after")
_FAILED_MARKER = "render-failed"
_STAMP = "render.json"


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step, so a reader never sees half a file.

    Matters once a drawing is served as immutable under its ``?rev=``: a browser that caught a
    half-written SVG would keep it for a year.

    Each write gets its own temporary name: two renders of one pull request at once (the
    backfill thread and a request thread) sharing one would replace each other's half-written
    file, and one ``os.replace`` would then find nothing to move.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: Every file served under a ``?rev=`` URL, and so cached as immutable: the rev must change
#: whenever any of them does. A graph can change with the picture unchanged (a re-annotated Xref,
#: an edited comment), and the viewer reads the identifier and link from the graph.
_REV_FILES = (
    "before.svg", "after.svg",
    "before-graph.json", "after-graph.json",
    "before-nodes.json", "after-nodes.json",
    "diff.json",
)

#: Renders of one pull request are serialised on one of these, picked by number. A fixed set, so
#: the locks never grow with the number of reviews; two numbers sharing a lock only wait.
_RENDER_LOCKS = tuple(threading.Lock() for _ in range(64))

#: How often the orphan sweep may run. It walks the cache directory and stats each entry, which
#: is cheap but pointless to repeat: nothing it collects appears faster than reviews terminalise.
_SWEEP_INTERVAL_SECONDS = 3600.0

#: How long a cache with no live review must sit untouched before the sweep takes it. This is a
#: race guard, not a retention policy: a render is written *before* the review row exists (see the
#: ordering comment in ``app.main``'s submit path), so for a moment every new submission looks
#: exactly like an orphan. An hour is far longer than that window and costs nothing.
_SWEEP_MIN_AGE_SECONDS = 3600.0


@dataclass(frozen=True)
class PreviewState:
    status: str  # 'pending' | 'ready' | 'failed'
    has_before: bool = False
    has_after: bool = False


class PreviewService:
    def __init__(
        self,
        *,
        cache_dir: str | Path,
        sweep_interval_seconds: float = _SWEEP_INTERVAL_SECONDS,
        sweep_min_age_seconds: float = _SWEEP_MIN_AGE_SECONDS,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._sweep_interval = sweep_interval_seconds
        self._sweep_min_age = sweep_min_age_seconds
        #: Monotonic stamp of the last sweep, so the throttle survives a clock adjustment. Held
        #: here rather than on ``CurationService`` because this object is app state and that one
        #: is rebuilt per request, which would reset the throttle on every single dashboard load.
        self._last_sweep: float | None = None

    # -- render at PR-creation time (issue #11, 1a) ----------------------------------------
    def render_local(
        self,
        pr_number: int,
        wpid: int,
        *,
        after_gpml: bytes,
        before_gpml: bytes | None = None,
        submitter_note: str | None = None,
        origin: str = "portal",
        head_sha: str | None = None,
    ) -> PreviewState:
        """Render the before/after SVGs in-process and cache them on disk.

        Best-effort: a side that can't be rendered is skipped, never raised — the preview must
        never sink the submission. The curation metadata parsed from the *after* GPML (data nodes,
        references, description, ontology tags) plus the submitter's note is cached alongside for
        the dashboard panel.

        ``before_gpml=None`` on an update that was rendered before (a revision) keeps comparing
        against the base it was first drawn with, which is on disk: the revise route does not
        fetch the base again, and dropping the comparison there lost the diff on every revision.

        One render per pull request at a time: the backfill thread and a request thread can both
        reach here for the same number, and interleaved writes would leave a stamp whose ``rev``
        describes files other than the ones on disk, which are then served as immutable.
        """
        with _RENDER_LOCKS[pr_number % len(_RENDER_LOCKS)]:
            return self._render_local(
                pr_number,
                wpid,
                after_gpml=after_gpml,
                before_gpml=before_gpml,
                submitter_note=submitter_note,
                origin=origin,
                head_sha=head_sha,
            )

    def _render_local(
        self,
        pr_number: int,
        wpid: int,
        *,
        after_gpml: bytes,
        before_gpml: bytes | None,
        submitter_note: str | None,
        origin: str,
        head_sha: str | None,
    ) -> PreviewState:
        out = self._cache_dir / str(pr_number)
        out.mkdir(parents=True, exist_ok=True)
        # 0 means "no id yet": a new pathway in pipeline mode.
        target_wpid = wpid or None
        if before_gpml is None and target_wpid is not None:
            before_gpml = self._cached_before(out, target_wpid)
        rendered: dict[str, bool] = {"before": False, "after": False}
        drawn_nodes: dict[str, list[dict]] = {}
        counts: dict[str, int | None] = {"before": None, "after": None}
        dialect: str | None = None
        for side, gpml in (("before", before_gpml), ("after", after_gpml)):
            if gpml is None:
                continue
            raw = gpml if isinstance(gpml, bytes) else str(gpml).encode("utf-8")
            try:
                # The source, so a later renderer can redraw this without asking GitHub.
                _write_atomic(out / f"{side}.gpml", raw)
            except OSError:
                pass
            try:
                result = render_gpml_full(raw)
            except RenderError:
                self._clear_side(out, side)
                continue
            counts[side] = result.element_count
            if result.element_count == 0 and result.declared > 0:
                # Content we could not place is a failed render, not an empty one: a blank
                # "ready" frame tells the curator there is nothing to review.
                self._clear_side(out, side)
                continue
            if side == "after" or dialect is None:
                dialect = result.dialect
            drawn_nodes[side] = [h.as_dict() for h in result.hotspots]
            try:
                _write_atomic(out / f"{side}.svg", result.svg)
            except OSError:
                continue
            # The clickable-node overlay (issue #14) and the viewer's graph. Written beside the
            # drawing they belong to, so a side that failed to render has nothing stale left
            # pointing at nothing.
            try:
                _write_atomic(
                    out / f"{side}-nodes.json", json.dumps(drawn_nodes[side]).encode("utf-8")
                )
                _write_atomic(
                    out / f"{side}-graph.json", json.dumps(result.graph).encode("utf-8")
                )
            except OSError:
                pass  # the overlay is an enhancement; never fail a render on it
            rendered[side] = True
        self._write_diff(out, drawn_nodes, before_gpml=before_gpml, after_gpml=after_gpml)
        self._write_metadata(out, after_gpml, submitter_note)
        drawn = rendered["before"] or rendered["after"]
        # The one moment the renderer's verdict is in hand for free. ``app.quality`` will not run
        # the renderer itself — that would break the offline-and-cheap rule its rules work under —
        # so this is the only caller that can answer the "is it drawable" rule at all.
        self._write_quality(
            out,
            after_gpml,
            before_gpml=before_gpml,
            drawable=drawn,
            origin=origin,
            target_wpid=target_wpid,
        )
        # Record the outcome so a re-upload that now renders clears a previous failure, and a
        # GPML we cannot draw reads as 'failed' rather than 'pending' forever.
        marker = out / _FAILED_MARKER
        if drawn:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text("", encoding="utf-8")
        self._write_stamp(
            out,
            {
                "renderer": RENDERER_VERSION,
                # Changes whenever anything served under it does, so a URL carrying it can be
                # cached for good; the page always asks for the current one.
                "rev": self._rev(out),
                "head_sha": head_sha,
                "origin": origin,
                "target_wpid": target_wpid,
                "submitter_note": (submitter_note or "").strip(),
                "dialect": dialect,
                "counts": counts,
                "rendered_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
        return PreviewState(
            "ready" if drawn else "failed",
            has_before=rendered["before"],
            has_after=rendered["after"],
        )

    @staticmethod
    def _rev(out: Path) -> str:
        """A short hash of every file served under ``?rev=``, read back from disk.

        Read back rather than collected while writing, so it describes what a browser will
        actually get, including a side cleared by this render. Each file is framed by its name
        and length, so moving bytes from one file to another changes it too.
        """
        h = hashlib.sha256(b"renderer %d\n" % RENDERER_VERSION)
        for name in _REV_FILES:
            try:
                data = (out / name).read_bytes()
            except OSError:
                h.update(b"%s -\n" % name.encode())
                continue
            h.update(b"%s %d\n" % (name.encode(), len(data)))
            h.update(data)
        return h.hexdigest()[:12]

    @staticmethod
    def _clear_side(out: Path, side: str) -> None:
        """Remove a side's drawing and sidecars, so a re-upload that no longer renders does not
        keep showing the previous file's picture under the new file's name."""
        for name in (f"{side}.svg", f"{side}-nodes.json", f"{side}-graph.json"):
            try:
                (out / name).unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _cached_before(out: Path, target_wpid: int) -> bytes | None:
        """The base this update was first compared against, when the cache is this pathway's.

        Keyed on the WPID in the stamp: the cache directory is named by pull request number
        alone, and a number reused against another repository must not inherit a stranger's
        base.
        """
        try:
            stamp = json.loads((out / _STAMP).read_text(encoding="utf-8"))
            if not isinstance(stamp, dict) or stamp.get("target_wpid") != target_wpid:
                return None
            return (out / "before.gpml").read_bytes()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_stamp(out: Path, data: dict) -> None:
        try:
            _write_atomic(out / _STAMP, json.dumps(data).encode("utf-8"))
        except OSError:
            pass  # without a stamp the backfill simply redraws it later

    # -- what drew this, and redrawing it ----------------------------------------------------
    def stamp(self, pr_number: int) -> dict | None:
        """``render.json``: renderer version, ``rev``, head sha, origin, WPID, note, dialect,
        counts and time of the cached render. None for a cache older than renderer 2."""
        path = self._cache_dir / str(pr_number) / _STAMP
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def needs_rerender(self, pr_number: int) -> bool:
        """True when nothing current is cached: no stamp, or one from an older renderer."""
        stamp = self.stamp(pr_number)
        if stamp is None:
            return True
        try:
            return int(stamp.get("renderer") or 0) < RENDERER_VERSION
        except (TypeError, ValueError):
            return True

    def source_path(self, pr_number: int, side: str) -> Path | None:
        """The GPML a side was drawn from, when it was kept (renders since renderer 2)."""
        if side not in _SIDES:
            return None
        path = self._cache_dir / str(pr_number) / f"{side}.gpml"
        return path if path.is_file() else None

    def rerender_from_source(self, pr_number: int) -> PreviewState | None:
        """Redraw from the GPML on disk, keeping what the stamp recorded. None without a source."""
        after = self.source_path(pr_number, "after")
        if after is None:
            return None
        stamp = self.stamp(pr_number) or {}
        note = stamp.get("submitter_note")
        if note is None:
            note = (self.metadata(pr_number) or {}).get("submitter_note")
        before = self.source_path(pr_number, "before")
        try:
            after_gpml = after.read_bytes()
            before_gpml = before.read_bytes() if before is not None else None
        except OSError:
            return None
        wpid = stamp.get("target_wpid")
        return self.render_local(
            pr_number,
            wpid if isinstance(wpid, int) else 0,
            after_gpml=after_gpml,
            before_gpml=before_gpml,
            submitter_note=note,
            origin=str(stamp.get("origin") or "portal"),
            head_sha=stamp.get("head_sha"),
        )

    def graph_path(self, pr_number: int, side: str) -> Path | None:
        """Where ``<side>-graph.json`` is, or None when that side has none on file."""
        if side not in _SIDES:
            return None
        path = self._cache_dir / str(pr_number) / f"{side}-graph.json"
        return path if path.is_file() else None

    def graph(self, pr_number: int, side: str) -> dict | None:
        """The viewer's graph for one side (``<side>-graph.json``), or None when not on file."""
        path = self.graph_path(pr_number, side)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _local_side_exists(self, pr_number: int) -> bool:
        d = self._cache_dir / str(pr_number)
        return any((d / f"{s}.svg").is_file() for s in _SIDES)

    # -- before/after diff sidecar (issue #24) ---------------------------------------------
    @staticmethod
    def _write_diff(
        out: Path,
        drawn: dict[str, list[dict]],
        *,
        before_gpml: bytes | None = None,
        after_gpml: bytes | None = None,
    ) -> None:
        """Cache what changed between the two sides, or clear a stale one (best-effort).

        Only an update has two sides. A new pathway has nothing to compare against, and a
        re-upload that drops a side must not leave the previous comparison on disk claiming to
        describe the render beside it.
        """
        path = out / "diff.json"
        if "before" not in drawn or "after" not in drawn:
            path.unlink(missing_ok=True)
            return
        try:
            data = diff_nodes(drawn["before"], drawn["after"]).as_dict()
            if before_gpml and after_gpml:
                # Title, description, references and tags: what a curator reads in the panels
                # below, and could not tell had changed without comparing them by eye.
                data["meta"] = diff_metadata(
                    parse_curation_metadata(before_gpml), parse_curation_metadata(after_gpml)
                )
            _write_atomic(path, json.dumps(data).encode("utf-8"))
        except OSError:
            pass  # the summary is an enhancement; never fail a render on it

    def diff(self, pr_number: int) -> dict | None:
        """What changed between the two sides (issue #24), or None — a new pathway, a side that
        would not render, or a cache written before this existed."""
        path = self._cache_dir / str(pr_number) / "diff.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # -- curation metadata sidecar (dashboard panel) --------------------------------------
    @staticmethod
    def _write_metadata(out: Path, after_gpml: bytes, submitter_note: str | None) -> None:
        """Cache the parsed curation metadata + submitter note next to the SVGs (best-effort)."""
        try:
            data = parse_curation_metadata(after_gpml).as_dict()
            data["submitter_note"] = (submitter_note or "").strip()
            _write_atomic(out / "metadata.json", json.dumps(data).encode("utf-8"))
        except (OSError, ValueError):
            pass  # the panel is cosmetic; never fail the write path on it

    def metadata(self, pr_number: int) -> dict | None:
        """The cached curation metadata for a PR (data nodes, references, description, ontology
        tags, submitter note), or None if it was never rendered."""
        path = self._cache_dir / str(pr_number) / "metadata.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- quality report sidecar (app.quality) ----------------------------------------------
    @staticmethod
    def _write_quality(
        out: Path,
        after_gpml: bytes,
        *,
        before_gpml: bytes | None,
        drawable: bool,
        origin: str = "portal",
        target_wpid: int | None = None,
    ) -> None:
        """Cache the graded quality report next to the render (best-effort).

        Cached rather than persisted on the review row, deliberately. Every finding is a pure
        function of (GPML, metadata, base metadata), and the part that has to survive is already
        on ``Review.checklist`` — the states and the notes a curator worked from. A second stored
        copy would be a second thing to keep in step with the first, and staleness on that row is
        not hypothetical: ``set_checklist_item``'s no-op guard exists precisely because writing
        re-derived values back re-posted the mirror comment on every page load.

        The cost is that a terminal review loses the panel when its cache is swept (issue #18).
        That is the right trade: the checklist is the record, and this is the aid.
        """
        from app.quality import inspect_gpml

        try:
            before = parse_curation_metadata(before_gpml) if before_gpml else None
            report = inspect_gpml(
                after_gpml,
                before=before,
                kind="update" if before_gpml else "new",
                drawable=drawable,
                origin=origin,
                target_wpid=target_wpid,
            )
            _write_atomic(out / "quality.json", json.dumps(report.as_dict()).encode("utf-8"))
        except (OSError, ValueError):
            pass  # the panel is an aid; never fail a render on it

    def quality(self, pr_number: int):
        """The cached quality report for a PR, or None.

        None covers three ordinary cases and is not an error in any of them: nothing rendered yet,
        a cache swept at a terminal transition, and a render cached before this existed — the same
        forgiveness ``diff`` already extends to a pre-issue-#24 sidecar.
        """
        from app.quality import QualityReport

        path = self._cache_dir / str(pr_number) / "quality.json"
        if not path.is_file():
            return None
        try:
            return QualityReport.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    # -- queue render ----------------------------------------------------------------------
    def status(self, pr_number: int) -> str:
        """'ready' once a side is on disk, 'failed' if the GPML could not be drawn, else
        'pending' (nothing rendered yet, or the cache was cleared under a live review)."""
        if self._local_side_exists(pr_number):
            return "ready"
        if (self._cache_dir / str(pr_number) / _FAILED_MARKER).is_file():
            return "failed"
        return "pending"

    def discard(self, pr_number: int) -> bool:
        """Delete a pull request's cached render (issue #18). True if there was one.

        Called when a review reaches a terminal state: nothing renders it again, and the cache
        lives on the GlusterFS volume shared with every other service on the cluster, so keeping
        every pathway ever submitted is a slow leak into someone else's disk.

        Best-effort like every other write here — a cache that will not delete must never be the
        reason a curator cannot close a review.
        """
        target = self._cache_dir / str(pr_number)
        if not target.is_dir():
            return False
        try:
            shutil.rmtree(target)
        except OSError:
            return False
        return True

    def cached_pull_requests(self) -> Iterator[int]:
        """Every pull request number with a cached render on disk.

        Anything not named after a pull request is skipped rather than reported, so a file or a
        directory some other hand put in the cache is invisible to the sweep below and cannot be
        deleted by it.
        """
        try:
            entries = sorted(self._cache_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                yield int(entry.name)
            except ValueError:
                continue

    def sweep(self, keep: Container[int], *, force: bool = False) -> int:
        """Delete cached renders for pull requests outside ``keep``. Returns how many went.

        The backstop half of issue #18. Freeing at each terminal transition is the primary path,
        but it is a list of call sites and a list of call sites drifts — this one is keyed off the
        state itself, so a transition that forgets to free costs an hour of disk rather than
        leaking forever. It also collects what no transition can: caches for reviews terminalised
        before any of this existed, and orphans from a submission that died between rendering and
        registering.

        ``keep`` is the set of live pull requests, and everything else is fair game, so the caller
        must pass a *complete* set — a partial one deletes renders still in use. Only the age
        guard makes that safe for the youngest entries; see ``_SWEEP_MIN_AGE_SECONDS``.

        Throttled to ``sweep_interval_seconds`` because the natural caller is the dashboard load.
        """
        now = time.monotonic()
        if (
            not force
            and self._last_sweep is not None
            and now - self._last_sweep < self._sweep_interval
        ):
            return 0
        self._last_sweep = now
        cutoff = time.time() - self._sweep_min_age
        freed = 0
        for pr_number in self.cached_pull_requests():
            if pr_number in keep:
                continue
            try:
                if (self._cache_dir / str(pr_number)).stat().st_mtime > cutoff:
                    continue  # too young to be sure it is not a submission still being registered
            except OSError:
                continue
            if self.discard(pr_number):
                freed += 1
        return freed

    def nodes(self, pr_number: int, side: str) -> list[dict] | None:
        """Clickable data-node hotspots for one side (issue #14), or None when there are none on
        file — a side that was never rendered, or a cache written before this existed."""
        if side not in _SIDES:
            return None
        path = self._cache_dir / str(pr_number) / f"{side}-nodes.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def svg_path(self, pr_number: int, side: str) -> Path | None:
        """Cached path to the before/after SVG, or None when that side wasn't rendered (a new
        pathway has no "before") — the endpoint then serves a placeholder."""
        if side not in _SIDES:
            return None
        path = self._cache_dir / str(pr_number) / f"{side}.svg"
        return path if path.is_file() else None
