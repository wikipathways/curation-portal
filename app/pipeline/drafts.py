"""Read the derived artifacts the target repo's own PR pipeline publishes.

`wikipathways/sandbox-wp-db`'s workflow 1 does not leave its output on the pull request. It
pushes a rendered draft into the *sister* site repo `wikipathways/sandbox-wp.gh.io`, filed under
a slug built from the PR number — `WP0__PR54` for a new pathway, `WP<id>__PR54` for an edit —
and links the resulting draft page from the PR description. Those files are the only rendered
view of a submission the pipeline produces, so the dashboard reads them to fill its preview slot
and to pre-fill the curation checklist from what the pipeline itself extracted.

Read anonymously over raw.githubusercontent.com, deliberately, and *not* through
`app.github.client`: that abstraction is about the target repo and its two authenticated
identities, and the GitHub App is installed on sandbox-wp-db only, so an installation token 404s
on the site repo. The files are public and a few kB each, so an unauthenticated GET is both
sufficient and simpler than arranging a second installation.

Everything here degrades quietly, because a draft is missing more often than not. Of the last 20
runs of `1_on_pull_request.yml` (measured 2026-07-27 with `gh run list`): 5 succeeded, 14 failed,
1 was cancelled. Twelve of the fourteen failures were in the `metadata` job, one in `get-gpml`
and one in `frontmatter`. So `fetch` never raises, and the check functions return None when they
have nothing to say, leaving the checklist whatever the app already derived from the GPML itself.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from app import obs
from app.review.checklist import CURATION_CHECKLIST, ChecklistState

_RAW_BASE = "https://raw.githubusercontent.com"

# Anything the pipeline writes is named from the PR number, so a slug is always of this shape.
# Matched with `fullmatch`: `$` would also accept a trailing newline, and this guard is what stops
# a slug reaching a URL or a cache filename — it arrives from a caller that may one day read it
# off a PR body rather than compute it.
_SLUG_RE = re.compile(r"WP\d+__PR\d+")

# The pipeline's own test for "this is an edit of an existing pathway", copied character for
# character from the `get-gpml` step of `1_on_pull_request.yml`:
#
#     if [[ $GPML_FILE =~ ^WP[1-9][0-9]{0,4}\.gpml$ ]]
#
# Copied rather than approximated as `WP\d+`, which would also accept `WP0001.gpml` and
# `WP123456.gpml`. The pipeline files both of those as *new* submissions under `WP0`, and
# predicting a slug the pipeline never writes makes the dashboard read artifacts that do not
# exist — an app-side mis-prediction that would otherwise be reported against the submitter.
_PIPELINE_EDIT_RE = re.compile(r"WP[1-9][0-9]{0,4}\.gpml")

# The filename a new pathway is committed under before the target repo assigns a real id. Its
# leading zero keeps it out of `_PIPELINE_EDIT_RE` on purpose, so the pipeline classifies it as
# new. Kept in step with `app.submit.gpml.PLACEHOLDER_WPID_STR` by a test rather than an import,
# so this module does not drag in the submission package.
_PLACEHOLDER_GPML = "WP0001.gpml"

# An identifier cell the generators leave behind when a data node carries no annotation. The
# GPML side of the app treats empty as unmapped; the TSV round-trip adds these spellings.
_UNMAPPED_VALUES = {"", "na", "null", "none"}

# How often the cache sweep may run, and how far past the TTL an entry must be before it is
# taken. See `DraftsReader.sweep` — the margin is for mtime across a GlusterFS replica, not for
# anything about the cache, since past the TTL nothing can serve the file anyway.
_DRAFT_SWEEP_INTERVAL_SECONDS = 3600.0
_DRAFT_SWEEP_TTL_MARGIN = 4

#: How long a failed read is not retried. Without it every background pass re-asked a site that
#: had just refused, three files per review, each able to wait out the 10 s timeout.
_ERROR_BACKOFF_SECONDS = 120.0

#: The pipeline's own drawing, served same-origin at ``/previews/{pr}/pipeline.svg``. Capped so a
#: runaway render cannot fill the cache volume; real ones are a few hundred kB.
_MAX_PIPELINE_SVG_BYTES = 5 * 1024 * 1024


def _checklist_auto_check(key: str):
    """The checklist's own auto-check for `key`, or a loud failure at import time.

    Reached through the public `CURATION_CHECKLIST` rather than the private function so the
    wording stays in one place: a change to "3 ontology tags" must not need a matching edit here.
    """
    for item in CURATION_CHECKLIST:
        if item.key == key and item.auto_check is not None:
            return item.auto_check
    raise KeyError(f"no auto_check defined for checklist item {key!r}")


# The only checklist auto-check the pipeline's artifacts can drive as-is. The others want a parsed
# GPML — the data-node and reference tables are not the same population as the GPML's (see
# `datanode_check`), so reusing their wording here would say something untrue.
_ONTOLOGY_AUTO_CHECK = _checklist_auto_check("ontology_tags")


@dataclass(frozen=True)
class DraftArtifacts:
    slug: str
    available: bool
    datanodes: list[dict] | None
    bibliography: list[dict] | None
    info: dict | None
    draft_url: str | None
    svg_url: str | None
    thumb_url: str | None


@dataclass(frozen=True)
class _Tag:
    ontology: str
    identifier: str


@dataclass(frozen=True)
class _OntologyView:
    """The duck-typed metadata `_auto_ontology` wants: `app.review.checklist` documents its
    auto-checks as taking metadata shaped like `app.preview.metadata.CurationMetadata`, and this
    is just enough of that shape to run one of them against the pipeline's info.json."""

    ontology_tags: list[_Tag]


class _TransientFetchError(Exception):
    """A fetch failed for a reason that says nothing about whether the draft exists."""


# ---- parsing ------------------------------------------------------------------------------


def _parse_tsv(raw: bytes) -> list[dict]:
    """Parse one of the pipeline's TSVs into rows.

    The generators quote fields that contain a tab, a quote or markup (citation strings are full
    HTML), so this goes through `csv` rather than splitting on tabs.
    """
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if not reader.fieldnames:
        return []
    return [dict(row) for row in reader]


def _identifier_column(rows: list[dict]) -> str | None:
    """The column holding a data node's identifier, matched fuzzily on the header.

    The header has changed shape across pipeline revisions (the current one says `Identifier`,
    with a per-database column beside it), so this matches rather than hard-codes. Returning
    None is meaningful: the caller must then decline to judge instead of reading a column it
    guessed at.
    """
    if not rows:
        return None
    headers = [h for h in rows[0] if h]
    for header in headers:
        if header.strip().lower() == "identifier":
            return header
    for header in headers:
        if "identifier" in header.strip().lower():
            return header
    for header in headers:
        if header.strip().lower() == "id":
            return header
    return None


def _label_column(rows: list[dict]) -> str | None:
    if not rows:
        return None
    for wanted in ("label", "name"):
        for header in rows[0]:
            if header and header.strip().lower() == wanted:
                return header
    return None


def _is_unmapped(value: object) -> bool:
    return str(value or "").strip().lower() in _UNMAPPED_VALUES


# ---- reader -------------------------------------------------------------------------------


class DraftsReader:
    """Fetches (and caches) one submission's draft artifacts from the pipeline's site repo."""

    def __init__(
        self,
        *,
        repo: str,
        branch: str,
        site_base_url: str,
        cache_dir: str | Path,
        ttl_seconds: int = 300,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo = repo.strip("/")
        self._branch = branch
        self._site_base_url = site_base_url.rstrip("/")
        self._cache_dir = Path(cache_dir)
        self._ttl_seconds = ttl_seconds
        #: Monotonic stamp of the last sweep — see ``sweep``. Held on the reader because the app
        #: builds it once at startup, so the throttle survives the requests that call it.
        self._last_sweep: float | None = None
        # One pooled client for the process: a dashboard page fetches three files per queue row,
        # and a fresh connection per file would dominate the render. Never closed — the reader is
        # built once at startup and lives as long as the process, and the app's lifespan has no
        # shutdown half to close it in.
        self._client = obs.instrument_httpx(
            httpx.Client(transport=transport, timeout=10.0, follow_redirects=True), "raw"
        )
        self._clock = clock
        #: slug -> wall-clock time before which a failed read is not retried. In memory: a
        #: restart retrying at once is fine, and an on-disk marker would read as a cached answer.
        self._error_until: dict[str, float] = {}
        #: slug -> when the pipeline SVG was last asked for, so a missing one is not re-asked on
        #: every background pass.
        self._svg_checked: dict[str, float] = {}

    # -- slugs -----------------------------------------------------------------------------
    @staticmethod
    def slug_for_filename(basename: str, pr_number: int) -> str:
        """The slug workflow 1 files a GPML basename under, following its `get-gpml` step.

        Two rules, applied in this order:

        1. A basename that already contains `__` is first collapsed to its prefix plus `.gpml`
           (the workflow's "TEMPORARY: Rename previously processed test GPMLs" branch), so
           `WP1001__PR60.gpml` is classified as an edit of WP1001 and refiled under the new PR
           number. The site repo has WP1001 drafts under PR60, 61, 62 and 64 from exactly this.
        2. What is left is an edit only if it matches `_PIPELINE_EDIT_RE`; anything else — a
           leading zero, six or more digits, a name that is not `WP<digits>.gpml` at all — is a
           new pathway and gets the `WP0` placeholder.
        """
        name = Path(basename).name
        if "__" in name:
            name = f"{name.split('__', 1)[0]}.gpml"
        if _PIPELINE_EDIT_RE.fullmatch(name):
            return f"{name.removesuffix('.gpml')}__PR{pr_number}"
        return f"WP0__PR{pr_number}"

    @staticmethod
    def slug_for(*, kind: str, wpid: int | None, pr_number: int) -> str:
        """The slug workflow 1 files this submission under.

        The app knows which file it committed, so it can predict the slug without reading the PR
        back: an update commits `WP<id>.gpml` (`app.submit.gpml.layout_paths`) and a new pathway
        commits the placeholder. Both go through the pipeline's own classification so an id the
        pipeline would not accept as an edit — 0, or 100000 and up — predicts `WP0`, the slug the
        pipeline actually writes, instead of one nobody files.
        """
        if kind == "update" and wpid is not None:
            return DraftsReader.slug_for_filename(f"WP{wpid}.gpml", pr_number)
        return DraftsReader.slug_for_filename(_PLACEHOLDER_GPML, pr_number)

    # -- urls ------------------------------------------------------------------------------
    def _raw_url(self, path: str) -> str:
        return f"{_RAW_BASE}/{self._repo}/{self._branch}/{path}"

    def _draft_url(self, slug: str) -> str:
        return f"{self._site_base_url}/drafts/{slug}"

    def published_url(self, wpid: int) -> str:
        """Where the pathway lives on the site once the repo has published it.

        Public, and deliberately not derived from `DraftArtifacts`: publication *moves* the
        drafts rather than copying them, so by the time this URL is the right one to show,
        `fetch()` reports `available=False` and every draft URL 404s. A published review would
        otherwise offer no link to the finished page at all — the one page a submitter most
        wants to see.

        Takes the id rather than a slug because the slug is a draft-time construct
        (`WP0__PR54`); the published page is filed under the id the repo assigned.
        """
        return f"{self._site_base_url}/pathways/WP{wpid}"

    # -- fetch -----------------------------------------------------------------------------
    def fetch(self, slug: str) -> DraftArtifacts:
        """Read a draft's artifacts, from the disk cache when it is still fresh.

        Network. Called from the background loop (``refresh_pipeline_checks``); a page reads
        ``cached`` instead, so no page waits on raw.githubusercontent.com.

        Never raises. A draft that does not exist, a site repo that is unreachable and a file
        that will not parse all come back as `available=False`, because none of them is a reason
        to fail a curator's page load.
        """
        if not _SLUG_RE.fullmatch(slug or ""):
            return self._unavailable(slug)

        cached = self._read_cache(slug)
        if cached is not None:
            return self._artifacts(slug, cached)

        # A read that failed moments ago is not retried yet; the last good answer stands.
        if self._clock() < self._error_until.get(slug, 0.0):
            return self.cached(slug)

        try:
            payload = self._fetch_remote(slug)
        except Exception:
            # Deliberately *not* cached as an answer. A transport failure or a non-404 status
            # says nothing about whether the draft exists, and caching it would freeze a healthy
            # draft as missing for the whole TTL. What was last read stays on disk and is served.
            self._error_until[slug] = self._clock() + _ERROR_BACKOFF_SECONDS
            return self.cached(slug)

        self._error_until.pop(slug, None)
        self._write_cache(slug, payload)
        return self._artifacts(slug, payload)

    def cached(self, slug: str) -> DraftArtifacts:
        """What the last successful fetch found, from disk only, however old. Never the network.

        The page path. ``fetch`` used to run on every page render and could hold one for the
        reader's 10 s timeout per file; the background loop keeps this copy current instead.
        """
        if not _SLUG_RE.fullmatch(slug or ""):
            return self._unavailable(slug)
        with obs.timed("disk"):
            envelope = self._read_envelope(slug)
        if envelope is None:
            return self._unavailable(slug)
        return self._artifacts(slug, envelope.get("payload") or {})

    # -- the pipeline's own drawing ----------------------------------------------------------
    def _svg_cache_path(self, slug: str) -> Path:
        return self._cache_path(slug).with_suffix(".svg")

    def svg_path(self, slug: str) -> Path | None:
        """The cached pipeline SVG for ``slug``, or None. Disk only."""
        if not _SLUG_RE.fullmatch(slug or ""):
            return None
        path = self._svg_cache_path(slug)
        return path if path.is_file() else None

    def fetch_svg(self, slug: str) -> bool:
        """Cache the pipeline's rendered SVG for ``slug``. Background only. True when it wrote.

        raw.githubusercontent.com serves ``.svg`` as ``text/plain`` with ``nosniff``, so an
        ``<img>`` pointed there draws nothing. The portal serves its copy same-origin instead,
        under the same locked-down headers as its own renders. Asked at most once per TTL.
        """
        if not _SLUG_RE.fullmatch(slug or ""):
            return False
        now = self._clock()
        last = self._svg_checked.get(slug)
        if last is not None and now - last < self._ttl_seconds:
            return False
        if now < self._error_until.get(slug, 0.0):
            return False
        self._svg_checked[slug] = now
        try:
            resp = self._client.get(self._raw_url(f"draft_assets/{slug}/{slug}.svg"))
        except Exception:  # noqa: BLE001 - transport trouble: keep what is on disk
            return False
        path = self._svg_cache_path(slug)
        if resp.status_code == 404:
            path.unlink(missing_ok=True)
            return False
        body = resp.content
        head = body[:256].lstrip()
        if (
            not resp.is_success
            or len(body) > _MAX_PIPELINE_SVG_BYTES
            or not (head.startswith(b"<svg") or head.startswith(b"<?xml"))
        ):
            return False
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        except OSError:
            return False
        return True

    # -- the published page --------------------------------------------------------------------
    def page_live(self, wpid: int, since: datetime | None = None) -> bool | None:
        """Is the published page up, and (for an update) rebuilt since ``since``? None: unknown.

        One HEAD request. A new pathway's page did not exist before, so a 200 is the answer. An
        update's page existed all along, so it counts only once GitHub Pages reports a
        ``Last-Modified`` at or after the publication; without that header there is no telling
        the old page from the new one, and the answer is None rather than a guess.
        """
        try:
            resp = self._client.head(self.published_url(wpid))
        except Exception:  # noqa: BLE001 - unreachable says nothing about the page
            return None
        if resp.status_code == 404:
            return False
        if not resp.is_success:
            return None
        if since is None:
            return True
        modified = resp.headers.get("last-modified")
        if not modified:
            return None
        try:
            when = parsedate_to_datetime(modified)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        # Last-Modified has whole seconds; the publication time does not.
        return when >= since.replace(microsecond=0)

    def draft_page_exists(self, slug: str) -> bool | None:
        """Is the draft page the publish workflow needs there? None when that cannot be told.

        Workflow 3A starts with ``find _drafts -name "WP*__PR<n>.md"`` in the site repo and
        stops at ``get-wpid`` ("No draft file found") when there is none — which is what happens
        whenever workflow 1's ``json-svg`` job fails, because ``commit-outputs`` is then skipped
        (sandbox-wp-db PR #87, 2026-11-19). So this asks for exactly that file.

        Never cached, unlike ``fetch``: it is asked once, at the moment of approval, and a stale
        "missing" would refuse a draft that has since been written. Only a 404 is an answer;
        anything else (a 429 from the anonymous rate limit, a timeout) is None, so a flaky read
        never blocks a curator on its own.
        """
        if not _SLUG_RE.fullmatch(slug or ""):
            return None
        try:
            resp = self._client.head(self._raw_url(f"_drafts/{slug}.md"))
        except Exception:  # noqa: BLE001 - transport trouble says nothing about the file
            return None
        if resp.status_code == 404:
            return False
        if resp.is_success:
            return True
        return None

    def _fetch_remote(self, slug: str) -> dict:
        datanodes = self._get(f"_data/drafts/{slug}-datanodes.tsv")
        bibliography = self._get(f"_data/drafts/{slug}-bibliography.tsv")
        info = self._get(f"draft_assets/{slug}/{slug}-info.json")
        return {
            "datanodes": _safe(_parse_tsv, datanodes),
            "bibliography": _safe(_parse_tsv, bibliography),
            "info": _safe(_parse_json, info),
        }

    def _get(self, path: str) -> bytes | None:
        """GET one artifact. None means 404 — the pipeline never wrote it.

        Every other error status raises instead, because only 404 is an answer about the file.
        raw.githubusercontent.com rate-limits anonymous callers and the dashboard asks for three
        files per queued review on every render, so a 429 is an expected reply rather than a
        hypothetical one; reading it as "absent" would put a healthy draft in the cache as
        missing until the TTL ran out.
        """
        resp = self._client.get(self._raw_url(path))
        if resp.status_code == 404:
            return None
        if resp.is_error:
            raise _TransientFetchError(f"{resp.status_code} for {path}")
        return resp.content

    # -- assembly --------------------------------------------------------------------------
    def _artifacts(self, slug: str, payload: dict) -> DraftArtifacts:
        datanodes = payload.get("datanodes")
        bibliography = payload.get("bibliography")
        info = payload.get("info")
        available = any(part is not None for part in (datanodes, bibliography, info))
        if not available:
            return self._unavailable(slug)
        assets = f"draft_assets/{slug}/{slug}"
        return DraftArtifacts(
            slug=slug,
            available=True,
            datanodes=datanodes,
            bibliography=bibliography,
            info=info,
            draft_url=self._draft_url(slug),
            svg_url=self._raw_url(f"{assets}.svg"),
            thumb_url=self._raw_url(f"{assets}-thumb.png"),
        )

    @staticmethod
    def unavailable(slug: str) -> DraftArtifacts:
        """The "nothing on file" answer, for a caller that knows not to look."""
        return DraftsReader._unavailable(slug)

    @staticmethod
    def _unavailable(slug: str) -> DraftArtifacts:
        return DraftArtifacts(
            slug=slug,
            available=False,
            datanodes=None,
            bibliography=None,
            info=None,
            draft_url=None,
            svg_url=None,
            thumb_url=None,
        )

    # -- cache -----------------------------------------------------------------------------
    def _cache_path(self, slug: str) -> Path:
        # The repo and branch are part of the key, not just the slug. Readers pointed at
        # different repos or branches share one cache_dir (the app hands every reader the same
        # `preview_cache_dir/drafts`), and on the slug alone they would serve each other's
        # payloads — changing the drafts branch would keep serving the old branch's artifacts.
        scope = hashlib.sha256(f"{self._repo}\n{self._branch}".encode()).hexdigest()[:12]
        return self._cache_dir / f"{slug}.{scope}.json"

    def _read_envelope(self, slug: str) -> dict | None:
        path = self._cache_path(slug)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return envelope if isinstance(envelope, dict) else None

    def _read_cache(self, slug: str) -> dict | None:
        envelope = self._read_envelope(slug)
        if envelope is None:
            return None
        try:
            fetched_at = float(envelope.get("fetched_at", 0))
        except (TypeError, ValueError):
            return None
        if self._clock() - fetched_at > self._ttl_seconds:
            return None
        return envelope.get("payload") or {}

    def _write_cache(self, slug: str, payload: dict) -> None:
        # A miss is cached alongside a hit, but only a miss that was answered — every file 404ed.
        # Most of the pipeline's runs produce no draft at all, and re-asking for three files that
        # are known to be absent on every dashboard render is exactly the load the cache exists
        # to prevent.
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(slug).write_text(
                json.dumps({"fetched_at": self._clock(), "payload": payload}), encoding="utf-8"
            )
        except (OSError, ValueError):
            pass  # the cache is an optimisation; losing it costs requests, not correctness

    def sweep(self, *, force: bool = False) -> int:
        """Delete cache entries too old to be served. Returns how many went.

        The same leak issue #18 fixed next door, in the other cache under the same directory:
        entries expire by the TTL but nothing ever removed the file, so the disk kept one per
        submission per scope forever. The dead ones are not hypothetical — the live deployment
        carries a whole scope's worth from before its drafts repo was repointed at the fork, which
        nothing will ever read again.

        Deleting past the TTL is behaviour-neutral by construction: ``_read_cache`` already
        refuses anything that old, so the file was going to be refetched and rewritten either way.
        The margin on top of the TTL is for mtime across a GlusterFS replica rather than for any
        property of the cache.
        """
        now = time.monotonic()
        if (
            not force
            and self._last_sweep is not None
            and now - self._last_sweep < _DRAFT_SWEEP_INTERVAL_SECONDS
        ):
            return 0
        self._last_sweep = now
        cutoff = time.time() - max(self._ttl_seconds * _DRAFT_SWEEP_TTL_MARGIN, 60.0)
        freed = 0
        try:
            # The pipeline SVGs age out with the payloads they belong to.
            entries = sorted([*self._cache_dir.glob("*.json"), *self._cache_dir.glob("*.svg")])
        except OSError:
            return 0
        for path in entries:
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
            except OSError:
                continue
            freed += 1
        return freed


def _safe(parse, raw: bytes | None):
    if raw is None:
        return None
    try:
        return parse(raw)
    except Exception:
        return None


def _parse_json(raw: bytes) -> dict | None:
    data = json.loads(raw.decode("utf-8", errors="replace"))
    return data if isinstance(data, dict) else None


# ---- checklist mapping --------------------------------------------------------------------
#
# Each returns a (state, note) the caller may apply to a checklist item, or None meaning "say
# nothing" — the item then keeps whatever the app's own GPML-derived auto-check put there. The
# pipeline's tables are a second opinion, not a replacement, and they must never turn a check
# that the app answered into an empty one.
#
# None of these ever returns `na`, on purpose. `build_checklist` pairs an auto-derived `na` with
# `required=False`, because `is_complete` demands `pass` on every required item; the caller here
# writes only state and note and never touches `required`. An `na` landing on a still-required
# `datanodes_mapped` or `references_valid` would therefore leave an item approval can never
# accept until a curator overrode it by hand. `pending` plus an explaining note says the same
# thing without wedging the review. The one exception is `ontology_tags`, which
# `CURATION_CHECKLIST` declares `required=False` outright, so an `na` there blocks nothing.


def datanode_check(artifacts: DraftArtifacts) -> tuple[str, str] | None:
    """`datanodes_mapped`, read from the pipeline's data-node table.

    The table lists annotated nodes only, so on its own it cannot carry this check. Measured on
    2026-07-27 over the eight drafts in `wikipathways/sandbox-wp.gh.io`: no row in any of them
    has an empty `Identifier`, and every table's row count is at most the number of GPML data
    nodes carrying an `Xref` id. WP9__PR34 is the clearest case — 35 data nodes in its GPML, 14
    of them with an id, and exactly 14 rows. Reading "every listed node has an identifier" as a
    pass would have passed that pathway with 21 unannotated nodes unmentioned. So the most this
    returns on a well-formed table is `pending` with the count, and the curator has the diagram.
    """
    if not artifacts.available or artifacts.datanodes is None:
        return None
    rows = artifacts.datanodes
    if not rows:
        return (
            ChecklistState.PENDING.value,
            "The pipeline's data-node table is empty. It lists only the nodes it could annotate, "
            "so either this pathway has no data nodes or none of them resolved — check the "
            "diagram before reading this as harmless.",
        )

    id_col = _identifier_column(rows)
    if id_col is None:
        # Never auto-fail on a table we did not understand: an unrecognised header means the
        # pipeline changed shape, which is our problem, not the submitter's.
        return (
            ChecklistState.PENDING.value,
            f"The pipeline listed {len(rows)} data nodes, but its columns "
            f"({', '.join(h for h in rows[0] if h)}) were not recognised — check them by hand.",
        )

    label_col = _label_column(rows)
    unmapped = [
        (str(row.get(label_col) or "").strip() if label_col else "") or "(unlabeled)"
        for row in rows
        if _is_unmapped(row.get(id_col))
    ]
    if unmapped:
        # Not seen on any live draft, since the generator drops the nodes it could not annotate
        # rather than writing them with an empty cell. Kept because an empty identifier is
        # unambiguous if a later revision of the generator does start emitting one.
        shown = ", ".join(unmapped[:6]) + ("…" if len(unmapped) > 6 else "")
        return (
            ChecklistState.FAIL.value,
            f"{len(unmapped)} of {len(rows)} rows in the pipeline's data-node table carry no "
            f"identifier: {shown}",
        )
    return (
        ChecklistState.PENDING.value,
        f"The pipeline annotated {len(rows)} data nodes. It lists only what it could annotate, "
        "so check the diagram for nodes missing from that count.",
    )


def reference_check(
    artifacts: DraftArtifacts, *, gpml_reference_count: int | None
) -> tuple[str, str] | None:
    """`references_valid`, comparing the pipeline's bibliography against the GPML's own count.

    The bibliography is the pipeline's *resolved* set: it looks each reference up and drops the
    ones it could not resolve. A shortfall against the GPML is therefore the signal worth having
    — it names references that will not render on the published page. Two live examples on
    2026-07-27: WP4846__PR35, whose GPML declares 31 references and whose bibliography holds 27,
    and WP554__PR11, 8 against 7.
    """
    if not artifacts.available or artifacts.bibliography is None:
        return None
    if gpml_reference_count is None:
        return None  # nothing to compare against; leave the item alone
    if gpml_reference_count == 0:
        return (
            ChecklistState.PENDING.value,
            "The GPML declares no literature references, so the pipeline had none to resolve.",
        )

    resolved = len(artifacts.bibliography)
    plural = "" if gpml_reference_count == 1 else "s"
    if resolved == gpml_reference_count:
        return (
            ChecklistState.PASS.value,
            f"The pipeline resolved all {resolved} reference{plural}.",
        )
    if resolved < gpml_reference_count:
        return (
            ChecklistState.FAIL.value,
            f"The pipeline resolved {resolved} of the {gpml_reference_count} "
            f"reference{plural} in the GPML.",
        )
    return (
        ChecklistState.PENDING.value,
        f"The pipeline listed {resolved} references but the GPML declares "
        f"{gpml_reference_count}; check which are extra.",
    )


def info_checks(artifacts: DraftArtifacts) -> dict[str, tuple[str, str]]:
    """`naming_ok`, `description_ok` and `ontology_tags`, from the pipeline's info.json.

    Empty when there is no info.json — the caller applies nothing and the checklist stands.
    """
    if not artifacts.available or not artifacts.info:
        return {}
    info = artifacts.info
    return {
        "naming_ok": _naming_check(artifacts.slug, info),
        "description_ok": _description_check(info),
        "ontology_tags": _ontology_check(info),
    }


def _naming_check(slug: str, info: dict) -> tuple[str, str]:
    """Did the pipeline file this submission the way the app expects?

    Not the checklist's own `naming_ok` auto-check, which asserts the app assigned the WPID and
    the layout. In pipeline mode it does not: workflow 1 classifies from the basename and
    workflow 3a assigns the id on approval. The assertion that is actually available here is
    that the pipeline's own record of the slug agrees with the one the app predicted.
    """
    declared = str(info.get("wpid") or "").strip()
    if not declared:
        return (
            ChecklistState.PENDING.value,
            "The pipeline's info.json records no id; check the filename by hand.",
        )
    if declared != slug:
        # Pending, not fail. `slug_for` predicts from the file the app committed, not from what
        # the PR ended up containing, so a disagreement is at least as likely to be our
        # mis-prediction as the submitter's mis-naming — and `naming_ok` is a required item, so
        # failing it here would block approval on our own guess.
        return (
            ChecklistState.PENDING.value,
            f"The pipeline filed this as {declared}, but the app expected {slug}. "
            "Check which file the pull request actually changed.",
        )
    base = declared.split("__", 1)[0]
    if base == "WP0":
        return (
            ChecklistState.PASS.value,
            f"Filed as {declared}: a new pathway, so the WPID is assigned on approval.",
        )
    return ChecklistState.PASS.value, f"Filed as {declared}, keeping the existing {base}."


def _description_check(info: dict) -> tuple[str, str]:
    """Whether the title and description survived extraction.

    Stops at `pending` when both are present: "meaningful" is the judgement the checklist
    deliberately leaves to a human, so the most this can do is put the extracted text in front of
    them. Absence, on the other hand, is a fact — and a common one, since the pipeline extracts
    an empty description whenever the GPML has no comment of the right type.
    """
    title = str(info.get("title") or "").strip()
    description = str(info.get("description") or "").strip()
    present = (("title", title), ("description", description))
    missing = [name for name, value in present if not value]
    if missing:
        return (
            ChecklistState.FAIL.value,
            f"The pipeline extracted no {' and no '.join(missing)}.",
        )
    excerpt = description if len(description) <= 160 else description[:159] + "…"
    return (
        ChecklistState.PENDING.value,
        f'The pipeline extracted the title "{title}" and this description: {excerpt}',
    )


def _ontology_check(info: dict) -> tuple[str, str]:
    raw = info.get("ontology-ids") or []
    tags = [_split_curie(str(value)) for value in raw if str(value).strip()]
    result = _ONTOLOGY_AUTO_CHECK(_OntologyView(ontology_tags=tags))
    return result.state, result.note


def _split_curie(value: str) -> _Tag:
    prefix, _, local = value.partition(":")
    return _Tag(ontology=prefix, identifier=local or prefix)
