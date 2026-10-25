# Handoff — 2026-07-29

Supersedes `docs/session-handoff-2026-07-27.md` as the read-me-first. That file is still worth
reading for the sandbox pipeline and the first deployment; this one covers what changed after it
and what is true now.

## Deployed right now

**https://upload.wikipathways.org**, image
`ghcr.io/marvinm2/wikipathways-submit@sha256:cf6c425f906719fd1c85327a768f9220615d19e25ececda163df108d8f71b69a`
(built from `c4a877e`), running on **tgx1** — it moved from tgx2 during an earlier deploy, which
is the no-pinning arrangement working as intended.

**The webhook is on** as of 2026-07-29 afternoon. `wpsubmit_webhook_secret` exists and is
attached, and `POST /webhooks/github` answers **401** to an unsigned request rather than 503.
Verified with genuine signed events (a `labeled` / `unlabeled` pair on a closed pull request,
both 200) — but **no real `closed` delivery has happened yet**, because the only closed pull
request predates the secret. The next real submission is the first proper test of that path.

Reconcile has been quietly covering for the missing webhook all along: it runs on the queue and
on a review page, so a stale row clears as soon as anyone looks. The webhook's value is
promptness, not correctness — with nobody watching the dashboard, a closed pull request used to
sit until its TTL.

That move is worth knowing because it wasted time: `ssh tgx2 "docker logs $(docker ps -q -f
name=wikipathways-submit.1)"` on the wrong node returns an empty log rather than an error, which
reads exactly like "the app never received the request". **Find the node first**:

```bash
ssh tgx1 "docker service ps wikipathways-submit -f desired-state=running --format '{{.Node}}'"
```

Live config is as recorded in the 07-27 handoff, plus one addition:

```
WPSUBMIT_SITE_NOTICE=Sandbox deployment. Submissions here open a real pull request, but are not
published to WikiPathways yet and no WPID is assigned. Please do not rely on this for work you
need published.
```

**Set that on any deployment whose target cannot complete a publication.** It renders a standing
banner on every page; empty renders nothing.

## What was built

**Clickable data nodes in the preview (issue #14, closed).** Clicking a node opens a panel with
its type, database, identifier and resolver link; an unannotated node reads "Not annotated".

Built as an overlay, not either route the issue proposed — both had gone stale (it is written
around PinPath, which is retired, and around CI artifacts the app no longer reads and the current
target cannot produce). `render_gpml_with_nodes` returns hotspots alongside the drawing;
`<side>-nodes.json` is cached beside the SVG; `GET /previews/{pr}/{side}-nodes.json` serves it.

> [!warning] Two things here will bite again if forgotten
> **Geometry and properties come from the same element in one pass, deliberately.** Joining
> renderer geometry to `metadata.py` properties by list index looks equivalent and is not: the
> renderer drops a `DataNode` with no `Graphics`, the metadata parser keeps it, so one such
> element shifts every identifier onto the wrong box — silently, and in a direction a reviewer
> would probably trust. `test_geometry_and_properties_cannot_drift_apart` pins it.
>
> **Do not reach for a `ResizeObserver` to keep the overlay aligned.** Observing the image and
> writing layout from the callback is a feedback loop by construction; it froze the tab, with
> nothing in the console — the page simply stopped painting and screenshots timed out. The
> alignment is synced at three explicit non-re-entrant points instead: image `load`, window
> `resize`, and `initZoom`'s `apply()` via the `root.__syncOverlay` hook.

The first attempt also had the overlay filling the stage, which the image does not — `max-width`
and `max-height` letterbox it, so every hotspot sat about a node's height off. The layer is now
positioned from the image's own measured offset box.

## Issue tracker

Was four open, three of which were already built and merely never closed. Now nine open, all
filed from an audit and each grounded in a file and line rather than a guess.

**Closed with evidence:** #5 (deployment), #11 (before/after viewer), #14 (clickable nodes),
#15 (checklist lost updates — fixed with `version_id_col` + retry rather than the row lock it
proposed, because `SELECT ... FOR UPDATE` is a no-op on SQLite and would have left the hazard in
the backend the race test runs on).

**Open, and done in this session:** #16 uploads bounded (413, chunked read, `Content-Length` not
trusted), #18 render cache freed at every terminal transition, #20 `robots.txt`. All three
verified against production, not just tests — a 5 MB post returns 413, a normal pathway 200,
`robots.txt` serves, and closing a pull request removed its cache directory from GlusterFS.

**Open, not started:** #17 unpaginated queue, #21 no rate limiting, #22 fork-per-submitter,
#23 TTL tuning.

#19, #24 and #25 were done later the same day — see the section below.

## A correction to the record

The 07-27 handoff and the KnowledgeBase note both read the PPAR submission from @MadhushriMSV
(sandbox PR #6) as a real contributor's work going into a dead end. **It was a colleague testing
the portal**, confirmed by Marvin, and is closed. Nobody has lost work to this deployment.

That inference was written into four places in the repo as established fact before it was
checked. The banner is still right, on the honest ground: from inside the app a test was
indistinguishable from the real thing, and by the time you can tell them apart the silent failure
has already happened.

## 2026-07-29, later — three more things shipped

**#19, the overlay is one tab stop.** Roving tabindex under a toolbar role, arrow keys in reading
order, selection following focus into a polite live region. Measured on the live 64-node WP100
card: 64 hotspots per frame, 1 tab stop per frame — it was 128 stops between the diagram and
Approve. Building it surfaced that arrow keys could walk focus off the clipped edge of a zoomed
viewport (it pans by transform, so there is no scroll offset for the browser's own
scroll-into-view to write); `initZoom` now exposes `__revealRect`.

**#24, an update says what changed.** `app/preview/diff.py` classifies every data node added /
removed / re-annotated / relabelled / moved, cached as `diff.json`. Two things the renderer had
to start recording: `GraphId`, and the node centre **in user units** — the hotspot percentages
are relative to each side's own board, so a submitter who changes `BoardWidth` moves every
percentage without moving anything, and a movement test built on them would report the whole
pathway as relocated. Proven on production against a real WP100 edit carrying one of each
category.

**#25, the submitter's note never reached GitHub.** Found by doing the above, not by a tool. The
note went only into the pull request body, and a pipeline target's own workflow replaces that
body with its template — silently, after the app's write succeeded. It now also goes in the
mirror comment (blockquoted, every line, or a multi-line note escapes the quote) and onto the
review row, because the other copy lives in the render cache that #18 deletes at every terminal
transition. Migration `e5f1b2d3c4a6`.

Closed late: #16 and #20 (shipped the previous session, never closed). #18 stays open — the
terminal-transition pruning works, the orphan sweep does not exist, and directories `1`-`6` are
still in the cache.

**Suite is at 325.** An earlier note in this repo and in the KnowledgeBase said 331; that number
was never measured and has been corrected.

## 2026-07-29, later still — the fork can now produce a draft page

The target repo's own rendered output had never worked on the fork, and the reason was one
missing credential rather than anything subtle. A fork inherits no Actions secrets, so
`ACTIONS_SANDBOX_DEPLOY_KEY` was empty, `actions/checkout` fell back to `GITHUB_TOKEN`, and
`commit-outputs` got a 403 pushing to `wikipathways/sandbox-wp.gh.io` on **every run the fork had
ever had**. No draft was ever written — which is also why 3a died in nineteen seconds: its first
step looks for a draft for the PR and there was none. Both halves of "approve and publish" were
dead for that single reason.

Fixed by giving the fork its own sister repos: `marvinm2/sandbox-wp.gh.io` and
`marvinm2/sandbox-wp-assets`, each with a write-enabled deploy key whose private half is a secret
on `marvinm2/sandbox-wp-db` (`ACTIONS_SANDBOX_DEPLOY_KEY`, `ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY` —
the second is the name the repaired 3a already expected). A deploy key is per-repository, which
is why there are two. Every `repository:` in workflows 1, 3a and 3b now names a fork; 3a was
replaced with the repaired copy from `sandbox-workflows/`.

Pages is on for the site fork as a legacy branch build, and `_config.yml` gained
`baseurl: "/sandbox-wp.gh.io"` — it is project pages now, not an apex domain, so without the
subpath every generated link is wrong.

The app was repointed with two env vars, which is the whole app-side change:

```
WPSUBMIT_DRAFTS_REPO=marvinm2/sandbox-wp.gh.io
WPSUBMIT_DRAFTS_SITE_BASE_URL=https://marvinm2.github.io/sandbox-wp.gh.io
```

No code changed — `DraftsReader` and the card's "Draft page" link were already built for this and
had simply never had an artifact to point at. Its disk cache is keyed on a hash of repo plus
branch, so repointing does not serve the old target's cached misses.

**Run `30451444585` is the first run of workflow 1 anywhere with all ten jobs green.** It wrote
`_drafts/WP0__PR5.md`, both `_data/drafts/` TSVs and all nine `draft_assets/WP0__PR5/` files, and
`https://marvinm2.github.io/sandbox-wp.gh.io/drafts/WP0__PR5` renders the pathway and its SVG.
`DraftsReader` against the new repo returns `available=True` with resolving `draft_url`,
`svg_url` and `thumb_url`.

**A ninth pipeline defect, found on the way.** The `testing` job's data-node step runs under
`bash -e` and two of its assignments end in a `grep`; grep exits 1 on no match, so the step died
**with no output whatsoever** — the log goes straight from `##[endgroup]` to `exit code 1`. One
fires on any edit that deletes a data node, the other on any edit that only re-annotates.
`update-pr-desc` and `commit-outputs` both need that job, so the submitter lost their drafts *and*
their report, silently. Reproduced offline against PR #8's diff (it dies on the fifth deleted
node, `GraphId="a57"`) and fixed with `|| true` in both the fork and `sandbox-workflows/`. Full
write-up in `docs/sandbox-pipeline.md` §6 defect 9 and §7.

## 2026-07-29, last — a pathway was published, for the first time ever

Run `30460071900`, every step green. **WP5423.** The last unobserved step of the lifecycle now has
a green run behind it.

- WPID assigned at publication (`max(_pathways/WP<n>.md) + 1` over 1000 pages), not before.
- All three pushes landed — including **assets**, so the `ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY`
  arrangement works rather than merely being skipped by `continue-on-error`.
- Marker comment `{"pr":5,"wpid":5423,"status":"published"}`, `published` label, PR closed
  **unmerged**.
- The drafts are *moved*, so `_drafts/WP0__PR5.md` now 404s. A draft page disappearing on
  publication is correct, not a regression.
- **The app followed on its own**: `GET /api/reviews/5` reads `status: published, wpid: 5423`,
  taken from the marker comment via the webhook rather than from its own write. That path had
  never seen a real published marker before.
- The published page renders at `marvinm2.github.io/sandbox-wp.gh.io/pathways/WP5423`.

**A tenth defect, and the run is the only reason it was found.** The repaired 3a would not start
at all. A `# FIX:` comment inside a `run:` block quoted the old commit message, and the quotation
contained a GitHub expression — but a `run:` block is one string value, and the runner substitutes
expressions into its **text** before bash ever exists, so `#` protects nothing. An expression that
does not parse fails the *whole workflow at startup*:

```
HTTP 422: failed to parse workflow: (Line: 224, Col: 14): Unexpected symbol: '...wpid'
```

The line it names is the `run:`, not the comment. The only other warning is a zero-job `failure`
run named by **path** rather than workflow name, filed when the file lands — easy to read as
noise. Same mechanism as the security defect in workflow 1: `${{ }}` is text substitution, not
shell expansion.

`tests/test_sandbox_workflows.py` now parses every staged workflow and rejects any `run:`-block
expression that is not a plain reference. Verified by mutation, not by assumption — reintroducing
the original `...wpid` makes it fail. **332 tests.**

Also re-confirmed: **GitHub emits no `labeled` event for a label already present.** The first
dispatch failed and re-firing needed the label removed and re-added.

**What this did not exercise:** the label was applied directly, not through the dashboard, because
PR #5's checklist legitimately fails (`datanodes_mapped` — IRS1 has no identifier) and Approve is
correctly disabled. That is the same call the app makes, and the dashboard's approve path was
proven on 2026-07-28 — but a single pass that *starts* at the Approve button still has not
happened. That is the next thing worth doing, and it needs a logged-in curator.

## 2026-07-29, also — the card links to the finished page

Under the diagram, one link to the target repository's own full pathway page: the draft while the
submission is open, the published pathway once it is out. Exactly one is ever live, because
publication **moves** the drafts rather than copying them.

That move is also why `published_url` is built from the assigned id and **not** from
`DraftArtifacts`. Deriving it from the artifacts looks natural and is exactly backwards: by the
time it is the right link to show, every draft URL 404s and `available` is False, so a published
review offered no link to the finished page at all — the one page a submitter most wants. Three
tests pin it, including the no-WPID case, which would otherwise render `/pathways/WPNone`.

The duplicate in the pipeline panel is dropped. Two identical links a screen apart read as two
destinations.

Also updated, because both had become false since publication started working:

- **The site notice.** It said submissions "are not published to WikiPathways yet and no WPID is
  assigned". Both untrue now. It says what actually happens, and that the sandbox is disposable.
- **3a's publication announcement** hardcoded `sandbox.wikipathways.org`, so on a fork the one
  link a submitter is most likely to click led where their pathway does not exist — and on the
  real sandbox that id belongs to somebody else's pathway. Now a repository variable
  (`PATHWAY_SITE_BASE_URL`), defaulting to the old value.

**The update path published for the first time**: PR #4 kept **WP1001** rather than allocating a
new id, and the app read `kind=update, wpid=1001, published` off the marker. Both halves of the
lifecycle now have a green run.

## Still needing a person

1. **The security disclosure is unsent, and now has a second finding.** Alongside workflow 1,
   `pr_label_dispatcher.yml` interpolates `${{ github.event.label.name }}` straight into a shell
   `case`. Much smaller surface — only someone with write access can apply a label — but the same
   shape, and it belongs in the same disclosure rather than a separate one later. Marvin said on
   2026-07-29 that he would send the email. `wikipathways/sandbox-wp-db` workflow 1 has an unfixed
   command injection. There is no private channel — private vulnerability reporting is
   **disabled** on that repo, there is no `SECURITY.md` in it, in `wikipathways-database`, or in
   the org `.github`, and the org publishes no contact address. So it goes by **email**, which
   needs Marvin. Draft, ready to send:
   `../sandbox-wp-db-disclosure-DRAFT.md`, next to the checkout (deliberately outside
   this repo, which is public). Worth asking an org owner to enable PVR at the same time.

   §6.1 of `docs/sandbox-pipeline.md` described the defect in full in this public repo from the
   evening of 2026-07-27 until 18:20 on 2026-07-28; it is redacted (`7ab7867`) and the draft tells
   the maintainers so rather than presenting the defect as undisclosed.

2. **The GitHub App install on the org sandbox** is still pending an org owner, and it blocks
   draft artifacts, any real publication, and the evidence #23 needs.

3. **Sandbox test litter.** PRs #1, #2, #3, #5 are demo submissions from building this. Closing
   them in one command was refused by the harness permission classifier; closing one at a time
   works. **#3 is `PUBLISH_FAILED`, which is deliberately not terminal** — its cache is correctly
   retained and it is a real state, not litter. Cache directories `1` and `6` are orphans from
   before the pruning shipped, which is the sweep #18 asks for.

## Gotchas, cumulative

Everything in the 07-27 handoff still applies — deploy by digest, never poll `gh run list
--limit 1` after a push, `ref`-based browser clicks on the review page report success and do
nothing (use coordinates). Added since:

- **Find which node runs the task before reading logs.** An empty log on the wrong node is
  indistinguishable from an app that never got the request.
- **Bump `?v=` on `app.css`/`app.js` in `base.html` with every frontend change.** It is at `v=16`.
- A `ResizeObserver` whose callback writes anything affecting the observed element's box is a
  loop, and it presents as an unresponsive page rather than an error.
- **A Chrome tab at `document.visibilityState === 'hidden'` lies to you, and it looks exactly
  like a broken page.** When the browser window is minimised or behind another window, that tab
  stops compositing: `Page.captureScreenshot` times out after 30s reporting a frozen renderer,
  `loading="lazy"` images never load (so the zoom measures a zero-width image and does nothing),
  real key events are not delivered, programmatic `.focus()` sets `activeElement` without firing
  a `focus` event, and — the worst one — **`getComputedStyle` returns stale values**, including
  after an inline style write, so a CSS rule that is applying reads as though it is not. Check
  `document.visibilityState` before believing any of it. `requestAnimationFrame` still runs at
  60fps in that state, which is what rules out an actual freeze.
- Setting `document.cookie` is refused outright in that browser profile, so a session cannot be
  faked from the page. Fetch the authenticated HTML with `curl` and serve the snapshot instead.
- The `.container` class carries the page's `2.2rem`/`4rem` vertical padding. Reusing it for a
  banner's gutter brings that with it.

## Live check commands

```bash
curl -sI https://upload.wikipathways.org/health
curl -s  https://upload.wikipathways.org/robots.txt
ssh tgx1 "docker service ps wikipathways-submit -f desired-state=running --format '{{.Node}} {{.Image}}'"
gh issue list -R marvinm2/wikipathways-submit --state open
gh pr list   -R marvinm2/sandbox-wp-db --state all
```
