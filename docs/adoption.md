# Adopting pull requests the portal did not open

The portal used to know about exactly one kind of submission: an upload it received itself. Every
other pull request against the content repository — the PathVisio plugin's, a curator's own
branch, anyone with push access — was invisible to the curation dashboard, because the dashboard
reads the app's own database and nothing wrote a row for them.

Adoption closes that. A pull request touching `pathways/` becomes a review with the full surface:
before/after render, quality report, checklist, check-out lock, mirror comment, and Approve at the
dashboard button.

## Turning it on

```
PORTAL_ADOPT_FOREIGN_PRS=true      # WPSUBMIT_ADOPT_FOREIGN_PRS also works
```

Off by default. It puts other people's pull requests into the curation queue and comments on
them, which is not a change any existing deployment should get merely by upgrading.

**Ignored outside `publish_mode=pipeline`, and that is a safety property.** In `direct` mode
approving *merges*. A pull request the app did not lay out is not one it can safely merge: the
plugin files a new pathway at a title-derived path (`pathways/testing_new_pathway/`), and its
placeholder submissions land at the shared `WP0001` slot every portal submission also writes to.
Merging either onto `main` is the 2026-07-30 incident through a different door. `Settings` turns
the flag off and logs when the combination is configured.

No GitHub-side change is needed: the App already subscribes to `pull_request`, and `opened`,
`reopened` and `synchronize` deliveries have always arrived and always been dropped.

## What arrives, and how it is classified

The classification is **the target repository's, not ours**: `1_on_pull_request.yml` decides new
versus edit by filename, so `app/review/adopt.py` reuses the pipeline's own regex rather than
approximating it. Where GitHub's per-file `status` disagrees, the disagreement is logged and the
filename still wins — predicting something the repository will not do makes the dashboard read
artifacts that do not exist and report the mismatch against the submitter.

| changed files | kind | wpid |
|---|---|---|
| `pathways/WP3894/WP3894.gpml` | `update` | 3894 |
| `pathways/WP0001/WP0001.gpml` | `new` | none — the placeholder is not an address |
| `pathways/testing_new_pathway/…gpml` | `new` | none — what the plugin writes for a new pathway |
| nothing under `pathways/` | not adopted | — |
| only `pathways/WP1/WP1.md` | not adopted | — |
| only deletions | not adopted | — |

Every refusal is logged with the pull request number and the reason. A pull request that quietly
fails to appear in the queue is indistinguishable from one nobody opened.

**More than one pathway in one pull request** is adopted, flagged, and blocked at the approval
gate by the `one_pathway_per_pr` checklist item — the repository publishes one pathway per pull
request, so approving would publish at most one of them. Not hypothetical: PR #58 and PR #74 on
`wikipathways/sandbox-wp-db` each touch two pathway directories.

## Where the bytes come from

Both reads address the **content repository**, never the submitter's fork:

- *after* — `get_file_content(content_repo, head_sha, path)`. GitHub serves a fork's pull request
  from the base repository at the head commit; verified on 2026-08-21 against PR #73, identical
  blob sha from either side. It keeps working after the fork is deleted, and one repository
  answers for everything.
- *before* — the base branch, exactly as an update's "before" already worked.

The render runs **before** the row is registered. That ordering is load-bearing: the mirror
comment reads the quality report out of the preview cache, so registering first posts a comment
with the automated-checks table missing.

## The lock

`acquire()` refuses when an open pull request touches the pathway — and during adoption that open
pull request *is* the one being adopted, so `PathwayLocks.adopt()` skips the scan for the same
reason the same-holder refresh branch already does.

It **never steals**. If a portal user holds the pathway, or another adopted pull request already
took it, the review is still created and the lock stays where it is. Several open pull requests
per pathway is the ordinary case on the live target — six of them touch WP1001.

Releasing is now per-pull-request (`CurationService._release_lock`): closing one of those six must
not free a lock another one holds.

## The race with the portal's own submissions

A `pull_request.opened` delivery for a pull request the portal just opened can arrive before
`register()` commits — opening, rendering and registering are three steps and GitHub delivers in
about a second. Branch shape cannot tell them apart, because the plugin uses the same
`WP<id>_<login>_<stamp>` convention the portal does.

So the order is made not to matter: a portal `register()` **upgrades** an adopted row it finds,
overwriting the submitter, kind, WPID and head, and re-rendering the welcome comment (which
upserts on its marker, so it edits rather than duplicates).

## What an adopted review does not get

- **Re-upload.** The branch is on the author's fork and the app never wrote it. `/revise` returns
  409 and the card offers no upload; the comment and the card both say to push a commit instead.
- **Rate limiting.** The limiter bounds what the app opens on a person's behalf, and an adopted
  row is not that. Counting them would spend a plugin author's portal quota on submissions the
  portal never accepted.

## Recovering a missed delivery

```
POST /api/reviews/{pr_number}/adopt      # curator only
```

One pull request, named explicitly. Deliberately not a sweep: on the live sandbox a sweep would
pull in two dozen rows, most of them one GSoC student's test submissions, and comment on each.

## Production is not the sandbox

`wikipathways/wikipathways-database` has no workflow 2/3a/3b, no label dispatcher, and only
GitHub's nine stock labels — so approve-by-label there is a silent no-op. Its workflow 1 also
still fails on every pull request from a fork, which is how the plugin submits. Adoption is for
the sandbox until both are addressed.
