# GitHub App (bot) setup

The portal acts on GitHub as **two identities, on purpose**:

- **Per-user OAuth** ([`oauth-setup.md`](oauth-setup.md)) pushes the branch and opens the pull
  request *as the submitter*, so authorship is real.
- **The GitHub App** (this doc) does what must not be tied to one person's token: it applies the
  `accepted` / `rejected` labels (pipeline mode) or merges (direct mode) when a curator approves,
  posts and edits the portal's comments, reads pull requests, conversations and workflow runs for
  the background loop, and receives webhooks. It also pushes the branch itself when a submitter's
  fork cannot be used.

Without it, approve and reject return **503**, comments are skipped and the background loop does
not start. Submitting still works.

The deployment at curation.wikipathways.org uses `wikipathways-submit-bot-dev` (App ID 4403728,
owned by the `marvinm2` account), installed on the `wikipathways` organisation as installation
149425545 with "Only select repositories". Registering a fresh App is only needed for a new
deployment or a local test target.

## 1. Register the App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Field | Value |
|---|---|
| Homepage URL | the portal's public URL |
| Webhook: Active | yes |
| Webhook URL | `https://<host>/webhooks/github` |
| Webhook secret | a random string; it becomes `PORTAL_GITHUB_WEBHOOK_SECRET` |

**Repository permissions:**

| Permission | Access | Why |
|---|---|---|
| Contents | Read and write | read pathway files and the WPID floor; push a branch when a fork cannot be used; merge in direct mode; the `push` event |
| Pull requests | Read and write | read pull requests and their files, open them as the bot, merge in direct mode; the review events |
| Issues | Read and write | comments and labels (pull request comments and labels are issue API calls); the `issue_comment` event |
| Actions | Read-only | workflow runs and their artifacts; needed to subscribe to `workflow_run` |
| Metadata | Read-only | mandatory |

**Organization permissions**, only when curators come from a GitHub team:

| Permission | Access | Why |
|---|---|---|
| Members | Read-only | list the curator team (`PORTAL_CURATOR_TEAM`) |

**Subscribe to events:** Pull request, Issue comment, Pull request review, Pull request review
comment, Workflow run, Push. Each one shows up only once the matching permission above is
granted.

Then:

1. **Generate a private key.** It downloads a `.pem`; keep it out of the repository.
2. Note the **App ID** from the App's page.
3. **Install** the App on the content repository only, and copy the **installation ID** from the
   installation's URL (`.../installations/<id>`).

## 2. Configure the portal

Every variable is read as `PORTAL_*` first and `WPSUBMIT_*` second (the prefix the service was
first deployed with; see `app/config.py`). Prefer a key-file path over an inline PEM, so the key
is a mounted Docker secret and never an environment value:

```bash
PORTAL_GITHUB_APP_ID=123456
PORTAL_GITHUB_APP_INSTALLATION_ID=78901234
PORTAL_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/portal_app_key
# local testing only:
# PORTAL_GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
```

All three (App ID, installation ID, a key) must be present, or the App stays off.

## Webhooks

`POST /webhooks/github` verifies every delivery with HMAC-SHA256 over the raw body
(`X-Hub-Signature-256`) against `PORTAL_GITHUB_WEBHOOK_SECRET`: a missing or wrong signature is
401, and no secret configured is 503. A delivery whose `repository.full_name` is not
`PORTAL_CONTENT_REPO` is acknowledged and ignored, since one App installation can cover several
repositories. The handler acknowledges first and does the work in the background.

| Event | What the portal does |
|---|---|
| `pull_request` | settles a closed review (published, merged or rejected), follows `accepted`/`rejected` labels applied on GitHub, sends a review back to open when its head moves after approval, and re-lists untracked pull requests |
| `issue_comment`, `pull_request_review`, `pull_request_review_comment` | refreshes the review's conversation; a curator's "changes requested" review on GitHub becomes the portal's changes-requested state |
| `workflow_run` | marks the review due, so the repository's verdicts are read without waiting for the loop |
| `push` | marks open updates of a pathway whose GPML changed on the default branch, so an approval made against the old version is withdrawn |

The background loop (`PORTAL_BACKGROUND_INTERVAL_SECONDS`, default 60) re-checks everything
anyway, so a missed delivery delays a state change by a minute rather than losing it. Locks and
WPID reservations also expire on their own after 14 days (`PORTAL_PATHWAY_LOCK_TTL_DAYS`,
`PORTAL_WPID_RESERVATION_TTL_DAYS`).

## Curators

```bash
PORTAL_CURATORS='["alice","bob"]'            # JSON list of GitHub logins
PORTAL_CURATOR_TEAM=wikipathways/curators    # optional: org/team-slug
```

The two **add up**. A team's members are fetched through the App (organisation **Members:
read**) and cached for 5 minutes, so a curator is added or removed on GitHub without a redeploy.
A team that cannot be read (it does not exist, the permission is missing, GitHub is down and
nothing is cached) adds nobody, and the listed logins remain curators. That makes it safe to set
the team before an organisation owner has created it, and to empty the list only once the team
resolves.

## 3. On the cluster

The private key is a Docker secret:

```bash
docker secret create portal_app_key ./app.private-key.pem
# service definition:
#   --secret portal_app_key
#   --env PORTAL_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/portal_app_key
```

The live service still uses the original names (`wpsubmit_app_key`,
`WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH`), which keep working. See
[`deployment.md`](deployment.md).

## How the code authenticates

`app/auth/github_app.py`:

```
sign an RS256 JWT with the App's private key   (iss = App ID, exp ≤ 10 min)
      │
      ▼
POST /app/installations/{id}/access_tokens (Bearer <JWT>)  →  installation token (~1 h)
      │  cached until about 60 s before it expires
      ▼
HttpGitHubClient(installation_token, identity="bot")
```

`get_bot_client` (strict: approve and reject return 503 without it) and `get_bot_optional`
(comments are skipped without it) both build the client from `app.state.bot_app`. A 401 on the
App's own token is reported as a server configuration problem (503), never as "sign in again".
