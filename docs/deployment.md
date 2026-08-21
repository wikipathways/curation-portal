# Deployment — Strato VHP4Safety cluster (issue #5)

Follows the cluster conventions (image → GHCR so both nodes can pull, `core` overlay network,
GlusterFS-backed data, **no node pinning**, secrets as Docker secrets, Traefik-routed). The
authoritative cluster docs live at `/mnt/gluster/documentation/` on `tgx1` — read `AGENTS.md`
and `operations/infrastructure-guide.md` before deploying; this file is the service-specific
recipe.

> Status (2026-08-21): deployed and live. The canonical hostname is
> **`curation.wikipathways.org`**; `upload.wikipathways.org` was the original name and is still
> routed to the same service, because every pull request comment the app has posted links to it
> absolutely. One Let's Encrypt certificate covers both.

## Hostname and DNS

The service answers on **`curation.wikipathways.org`** (canonical, added 2026-08-21) and
`upload.wikipathways.org` (original, kept). The router rule names both and Traefik issues a single
certificate with both in its SAN list:

```
traefik.http.routers.wikipathways-submit.rule=Host(`upload.wikipathways.org`) || Host(`curation.wikipathways.org`)
```

Neither is a `*.cloud.vhp4safety.nl` name. That zone is run by
WikiPathways on Cloudflare, not by Strato, which changes two things:

- **There is no wildcard to rely on.** `*.wikipathways.org` is proxied through Cloudflare to
  GitHub Pages, so an unconfigured name silently answers as a GitHub 404 rather than not
  resolving. Verify with `dig +short upload.wikipathways.org A`: the cluster is reachable only
  when that returns `81.169.246.233`, not Cloudflare addresses (`104.21.x.x` / `172.67.x.x`).
- **The record must be DNS-only (grey cloud).** Traefik issues certificates over HTTP-01, so
  Let's Encrypt has to reach tgx1 directly. Proxied through Cloudflare the challenge cannot
  complete. `curation.wikipathways.org` was created **proxied** on 2026-08-21 and *appeared* to
  work — Cloudflare answered 200 from the origin — so check `server:` and `cf-ray` in the response
  headers before concluding you measured the cluster, and probe the origin directly with
  `curl --resolve <host>:443:81.169.246.233`. A local resolver will also keep serving the old
  answer for minutes after `dig @1.1.1.1` has the new one. `sandbox.wikipathways.org` and `classic.wikipathways.org` are already DNS-only in
  that zone and are the precedent to point at.

Per the cluster's `AGENTS.md`, **add the Traefik router labels only after DNS resolves to the
cluster** — a Host rule on a name that does not point here makes the ACME challenge loop. Deploy
the service without them first, then add them with `docker service update`.

Check the origin is reachable before adding the router:

```bash
curl -sI -H "Host: upload.wikipathways.org" http://81.169.246.233/     # expect Traefik's 308
```

## Image (CI → GHCR)

`.github/workflows/docker-publish.yml` builds on every push to `main` and publishes:

```
ghcr.io/marvinm2/wikipathways-submit:latest
ghcr.io/marvinm2/wikipathways-submit:<sha>
```

Make the GHCR package **public** once (so the cluster pulls without auth), or deploy with
`--with-registry-auth`. `.github/workflows/ci.yml` runs ruff + pytest on every push/PR.

## Datastore — deployed

PostgreSQL (SQLite is dev-only). Running as its own swarm service, following the pattern the
cluster's other Postgres services use — a `local` volume bind-mounted onto GlusterFS, and
`stop-first` so two containers never open the same data directory:

```bash
mkdir -p /mnt/gluster/docker/wikipathways-submit-db/data/db_data

docker service create \
  --name wikipathways-submit-db \
  --network core \
  --replicas 1 \
  --update-order stop-first \
  --restart-condition on-failure \
  --secret wpsubmit_db_password \
  --env POSTGRES_USER=wpsubmit \
  --env POSTGRES_DB=wpsubmit \
  --env POSTGRES_PASSWORD_FILE=/run/secrets/wpsubmit_db_password \
  --env PGDATA=/var/lib/postgresql/data/pgdata \
  --mount 'type=volume,source=wikipathways-submit-db-data,target=/var/lib/postgresql/data,volume-driver=local,volume-opt=type=none,volume-opt=o=bind,volume-opt=device=/mnt/gluster/docker/wikipathways-submit-db/data/db_data' \
  postgres:16
```

`PGDATA` points at a subdirectory of the mount because Postgres refuses to initialise into a
directory that is itself a mount point. The app entrypoint runs `alembic upgrade head` before
serving whenever the URL is Postgres (see `docs/migrations.md`).

## Secrets (Docker secrets, never in the repo)

Generate the machine-generated ones **on the node**, so the values never travel:

```bash
umask 077; TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
openssl rand -base64 33 | tr -d '\n/+=' | cut -c1-32 > "$TMP/dbpass"
openssl rand -base64 48 | tr -d '\n'                 > "$TMP/session"
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end='')" > "$TMP/fernet"
printf 'postgresql+psycopg://wpsubmit:%s@wikipathways-submit-db:5432/wpsubmit' "$(cat "$TMP/dbpass")" > "$TMP/dburl"

docker secret create wpsubmit_db_password           "$TMP/dbpass"
docker secret create wpsubmit_session_secret        "$TMP/session"
docker secret create wpsubmit_token_encryption_key  "$TMP/fernet"
docker secret create wpsubmit_database_url          "$TMP/dburl"
```

Those four exist already. The remaining three come from the GitHub-side registration
(`docs/oauth-setup.md`, `docs/github-app-setup.md`) and still have to be created:

```bash
docker secret create wpsubmit_oauth_client_secret ./oauth_client_secret.txt
docker secret create wpsubmit_webhook_secret      ./webhook_secret.txt
docker secret create wpsubmit_app_key             ./wikipathways-submit-bot.private-key.pem
```

The App private key stays a **path** (`WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH`), never an env value.

The entrypoint hydrates `WPSUBMIT_SESSION_SECRET`, `WPSUBMIT_GITHUB_OAUTH_CLIENT_SECRET`,
`WPSUBMIT_GITHUB_WEBHOOK_SECRET`, `WPSUBMIT_TOKEN_ENCRYPTION_KEY`, and `WPSUBMIT_DATABASE_URL`
from `/run/secrets/*`. The App private key stays a **path** (`WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH`).

## Deploy — stage 1, no router

Deploy the app first without Traefik labels, so the image, the secrets and the Alembic migration
are all proven against the real Postgres before DNS is in play.

```bash
mkdir -p /mnt/gluster/docker/wikipathways-submit/data   # preview/draft cache

docker service create \
  --name wikipathways-submit \
  --network core \
  --replicas 1 \
  --restart-condition on-failure \
  --secret wpsubmit_session_secret \
  --secret wpsubmit_token_encryption_key \
  --secret wpsubmit_database_url \
  --secret wpsubmit_oauth_client_secret \
  --secret wpsubmit_webhook_secret \
  --secret wpsubmit_app_key \
  --mount type=bind,source=/mnt/gluster/docker/wikipathways-submit/data,target=/data \
  --env WPSUBMIT_CONTENT_REPO=wikipathways/sandbox-wp-db \
  --env WPSUBMIT_DRAFTS_REPO=wikipathways/sandbox-wp.gh.io \
  --env WPSUBMIT_DRAFTS_SITE_BASE_URL=https://sandbox.wikipathways.org \
  --env WPSUBMIT_PUBLISH_MODE=pipeline \
  --env WPSUBMIT_SUBMIT_IDENTITY=bot \
  --env WPSUBMIT_REQUIRE_PREVIEW_CHECK=false \
  --env WPSUBMIT_APP_BASE_URL=https://curation.wikipathways.org \
  --env WPSUBMIT_OAUTH_REDIRECT_URI=https://curation.wikipathways.org/auth/callback \
  --env WPSUBMIT_SESSION_HTTPS_ONLY=true \
  --env WPSUBMIT_PREVIEW_CACHE_DIR=/data/preview-cache \
  --env WPSUBMIT_GITHUB_OAUTH_CLIENT_ID=<oauth-app-client-id> \
  --env WPSUBMIT_GITHUB_APP_ID=<app-id> \
  --env WPSUBMIT_GITHUB_APP_INSTALLATION_ID=<installation-id> \
  --env WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/wpsubmit_app_key \
  --env WPSUBMIT_CURATOR_TEAM=wikipathways/curators \
  --with-registry-auth \
  ghcr.io/marvinm2/wikipathways-submit:latest
```

`WPSUBMIT_REQUIRE_PREVIEW_CHECK=false` is not optional here. It defaults to true and gates on
`pr-preview.yml`, which does not exist on `sandbox-wp-db` and never will — left on, every
approval returns 409.

**The two `WPSUBMIT_DRAFTS_*` values move with `WPSUBMIT_CONTENT_REPO`.** They are shown at their
defaults above, so the command works as written; the point of spelling them out is that a
deployment aimed at a *fork* of the content repo must repoint them at that fork's own site repo
too. The target repo pushes its rendered drafts into a sister site repo, and the app reads them
back from there — so leaving these at the org's site makes the dashboard's pipeline panel and
"Draft page" link permanently and silently empty, because a missing draft is the ordinary case
and the reader degrades quietly rather than erroring. Standing up the fork's side of this
(sister-repo forks, deploy keys, Pages) is `docs/sandbox-pipeline.md` §7.

### `WPSUBMIT_SITE_NOTICE` — say so when the target cannot publish

A standing banner on every page. Empty (the default) renders nothing.

Set it on any deployment whose target repository cannot actually complete a publication — a
sandbox, or a fork that lacks the sister-repo credentials the publish workflow pushes with. The
submit page tells people the database will publish their pathway and assign its WPID; where that
is not true, this is the only thing that says so.

The prompt for it, on 2026-07-28: a pathway arrived from an unfamiliar account through
`upload.wikipathways.org` while it pointed at a fork where neither the publish workflow nor the
rejection workflow can close a pull request. That one turned out to be a colleague testing, so
nobody lost anything — but from inside the app it was indistinguishable from a real submission,
and had it been real it would have gone nowhere with nothing on screen to say so. That is the
case for the banner: by the time you can tell the two apart, the silent failure has happened.

```bash
docker service update \
  --env-add 'WPSUBMIT_SITE_NOTICE=Sandbox deployment. Submissions open a real pull request but are not published to WikiPathways yet, and no WPID is assigned. Please do not rely on this for work you need published.' \
  wikipathways-submit
```

It is free text rather than something derived from `WPSUBMIT_PUBLISH_MODE`, because whether a
target *can* publish depends on credentials held by other repositories that this app cannot see.

### `WPSUBMIT_SUBMIT_RATE_LIMIT` — how many pull requests one account may open

Ten per `WPSUBMIT_SUBMIT_RATE_WINDOW_MINUTES` (default 60) by default; `0` disables it. Both
defaults are fine for a sandbox and neither needs setting to deploy.

The bound exists because the cost of getting it wrong is paid by the content repository rather
than by this app: branches, a notification to every watcher, and one run of a full generation
pipeline per submission, all cleaned up by hand by its maintainers. It does not take malice — a
retry loop in a script, or a submitter double-clicking through a slow response, produces the same
thing more slowly.

Counted out of the `review` table rather than an in-process bucket, so it survives a redeploy,
and keyed on the GitHub login rather than the address, since these endpoints are authenticated
and one person behind a shared address is not several submitters. Raise it if a real curation
session ever hits it; ten an hour is far above the audit's observed rate of 51 pull requests in
three months across everybody.

Two things it deliberately does not cover. Re-uploading onto a pull request that already exists
is exempt, because it opens nothing and is how a submitter answers a change request. And
`/api/validate` has no login to key on, so it wants a blunt per-address bound at Traefik if it
ever needs one — it does parse-and-render work without reaching GitHub at all.

### `WPSUBMIT_SUBMIT_IDENTITY` — whose pull request it is

Three values, differing in one thing: which repository holds the submission branch.

| value | branch lives on | the pull request belongs to |
|---|---|---|
| `user` (default) | the content repo, pushed with the submitter's token | the submitter — but only works where they have push access |
| `bot` | the content repo, pushed by the GitHub App | **the bot** |
| `fork` | the submitter's own fork | the submitter |

`bot` is what a shared content repository forced before fork mode existed: an ordinary
contributor has no push access, so the App pushes for them. Authorship survives on the *commit*,
but the pull request is the bot's — it is who GitHub notifies, who can edit the description, who
can close it, and every submission then appears to come from one account. That is the
contribution history the audit behind this project was partly trying to un-flatten.

`fork` is the ordinary way to contribute to a repository you cannot write to, and on the content
repo it is already the norm: 36 of the last 53 closed pull requests there came from contributor
forks. **It needs no scope the app does not already request** — GitHub defines `public_repo`,
which `OAUTH_SCOPE` has always included, as read/write to code on public repositories, and that
covers creating a fork of one and pushing to it. Submitters see no new consent screen.

Three behaviours worth knowing before turning it on:

- **It falls back to `bot`, before writing anything.** Forking can fail for reasons that have
  nothing to do with the submitter — an organisation that forbids it, a token revoked between
  login and submission, GitHub being slow to create the repository. The fork is resolved before
  the first write, so falling back costs nothing and the submission proceeds as it does today.
  A failure *after* writing has begun is not retried against a different repository; see
  `app/submit/targets.py` for why that boundary is where it is.
- **The owner of the content repo never forks it.** GitHub refuses to fork a repository into the
  account that owns it, and that account has push access anyway. This matters on the current
  deployment, where the target is `marvinm2/sandbox-wp-db` and `marvinm2` is who tests it.
- **A revise always uses the submitter's own token when the branch is on a fork**, whatever this
  setting says, because a GitHub App installation token cannot push to a personal fork — the App
  is not installed there.

Switching between values is safe and needs no migration: `Review.head_repo` records where each
submission's branch actually went, so pull requests opened under one setting keep working after
a change to another.

Verify from inside the overlay network, since nothing is routed yet:

```bash
docker service logs wikipathways-submit | grep -i alembic     # migrations ran
docker run --rm --network core curlimages/curl:latest -sS http://wikipathways-submit:8000/health
```

## Deploy — stage 2, add the router once DNS lands

Only after `dig +short upload.wikipathways.org A` returns `81.169.246.233`:

```bash
docker service update \
  --label-add traefik.enable=true \
  --label-add 'traefik.http.routers.wikipathways-submit.rule=Host(`upload.wikipathways.org`)' \
  --label-add traefik.http.routers.wikipathways-submit.entrypoints=websecure \
  --label-add traefik.http.routers.wikipathways-submit.tls=true \
  --label-add traefik.http.routers.wikipathways-submit.tls.certresolver=letsencrypt \
  --label-add traefik.http.services.wikipathways-submit.loadbalancer.server.port=8000 \
  --label-add traefik.docker.network=core \
  wikipathways-submit

curl -sI https://upload.wikipathways.org/health
```

## Turning the webhook on (issue #8)

Everything in the app is already built for this: `docker-entrypoint.sh` loads
`/run/secrets/wpsubmit_webhook_secret` into `WPSUBMIT_GITHUB_WEBHOOK_SECRET`, and
`POST /webhooks/github` verifies HMAC-SHA256 over the raw body. **The only missing piece is the
secret itself.** Until it exists the endpoint answers **503** and nothing is lost — but nothing
is heard either, so a pull request closed outside the app sits in the queue until its TTL.

Confirm the current state before and after:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://upload.wikipathways.org/webhooks/github
# 503 = no secret configured.  401 = configured, and this unsigned request was correctly refused.
```

**401 is the success signal.** An unsigned `curl` *should* be rejected once the secret is set; a
200 there would mean the signature check is not running.

### 1. Generate the secret on the node

This one differs from the other four: GitHub needs the same value, so unlike the session key or
the Fernet key it cannot be write-only. Generate it once on tgx1, keep it on screen long enough
to paste into GitHub in step 3, and do not save it anywhere else.

```bash
ssh tgx1
SECRET=$(openssl rand -hex 32)
printf '%s' "$SECRET" | docker secret create wpsubmit_webhook_secret -
echo "$SECRET"          # paste this into the GitHub App in step 3
docker secret ls | grep wpsubmit_webhook_secret
```

`printf '%s'` rather than `echo` matters. `echo` appends a newline, that newline becomes part of
the stored secret, GitHub signs without it, and every delivery then fails the HMAC check with a
401 that looks exactly like a wrong secret.

If you lose the value before step 3, read it back from the running container once step 2 has
attached it — or just delete the secret and start over, which is cheaper:

```bash
docker exec $(docker ps -q -f name=wikipathways-submit.1) cat /run/secrets/wpsubmit_webhook_secret; echo
```

### 2. Attach it to the service

A Swarm secret cannot be added to a running service without recreating its task, so this is a
short restart:

```bash
docker service update --secret-add wpsubmit_webhook_secret wikipathways-submit
docker service ps wikipathways-submit -f desired-state=running --format '{{.Node}} {{.CurrentState}}'
```

### 3. Point the GitHub App at it

This part cannot be scripted from here — it is in the App's own settings, under the account that
owns it. GitHub → **Settings → Developer settings → GitHub Apps → wikipathways-submit-bot (dev)**
(App ID `4403728`) → **General**:

| Field | Value |
|---|---|
| Webhook — Active | ✔ |
| Webhook URL | `https://curation.wikipathways.org/webhooks/github` |
| Webhook secret | the string from step 1 |

Then **Permissions & events → Subscribe to events → Pull requests** ✔.

One subscription covers everything the handler uses: it acts on the `closed` action (release the
lock, finalise the reservation, terminalise the review) and on `labeled` / `unlabeled`, which are
how a curator applying the target repo's own labels on GitHub stays in step with the dashboard.

### 4. Verify

GitHub sends a `ping` on save. The app answers it, so the App's **Advanced → Recent Deliveries**
tab is the fastest check — look for a `ping` with a green 200 and a `{"ok":true,"pong":true}`
body. Then:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://upload.wikipathways.org/webhooks/github  # expect 401
ssh tgx1 "docker service logs wikipathways-submit --since 10m 2>&1 | grep -i webhook"
```

The end-to-end proof is to close a pull request **on GitHub** and watch the review leave the open
queue without anyone touching the dashboard. That is the whole point of the feature.

### If deliveries fail

- **401 on every delivery** — the secret differs. Most often the trailing newline above; otherwise
  the value in GitHub was pasted from a different generation than the one in the secret.
- **503** — the secret is not reaching the container. Check it is attached
  (`docker service inspect wikipathways-submit --format '{{range .Spec.TaskTemplate.ContainerSpec.Secrets}}{{.SecretName}} {{end}}'`)
  and that the task actually restarted after step 2.
- **Nothing arrives at all** — the App is installed on the repo but not subscribed to *Pull
  requests*, or the webhook URL still points at a previous host.
- Deliveries are **replayable** from the Recent Deliveries tab, and the handler is idempotent, so
  redelivering to test costs nothing.

## Update

```bash
docker service update --image ghcr.io/marvinm2/wikipathways-submit:latest wikipathways-submit
```

That form is a **no-op** when the service spec holds a bare tag — Swarm compares the spec string,
sees no change, and keeps the old task running while printing success. Deploy by digest:

```bash
DIG=$(ssh tgx1 "docker pull -q ghcr.io/marvinm2/wikipathways-submit:latest >/dev/null; \
  docker image inspect ghcr.io/marvinm2/wikipathways-submit:latest --format '{{index .RepoDigests 0}}'")
ssh tgx1 docker service update --image "$DIG" wikipathways-submit
```

Confirm by something version-specific the app serves — the `?v=` on `app.css`/`app.js`, or a new
route in `/openapi.json` — not by the update command's output.

## Reminders

- **No node pinning** — the image is on GHCR and state is in Postgres, so the task can schedule on
  either node.
- The GitHub App must be installed on the content repo with contents RW, pull_requests RW,
  issues RW, and (for the curator team) org Members:read — see `docs/github-app-setup.md`.
- Register the OAuth App callback + the App webhook URL against the deployed host.
- Update the cluster's `services/service-registry.md` and add `services/wikipathways-submit.md`
  after the first real deploy.
