# sandbox-workflows

Repaired copies of GitHub Actions workflows belonging to
[`wikipathways/sandbox-wp-db`](https://github.com/wikipathways/sandbox-wp-db). They are
staged here so they can be reviewed and then opened as a pull request against that
repository.

> [!warning] **Staged is not installed, and the gap has already cost a publication.**
> On 2026-08-13, PR #37 published correctly and was then labelled `publish failed` because
> `gh pr close` cannot close a pull request somebody merged by hand. That failure was diagnosed
> on 2026-07-30 and repaired in the staged 3A the same day — and the fork never got the change,
> so it happened again six weeks later. **A repair written here does nothing until it is applied
> to the repository that runs it.** `diff` the staged file against the running one before
> assuming any repair is live.
>
> Measured 2026-08-20, normalising the owner in the `repository:` inputs: the fork's `3A` and
> `on_gpml_change` are now **identical** to the staged copies, so the 08-14 note that it was
> behind on three 3A changes is out of date. `pr_label_dispatcher` still differs, and the
> **staged copy is the better one** — the fork's passes `pr_number` for the `resubmitted` case,
> which workflow 1 rejects as an unexpected input, so that label has never worked there.
> `1_on_pull_request.yml` runs the other way: the **fork's** copy is the one to take, because
> the two `refs/pull/N/head` checkouts were removed as a change on the fork and never written
> back here, so the staged file still carries them.

> [!important] Two changes are needed before *any* fork pull request works, and both are applied
> on `marvinm2/sandbox-wp-db` only
>
> **1. `1_on_pull_request.yml` — `get-gpml` must not check out the fork.** Applied as a change
> rather than staged as a file here, because this workflow names fork repositories throughout its
> `repository:` inputs (see the warning below). The change, for applying upstream:
>
> **There are two such checkouts, not one.** `get-gpml` and `update-pr-desc` both check out the
> pull request head; the other six checkouts in the file take the base default and are fine.
> `update-pr-desc` was missed on the first pass because the audit checked six of the eight and
> generalised — the same sample-excludes-the-case error recorded twice elsewhere in these notes,
> committed by the person who had just written it down. **Grep the whole file for `refs/pull`
> rather than reading the checkouts you happen to look at.** It never used the checkout at all: it
> reads job outputs and runs `gh pr edit`.
>
> - Delete the `Checkout repository` step (`ref: refs/pull/${{ … }}/head`) from `get-gpml`.
>   `actions/checkout@v6` refuses it from a `pull_request_target` workflow, so the job fails at its
>   first step for every fork pull request. Do **not** answer that with
>   `allow-unsafe-pr-checkout: true`: the workflow holds the deploy keys, and that is the whole
>   pwn-request hazard rather than a warning to silence.
> - Replace `cp "$GPML_FILEPATH" ./"$GPML_FILE"` with an API fetch of the file from the pull
>   request's head (`gh pr view --json headRepositoryOwner,headRepository,headRefOid`, then
>   `gh api "repos/$HEAD_REPO/contents/$GPML_FILEPATH?ref=$HEAD_SHA" -H "Accept: application/vnd.github.raw"`),
>   plus an empty-file guard. This keeps the fork's contribution as **data**; the generators
>   downstream already run from the base repository's own code via the `gpml-file` artifact, so
>   nothing from the fork is ever executed.
> - Add `GH_REPO: ${{ github.repository }}` to the step's `env` — without a checkout there is no
>   git remote for `gh` to infer the repository from.
> - Delete the `git ls-remote` / `git push origin HEAD:refs/heads/$BRANCH_NAME` lines in
>   `Get branch name`. They need a working tree, and they answer the step's own TODO: `branch-name`
>   is declared as a job output and **consumed by no job**, while 3A checks out `main`. It was
>   copying a fork's commits into the base repository for nothing.
>
> Verified on `marvinm2/sandbox-wp-db` 2026-08-04, and the second verification is the one that
> counts. Run `30892475738` on PR #23 was **all ten green but `workflow_dispatch`**, where the
> checkout guard does not apply at all — so it proved nothing about the trigger that matters. Run
> `30906919228` on PR #28 is **all ten green under `pull_request_target`**, on a genuine
> cross-repository pull request, raised by the `synchronize` event of a revision. The `testing`
> marker came back, the pull request description was rewritten, and the portal showed the
> repository's verdicts beside its own.
>
> **Re-running a fork pull request by hand does not test a fork pull request.** `workflow_dispatch`
> runs in the base context with a full token; the refusal only happens on the real event. Any
> check of this fix has to come from an actual pull request or a push to one.
>
> **2. `pr_label_dispatcher.yml` is the other half**
> Unlike the others here, this one is **not** a nicety: without it, approving a submission that
> came from a contributor's fork applies the label and then nothing happens. The dispatcher runs
> on `pull_request`, and a pull request from a fork gets a **read-only** `GITHUB_TOKEN` whatever
> the repository's default workflow permissions say — so `gh workflow run`, which needs
> `actions: write`, cannot dispatch the publish workflow. Changing the trigger to
> `pull_request_target` fixes it, and is safe here specifically because the job checks out nothing
> and runs no code from the pull request; the "pwn request" hazard needs both.
>
> This has never been observed failing on `marvinm2/sandbox-wp-db` for a good reason: **no fork
> pull request has ever been labelled there.** All 40 of the dispatcher's recent successes are
> same-repo pull requests the portal opened itself, and the single failure in that window was also
> same-repo. The note elsewhere that "the label dispatcher has recovered" was drawn from that
> population and says nothing about the fork case.

**These files ship into `sandbox-wp-db`, not into this app.** Nothing here runs as part of
the submission app, and nothing here is imported by `app/`. The directory mirrors the
target layout (`.github/workflows/...`) so the files can be copied across verbatim.

> [!warning] Do not copy these over a fork's copies wholesale
> The files here name the **upstream** repositories in their `repository:` inputs, because that is
> what a pull request to `wikipathways/sandbox-wp-db` needs. A fork's own copies name the fork —
> `marvinm2/sandbox-wp.gh.io` in workflow 1's `commit-outputs`, `marvinm2/sandbox-wp-db` in 3A.
> Copying these files over a fork's therefore repoints its draft push at a repository it cannot
> write to, and the symptom is the one already documented in this repo: the job 403s, no draft
> appears, and the dashboard's pipeline panel goes quietly empty because a missing draft is the
> ordinary case. Apply the *changes* to the fork's copies; do not replace the files.

The three files:

- `.github/workflows/1_on_pull_request.yml` — the PR processor. Two one-line fixes on the
  new-contributor path, two on the data-node test, a fix for the data-node counts, and one
  added step announcing the test results in a form a machine can read. See the four
  "Workflow 1" sections below.
- `.github/workflows/3a_approved_pull_request.yml` — the publish workflow. Renames the
  draft files produced by workflow 1 to their final WPID, pushes them to `sandbox-wp-db`,
  `sandbox-wp.gh.io` and `sandbox-wp-assets`, announces the WPID on the PR, and closes it.
- `.github/workflows/pr_label_dispatcher.yml` — turns the `accepted` / `rejected` /
  `resubmitted` labels into runs of 3A / 3B / workflow 1.

`labels.md` lists the two labels that have to be created before 3A can use them.

## Workflow 1: the first-contributor path

Unlike the 3A changes, these two are **not** read out of the YAML — both were hit on a live
run, fixed, and the fix confirmed by re-running. They sit on the branch that adds an author
who is not yet in `author_list.csv`, so they fire on a person's **first ever submission**
and not otherwise. That is why they have survived: the recent test submissions all come from
contributors already in the CSV.

The consequence is worth stating plainly, because it is the opposite of harmless. When
`authors` fails, `commit-outputs` and `update-pr-desc` are skipped, so a first-time
contributor gets no draft page, no data-node table, no bibliography and no report on their
pull request — while everyone already in the CSV gets all of it.

**1. `authors`, line 483.** The counter used PHP syntax:

```bash
$k=$k + 1        # $k expands to 0, bash runs "0=0" as a command, exit 127
k=$((k + 1))     # fixed
```

Observed on `marvinm2/sandbox-wp-db` PR #2: `Adding marvinm2` followed by
`line 58: 0=0: command not found` and exit code 127.

**2. `commit-outputs`, line 1071.** With the counter fixed, the next run reached a second
defect on the same path:

```bash
cp author_list.csv wikipathways.github.io/scripts/.          # cannot stat
cp authors/author_list.csv wikipathways.github.io/scripts/.  # fixed
```

`authors` moves `scripts/author_list.csv` into `authors/` (line 487) and uploads that
directory as the `authors` artifact, so on download the file is at `authors/author_list.csv`.
The copy looked for it in the workspace root.

## Workflow 1: the node test dies without saying so

Also observed on a live run rather than read out of the YAML, and it hits far more people than
the first-contributor path: the `testing` job's data-node step runs under `bash -e`, and two of
its assignments end in a `grep`. An assignment takes its exit status from the command
substitution, and grep exits 1 when it matches nothing, so the step aborts — with **no output at
all**, not even a stderr line. The log shows the step's `##[endgroup]` and then
`Process completed with exit code 1`.

```bash
matching_added_node=$(echo "$added_or_modified_nodes" | grep "GraphId=\"$graph_id\"")           # dies
matching_added_node=$(echo "$added_or_modified_nodes" | grep "GraphId=\"$graph_id\"" || true)   # fixed

actual_deleted_nodes=$(echo "$deleted_nodes" | grep -vF "$safe_modified_nodes")                 # dies
actual_deleted_nodes=$(echo "$deleted_nodes" | grep -vF "$safe_modified_nodes" || true)         # fixed
```

The first fires on any edit that **deletes** a data node, which is precisely the case the test
exists to detect. The second fires on any edit that only re-annotates, where the filter removes
every line. `update-pr-desc` and `commit-outputs` both `needs: testing`, so the submitter loses
their drafts and their PR-body report, and nothing anywhere says why.

Observed on `marvinm2/sandbox-wp-db` run `30442228975` (an edit to WP100) and reproduced offline
against that pull request's diff, where the loop dies on the fifth deleted node, `GraphId="a57"`.

## Workflow 1: the node counts are wrong, and now something reads them

This was deliberately left alone before, on the grounds that nothing gated on the numbers — they
rendered as coloured text in a report. That stopped being true the moment the curation portal
began reading this job's verdicts (next section), so it is fixed here.

`modified_nodes` was accumulated as `"$modified_nodes\n$deleted_node"`. Inside double quotes bash
does not interpret `\n`, so the list was one long line with literal backslash-n separators, and
all three counts were wrong because of it:

- `wc -l` reported **1** however many nodes were modified;
- `grep -vF "$modified_nodes"` matched nothing, so modified nodes were counted as **added** too;
- `grep -vF "$safe_modified_nodes"` likewise, so they were counted as **deleted** as well.

```bash
modified_nodes="$modified_nodes\n$deleted_node"          # literal backslash-n
modified_nodes="${modified_nodes}${deleted_node}"$'\n'   # fixed
```

The three `echo "…=${…}" >> $GITHUB_ENV` lines at the end of that step are **removed** rather than
repaired. Nothing in the workflow ever read them, and all three values are multi-line, so the
writes were already malformed — without a heredoc delimiter every line after the first is parsed
as its own `NAME=VALUE` assignment. That was harmless only while `modified_nodes` was accidentally
single-line; with real newlines in it, keeping them would turn a dead write into a failing one.

## Workflow 1: announcing the test results where something can read them

The `testing` job's three verdicts — title length, description length, data-node changes — have
always existed and have always reached nobody outside GitHub. They are written into the pull
request **description**, and `update-pr-desc` rewrites that description wholesale on every run, so
anything trying to read them would be racing a rewrite. That is the same problem the publish
workflow already solved by announcing itself in a comment (`<!-- wikipathways-publish … -->`),
which the portal parses today.

So the `testing` job gains one step, `Announce the test results for machines`, printing the same
device:

```
<!-- wikipathways-testing {"pr":54,"title":"pass","description":"review","nodes":"review"} -->
```

One comment per pull request, edited in place via `gh api`, so a resubmission does not stack them
up. `continue-on-error: true`: it is a convenience for a downstream reader and never a reason to
fail a run that has already done its real work. Comments are untouched by the description
rewrites, which is the whole point.

> [!note] Live on the fork since 2026-08-03, and proven
> `marvinm2/sandbox-wp-db` carries these three changes. The round-trip was verified on two pull
> requests: #15 posted `{"title":"review","description":"review","nodes":"review"}` and #16 posted
> `{"title":"pass","description":"pass","nodes":"review"}`, both read back and rendered beside the
> portal's own predictions. The corrected counts were confirmed by replaying #16's real diff
> through both versions of the logic — the old one reported `deleted=3 modified=1 added=3` where
> the truth was `deleted=0 modified=3 added=0`.
>
> **One leftover, deliberately not fixed:** `note_test_nodes` and `review_note` still build their
> text with a literal `\n`, so the PR table reads `Modified nodes: 3\n`. Same class of bug as the
> counts, but in the prose rather than the arithmetic, and nothing reads it. A one-line fix
> whenever that job is next touched.

The portal predicts all three offline as well, from the uploaded GPML, so a submitter sees them
before the pull request exists (`app/quality/`). Showing both answers side by side is deliberate:
where they disagree, the portal's copy of these thresholds has fallen behind the real ones, and
that becomes visible instead of silently wrong.

## What is known, and what is not

Read this section as the reason for the changes, not as a diagnosis. Nothing has ever been
published by the **original** 3A, and the evidence that would explain why is gone.

> [!note] The repaired 3A has now published, 2026-07-29
> Run `30460071900` on `marvinm2/sandbox-wp-db` succeeded with every step green: WPID `5423`
> assigned, all three pushes landing (assets included, so the `ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY`
> arrangement works as designed), marker comment posted, `published` label applied, pull request
> closed unmerged. So the changes below are no longer only reasoned-from-YAML — the whole workflow
> has been observed end to end, on forks of all three repositories.
>
> It also collected a defect on the way, which is the strongest argument for having run it: see
> "Do not put an expression in a run-block comment" below. Nothing had ever dispatched 3A, so a
> file that fails to parse at startup looked exactly like a file that was fine.

**Observed, from the API on 2026-07-27:**

- 3A has run exactly once. Run `17442557461` was a `workflow_dispatch` by `egonw` off
  `main` on 2025-09-03, ran from 18:25:34Z to 18:25:53Z — **19 seconds** — and failed.
- That run's logs are expired (HTTP 410) and its steps array is empty, so **which step
  failed cannot be recovered**. What survives is the job's three annotations: two
  `set-output` deprecation warnings and one failure, "Process completed with exit code 1".
  The warnings can only come from the two `::set-output` lines at the end of `Get WPID`,
  so the run reached at least that far. Where it stopped after that is unknown.
- Neither `sandbox-wp-db` nor `sandbox-wp.gh.io` contains a "Publish approved pathway" or
  "Add files for approved pathway" commit, so **the run pushed nothing anywhere**. The
  three pushes come before the `gh pr edit --add-body` step, so that step was never
  reached, whatever else is true of it.

**Consequently:** the fixes below are read out of the YAML. Each is a defect in its own
right, and none of them is offered as the cause of that run. The first successful run will
very likely turn up something none of us saw by reading.

The submission app depends on this pipeline. The app opens the PR and gives curators a
dashboard, but publication and WPID assignment belong to the target repository. If 3A does
not work, an approved submission never becomes a pathway.

## Do not put an expression in a run-block comment

Found by dispatching the repaired 3A for the first time. A `# FIX:` note inside a `run:` block
quoted the commit message it was replacing, and that quotation contained a GitHub expression.

A `run:` block is **one string value**. The runner substitutes expressions into its *text* before
bash ever sees any of it, so a leading `#` protects nothing — it is a bash comment, and the
substitution happens before bash exists. The expression did not parse, and an unparseable
expression does not fail a step; it fails the **entire workflow at startup**:

```
could not create workflow dispatch event: HTTP 422: Invalid Argument -
failed to parse workflow: (Line: 224, Col: 14): Unexpected symbol: '...wpid'
```

Line 224 is the `run:` line, not the comment eight lines below it, which is what makes this
expensive to find. The only other warning is a zero-job `failure` run that GitHub files when the
file lands, named by **path** rather than by workflow name — easy to mistake for noise.

This is the same mechanism as the security defect in workflow 1: `${{ }}` is text substitution,
not shell expansion. Worth internalising once rather than meeting twice.

The three staged workflows are now checked with a parser that walks every `run:` block and
rejects any expression that is not a plain reference, so this cannot recur silently.

## What changed in 3A

1. **The WPID was parsed out of a full path, and the failure is silent.**
   `sed -E 's/WP([0-9]+)__PR.*/\1/'` over `_drafts/WP5464__PR61.md` yields `_drafts/5464`,
   not `5464`. The next line, `[ "$WPID_NUM" -eq "0" ]`, does print "integer expression
   expected" — but a failing command in an `if` **condition** is exempt from `set -e`, and
   the original does not set `-e` anyway, so control simply falls to the else branch. There
   `WPID_NEW=$((WPID_NUM))` evaluates `_drafts/5464` as arithmetic, reads the unset name
   `_drafts` as 0, and assigns **0**. An edit would publish as **WP0**. For a new
   submission the same expression is `_drafts/0`, a division by zero, so `WPID_NEW` is
   never assigned at all. Reproduced in a plain shell. Fixed by taking the `basename`
   first and refusing anything that is not a number, rather than letting it become one.
2. **`gh pr edit --add-body` is not a flag.** `gh pr edit` has `--body` and `--body-file`
   and no append (`gh pr edit --help`). The step would exit 1 and take the `Close PR` step
   behind it with it. Replaced by reading the current body with `gh pr view --json body`
   and writing back the concatenation.
3. **`::set-output` is deprecated.** It still worked on the 2025-09-03 run — the two
   annotations it left are warnings, "will be disabled soon" — but GitHub has said it will
   stop working, so it is replaced by `>> "$GITHUB_OUTPUT"`.
4. **`sandbox-wp.gh.io` was checked out with `token: ${{ secrets.GITHUB_TOKEN }}`.** All
   three repositories are public, so that token can *read* them; what it cannot do is
   *push* to a repository other than the one the workflow runs in. 3B does the same
   checkout with `ssh-key: ${{ secrets.ACTIONS_SANDBOX_DEPLOY_KEY }}`; 3A now matches it.
5. **No `permissions:` block**, so the job took whatever the repository default is. Now
   `contents: write` and `pull-requests: write`, which is everything it does — PR comments
   and PR labels both fall under `pull-requests`, so `issues: write` is not needed.
6. **No `git pull --rebase` before the three pushes.** Workflow 1 pushes drafts to
   `sandbox-wp.gh.io` on every processed pull request, so a concurrent commit is ordinary
   and would make 3A's push non-fast-forward. 3B already rebases. The three checkouts also
   move to `fetch-depth: 0`: on the default depth-1 clone the local history is a single
   grafted commit and the rebase cannot be relied on, which is precisely the case the
   rebase exists for.
7. **The commit messages said `WP${{ steps.get_wpid.outputs.wpid }}`** while the output
   already carries the prefix, producing `WPWP1234`. The bare `git commit` also exits 1
   when there is nothing staged; `|| echo "No changes to commit"` matches 3B.
8. **`find` and `set -e` do not mix by default.** The strict mode this file now sets would
   have changed behaviour in three places, so each one is written to tolerate what the
   original tolerated: `find … | head -n 1` becomes `find … -print -quit` (with enough
   matching lines, `head` closing the pipe kills `find` with SIGPIPE and `pipefail` turns
   that into exit 141 — reproduced), `ls | grep | sort | tail` gets `|| true` plus an
   explicit emptiness check (`grep` exits 1 on no match), and the loops over
   `_data/drafts` and `draft_assets` are guarded with `[ -d … ]` and read from a process
   substitution, because a `find` on a missing directory exits 1 and would otherwise abort
   the step.

9. **Renaming a file does not change what it says it is** (added 2026-08-14, and the one
   change here that a curator would notice in the published data). `Rename and Move Files`
   renamed everything and rewrote the contents of the `.md` only. Everything else carried
   its draft identity into publication. Measured on all four pathways published from the
   portal, WP5426 through WP5429:

   | File | Said | Should say |
   |---|---|---|
   | `WP5429.gpml` | `Version="WP0001_r20260813082819"` | `WP5429_r20260813082819` |
   | `WP5429-info.json` | `"wpid": "WP0__PR37"` | `WP5429` |
   | `WP5429.json` | the draft slug and `WP0001_r…` throughout the pvjson | `WP5429` |
   | `WP5429.svg` | element ids `WP0__PR37`, `-icon`, `-text` | `WP5429…` |

   The directory and the filename said one thing and the file said another, which is the
   identity anything reading the data downstream trusts. Two substitutions, because the
   wrong string is not the same in both cases: the generators name their outputs after the
   draft file and so carry the slug, while anything derived from the GPML carries its old
   `Version`. The pvjson carries both. The GPML itself is handled with a short `python3`
   script rather than `sed`, because the opening `<Pathway>` tag may span lines and `sed`
   is line-based; it keeps whatever revision the file came in with and replaces only the
   `WP<n>` half, so it is a **no-op for an edit**, whose GPML already names its own id.
   `.png` files are skipped.

   The same script fixes two smaller things in the same tag, since it is already open there.
   **`Last-Modified` is refreshed**, taken from the revision the `Version` already carries so
   the two agree and the stamp is when the pathway was last *edited* rather than when it
   happened to be approved; a revision that is not a 14-digit stamp (a MediaWiki revision
   number on an older file) falls back to the run time. WP5429 shipped with
   `Last-Modified="20220717141800"`, four years stale, because nothing had ever refreshed it.
   And **`Data-Source` is filled in when absent** — every published pathway checked carries
   `WikiPathways` and a submitter's file may simply omit it, as WP5429's did. Never
   overwritten: a file recording where it genuinely came from has to keep saying so, and this
   step cannot tell that apart from an omission. Checked with a `Data-Source="Reactome"` file,
   which comes through untouched.

   This has to happen here and nowhere else: the publish push is made with `GITHUB_TOKEN`,
   which starts no further workflow run, so nothing downstream ever re-derives these files.
   Checked by running the step against the real WP5429 artifacts, and against a GPML with no
   `Version` attribute, one whose `Version` has no `WP` prefix, one with an empty `Version`,
   and one with no `<Pathway>` root at all — the last stops the publication rather than
   guessing.
10. **The publish commits were anonymous** (added 2026-08-14). All three were
    `GitHub Action <action@github.com>`; WP5425 through WP5429 show up under the
    `actions-user` account, so the person who drew the pathway appears nowhere in the
    history of the repository that holds it. A new `Resolve the submitter` step reads the
    pull request's author and the three pushes commit as them, both author and committer.

    The address is `<id>+<login>@users.noreply.github.com`, which is what GitHub itself
    writes on commits made through its web interface: it resolves to the account whatever
    that account's email-privacy setting is, and it is what makes the commit count as a
    contribution. A guessed public address does neither. Nothing about authentication
    changes — the push is still made with the deploy key, and the commit is unsigned exactly
    as before.

    The pull request's author is the right answer for a fork submission, a pull request
    opened by hand, and the PathVisio plugin. Where a portal opens pull requests under its
    own app identity the author is a bot and the human is not recoverable from here, so the
    step falls back to the action rather than guessing. Checked against three real pull
    requests on the fork: #37 resolves to `egonw`, #39 to `MadhushriMSV`, #34 to the
    fallback. `git pull --rebase` replays the commit under the same settings, so the
    identity survives a concurrent push — also checked, in a throwaway repository.

Beyond the defect list, 3A gains three things it did not have:

- A **guard** that refuses to publish when `pathways/WP<new>/` already exists on `main`
  for a newly allocated id, which would otherwise overwrite a published pathway.
- A **check that the draft page was actually found**, by counting the files the first loop
  moved. Testing the target directory for emptiness instead would be a no-op for an edit,
  since that directory already holds the previous publication.
- The **publish marker comment and its failure counterpart**, described below.

Two things deliberately *not* added:

- **No `concurrency:` group.** Serialising publication would stop two approvals reading
  the same `max(WP*)`, but GitHub keeps at most one *pending* run per group: with one run
  going and one queued, a third approval cancels the queued one, and a cancelled run never
  reaches the `if: failure()` step — that submission would vanish with no comment and no
  label. A duplicate id surfaces loudly instead: the second run either finds the target
  directory already there, or ends up adding the same file path as the first, so its
  rebase conflicts and its push fails with a comment on the PR.
- **No check for the `test pathway` label.** 3A does not look at it and this change does
  not add it, so applying `accepted` to a pathway labelled "do not publish" still
  publishes it. Worth deciding upstream; it is not something to slip in silently.

## What changed in the dispatcher

1. **The trigger moves from `pull_request` to `pull_request_target`.** Most submissions
   here come from a fork, and for a `pull_request` event on a fork GitHub caps
   `GITHUB_TOKEN` at read — a `permissions:` block cannot raise it — so `gh workflow run`
   is refused and the label does nothing. This is the one part of the pipeline where the
   evidence survives: run `26719401516` (2026-05-31, `accepted` on PR #45, head
   `mkutmon/sandbox-wp-db`) logs every token scope as read and then
   `HTTP 403: Resource not accessible by integration` on the dispatch. The two runs that
   succeeded, `16786503153` and `17838390305`, both came from branches inside
   `wikipathways/sandbox-wp-db` itself. A `pull_request_target` run of workflow 1 on
   2026-07-15 shows the repository's own default is `Actions: write`, so the read-only
   token on the fork run is the fork cap and not a repository setting. Two further
   dispatcher failures (2025-08-20 and 2025-09-18) came from in-repo branches and their
   logs are expired, so those have no explanation.
   `pull_request_target` is only safe here because the job checks nothing out and runs no
   code from the pull request — it reads two fields off the event and calls the API. Do
   not add a checkout step to it.
2. **An explicit `permissions:` block** (`actions: write`, `contents: read`). In the base
   context the token is not capped, so today this only narrows what it gets; it also keeps
   the workflow working if the repository default is ever tightened.
3. **The `resubmitted` case passed the wrong input name.** It sent `-f pr_number=`, but
   workflow 1's `workflow_dispatch` input is `manual-pr-number`, so GitHub rejected the
   call. The name is corrected and the case kept: workflow 1's `pull_request_target`
   trigger is filtered to `paths: ['**/*.gpml']`, so a push that does not touch a GPML
   never reprocesses a submission, and this label is the only way to ask for it by hand.
4. **`${{ github.event.label.name }}` was interpolated straight into the shell.** Labels
   are repository-controlled so this is hardening rather than a live hole, but it is the
   same shape the app's own `mvp1/pr-preview.yml` documents avoiding. Values now travel
   through `env:`.

## What changed in on_gpml_change

> [!warning] **This one is staged as the fork's copy, and must not go upstream verbatim.**
> Unlike the other files here, `on_gpml_change.yml` names no repositories at all, so there is
> nothing to rewrite between fork and upstream — but three of its four sync jobs
> (`sync-site-repo-added-modified`, `sync-assets-repo-deleted`, `sync-site-repo-deleted`) have
> been **stubbed out on the fork** and do nothing but `echo "SANDBOX: Skipping…"`. Only
> `sync-database-repo-deleted` still does real work. The change below is the part worth carrying
> to `wikipathways/sandbox-wp-db`; the stubs are emphatically not. Staged in full anyway, so the
> file is under `tests/test_sandbox_workflows.py` and a diff against the fork is meaningful.

1. **`git rm` is not idempotent, and the non-idempotent case is the ordinary one.**
   `git rm pathways/"$wpid"/*` exits **128** with `pathspec … did not match any files` when the
   path is already gone, and the loop dies on the first such name. That is not a rare race: the
   curation portal removes the `WP0001` submission placeholder from the default branch itself,
   whenever somebody merges a pipeline pull request instead of letting it close — and the commit
   that removes it is the same commit this job then reacts to, so the file it is told to delete
   is already deleted. Every hand-merge therefore produces a red run *because the repair worked*.
   Observed 2026-08-13, run `31712062937`, after PR #37 was merged by hand.

   Fixed with `git rm -r --ignore-unmatch`. `-r` because the argument is a directory's worth of
   files. Measured in a throwaway repository: the old form exits 128 with the path absent, the
   new form exits 0 with the path absent and still stages both files with the path present.

> [!caution] **Not changed, and worth a decision: `git push --force` onto the default branch.**
> The same job ends `git pull --rebase && git push --force`, against `main` of the content
> repository, from a `fetch-depth: 1` checkout. A forced push to the branch that holds every
> published pathway is a large weapon for a job whose purpose is deleting a directory, and the
> shallow clone is exactly the condition under which the preceding rebase is least trustworthy —
> the same reasoning that moved 3A's checkouts to `fetch-depth: 0`. It has not misfired, and it
> is left alone here rather than changed on a guess: unlike the `git rm` above there is no
> observed failure to measure a fix against. Raise it upstream with the rest.

## The marker comment

3A posts a comment carrying a machine-readable marker:

```
<!-- wikipathways-publish {"pr":54,"wpid":5678,"status":"published"} -->
Published as [WP5678](https://sandbox.wikipathways.org/pathways/WP5678).
```

and, on failure, an `if: failure()` counterpart:

```
<!-- wikipathways-publish {"pr":54,"status":"failed","step":"push-sandbox-wp-db"} -->
```

This is the contract between the pipeline and the submission app. The app must not recover
the assigned WPID by parsing English out of the PR description: workflow 1 overwrites the
description with `gh pr edit --body` on every run, so anything written there disappears the
next time the submitter pushes. Comments survive. The `published` and `publish failed`
labels make the same state visible in GitHub's own UI.

The failure comment says what actually happened, which depends on how far the run got. The
draft files are **moved**, not copied, and `sandbox-wp.gh.io` is pushed first, so:

- Failure before that push: nothing was pushed anywhere. 3A **removes the `accepted`
  label** so that applying it again re-fires `labeled` and starts a fresh run — without
  that, re-approving does nothing, since re-applying a label the PR already carries emits
  no `labeled` event.
- Failure after it: the drafts have already been moved into the published folders of the
  website repository and `sandbox-wp-db` did not get them. A re-run stops at `Get WPID`.
  The comment says so, and the label is left in place; the two repositories have to be
  brought back in line by hand.
- Failure after both pushes: the pathway is published and only the tail of the workflow
  did not finish. Nothing needs re-running.

## Testing it

`workflow_dispatch` **runs the copy of the workflow file on the ref you dispatch**, not
the copy on the default branch:

```bash
gh workflow run 3a_approved_pull_request.yml \
  -R wikipathways/sandbox-wp-db --ref fix/publish-workflow -f pr_number=NN
```

The default-branch rule that is easy to confuse this with governs only whether a workflow
is *dispatchable at all* — a workflow that exists nowhere on the default branch cannot be
started this way. 3A is already on `main`, so it is dispatchable, and a rewritten copy on
a branch can be run without merging first. Test on the branch.

That is also the manual fallback when a run has failed and the label route is not
available: dispatching 3A by hand takes the same `pr_number` the dispatcher would pass.

Two cautions about the first run:

- **It publishes for real.** 3A pushes straight to `main` in two repositories and closes
  the PR; there is no dry-run mode and no undo beyond a revert commit. Use a submission
  that is meant to be published and that you are willing to clean up.
- **Do not use a pathway labelled `test pathway`.** That label reads "This is a test or
  tutorial pathway; do not publish", and 3A does not check for it, so publishing one is
  exactly what would happen.

The dispatcher cannot be tested the same way. `pull_request_target` takes the workflow
file from the **base** branch, so the change only takes effect once it is on `main`; after
that it applies to already-open pull requests immediately.

## Opening the pull request

Marvin has push access to `sandbox-wp-db` (`push: true`, verified), so a branch in the
repository works and no fork is needed. From a clone of the target repository:

```bash
git clone git@github.com:wikipathways/sandbox-wp-db.git
cd sandbox-wp-db
git checkout -b fix/publish-workflow

CURATOR=~/Documents/Services/WikiPathways/wikipathways-curator
cp "$CURATOR"/sandbox-workflows/.github/workflows/*.yml .github/workflows/

git add .github/workflows
git commit -m "Repair the approved-PR publish workflow and the label dispatcher"
git push -u origin fix/publish-workflow
gh pr create --fill
```

Then create the two labels from `labels.md`.

## What still needs someone with more access

**A write credential for `sandbox-wp-assets`.** 3A pushes to
`wikipathways/sandbox-wp-assets`, and Marvin has read-only access there
(`push: false`, verified). The workflow expects a deploy key with write access under
`ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY`, matching what `ACTIONS_SANDBOX_DEPLOY_KEY` already
does for `sandbox-wp.gh.io`. Only an admin on `sandbox-wp-assets` can create it.

Until that secret exists the expression is empty, `actions/checkout` falls back to the
job's `GITHUB_TOKEN`, and the checkout of that public repository succeeds — it is the push
that cannot work. Both steps are `continue-on-error`, so a publication is not held up by
it. The cost of running without the credential is worth stating plainly: the draft assets
are **moved** out of `draft_assets/`, so once the website repository is pushed, the SVG —
the one file that is not also copied into `sandbox-wp-db` — is no longer in the working
tree of any repository. It stays in `sandbox-wp.gh.io`'s git history and can be recovered
from there, but it is not published anywhere until the assets push works. This is how the
original behaves too; it is not introduced here.

`ACTIONS_SANDBOX_DEPLOY_KEY` needs no such confirmation: `1_on_pull_request.yml` uses the
same secret to push to `sandbox-wp.gh.io`, and the most recent such commit is 2026-07-15,
so the key still has write access.

**Both credentials now exist on the fork**, which is the closest thing to a rehearsal available
without org access. `marvinm2/sandbox-wp.gh.io` and `marvinm2/sandbox-wp-assets` were forked on
2026-07-29 and each carries its own write-enabled deploy key, under exactly the two secret names
above. That is what turned run `30451444585` into the first all-green workflow 1 anywhere,
`commit-outputs` included. The assets key is still unexercised — 3A has not been run since — but
the shape an admin would have to reproduce upstream is now written down and demonstrated rather
than proposed. See `docs/sandbox-pipeline.md` §7.

## How the claims here were checked

```bash
# The one 3A run: event, actor, timing, and the fact that its logs are gone
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17442557461 \
  --jq '{event, actor: .actor.login, head_branch, created_at, updated_at, conclusion}'
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17442557461/jobs \
  --jq '.jobs[] | {name, conclusion, steps: (.steps | length)}'
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17442557461/logs        # HTTP 410
gh api repos/wikipathways/sandbox-wp-db/check-runs/49528874274/annotations \
  --jq '.[] | {level: .annotation_level, message}'

# Nothing was ever published
gh api 'repos/wikipathways/sandbox-wp-db/commits?per_page=100' --jq '.[].commit.message'
gh api 'repos/wikipathways/sandbox-wp.gh.io/commits?per_page=100' --jq '.[].commit.message'

# The dispatcher: which runs failed, from where, and the one surviving log
gh run list -R wikipathways/sandbox-wp-db --workflow pr_label_dispatcher.yml --limit 10 \
  --json databaseId,conclusion,createdAt,displayTitle
gh api repos/wikipathways/sandbox-wp-db/actions/runs/26719401516 \
  --jq '{head_repository: .head_repository.full_name, conclusion}'
gh api repos/wikipathways/sandbox-wp-db/actions/runs/26719401516/attempts/1/logs > log.zip

# The repository default token permissions, seen from a pull_request_target run
gh api repos/wikipathways/sandbox-wp-db/actions/runs/29390429466/logs > wf1.zip
# ... GITHUB_TOKEN Permissions: Actions: write, Contents: write, PullRequests: write ...

# All three repositories are public, and what access we have
for r in sandbox-wp-db sandbox-wp.gh.io sandbox-wp-assets; do
  gh api repos/wikipathways/$r --jq '.name + " " + .visibility'
  gh api repos/wikipathways/$r --jq .permissions
done

# The label vocabulary, including the colours and `test pathway`'s description
gh api repos/wikipathways/sandbox-wp-db/labels --jq '.[] | "\(.name)\t\(.color)\t\(.description)"'

# `gh pr edit` has no --add-body
gh pr edit --help
```

The shell behaviour claims — the `-eq` mis-assignment, the SIGPIPE exit 141, `find` on a
missing directory under `set -e` — were reproduced in a local shell rather than reasoned
about, and the `Get WPID` and `Rename and Move Files` blocks were run against a fixture
tree for a new pathway, an edit, an edit over an existing publication, a missing
`_data/drafts`, a missing `draft_assets`, an empty `_pathways/`, an unparseable slug, and
a PR with no draft.
