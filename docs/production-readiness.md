# Production readiness: what stands between the portal and `wikipathways-database`

Written 2026-11-11 from a review of the portal against the production repository. The portal is
built and proven against the org sandbox (`wikipathways/sandbox-wp-db`, `pipeline` mode) and that
stays its working target. This file is the list of gaps for the day it becomes the pull-request
front door for production. None of it is scheduled; it is here so the gaps are written down in
one place rather than rediscovered.

The scope decided with the maintainer: the portal becomes the front door for **submitting,
updating and reviewing pull requests** on production. Curation across the whole database
(worklists, per-pathway tags, deprecation) stays outside it for now. Changes to the production
repository's own workflows are recommendations for its maintainers, not portal work.

## How production actually works today

- **Publication is merge-then-generate.** `on_gpml_change.yml` runs on a push to `main` that
  touches `pathways/**/*.gpml`, regenerates the derived files and pushes to the site, assets and
  homology repositories. It is healthy: 180 of its last 200 runs succeeded (checked 2026-11-11).
  Production has **no** `2`, `3A`, `3B` or label-dispatcher workflows and none of the pipeline
  labels (`accepted`, `rejected`, `resubmitted`, `published`, `publish failed`).
- **So the portal's `direct` mode fits production and `pipeline` mode does not.** In `direct`
  mode approving squash-merges the pull request as the App, the portal allocates the WPID up
  front, and `on_gpml_change` publishes unchanged. This is what `docs/design-proposal.md`
  specified before the sandbox existed; pipeline mode is the sandbox's model.
- **Most curation does not go through pull requests at all.** Of the 63 human commits on
  production `main` between 2026-09-02 and 2026-11-11, 62 have no pull request, and 46 of those
  change GPML. Contributor pull requests (#118, #120) were closed and their content committed by
  hand after discussion in the thread. About 60% of the direct edits are metadata only:
  references, descriptions, identifiers.
- **Workflow 1 (`1_on_pull_request.yml`) is active on production** since #100 was merged on
  2026-09-06 and has failed on every run since. It assumes one GPML per pull request, its
  frontmatter and json-svg steps run the publish-time scripts against the wrong paths, and its
  commit step targets the sandbox drafts repository with a deploy key production does not hold.

## Gaps, roughly in the order they would have to close

### Decisions for the maintainers (not code)

1. **The publish model.** Direct mode is the recommendation: no new production workflows.
2. **Direct pushes versus portal reviews.** A portal approval cannot see a direct push to the
   same pathway, and an approved update could silently overwrite one. Either route pathway edits
   through the portal, or accept direct pushes and have the portal detect them (see 9).
3. **One WPID allocator.** Production hands out identifiers in at least three uncoordinated
   ways (editors by hand, agreed blocks for batch deposits, contributors choosing their own). The
   portal's allocator reads the tree, open pull requests and its own reservations, and cannot see
   an identifier promised elsewhere. The sandbox also reuses production identifiers
   (WP5425–WP5428); moving its floor well above production's would stop test pathways colliding.
4. **App and curator identity.** The App (`wikipathways-submit-bot-dev`, owned by a personal
   account) must be installed on `wikipathways-database`, which needs an org owner, and should be
   transferred to the org. Curators are a three-name environment list today, while production
   merges come mostly from `egonw`, `DeniseSl22` and `khanspers`. A `wikipathways` team for
   `CURATOR_TEAM` needs an org owner too, and the App needs Members: read to resolve it.
5. **Production workflow 1: disable it, or fix it.** The portal does not need it in direct mode.
   It also carries **security weaknesses**: it runs on `pull_request_target` with a write token
   against an unprotected `main`, and it passes contributor-controlled pull-request content into
   shell steps in two places. One of those places is reached on every fork pull request today.
   The same pattern exists in the sandbox's copy of workflow 1. Details belong in a private
   report to the maintainers, not in this file.
6. **The PathVisio plugin's target and README**, which point at the sandbox and describe the
   portal as unable to see plugin pull requests.

### Portal work, to be built and proven on the sandbox first

7. **Direct-mode publication confirmation.** In direct mode a merge is recorded as terminal
   `MERGED` even when `on_gpml_change` then fails or is cancelled. MERGED is not published.
8. **Serialise merges.** Several approvals within minutes can make `on_gpml_change` skip one
   pathway's derived files.
9. **Stale-base guard.** Record the GPML blob on `main` at submission; refuse approval when it
   has changed since, with a re-base message. Subscribe the App to `push` to flag open reviews
   whose pathway was touched directly.
10. **Pull-request file list.** Five of 26 production forks are diverged from upstream, so a
    fork-mode branch cut from the fork's own head carries the fork's commits into the pull
    request (production #120 carried a stray file). After opening, list the pull request's
    files; refuse approval when it touches anything but the expected path.
11. **Embedded-WPID checks.** A file declaring another pathway's identifier is accepted as new,
    or renumbered on update, without a word.
12. **Grade updates relative to the base.** The ruleset fails 1175 of the 2218 published
    pathways outright (mostly unmapped data nodes), so an edit that improves a pathway is graded
    on legacy debt it did not introduce. Fail only on regressions; report the rest as
    pre-existing.
13. **Author identity.** The portal stamps the GitHub login as `Author`; production keys authors
    on the WikiPathways username (`scripts/author_list.csv`).
14. **Metadata-only edits** — most production edits — have no portal path: the portal accepts a
    whole GPML upload and nothing else. Structured edit forms (title and description, references
    by PMID/DOI, node identifiers, ontology tags, community membership) would be needed before
    the front door covers what curators actually change. Deliberately not scheduled.
15. **Show the `wikipathways-collection` reports** (`reports/WPxxxx.json`, the site's curation
    badge) beside the portal's own rules, so the two quality systems stop disagreeing silently.

### Cutover mechanics

16. Rehearse on a fresh fork of production with forked site and assets repositories.
17. Archive the sandbox review rows before repointing (`base_repo` is now recorded on every
    portal submission, and the webhook ignores other repositories, but a clean table is still the
    safe start).
18. Set `CONTENT_REPO`, `PUBLISH_MODE=direct`, the App installation id, `CURATOR_TEAM`; clear the
    `DRAFTS_*` variables; rewrite `SITE_NOTICE`.
19. Update production's README and announce.
