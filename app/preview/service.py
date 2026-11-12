"""Pathway preview service (issue #11) — the before/after render the dashboard shows.

The app draws both sides itself (``app/preview/render.py``) when the PR is created and caches the
SVGs on disk, so a curator sees the diagram immediately with no CI wait.

There is no second source. CI used to render with PinPath and upload the SVGs as a run artifact,
and this service downloaded them; that path was retired on 2026-07-27 along with the render step
itself (``mvp1/pr-preview.yml`` now converts to pvjson only — a PR comment cannot show an image
anyway). What CI produces is validation, not pictures, so nothing here talks to GitHub.

Cached per PR under ``<cache_dir>/<pr_number>/``: ``before.svg`` / ``after.svg``, the
``metadata.json`` sidecar the dashboard panel reads, and a ``render-failed`` marker when a GPML
could not be drawn (so the queue can say so instead of spinning on "generating" forever).
"""
from __future__ import annotations

import json
import shutil
import time
from collections.abc import Container, Iterator
from dataclasses import dataclass
from pathlib import Path

from app.preview.diff import diff_nodes
from app.preview.metadata import parse_curation_metadata
from app.preview.render import RenderError, render_gpml_with_nodes

_SIDES = ("before", "after")
_FAILED_MARKER = "render-failed"

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
    ) -> PreviewState:
        """Render the before/after SVGs in-process and cache them on disk.

        Best-effort: a side that can't be rendered is skipped, never raised — the preview must
        never sink the submission. The curation metadata parsed from the *after* GPML (data nodes,
        references, description, ontology tags) plus the submitter's note is cached alongside for
        the dashboard panel.
        """
        out = self._cache_dir / str(pr_number)
        out.mkdir(parents=True, exist_ok=True)
        rendered: dict[str, bool] = {"before": False, "after": False}
        drawn_nodes: dict[str, list[dict]] = {}
        for side, gpml in (("before", before_gpml), ("after", after_gpml)):
            if gpml is None:
                continue
            try:
                svg, hotspots = render_gpml_with_nodes(gpml)
            except RenderError:
                continue
            drawn_nodes[side] = [h.as_dict() for h in hotspots]
            (out / f"{side}.svg").write_bytes(svg)
            # The clickable-node overlay (issue #14). Written beside the drawing it belongs to,
            # so a side that failed to render has no stale hotspots left pointing at nothing.
            try:
                (out / f"{side}-nodes.json").write_text(
                    json.dumps(drawn_nodes[side]), encoding="utf-8"
                )
            except OSError:
                pass  # the overlay is an enhancement; never fail a render on it
            rendered[side] = True
        self._write_diff(out, drawn_nodes)
        self._write_metadata(out, after_gpml, submitter_note)
        drawn = rendered["before"] or rendered["after"]
        # The one moment the renderer's verdict is in hand for free. ``app.quality`` will not run
        # the renderer itself — that would break the offline-and-cheap rule its rules work under —
        # so this is the only caller that can answer the "is it drawable" rule at all.
        self._write_quality(
            out, after_gpml, before_gpml=before_gpml, drawable=drawn, origin=origin
        )
        # Record the outcome so a re-upload that now renders clears a previous failure, and a
        # GPML we cannot draw reads as 'failed' rather than 'pending' forever.
        marker = out / _FAILED_MARKER
        if drawn:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text("", encoding="utf-8")
        return PreviewState(
            "ready" if drawn else "failed",
            has_before=rendered["before"],
            has_after=rendered["after"],
        )

    def _local_side_exists(self, pr_number: int) -> bool:
        d = self._cache_dir / str(pr_number)
        return any((d / f"{s}.svg").is_file() for s in _SIDES)

    # -- before/after diff sidecar (issue #24) ---------------------------------------------
    @staticmethod
    def _write_diff(out: Path, drawn: dict[str, list[dict]]) -> None:
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
            path.write_text(json.dumps(data), encoding="utf-8")
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
            (out / "metadata.json").write_text(json.dumps(data), encoding="utf-8")
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
            )
            (out / "quality.json").write_text(json.dumps(report.as_dict()), encoding="utf-8")
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
