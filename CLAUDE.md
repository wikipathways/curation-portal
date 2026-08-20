# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**MVP-2 in progress — the transactional core is built and tested.** Read `docs/design-proposal.md`
(the "why", grounded in a 3-month audit of 51 PRs) and `docs/scaffolding-plan.md` (the build
blueprint) first; they are authoritative over any assumption — keep them in sync as you build.
This is deliberately kept **local** — we do **not** file PRs against the upstream `wikipathways`
repos.

### What exists now

- `mvp1/` + `fork-staging/` — MVP-1 PR-preview pipeline (two GitHub Actions workflows +
  `validate_pathway.py`), adversarially reviewed and hardened. Ships to a **fork** of
  `wikipathways-database`; `fork-staging/CHECKLIST.md` is the test procedure. See `mvp1/README.md`.
- `app/` — the FastAPI app (MVP-2 → MVP-4). Implemented + tested (287 tests): the transactional
  registry (`app/wpid/` atomic allocator, `app/locks/` pathway check-out lock — both with
  threaded race tests), app-owned GPML naming/layout (`app/submit/gpml.py`), the `GitHubClient`
  abstraction (`app/github/` — ABC + `FakeGitHubClient` + httpx impl), the **submission service**
  (`app/submit/service.py`), the **update flow** (`app/update/service.py`, lock →
  branch-off-latest → PR, reuses an open PR on re-upload), and the **curation dashboard**
  (`app/review/` — `Review` model, checklist template, `CurationService`: queue / checklist /
  assign / approve-that-merges gated to the curator whitelist, cascading reservation→MERGED +
  lock release). All write paths roll back their WPID/lock on GitHub failure. Endpoints under
  `/api/*` (validate, submit, pathways/{wpid}/update, pathways/{wpid}/release, reviews[/{n}][/
  checklist|assign|approve]).
  **GitHub OAuth is wired** (`app/auth/`, `/auth/login|callback|logout|me`): writes act as the
  logged-in user (`get_current_user` reads the session, never a form field), and endpoints return
  **401** when not logged in. Configure it per `docs/oauth-setup.md` (register a GitHub OAuth App,
  set `WPSUBMIT_*` env vars).
  **GitHub App (bot) identity is wired** (`app/auth/github_app.py`, issue #1): RS256 JWT →
  cached installation token; the **merge** (`approve_and_merge`) and the **read-only PR mirror
  comment** (`render_mirror_comment` / `upsert_issue_comment`, best-effort — swallows both
  `GitHubError` and `httpx.HTTPError` so a comment blip never fails an already-merged action)
  run as the bot via `get_bot_client` (503 if unconfigured) / `get_bot_optional`, never a
  curator's personal token. The bot's installation token also feeds the WPID floor when no
  `WPSUBMIT_GITHUB_TOKEN` is set (issue #3). Configure per `docs/github-app-setup.md`.
  **The GitHub webhook is wired** (issue #8): `POST /webhooks/github` verifies HMAC-SHA256
  (`WPSUBMIT_GITHUB_WEBHOOK_SECRET`) and, on a `pull_request` `closed` event, releases the lock
  + finalises the reservation (MERGED if merged, returned to the pool if closed unmerged) +
  terminalises the review — idempotent, so a PR closed *outside* the app no longer waits for the
  TTL. TTL tuning against real behaviour remains open.
  **The before/after pathway preview is wired** (issue #11, `app/preview/`), two sources with the
  **in-app renderer preferred**: (1) **instant in-app render** (`app/preview/render.py`, 1a) — a
  dependency-free GPML→SVG drawer runs at PR-creation time (`render_local`, wired into submit +
  update via `_render_preview`), rendering the uploaded GPML as *after* and the base-`main` GPML
  (fetched via the new `GitHubClient.get_file_content`) as *before*, cached to disk so the preview
  is ready immediately with no CI wait. Serves at `GET /previews/{pr}/{before,after}.svg`
  (locked-down CSP + sandbox so a hostile SVG can't run script); `_review_view` fills the
  dashboard `preview` slot from a cheap disk-based `PreviewService.status()`.
  **CI draws no image at all** (2026-07-27): PinPath was retired once 1a existed, and
  `pr-preview.yml` now converts to **pvjson only** — a GPML `gpml2pvjson` refuses is broken, so
  the `.json` is a validity signal, not a picture. A PR comment cannot embed an artifact anyway,
  and camo refuses SVG, so an image in the PR would need deployment plus a PNG endpoint. The
  app's old artifact-download path was **removed** with it, so `PreviewService` no longer talks to
  GitHub at all. Marvin's call: the PR does not need an image; it carries the validation and metadata tables, and `WPSUBMIT_APP_BASE_URL` (when set)
  links the mirror comment to the dashboard page that holds the render.
  **Alembic is wired** (issue #2): `migrations/` + `alembic.ini`; `create_all` now runs **only**
  for SQLite dev, Postgres deploys run `alembic upgrade head` (`docs/migrations.md`); a test
  asserts zero drift between the migration and the models. Checklist/assign endpoints are now
  curator-gated (403 for non-curators), matching approve.
  The **dashboard/landing UI was redesigned** (issue #7, `templates/` + `static/app.{css,js}`,
  server-rendered Jinja + vanilla JS, served from `/static`): landing/submit stepper, curation
  queue with before/after preview slots, reviewer assignment, per-review detail page. The review
  card lives in `templates/_review_card.html` and is imported `with context` by both pages —
  importing it from `dashboard.html` executed that page's body and rendered its empty state
  against a context with no queue in it.
  **Curator whitelist resolves from a GitHub Team** (issue #9, `app/curators.py`,
  `WPSUBMIT_CURATOR_TEAM='org/slug'`): TTL-cached, fail-closed, `WPSUBMIT_CURATORS` list is the
  fallback. **OAuth token is encrypted at rest** (issue #4, `app/auth/session_tokens.py`, Fernet)
  and `SessionMiddleware` `https_only` is config-driven. **Cluster deployment is authored** (issue
  #5, `Dockerfile` + `docker-entrypoint.sh` + `.github/workflows/{ci,docker-publish}.yml` +
  `docker-compose.yml` + `docs/deployment.md`; image builds/boots, not yet deployed live).
  **The before/after preview says what changed** (issue #24, `app/preview/diff.py`): every data
  node is classified added / removed / re-annotated / relabelled / moved by matching GraphId,
  then label plus type, then database plus identifier, cached as `diff.json` and served at
  `GET /previews/{pr}/diff.json`. The card carries the count sentence server-rendered; the
  overlay colours each hotspot and the panel strikes the previous value through. The overlay is
  also **one tab stop, not one per node** (issue #19): a roving tabindex under a toolbar role,
  arrow keys in reading order, selection following focus into a polite live region.
  **Quality control is one graded ruleset** (`app/quality/`, 2026-08-03). Before it there were
  five, in four vocabularies, and the richest of them never ran: `mvp1/validate_pathway.py`
  grades thirteen checks but ships inside `pr-preview.yml`, which the live target repository has
  never had. `app/quality/rules.py` holds the union — the four reasons `validate_gpml` refuses a
  file for (kept **word for word**: they reach a submitter through `describeError`), the GPML-side
  checks from `mvp1`, and the target repo's own `testing` job (title >= 10 chars, description
  >= 15 words or an edit changing it by <= 3 words / 10 chars, data-node changes) ported rule for
  rule and flagged `predicts_repo`. Severities are `na < pass < warn < fail < block`; `na` ranks
  *below* pass so "nothing to check" cannot win a rollup. **The package must import nothing from
  `app.*` at module scope** — `app.models` imports `app.review.checklist`, which imports this —
  so metadata is duck-typed and the one call into `app.submit.gpml` is function-local; an AST test
  pins it. `validate_gpml` is now defined as the `block` subset, so the portal cannot refuse a
  file for a reason its own report called fine. The report is **cached in the render sidecar**
  (`quality.json`), never persisted: it is a pure function of the GPML and the checklist is
  already the record. Surfaces: `/api/validate` (**which nothing called before** — the submit form
  now posts to it on file choice, so a submitter sees warnings before the pull request exists),
  one "Automated checks" block on the review card that absorbed the old free-floating
  pipeline-failure notice, and a table in the mirror comment. `_render_preview` therefore runs
  **before** `register`, or the first mirror comment has the table missing.
  **An item marked `na` never blocks approval, and one leaving `na` blocks again** — the invariant
  lives in `requirement_for` (`app/review/checklist.py`) and nowhere else (issue #27).
  **The checklist is aligned with the repository's own reviewer checklist** — added
  `interactions_connected`, gave `description_ok` an auto_check. That one can never return `pass`:
  `refresh_pipeline_checks` only writes items still `pending`, so anything the app puts there
  pre-empts the repo's own description check, which is strictly better (it quotes what its
  extractor pulled out, the text that reaches the published page).
  **The repo's `testing` verdicts are read back** off a `<!-- wikipathways-testing … -->` marker
  comment (`parse_testing_marker`), the same device 3a's publish marker already uses, and shown
  beside the app's predictions — a disagreement means the ported thresholds have drifted. The
  workflow step that posts it is staged in `sandbox-workflows/`, **not proposed**, so the field is
  empty on the live target until it is.
  Everything is verified against `FakeGitHubClient` (tests override `get_github_client`,
  `get_bot_client`/`get_bot_optional`, `get_current_user`); the OAuth + App token flows are
  tested via injected `httpx.MockTransport`.

### Commands

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"   # one-time setup
.venv/bin/python -m pytest tests/                     # run all tests
.venv/bin/python -m pytest tests/test_wpid_allocator.py::test_concurrent_allocation_no_collisions  # single test
.venv/bin/ruff check app/ tests/                      # lint
.venv/bin/uvicorn app.main:app --reload               # run the app locally
```

The allocator/lock atomicity rests on the **WPID/pathway being the table primary key** — a
concurrent duplicate insert fails with `IntegrityError` and the caller retries. Do not "optimize"
this into a compute-then-insert without the unique constraint; the race tests exist to catch that.

## What this is

`wikipathways-curator` (provisional name) is a **hosted web app that is the front door for
submitting and curating WikiPathways pathways** now that all content lives on GitHub in
[`wikipathways/wikipathways-database`](https://github.com/wikipathways/wikipathways-database).
It lets anyone submit or update a pathway without touching git: it opens a real pull request
against the content repo, assigns the WPID, and gives curators a review dashboard with a
rendered before/after preview.

The app talks to `wikipathways-database` **purely through the GitHub API as an external client**.
The only code that ships *into* the content repo is one added Actions workflow
(`pr-preview.yml`) that renders + validates GPML on `pull_request`. Do not conflate the two
repos: this repo is the app; the content repo stays a content repo.

## The five problems this solves (from the PR audit)

Every design decision traces to one of these observed failures — preserve the mapping when
changing the design:

1. **Reviewers approve unreadable XML** — reviewable artifacts (SVG, `-datanodes.tsv`,
   `-bibliography.tsv`, validation) are only generated *after* merge → fixed by the PR-preview
   pipeline (MVP-1).
2. **Manual out-of-band merges** → fixed by dashboard approve-that-merges.
3. **Unmergeable concurrent GPML edits** (GPML is XML + layout, does not line-merge) → fixed by
   the check-out lock + never line-merging GPML + always branching off latest `main`.
4. **Malformed new submissions** (no WPID, wrong filename) → fixed by app-owned naming/layout.
5. **WPID collisions** (next-id computed only over the merged tree) → fixed by the atomic
   allocator computing `1 + max(WPID)` over **repo tree ∪ open PRs ∪ live reservations**.

## Architecture (as planned)

- **Two GitHub identities, deliberately** (`app/auth/`):
  - **Per-user OAuth** — pushes the branch / opens the PR *as the submitter*, so authorship is
    real and attributed, and the user never runs git.
  - **GitHub App (bot)** — privileged cross-cutting actions the user token must not do: posting
    the preview comment, merging on curator approval, receiving webhooks (PR opened/closed → to
    expire locks).
- **The transactional core is the registry** (`app/models/`, `migrations/`) — this is the one
  place that cannot be sloppy:
  - `wpid_reservation` — allocation is an `INSERT` of `max+1` (over tree ∪ open PRs ∪ live
    reservations) **inside one transaction**, so simultaneous submissions cannot collide.
    Unmerged reservations expire and return the ID to the pool.
  - `pathway_lock` — one open edit per pathway; acquiring is a conditional upsert that **also
    scans GitHub for an open PR touching that pathway** and refuses if one exists (power users
    can bypass the app with a raw PR). Locks auto-expire; curators can force-release.
  - `review` — dashboard approval state; the single source of truth the read-only PR comment
    mirrors.
- **Two review venues, one source of truth:** the app dashboard is the reviewer's home
  (before/after render, checklist, approve-that-merges); the same preview + checklist is
  mirrored as a **read-only** PR comment on GitHub. Approval always flows through the app so the
  two never diverge.
- **Merge model:** GPML is the single source of truth and is **never line-merged**; derived
  files (`*.json`, `*.md`, `*.tsv`, `*-thumb.png`) are regenerated, never hand-reconciled.

## Locked stack decisions (from scaffolding-plan §0)

- **Backend:** Python + **FastAPI** (async, OpenAPI, clean OAuth/webhook handling).
- **GitHub client:** `githubkit` or `PyGithub` + `httpx`.
- **Datastore:** **PostgreSQL** + SQLAlchemy + Alembic (SQLite acceptable for MVP-2 dev only).
- **Frontend:** server-rendered templates + light JS; defer any SPA until the MVP-4 dashboard
  warrants it.
- **Tooling:** `uv` + `ruff` + `pytest`.
- **Deploy:** Docker → GHCR → Strato cluster service, Traefik-routed, GlusterFS-backed data.

The proposed `app/` layout (auth, github, wpid, locks, submit, review, models) is in
scaffolding-plan §1 — follow it when scaffolding.

## Build phasing — build in this order, each phase independently shippable

- **MVP-1** — PR-preview pipeline. Ships to `wikipathways-database` as `pr-preview.yml`, **not
  this repo**. Reuses the existing render/metadata generators from `on_gpml_change.yml` (subset:
  render + datanodes + refs + validation), writes a preview artifact + PR comment, does **not**
  commit derived files or push to sister repos. Highest leverage, smallest build, no app needed —
  do this first, in parallel with scaffolding.
- **MVP-2** — Submission app for new pathways: OAuth, atomic WPID allocator (write the race test),
  naming/layout, PR creation, metadata capture.
- **MVP-3** — Updates + check-out lock, branch-off-latest.
- **MVP-4** — Curation dashboard, checklist, approve-that-merges, reviewer auto-assignment,
  curator whitelist (~20 people; GitHub Team vs repo-tracked config is an open decision).

## Deployment context

This deploys to the VHP4Safety Strato Docker Swarm cluster (see the user's global instructions
and the cluster docs at `/mnt/gluster/documentation/` on `tgx1`). Follow cluster conventions:
image built by CI → GHCR so both swarm nodes can pull (real failover), `core` overlay network,
GlusterFS-backed data at `/mnt/gluster/docker/<service>/data`, **no node pinning**, secrets as
Docker secrets (never in the repo). The app needs a GitHub App identity installed on
`wikipathways-database` with contents RW, pull_requests RW, and issues/comments RW.

## Current state (2026-08-20)

**The app and its testbed are moving into the `wikipathways` org.** The maintainers agreed the
fork's work can be merged into the org's own sandbox repositories, so the decision was *merge
upstream*, not *create new repos*. The app repo moved; the three content merges are open and
waiting on people.

**`wikipathways/curation-portal` is the app repo now** (transferred from `marvinm2/pathway-portal`,
2026-08-20). 27 issues and 3 pull requests came with it, the old URL redirects, and `origin` here
points at the new one. Two consequences worth knowing before touching it:

- **Marvin is `push` on it, not `admin`.** The transfer did not carry admin over, so repository
  settings and **Actions secrets** need an org owner. That matters immediately: see the GHCR note
  below.
- **`docker-publish.yml` pins the namespace to `marvinm2`** rather than deriving it from
  `github.repository_owner`. Left alone it would have started publishing to
  `ghcr.io/wikipathways/wikipathways-submit`, which **defaults to private**, and the swarm
  service pulls `ghcr.io/marvinm2/…` — so both nodes would have failed to pull with nothing in
  the repo explaining why. A workflow token's permissions stop at its own repository, so once the
  org owns this repo its `GITHUB_TOKEN` **cannot** write a user-owned package; the login step
  takes `GHCR_USER`/`GHCR_TOKEN` if present. **Creating those secrets needs the admin Marvin does
  not have.** Renaming the package is the better end state, but as its own change with a redeploy
  beside it.

> [!warning] **CI cannot publish an image right now.** Measured, not predicted: run
> `32391162408` (commit `1478690`, the first push after the transfer) failed with
> `denied: permission_denied: The requested installation does not exist` pushing to
> `ghcr.io/marvinm2/wikipathways-submit`. The prediction was that a workflow token's reach stops
> at its own repository, and that is what it looks like from the inside.
>
> **The fix I first recommended does not exist.** The idea was that the package is owned by the
> *user*, so its own Actions-access list could grant the org repo write without needing repository
> admin. Checked in the browser on 2026-08-20: the package's **Add Repository** picker lists only
> repositories owned by `marvinm2` — his own and his forks, `marvinm2/wikipathways-database`
> included — and **no org repository at all**. A user-owned package cannot be granted to an
> org-owned repository. (Its filter box is also broken: it returns "No repositories found" for
> `AOPWikiRDF`, which is visibly in the unfiltered list. Scroll the list; do not trust the filter,
> and do not conclude anything from an empty result.)
>
> **Every remaining path needs an org owner**, so the smallest ask is the last one:
>
> 1. `GHCR_USER`/`GHCR_TOKEN` secrets on `wikipathways/curation-portal` — the workflow already
>    reads them. Creating a secret needs repository **admin**.
> 2. Publish to `ghcr.io/wikipathways/curation-portal` instead and make the package public, then
>    redeploy the swarm onto the new digest. Publishing works with `GITHUB_TOKEN` because the
>    owner then matches; setting a new org package's visibility does not.
> 3. **Ask an org owner for admin on `wikipathways/curation-portal`.** That unblocks either of the
>    above without another round trip, and it is the access Marvin had before the transfer.
>
> **Nothing is down.** The swarm runs a pinned digest (`sha256:f924cdf1…`), which still exists;
> `upload.wikipathways.org` and `sandbox.wikipathways.org` both answer 200. Only *future*
> publishes are blocked, so this costs nothing until the next deploy.

**Two pull requests are open against the org, both content-free.**

| | PR | state | why |
|---|---|---|---|
| `sandbox-wp-db` | [#77](https://github.com/wikipathways/sandbox-wp-db/pull/77) — the four workflows | BLOCKED | `main` is protected and Marvin is a `push`-only collaborator |
| `sandbox-wp.gh.io` | [#2](https://github.com/wikipathways/sandbox-wp.gh.io/pull/2) — `assets_base_url` + `baseurl` prefixes, 11 files | CLEAN | mergeable; he has `maintain` |

> [!warning] **PR #2 was a 999-page regression until it was measured on the live site.**
> The `assets_base_url` indirection is right; the value was not. Pointed at
> `wikipathways/sandbox-wp-assets`, it would have repointed **1000** pathway pages at a repository
> holding exactly **one** pathway (WP554) — `.../sandbox-wp-assets/main/pathways/WP1/WP1.svg`
> answers **404** where production answers **200**.
>
> Measuring it found the opposite bug. The download links are root-relative
> (`/wikipathways-assets/…`), so they resolve against whatever host serves the site:
> `sandbox.wikipathways.org/wikipathways-assets/pathways/WP1/WP1.png` → **404**, on every one of
> those 1000 pages. Only the diagram works, because that single URL was written absolute. The
> default is the production host now, so diagrams are untouched and the downloads start
> resolving — a repair rather than a regression, and a fork still overrides the one value.
>
> The general form: **a config default that is right for the deployment you are working on can be
> wrong for the repository you are sending it to.** The fork needed its own assets host; this
> repository needs the one it already had.

**The test pathways are not being transferred** (decided 2026-08-20). #76 and `sandbox-wp-assets`
#1 are **closed**. Nothing original was in them: five of the nine are the same insulin demo
fixture, and WP5427 / WP5428 / WP5429 are copies of pathways that already exist, re-published
under sandbox identifiers. The reason that only appeared on inspection is better: **#76 also
replaced WP1001**, which upstream is *Peptide GPCRs* with 80 data nodes and which the fork had
overwritten with the 3-node insulin demo while using it as an update-test target. The pathways
stay on `marvinm2/sandbox-wp-db`.

> [!warning] **"No branch protection" was read off a 404, and the 404 meant "you can't see it".**
> `GET /repos/{o}/{r}/branches/main/protection` **requires admin**, so a non-admin gets 404
> whether protection exists or not. `sandbox-wp-db`'s `main` **is** protected
> (`GET .../branches/main` → `protected: true`); `sandbox-wp.gh.io` and `sandbox-wp-assets` are
> not. Use the `branches/{b}` endpoint, not the `protection` one, when you are not an admin.

**#76 cannot make its own check pass, and that is not a flaw in it.** Workflow 1 runs on
`pull_request_target`, which always executes the copy on the **base** branch. #76 adds `.gpml`
files, so it triggers workflow 1, and `get-gpml` dies at its first step with *"Refusing to check
out fork pull request code from a 'pull_request_target' workflow"* — which is precisely the
defect #76 repairs. Hence the split: **#77 touches no GPML, so workflow 1 never fires on it**;
merge #77, then re-run #76. The workflow files are byte-identical in both, so #76 becomes
content-only afterwards. Every one of the org repo's last five workflow-1 runs is a failure, all
predating this, so the repository has not processed a pull request in some time.

**Which copy of each workflow is the right one is not uniform, and getting it wrong is silent.**
Measured 2026-08-20 with the owner normalised out of the `repository:` inputs:

| file | take | why |
|---|---|---|
| `1_on_pull_request.yml` | **the fork's** | the two `refs/pull/N/head` checkouts were removed *as a change on the fork* and never written back, so `sandbox-workflows/`'s copy still carried them. Backported now. |
| `3a`, `on_gpml_change` | either | **identical** to the staged copies. The 08-14 note that the fork was behind on three 3A changes is out of date. |
| `pr_label_dispatcher` | **the staged** | the fork's passes `pr_number` for the `resubmitted` case; workflow 1's input is `manual-pr-number`, so that label has never worked on the fork. |
| `3b` | the org's | the fork differs only by its self-pointing `repository:`. |

The site merge carries **one** fork-specific file, `_config.yml`, and it is not a plain revert:
the org's `baseurl: ""` / `url: sandbox.wikipathways.org` are restored, but the fork's added
`assets_base_url` is **kept**, repointed at `wikipathways/sandbox-wp-assets` — the layouts
hardcoded the production assets host, so a published pathway showed a broken diagram on every
non-production deployment, the org's sandbox included. The `{{ site.baseurl }}` prefixes render to
nothing with an empty baseurl and are correct either way. 275 draft files keyed to the fork's pull
request numbers were dropped, 23 of them `_drafts/*.md` that the site **serves as pages** — the
collision argument for dropping them was wrong (the org repo is already at PR #75, above every
fork slug), the ghost-pages one is not.

> [!warning] **A merge branch built by `--diff-filter=A` deletes things you did not look at.**
> The fork had deleted upstream's own `draft_assets/WP0__PR10` — its PR #10, not the fork's,
> consumed by coincidence of numbering — and rename detection then paired that deletion with a
> fork addition, hiding a `WP0__PR24` file from `--diff-filter=A` as well. Both only showed up
> under `git diff --no-renames --name-status upstream/main...HEAD`, which is the check to run:
> **the branch should delete nothing.** That took the diff from 300 files to 45, and dropping
> the test content took it to 11.

### Still to do, in order

1. Someone who can push to protected `main` merges **#77**. **#2** can go in any time.
2. **Then** the cutover: drain the 8 open pull requests on `marvinm2/sandbox-wp-db`, confirm the
   GitHub App covers `wikipathways/sandbox-wp-db` (see below — it may already), and
   repoint the live service — `PORTAL_CONTENT_REPO=wikipathways/sandbox-wp-db`,
   `PORTAL_DRAFTS_REPO=wikipathways/sandbox-wp.gh.io`,
   `PORTAL_DRAFTS_SITE_BASE_URL=https://sandbox.wikipathways.org`. No image change, no migration.
3. `ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY` **does not exist** on `wikipathways/sandbox-wp-db` (it has
   `ACTIONS_SANDBOX_DEPLOY_KEY` and `PICOPAT`), and Marvin has `pull` on the assets repo, so he can
   create neither the deploy key nor the secret. Until an owner does, 3a's assets push upstream
   stays credential-less — defect 4 in `docs/sandbox-pipeline.md`, unchanged by the move.
4. Prove it the usual way: one submission end to end against the org repo, approved **at the
   dashboard button**. Expect the assets push to be the one red step until 3 is done, and confirm
   it is the *only* one rather than assuming.

### The GitHub App does not need rebuilding, but it does need an owner

Measured 2026-08-20. `wikipathways-submit-bot-dev`, **App ID 4403728**, owned by the **user
account** `marvinm2`, permissions metadata read + contents/issues/pull_requests write, events
`pull_request`.

- It is installable on **any** account, and it is **already installed on the `wikipathways` org**
  — installation **149425545**, scoped to **"Only select repositories"**. So the cutover does not
  need a fresh installation, which the earlier to-do assumed.
- **Adding a repository to it is a `request` for Marvin, not an action.** Every org repository in
  that installation's picker carries an orange `request` badge, because he is a member rather than
  an owner. An owner approves.
- `sandbox-wp-db` **did not appear** in that picker while `sandbox-wp-assets` and `sandbox-wp.gh.io`
  did, both as `request`. These pickers list what is *not yet* selected, so that probably means
  `sandbox-wp-db` is already in the installation — **probably, not confirmed.** Reading the
  selected set needs the org settings page, and the end-to-end test settles it for free either way.
  (This one is stated as a guess on purpose: inferring from absence in a GitHub picker is exactly
  what went wrong with the package's Add Repository dialog earlier the same day.)

**Whether it should be *owned* by the org is a separate question from whether it works.** As it
stands the App dies with Marvin's account, org owners cannot manage it, and every comment and
merge it makes reads `app/wikipathways-submit-bot-dev` — a name with `-dev` in it, on the org's
front door. GitHub supports transferring a GitHub App to an organisation, which keeps the App ID,
the private key and the installations, so it costs no secret change and no redeploy; minting a
fresh org-owned App instead means a new App ID, private key and webhook secret, so three Docker
secrets and a redeploy. The **OAuth App** (`Ov23lig1IHGpNd2Y4l7u`) is functionally unaffected by
any of this — its tokens are account-wide and do not care which repository is the target — but it
is likewise a personal app, and it is what a submitter sees on the consent screen.

**Admin on `wikipathways/curation-portal` fixes the GHCR publish and nothing else.** The other
three blockers are all on `wikipathways/sandbox-wp-db`, a different repository: merging #77 past
the protected branch, the assets deploy key, and the App's repository selection. One ask to an
owner should cover all of them at once.

**After the cutover Marvin stops being the owner of the content repo**, so his own submissions go
down the ordinary fork path rather than the never-fork-your-own-repo shortcut. His fork already
exists, so `ensure_fork` returns it. `test_the_owner_of_the_content_repo_never_forks_it` keeps its
own fixture names rather than borrowing a live one that has to keep up.

## Previously (2026-08-14)

**A published pathway did not say which pathway it was.** `pathways/WP5429/WP5429.gpml` on the
content repository declared `Version="WP0001_r20260813082819"` — the placeholder it was uploaded
under. So did WP5426, WP5427 and WP5428: every pathway this portal has ever published. Beside the
GPML, `WP5429-info.json` said `"wpid": "WP0__PR37"` and the pvjson and the SVG carried the draft
slug throughout. Only the `.md` page was right, because 3A `sed`s that one file and nothing else.

The cause is a seam nobody owned. Workflow 1 names every product after the draft file
(`WP0__PR<n>`), the portal writes `WP0001` into the GPML's `Version` — honestly, since at upload
time the file *is* `WP0001.gpml` — and 3A's `Rename and Move Files` renames without opening. It
has to be fixed there and nowhere else: **the publish push is made with `GITHUB_TOKEN`, so it
starts no further workflow run**, and nothing downstream ever re-derives those files.
`docs/sandbox-pipeline.md:472-481` predicted this in writing weeks ago and left it as an open
question; it is measured now.

Fixed in the staged 3A (`sandbox-workflows/`, README items 9 and 10) with two substitutions —
the draft slug for the generated products, and a short `python3` rewrite of the GPML's `Version`
that keeps the revision and replaces only the `WP<n>` half, so it is a no-op for an edit. Exercised
against the real WP5429 artifacts plus a GPML with no `Version`, one with a non-`WP` `Version`, one
with an empty one, and one with no `<Pathway>` root — the last stops publication rather than
guessing.

**The publish commits were also anonymous**, `GitHub Action <action@github.com>` under the
`actions-user` account for all of WP5425–WP5429: the person who drew the pathway appeared nowhere
in the history of the repository holding it. 3A now resolves the pull request's author and commits
as them, author *and* committer, at `<id>+<login>@users.noreply.github.com` — the address GitHub
writes on its own web commits, which resolves regardless of the account's email-privacy setting and
is what makes the commit count as a contribution. A bot author (portal in `bot` identity) falls
back to the action rather than guessing. Resolved correctly against PRs #37, #39 and #34 on the
fork; `git pull --rebase` was checked to preserve the identity.

**The app now reads the published file back.** `_published_file_note` replaces `_wpid_is_on_main`
and asks the bytes it was already fetching what they say they are. Four publications went by before
anybody opened one — the same house failure as everywhere else in this file, and reverting the
check makes the new test fail, which is the only thing that makes it worth having.

> [!warning] **A repair staged here does nothing until it is applied to the fork.**
> PR #37 published correctly on 2026-08-13 and was then labelled `publish failed`, because
> `gh pr close` cannot close a pull request somebody merged by hand. That was diagnosed on
> 2026-07-30 and repaired in the staged 3A the same day — and the fork never got it, so it
> recurred six weeks later. `diff` the staged file against the fork's before believing any repair
> in `sandbox-workflows/` is live. Three are outstanding as of 2026-08-14.

Two things about PR #37 itself worth keeping. **It was merged by hand** rather than left to close,
which put `WP0001` on `main` (two `on_gpml_change` runs died on it) — the app's
`_repair_stray_placeholder` cleaned up, so the four-layer defence from 2026-07-30 held. And
`on_gpml_change`'s `sync-database-repo-deleted` job fails with `pathspec … did not match any files`
when the path is already gone; `git rm --ignore-unmatch` would make it idempotent. That workflow is
not staged in this repo.

The same script fixes two smaller things in the tag it already has open. **`Last-Modified` is
refreshed** from the revision the `Version` carries, so the two agree and the stamp is when the
pathway was last edited rather than when it was approved — WP5429 shipped with a 2022 timestamp
because nothing had ever refreshed it. **`Data-Source` is filled in when absent**, never
overwritten: a file that records real provenance has to keep saying so, and this step cannot tell
that apart from an omission, so not overwriting removes the question. Checked with a
`Data-Source="Reactome"` file, which comes through untouched.

WP5426–WP5429 are **not** being repaired; the decision was to fix forward. Merging a pipeline pull
request stays mitigated by the mirror comment alone — draft pull requests and branch protection
were both considered and declined, so a hand-merge remains possible and its collateral (below) is
still reachable.

**`on_gpml_change.yml` is now staged too**, for its `sync-database-repo-deleted` job: `git rm
pathways/WP<n>/*` exits **128** when the path is already gone, and that is the ordinary case, not a
race — the app's own `_repair_stray_placeholder` removes the placeholder, and the commit that
removes it is the commit this job reacts to. So every hand-merge produced a red run *because the
repair worked* (run `31712062937`). `git rm -r --ignore-unmatch`, measured both ways in a throwaway
repository. Note this file is staged as **the fork's copy**: three of its four sync jobs are
stubbed out for the sandbox and must not go upstream — see the warning in
`sandbox-workflows/README.md`.

Raised and deliberately **not** changed: the same job ends `git push --force` onto `main` of the
content repository from a `fetch-depth: 1` checkout. There is no observed failure to measure a fix
against, and guessing at a forced push to the branch holding every published pathway is worse than
leaving it documented.

**Proven end to end, and the fix is measurable: WP5430, PR #42.** `mmarvinm2` submitted through
the portal; cross-repository pull request authored by them; workflow 1 all ten green under a real
`pull_request_target`; a revision landed on **their own fork** and went ten-green again on
`synchronize`; Approve pressed **at the dashboard button**; `accepted` label → dispatcher → 3A
green on every step including the new `Resolve the submitter` and a clean `Close PR`; pull request
closed **unmerged** with `published` and **no** `publish failed`; and the app settled itself over
the webhook to `status=published, wpid=5430, approved_by=marvinm2`.

Against yesterday's publication, same repository, one day apart:

| | WP5429 | WP5430 |
|---|---|---|
| `Version` | `WP0001_r20260813082819` | `WP5430_r20260814093636` |
| `Last-Modified` | `20220717141800` | `20260814093636` |
| `Data-Source` | absent | `WikiPathways` |
| `info.json` wpid | `WP0__PR37` | `WP5430` |
| draft slug in pvjson/SVG | present | none anywhere |
| publish commit | `GitHub Action <action@github.com>` | `mmarvinm2 <312958610+mmarvinm2@…>` |

WP5425–WP5429 are all `GitHub Action`. WP5430 is the first publication in this repository
attributed to the person who made the pathway.

### Four defects found by *using* the portal, none of which a test could have surfaced

The pattern is worth more than the individual bugs: every one lived in a surface nothing renders,
follows or asserts on from inside the app.

- **The welcome comment sent every submitter to a dead link.** It linked `/reviews/{pr}`; there
  has never been such a page (`/api/reviews/{pr}` is JSON, the HTML route is `/dashboard/{pr}`).
  Every submission since welcome comments were added carried it, and the mirror comment directly
  beneath had the right URL the whole time — which is exactly why the difference was invisible.
  `tests/test_comment_links.py` now checks every link in every rendered comment against the app's
  own route table, so the next comment gets the guarantee for free; asserting on the comment text
  would only have pinned that day's string.
- **`content.references` counted uncited references.** It counted every `<bp:PublicationXref>`
  rather than the ones a `<BiopaxRef>` cites. The review page therefore said "Open the 1 reference
  listed above" directly over a checklist item saying "The GPML declares no literature references"
  — and the checklist was right. `cited_reference_count` already existed and `main.py` already
  used it for the required item; this rule never did.
- **A pipeline verdict could not correct itself after a revision.** `refresh_pipeline_checks` only
  wrote items still `pending`, though its docstring has always said "pending **and** auto-derived".
  Once it wrote `fail`, the item was no longer pending — so a submitter fixing exactly what it
  complained about, and a fresh run agreeing, left the failure standing. `auto` is the right test
  and already means "nobody answered by hand"; `_merge_checklist` uses it for the same question.
- **A curator's override kept the note that contradicted it**, so the item read "References
  resolve — pass" above "The pipeline resolved 0 of the 1 reference". The note is kept but marked,
  not cleared: what the pipeline saw is evidence, and a curator overruling it is the interesting
  part of the record.

**And the demo fixtures cited papers they are not.** PMID `12829793` is a colon-cancer paper, not
"IRS proteins and the common path to diabetes" (`12169433`); `17635937` is about EGF-stimulated
migration, not "AKT/PKB signaling: navigating downstream" (`17604717`). Both also declared their
references with **no `<BiopaxRef>` citing them**, so every downstream generator saw none — which is
where the same defect in the verification fixture came from, and why the references rule had never
been exercised against a file that cites anything. Fixed in `demo/`.

> [!note] Approving honestly is what surfaced two of these
> The first upload's checklist could not be completed truthfully — its interactions were unanchored
> floating lines and its one reference was uncited — so the gate refused, correctly. Recording a
> false `pass` to make the run go green would have hidden both the stale-verdict bug and the
> orphan-reference bug, and proved nothing about publication. Fixing the *fixture* and re-uploading
> was the shorter path as well as the honest one, and it exercised revise-on-fork for free.

540 tests. Live at `sha256:f924cdf1…` (from `7581b2d`), deployed and verified **against behaviour**:
`/dashboard/42` returns 200 while `/reviews/42` still 404s, and `POST /api/validate` on an
orphan-reference probe returns the new `warn` with the new wording. Rollback target
`sha256:8fe50ea5…` — a plain digest change, no migration and no new secret.

### Previously (2026-08-03) — `docs/session-handoff-2026-08-03.md`

That is the read-me-first handoff. It supersedes the 07-29 one, which remains the account of the
deployment, the fork's draft pipeline and the first publication. In short, since then: quality
control was consolidated into one graded ruleset (`app/quality/`) that runs at upload time and is
mirrored to the pull request; the app's checklist was aligned with the reviewer checklist the
target repository appends to every pull request; and the two systems can now read each other's
verdicts over a marker comment, proven end to end on the fork. A missing root `<Graphics>` canvas
was identified as a hard crasher for the repository's `metadata` job and is now a `fail` rule.

Then, in a second round the same day, three of the five open audit issues were closed: the render
cache is freed at every terminal transition and swept as a backstop (#18 — it had been leaking on
`_settle_publication`, which in pipeline mode is *how a submission succeeds*, and a second
unswept cache turned up beside it under `preview-cache/drafts`); the curation queue pages at
twenty (#17); and one account may open ten pull requests an hour, counted out of the `review`
table so it survives a redeploy (#21). **#22** (fork-per-submitter) and **#23** (TTL tuning)
remain open — the first needs a decision and a broader OAuth scope, the second needs real
submission data that does not exist yet.

Then **#23** closed too: the three timers are set from measurement rather than guesswork (the
publish workflow has succeeded since the issue was filed, and 53 real pull requests on the content
repo give the lock and reservation lifetimes), and every expiry now logs how long the thing was
held so the numbers can be corrected again later.

In a third round the same day, two things:

- **A GPML with no declared encoding converts to nothing.** `gpml2pvjson` returns **zero bytes and
  exit status 0** for a file whose XML declaration omits `encoding="UTF-8"`, or that has none, so
  the target repo's `json-svg` job dies one step later in `JSON.parse`. It had killed the last
  three new-pathway runs on the fork while updates went green. The app was passing the declaration
  through verbatim; `assign_wpid_str` now writes it, which is the one choke point all three write
  paths share. **Third instance of the house failure mode**: the app's own renderer draws such a
  file happily, so only the real pipeline objects — see also the missing root `<Graphics>` canvas.
- **Step 3 of #22 is built** (`find_open_pr` takes a head repo; the lock scanner only treats a
  same-repo head as "one of ours"; `Review.head_repo`; revise scopes its branch-side writes to the
  head repo). These were latent correctness bugs for any cross-repository pull request today, and
  they turn **no fork mode on**. **#22** itself stays open: it is a design decision about whether
  the user OAuth token goes back into the write path, and its remaining steps need a person.

In a fourth round the same day, the two issues that came out of driving the fork closed, leaving
**#22 as the only open issue**:

- **An N/A on a required checklist item wedged approval (#27).** `is_complete` demands `pass` on
  every required item, so a required item at `na` was a gate nothing could open — waiting does
  nothing because `na` is already an answer, and a re-upload re-derives it. Two separate faults.
  The verdict: `references_valid` auto-resolved to `na` for a pathway declaring no references,
  which is the wrong word when the repository's own reviewer checklist asks for at least one; it
  is `warn` now, reading as `pending`. The rule: three writers each decided independently whether
  an item blocks and only `build_checklist` was right. `requirement_for` is the single answer, and
  it reads **both ways** — an item leaving `na` gets its requiredness back, without which a
  curator clicking N/A then Fail would leave a failed required item blocking nothing. The disabled
  button now names what is outstanding, in the template and in `recomputeApprove`.
- **An interaction with no `LineThickness` kills the `metadata` job (#26)**, in
  `readLineStyleProperty`, measured one variable apart (runs `30827814897` / `30829825691`).
  `gpml.line_thickness`, severity `fail`, sibling of `gpml.board`; `<GraphicalLine>` is checked
  too because `readLineElement` is shared. All three `demo/pathway_*.gpml` carried the defect and
  now declare a thickness. **Fourth instance of the house failure mode** — and the quality
  fixture named `GOOD` turned out to carry no references at all, so the "clean pathway" test was
  passing a file missing something the repository asks for. It has one now.

Both were driven through the demo in the browser (`WPSUBMIT_DEMO_FAKE=1`, so a fake GitHub client
and not the live service): submit, the upload-time report, the checklist, the N/A → Fail → Pass
round trip on the gate, and an approve that merged.

**Real published pathways are now in the suite** — `tests/fixtures/published/` +
`test_published_pathways.py`. Read that directory's README before adding a rule. Hand-written
fixtures encode what their author already knew to include, which is why the same class of defect
got through five times; these two are verbatim WikiPathways content and act as the negative
control, since **no rule may report `fail` or `block` on a file the project itself published**.
Measured while adding them, by running the ruleset over 30 sampled pathways: `gpml.line_thickness`
and `gpml.board` fire on **none** of them, which is the evidence real PathVisio output always
writes both; `content.datanode_annotation` fails on **21 of 30** and `content.references` warns on
**6 of 30**, so a rollup over real content is not a health score.

**Fork mode is live and the authorship problem is fixed, measurably.** Same repository, an hour
apart: PR #20 under `bot` was authored by `app/wikipathways-submit-bot-dev`; PRs #21 and #22 under
`fork` by **`marvinm2`**. Both took the *owner* branch rather than actually forking — the target is
`marvinm2/sandbox-wp-db` and he owns it — so **the fork path itself is still unproven against live
GitHub**. Its exit condition is one submission by anybody who is not Marvin, which exercises
`ensure_fork`, the cross-repository pull request and the head-repo plumbing at once. #22 stays open
until then, deliberately: this repo has been bitten four times by "the fake agreed and reality did
not". What *is* confirmed against the real API: `POST /forks` on an already-forked repo returns the
existing fork with `full_name` intact and creates nothing.

**#22 is closed. A real third-party submitter published through the fork path, 2026-08-04.**
`MadhushriMSV` submitted through the portal; `MadhushriMSV/sandbox-wp-db` was created by
`ensure_fork`, her pull request was `isCrossRepository: true` and **authored by her**, workflow 1
went green on all ten jobs, the `accepted` label dispatched 3A, and it **published as WP5427** with
the pull request closed unmerged and the app settling itself over the webhook. Eight artifacts on
the content repo plus the assets repo. The first fork pull request that repository has ever
processed, and the contribution history is no longer flattened onto one account.

> Approval was applied as a **label**, not through the dashboard button, and deliberately: her
> checklist carries a real failure (17 of 88 data nodes unannotated) and the gate correctly refuses
> to open — issue #27's fix working. Recording a false `pass` on somebody else's submission to make
> a test go green would have been the wrong trade; the Approve button was already proven on PR #20.

**Revise on a fork pull request is proven (2026-08-04, PR #28).** A dedicated test account
(`mmarvinm2`, no repos, not a curator) submitted: the fork was created and the branch cut one
second later, the pull request was cross-repository and **authored by that account with no
fallback**, `head_repo` was captured, a curator's change request reached GitHub, and the account's
re-upload landed a second commit on **its own fork** — the exact path the `head_repo` bug broke.
The `synchronize` event that revision raised then ran workflow 1 **all ten jobs green under
`pull_request_target`**. Only **update-in-fork** remains untested.

> [!important] Two lessons from that pass, and I committed the second one myself
> **A fresh OAuth grant writes; a stale one may not.** The consent screen for a new authorisation
> reads "read and write all public repository data … Code", so the app asks for the right scope.
> Signing out of the *portal* does not re-prompt — GitHub reuses the existing grant — so the remedy
> for a submitter whose writes are refused is to **revoke the app** at
> `github.com/settings/applications` and authorise again, not to sign in again.
>
> **`workflow_dispatch` does not test a fork pull request.** A hand re-run executes in the base
> context with a full token, so the `checkout@v6` refusal never fires; only the real event
> exercises it. The first "all ten green" on a fork was a manual re-run and proved nothing about
> the trigger that mattered — which was not noticed until a second checkout turned up.
>
> And that second checkout is the sharper lesson: `update-pr-desc` also checked out the fork head,
> and was missed because the audit read **six of the eight** checkouts in the file and generalised.
> That is the *same* sample-excludes-the-case error recorded twice above, committed hours after
> writing it down. **Grep for the pattern; do not read the instances you happen to open.**

> [!warning] **A submitter's token can read and not write, and the app cannot tell.**
> The day after publishing successfully, the same submitter's next two uploads both died with a
> 502: the fork resolved, the base was read, and `POST /git/refs` came back **404**. GitHub answers
> a write you may not make with 404 rather than 403, and `create_branch` did not name the
> repository — so the only report that reached a human read
> `create_branch(update/WP5427) failed: 404` and could not say whether the app had aimed at the
> fork or the base. Diagnosis was by elimination: a submission as Marvin at the same moment
> succeeded, so the write path was fine; and a ref *can* be created in a fork at a parent-only SHA
> (probed directly on a fork 24 commits behind), so it was not drift.
>
> **That attribution was wrong. So were the three that replaced it** — recorded in order, because
> the sequence is the lesson (2026-08-04):
>
> 1. *Token staleness (issue #28).* Falsified: after revoking the app and authorising again, a
>    **freshly granted** token with the same scopes was refused identically, as was an independent
>    classic PAT on the same account. #28 is real and still open; it is not this.
> 2. *A restricted new account.* `mmarvinm2` was two hours old, so this fitted — until
>    `MadhushriMSV`, a 2021 account with eight repositories that had **published through this app
>    the same morning**, failed in exactly the same way.
> 3. *Insufficient scope.* `POST /git/refs` answers `x-accepted-oauth-scopes: repo` while the app
>    requests `public_repo`, which looked decisive. It is a red herring: a `public_repo`-only PAT
>    created refs on both a normal repository and a fork, **201** each time. **Do not widen
>    `oauth_scope`** — it would force every submitter to re-authorise for nothing.
>
> **What it actually is: which commit the ref points at.** GitHub answers ref creation three ways,
> all three measured — an object from another network entirely gives **422 Object does not exist**;
> the repository's own object gives **201**; an object *readable through the fork network but not
> the fork's own* gives **404**, indistinguishable from a permission denial. Every failure was the
> third. `a4bc119` (the parent's head) reads back fine from `mmarvinm2/sandbox-wp-db`, whose own
> head was `828ba1f`, and `GET /repos/...` truthfully reported `push: true` the whole time.
>
> **So the correlation was fork age, not account age.** A fork created seconds ago is level with
> its parent, so the base commit *is* its own and the write succeeds — which is every success in
> the record (07:22 and 11:45). Once the parent moves ahead, every submission points at a commit
> the fork does not hold. The web UI is unaffected because it branches from the fork's own head.
>
> **And it is largely an artifact of the sandbox.** `wikipathways/wikipathways-database` is a
> network **root**, so in production a submitter's fork is first-level, its source *is* the content
> repository, and `merge-upstream` syncs it correctly. The sandbox target is itself a fork, so
> submitters get forks **of forks**, where `merge-upstream` aims at a third repository and a direct
> ref update cannot substitute.
>
> **Fixed on 2026-08-05 by not needing the sync at all: a fork's branch is now cut from the
> fork's own head** (`WriteTarget.base_repo`), which is native by definition and therefore legal
> on any topology. `_sync_fork` also picks its method from the topology now and still runs, but as
> an optimisation that keeps the base recent rather than a precondition. A stale base is safe
> *here* because GPML is never line-merged and a pull request's diff is computed against the merge
> base — so the "silent revert" the old note feared was not a real risk either. Live at
> `sha256:f3736681…` (from `76df5cb`); rollback target `sha256:15fefbdd…`, a plain digest change
> with no migration and no new secret.
>
> `FakeGitHubClient` could not express any of this, so 509 tests agreed with a write path GitHub
> rejects — **the sixth time the fake has been the more capable of the two**. It now refuses a ref
> at a commit the repository does not hold and takes `fork_can_sync=False`. Confirmed by reverting
> the fix and watching two tests fail, which is the only thing that makes a regression test worth
> having: the reverted code passed the *previous* suite in full.
>
> **Proven live the same day — PR #35, and #29 is closed.** `mmarvinm2` updated WP5427: the pull
> request is cross-repository and **authored by that account with no fallback**, the commit's
> parent is `828ba1f` (the fork's own head, not `a4bc119`), the diff is **one file** despite the
> fork being a commit behind, and workflow 1 went green on **all ten jobs** under a real
> `pull_request_target` event. The fork was in exactly the state that produced the 404, so this is
> a before-and-after on the defect rather than an argument from a different account. **The sync
> still fails in this topology and no longer matters** — both log lines appear and the submission
> succeeds, which is what demoting it to an optimisation was for. All three write paths —
> submit, revise, update — are now proven from a contributor's fork.
>
> `mmarvinm2` was **never restricted**, contrary to what was written here yesterday. Every failing
> probe had named `a4bc119`, the one object GitHub refuses, so none of them said anything about
> the account.
>
> The reusable lesson is the shape of the list above: four consecutive explanations each fitted
> every observation available when it was formed, and each died to one new measurement. The ones
> that died fastest were the ones that named a *property of the actor* (this token, this account,
> this scope) rather than a property of the *operation*.
>
> What *is* fixed: the error names the repository and says what a write-404 usually means, and a
> refusal at `create_branch` — **the first mutating call in every write path**, so nothing has been
> created — now retries under the bot rather than losing an upload the submitter has already made.
> `FakeGitHubClient` had no notion of an identity that may read a repository and not write it, so
> no test could have failed; it takes `deny_writes_to` now. **Sixth** time the fake has been the
> more capable of the two.

Three defects had to be fixed to get there and **only the first was ours** — the other two were
pre-existing in the target repository and are worth carrying to
`wikipathways/wikipathways-database`, where most contributions are fork pull requests and the same
patterns are present.

> [!warning] **`open_pull_request` echoed the request instead of reading the answer.**
> It returned the `head` it was *asked for* and never parsed `head_repo`, so both of her pull
> requests recorded their branch as if it were on the base repo — and every branch-side lookup
> then goes to the wrong repository, so **revise raises `NoPendingSubmission`** and a curator
> requesting changes leaves the submitter unable to answer. Echoing was wrong for `head_branch`
> too: a cross-repository `head` is `owner:branch`, so the owner prefix would have been stored as
> part of the branch name.
>
> **`FakeGitHubClient` parsed both correctly**, so 494 tests agreed while production did not —
> the *fifth* time here that the fake has been more capable than the thing it stands in for. Only
> a `MockTransport` test against a real-shaped response catches this class.
>
> The affected rows **heal themselves**: the reconcile already reads the pull request from GitHub,
> so it now fills a blank head repo from what GitHub says. No database was touched by hand — which
> also meant the permission classifier's refusal of a production `UPDATE` pushed the work toward
> the better fix.

> [!warning] **The target repository could not process *any* fork pull request.**
> `get-gpml` ran on `pull_request_target` — holding the deploy keys — and checked out
> `refs/pull/N/head`, the fork's own code. `actions/checkout@v6` now refuses that outright, so the
> job failed at its first step. **Pre-existing and nothing to do with fork mode**, and the same
> pattern is in `wikipathways/wikipathways-database`'s own workflow 1, where most contributions
> *are* fork pull requests.
>
> Fixed by removing the checkout rather than by `allow-unsafe-pr-checkout: true`. The checkout was
> never needed for code: it read one GPML file and pushed a branch nothing consumes. The file is
> now fetched over the API as **data**, and the generators already ran from base code via the
> artifact — so a fork's contents are never executed. The dead `git push` went with it
> (`branch-name` feeds no job; 3A checks out `main`). Full change in `sandbox-workflows/README.md`.

> [!warning] **A fork pull request would have been approved and then silently never published.**
> `pr_label_dispatcher.yml` ran on `pull_request`, and a fork pull request gets a **read-only**
> `GITHUB_TOKEN` whatever the repository default says — so `gh workflow run`, needing
> `actions: write`, could not dispatch 3a at all. Fixed on the fork (`pull_request_target` plus an
> explicit permissions block, commit `0e5d666`; staged copy in `sandbox-workflows/`), safe there
> because the job checks out nothing and runs no pull-request code.
>
> **How it was nearly missed is the reusable part.** It had never been *seen* failing, because no
> fork pull request had ever been labelled on that repo: all 40 recent dispatcher successes are
> same-repo pull requests the portal opened itself. The note that it "has recovered" was drawn
> from a population that excludes the case in question — the same error as judging the ruleset by
> hand-written fixtures. **When a component is declared healthy, check what its sample actually
> contained.**
>
> Also settled empirically while looking: `youphendriks/youp-sandbox-wp-db` is a fork whose **name
> differs from its parent's**, so assembling `owner/parent-name` rather than reading `full_name`
> off GitHub's response would have sent every write for that user to a repository that does not
> exist.

> [!warning] **No application log line had ever reached production.** Uvicorn configures only its
> own `uvicorn*` loggers and nothing here called `basicConfig`, so the root logger had no handler;
> Python's `lastResort` emitted WARNING and above unattributed and dropped everything below. The
> cost was not the line being looked for: `expire_stale` and the WPID reclaim log how long a lock
> or reservation was *actually held*, at INFO — the observability built in the #23 round so those
> TTLs could be corrected against real behaviour. It had been collecting nothing the whole time.
> Now configured on the `wpsubmit` parent at app start (`WPSUBMIT_LOG_LEVEL`). Generalises to any
> stdlib-logging service behind uvicorn: **the symptom is silence, which reads exactly like
> "nothing happened".**

494 tests. Live at `sha256:bed99bf7…` (from `e4bb0b9`), deployed and verified the same day. The
rollback target is `sha256:fcbcb8f3…` (from `1c73e4f`) — no migration and no secret was added, so
a rollback is a plain digest change; only `WPSUBMIT_SUBMIT_IDENTITY=fork` would want reverting to
`bot` with it.

**#22 is decided and built: fork-per-submitter, with the bot as fallback.** `submit_identity`
gains `fork`; `app/submit/targets.py` owns the whole decision (which client writes, which
repository the branch goes to, what the pull request declares as its head) and every other mode
resolves to "the same repository", so the default is unchanged.

> The issue framed this as *should the user's OAuth token go back into the write path*. That is
> backwards, and checking rather than accepting it is what unblocked a decision that had been
> parked for a week. `submit_identity` has **always defaulted to `user`**, and `_submit_client`'s
> own docstring said the submitter's token is *better* where they can push — `bot` is a capability
> workaround for having no write access, not a security position. The scope cost was not real
> either: GitHub defines `public_repo`, which the app already requests, as read/write to code on
> public repositories, and that covers forking one and pushing to it. So fork mode exercises
> capability the app already holds rather than acquiring any, which is why it turned out to be a
> smaller change than the issue implies.

Four places a cross-repository submission differs, each a way to get it quietly wrong: the base
commit is read from the **content repo**, never the fork, which can be a year stale; the head is
`owner:branch`; `find_open_pr` must be told the head repo or an update opens a *second* pull
request; and a revise writes with the **submitter's** token whatever the setting says, because an
App installation token cannot push to a personal fork. The fallback to `bot` happens **before the
first write** and nowhere else — a failure afterwards is not retried against a different
repository, because reconciling half-written state across two repos is a worse bug than the case
it would cover. And **the owner of the content repo never forks it**: GitHub refuses, and they
have push access anyway. That is not hypothetical — it is the live deployment's own configuration,
and without it every submission Marvin makes would take the fallback and none would exercise
anything.

**Proven on the live service, not only the demo — WP5426, PR #20.** `demo/pathway_revised.gpml`
was chosen deliberately: it is one of the two repaired fixtures that had *never* been through a
real pipeline run, so it tests the #26 fix rather than re-testing the file the issue already
measured. Workflow 1 went green on **all ten jobs**, and the whole lifecycle ran from the
dashboard: the disabled Approve button named what was outstanding; `references_valid` moved
N/A → Fail → Pass with DOM and server agreeing at each step (not required / required+blocked with
the button naming it / required+clear); Approve applied the `accepted` label; workflow 3A
published; the pull request closed **unmerged**; and the app read WP5426 off the publish marker
over the webhook. **This is the first approval that started at the dashboard button** — every
earlier publication applied the label by hand, which is why the 07-29 handoff lists that as
outstanding. The card afterwards says "No render on file", which is correct: #18 frees the render
cache at every terminal transition.

> A browser probe that clicks and reads back needs to **wait for the server's answer, not a fixed
> delay**. A 700ms sleep is fine against localhost and races the live service, and the failure
> mode is silent and very convincing — every reading is one step stale, so the sequence looks like
> a real off-by-one bug in the app. Poll until the DOM matches what was clicked, then cross-check
> against the API.

**Verify a deploy against behaviour, not the digest.** The image carried no
`org.opencontainers.image.revision` label, so the tag alone proved nothing about which commit it
held; `docker run --rm --entrypoint python <digest> -c "…"` on the rule table answered it before
shipping. After the rollout, the same `POST /api/validate` probe that had shown both bugs *present
in production* an hour earlier returned the new `fail` and the new wording — which is the only
evidence that actually settles it.

The 07-29 summary below is kept because its details still hold.

### Previously (2026-07-29)

The whole submit → review → approve → publish lifecycle has been driven against live GitHub,
clickable data nodes shipped (#14), `WPSUBMIT_SITE_NOTICE` warns when a target cannot publish, and
the issue tracker was reconciled against the code (nine open at the time; five as of 2026-08-03).

**The app is deployed and live at https://upload.wikipathways.org**, pointed at the fork
`marvinm2/sandbox-wp-db` in `pipeline` mode. The handoff doc is authoritative for what is
proven, what is pending on other people, and the gotchas. Three things it says that matter most
here:

- **Approval does not merge.** On a target repo that publishes through its own Actions
  (`WPSUBMIT_PUBLISH_MODE=pipeline`), approving applies that repo's `accepted` label and stops;
  the repo assigns the WPID, publishes, and closes the pull request unmerged. A close without a
  merge is the *success* signal there — but only when the repo's marker comment says so. A silent
  close is `PUBLISH_FAILED`, for updates as much as for new pathways. `direct` mode still merges,
  and is the default, so `wikipathways-database`, a personal fork and the demo are unchanged.
- **A new pathway carries no WPID** until publication. It is submitted on branch
  `WP0001_<user>_<stamp>` at `pathways/WP0001/WP0001.gpml`; `Review.wpid` is nullable and the
  branch is recorded on the row, because it can no longer be derived. Revise is therefore keyed
  by pull request (`POST /api/reviews/{pr}/revise`), not by WPID. `WP0001` is a placeholder and
  **not** an address: the WPID routes refuse a leading zero rather than coercing it to WP1.
- **Every `ReviewStatus` is reachable in the UI.** `app/review/status.py` owns the on-screen
  vocabulary — the label, the banner sentence, the empty state, and which tabs the queue shows
  per publish mode. It also owns `ACTIONABLE` (open / changes_requested), which gates both the
  controls and `CurationService`'s own refusals. Add a status there, not in the template.

- **The fork now produces the target repo's own rendered draft page.** A fork inherits no Actions
  secrets, so `commit-outputs` had 403'd on every run the fork ever had and no draft was ever
  written — which is also why 3a died instantly, since it looks for a draft first. The fix is
  entirely account-side: `marvinm2/sandbox-wp.gh.io` and `marvinm2/sandbox-wp-assets` are forked,
  each with its own write-enabled deploy key (`ACTIONS_SANDBOX_DEPLOY_KEY` /
  `ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY`), Pages is on with `baseurl: "/sandbox-wp.gh.io"`, and every
  `repository:` in workflows 1/3a/3b names a fork. The app needed **no code change** — just
  `WPSUBMIT_DRAFTS_REPO` and `WPSUBMIT_DRAFTS_SITE_BASE_URL`. Run `30451444585` is the first
  all-ten-jobs-green run of workflow 1 anywhere.
- **A pathway has been published — WP5423, run `30460071900`, every step green.** The first ever,
  on the fork. All three pushes landed (assets included), the marker comment carried the WPID, the
  pull request closed unmerged, and the app moved itself to `published` by reading that marker
  over the webhook. The drafts are *moved* at publication, so a draft page 404ing afterwards is
  correct. Approve was applied as a label directly rather than through the dashboard, because
  PR #5's checklist legitimately fails — a pass that *starts* at the Approve button was still
  outstanding. **Closed 2026-08-03: WP5426, PR #20** (see below).
- **Never merge a pipeline pull request** (2026-07-30, PR #11 on the fork). Merging commits
  `pathways/WP0001/WP0001.gpml` to `main`, and that is the placeholder slot every new submission
  writes to — the app created rather than updated it, so every submission by anyone then failed
  with `422 "sha" wasn't supplied` until the file was deleted by hand. Fixed in four layers
  (submission overwrites; the mirror comment says not to merge; the webhook deletes a stray
  placeholder off the base branch via `CurationService._repair_stray_placeholder`; a merged
  pipeline PR still settles from the publish marker rather than falling through to `MERGED`).
  `docs/sandbox-pipeline.md` §6 defect 12 has the full account. The general lesson is broader
  than this path: a **shared fixed path is never safe to create**, only to upsert.
- **Never put a GitHub expression in a `run:`-block comment.** A `run:` block is one string value
  and the runner substitutes into its *text* before bash exists, so `#` protects nothing; an
  expression that does not parse fails the **whole workflow at startup**, naming the `run:` line
  rather than the comment. It kept 3a from starting at all. `tests/test_sandbox_workflows.py`
  parses every staged workflow and rejects this.

`docs/sandbox-pipeline.md` maps the target repo's five workflows and its known breakages; §7 is
the fork-specific setup and is the one to read when a run goes red. `sandbox-workflows/` holds
repaired copies staged for a pull request to that repo — **not opened yet**, and not part of this
app; the fork already runs them.

## Open decisions (scaffolding-plan §0, proposal §9)

Settled 2026-08-05:

- **Name: `pathway-portal`.** Applied to the packaging metadata, the page title, the FastAPI app
  and the docs. The **env prefix is deliberately not a flag day**: `Settings` reads `PORTAL_*`
  first and falls back to `WPSUBMIT_*`, so the live service can migrate its variables and secrets
  whenever, or never (`tests/test_config.py`). The **GHCR image is still
  `ghcr.io/marvinm2/wikipathways-submit`** — renaming it mints a fresh package that defaults to
  *private*, which would break the pull on TGX2 and so needs a visibility change alongside a
  coordinated redeploy. Worth doing, not worth doing casually.
- **Licence: Apache-2.0** (`LICENSE`, `NOTICE`), matching the org's software norm. It covers the
  code only; pathway content stays CC0, which `NOTICE` says explicitly so the two are never
  conflated.
- **Curator whitelist: a GitHub Team** (`app/curators.py`, `*_CURATOR_TEAM='org/slug'`),
  TTL-cached and fail-closed, with the config list as fallback. Closed by issue #9.
- **Lock / reservation TTLs**: set from measurement rather than guesswork, and every expiry logs
  how long the thing was actually held so they can be corrected again. Closed by issue #23.

Still open:

- Bot merge vs `main` branch protection interaction.
- Whether to detach the sandbox from its parent so the testbed stops being a fork of a fork
  (a GitHub Support request; see the 2026-08-05 notes).
