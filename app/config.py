"""Application settings (12-factor: env-driven, with dev-friendly defaults)."""
from __future__ import annotations

import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsConfigDict

#: The prefix this project was born with, kept readable indefinitely. The renames to
#: ``pathway-portal`` (2026-08-05) and ``curation-portal`` (2026-08-21) would otherwise each have
#: been a flag day: every environment variable on the live swarm service, plus the Docker secrets,
#: would have had to change in the same breath as the image, and a typo in that window takes the
#: service down for a cosmetic gain. Reading both instead means the deployment migrates whenever
#: it likes, or never.
_LEGACY_ENV_PREFIX = "WPSUBMIT_"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PORTAL_", env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Read ``PORTAL_*`` first, then fall back to ``WPSUBMIT_*`` for anything unset.

        Order matters and is the whole point: a deployment that sets both during a migration gets
        the new name, so the cutover is a no-op rather than a coin toss.
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            EnvSettingsSource(settings_cls, env_prefix=_LEGACY_ENV_PREFIX),
            file_secret_settings,
        )

    # Registry datastore. SQLite for dev; PostgreSQL in production (see scaffolding-plan §0).
    database_url: str = "sqlite:///./registry.db"

    # Target content repo the app opens PRs against.
    content_repo: str = "wikipathways/wikipathways-database"
    default_branch: str = "main"

    # How the target repo publishes an approved pathway (docs/sandbox-pipeline.md).
    #
    #   "direct"   — the app owns publication: it allocates the WPID, and approving merges the PR.
    #                Correct for a plain content repo (wikipathways-database, a personal fork,
    #                the demo).
    #   "pipeline" — the target repo's own Actions own publication. Approving applies the
    #                `accepted` label and stops; the repo assigns the WPID, promotes the files and
    #                closes the PR unmerged. Correct for wikipathways/sandbox-wp-db.
    #
    # The default stays "direct" so existing targets keep working unchanged.
    publish_mode: str = "direct"

    # Build a review around pull requests the app did *not* open — the PathVisio plugin, or
    # anyone with push access to the content repo. Off by default: it puts other people's pull
    # requests into the curation queue and comments on them, which is not a change any existing
    # deployment should get by upgrading.
    #
    # Ignored outside pipeline mode, and that is a safety property rather than a simplification.
    # In `direct` mode approving *merges*, and a foreign pull request is not a file the app laid
    # out: merging one would land `pathways/testing_new_pathway/` — or the `WP0001` placeholder
    # every submission writes to — on `main` permanently. In pipeline mode approving applies a
    # label and the repository decides, which is the only shape where this is safe.
    adopt_foreign_prs: bool = False

    # Where the target repo's PR workflow writes its derived artifacts (pipeline mode). These are
    # read anonymously over raw.githubusercontent.com — the App is not installed on that repo and
    # the files are public. Empty drafts_repo disables the read.
    drafts_repo: str = "wikipathways/sandbox-wp.gh.io"
    drafts_branch: str = "main"
    drafts_site_base_url: str = "https://sandbox.wikipathways.org"

    # The target repo's own workflows. Advisory/display only — workflow 1 fails often enough
    # (its bridgeDb step) that it must never gate an approval.
    pipeline_workflow_file: str = "1_on_pull_request.yml"
    publish_workflow_file: str = "3a_approved_pull_request.yml"

    # Label vocabulary, as already defined in sandbox-wp-db. `accepted` and `rejected` are the
    # ones that *do* something: the repo's pr_label_dispatcher turns them into workflow runs.
    label_accepted: str = "accepted"
    label_rejected: str = "rejected"
    label_resubmitted: str = "resubmitted"
    label_new_submission: str = "new pathway submission"
    label_edited_submission: str = "edited pathway submission"
    label_author_feedback: str = "author feedback required"

    # How long to wait after labelling `accepted` before calling the publish failed.
    #
    # Measured 2026-08-03 against the two publications that have actually succeeded on
    # marvinm2/sandbox-wp-db (issue #23, which was filed when the number was still unknowable
    # because the workflow had never succeeded once). Label applied -> pull request closed:
    # **70 seconds** (PR 5) and **42 seconds** (PR 11); the runs themselves took 63s and 54s, with
    # ~10s of queueing before each. So 30 minutes was 26-43x the real thing.
    #
    # Ten rather than the two-or-three those numbers alone would suggest, because the two failure
    # directions are not symmetric. Declaring failure late costs a curator some waiting. Declaring
    # it *early* puts "the repository never published this" on screen under a publication that is
    # merely queued — and the documented way a curator responds to that is to re-apply the
    # accepted label, which dispatches a **second** publish run. A false negative is a wait; a
    # false positive risks publishing twice. The generous margin is for Actions queueing, which is
    # the one part of the chain with no upper bound.
    publish_timeout_minutes: int = 10
    # If the repo's rejection workflow never closes a rejected PR, close it ourselves after the
    # same window. Safe because rejection has no side effects worth waiting for.
    close_rejected_after_timeout: bool = True

    # Who pushes the submission branch, and where to. See ``app.submit.targets``.
    #   "user" — the submitter's own OAuth token, straight at the content repo. Only works where
    #            they have push access (their own fork, the demo). The historical default.
    #   "bot"  — the GitHub App installation pushes, with the submitter as git commit author.
    #            Works anywhere, but the *pull request* belongs to the bot: it is who GitHub
    #            notifies, who can edit the description and who can close it, and every
    #            submission then appears to come from one account.
    #   "fork" — the submitter's own fork holds the branch and the pull request is
    #            cross-repository, so it is genuinely theirs. Needs no scope the app does not
    #            already request. Falls back to "bot" if the fork cannot be had (issue #22).
    submit_identity: str = "user"
    noreply_email_domain: str = "users.noreply.github.com"

    # Floor on how often a single review is re-checked against GitHub during the dashboard
    # reconcile pass. Reviews waiting on the repo's pipeline accumulate, so this matters.
    reconcile_min_interval_seconds: int = 30

    # Level for the `wpsubmit.*` loggers. INFO because the things logged at it are the ones that
    # explain a quiet decision after the fact — how long an expired lock was held, why a
    # submission fell back to the bot — and none of them fire per request.
    log_level: str = "INFO"

    # TTLs for the transactional registry (design §4.2/§4.3).
    #
    # Both were guesses until 2026-08-03, when the 53 closed pull requests on
    # wikipathways/wikipathways-database were measured (issue #23). A lock and a reservation are
    # each held for the life of the pull request, so that distribution is the thing they have to
    # cover:
    #
    #   median 0.36 days | 75th 3.2 | 90th 6.8 | 95th 7.8 | max 109 (one PR)
    #   longer than  3 days: 14 of 53  (26%)
    #   longer than 14 days:  2 of 53   (4%)
    #
    # 14 days covers 96% of real reviews and is right for both. The old 3-day lock expired under
    # **more than a quarter** of them, which does not hand the pathway to a second editor outright
    # — a fresh check-out re-runs the open-PR scanner, which would see the live pull request — but
    # it does downgrade the guarantee from a database constraint to a best-effort GitHub read that
    # **fails open** on error. For a quarter of reviews, on the one invariant the lock exists for.
    #
    # Erring long is the cheaper mistake here: an over-held lock is visible and a curator can
    # force-release it, whereas an expired one is silent and its failure mode is the unmergeable
    # concurrent edit this whole app was built to prevent.
    wpid_reservation_ttl_days: int = 14
    pathway_lock_ttl_days: int = 14

    # How many pull requests one logged-in account may open on the content repo in a window
    # (issue #21). The blast radius is somebody else's repository — branches, notifications to
    # every watcher, and a full generation pipeline run per pull request — and cleaning up lands
    # on its maintainers. Ten an hour is far above any real curation session and stops a retry
    # loop or a double-click storm dead. Set the limit to 0 to disable.
    submit_rate_limit: int = 10
    submit_rate_window_minutes: int = 60

    # Curator whitelist (~20 people, design §4.5). Only these may approve-that-merges.
    # Preferred: resolve from a GitHub Team, WPSUBMIT_CURATOR_TEAM='org/team-slug' (issue #9) —
    # curator management then happens on GitHub, not in a redeploy (needs the App's org
    # Members:read permission). If unset, falls back to the static WPSUBMIT_CURATORS list
    # (JSON, e.g. '["alice","bob"]'), which is also used for tests / local dev.
    curator_team: str | None = None
    curators: list[str] = []

    # Bot/reader identity for server-side GitHub reads (the WPID floor). Distinct from the
    # per-user OAuth tokens used for writes. Absent in local dev → uses dev_wpid_floor.
    github_token: str | None = None
    #: Floor used when no token is configured (local dev). Real deployments read GitHub.
    dev_wpid_floor: int = 0

    # GitHub App (bot) identity, scaffolding-plan §3 — privileged merge + mirror comment, and
    # (when no github_token is set) the WPID floor read. The private key comes from a Docker
    # secret: prefer github_app_private_key_path (a mounted secret file) over an inline PEM.
    # All absent → merge/approve routes 503; mirror comments are skipped.
    github_app_id: str | None = None
    github_app_installation_id: str | None = None
    github_app_private_key: str | None = None
    github_app_private_key_path: str | None = None

    # Shared secret for verifying inbound GitHub webhooks (issue #8) — the App's webhook secret,
    # a Docker secret in production. Absent → POST /webhooks/github returns 503.
    github_webhook_secret: str | None = None

    # Pathway preview (before/after render, issue #11). The app reads the SVGs the PR-preview
    # workflow uploads as a run artifact and serves them to the dashboard. Needs the bot identity
    # (Actions read); without it, previews stay in the "generating" state.
    preview_workflow_file: str = "pr-preview.yml"
    preview_artifact_name: str = "pr-preview"
    preview_cache_dir: str = "./preview-cache"
    preview_cache_ttl_seconds: int = 60
    # Refuse approve-and-merge until the PR-preview CI workflow has completed successfully
    # (design problem #1: never merge a pathway whose render/validation hasn't run green).
    require_preview_check: bool = True

    # Public URL of this app, e.g. https://curator.wikipathways.org. Used to link a GitHub
    # reviewer from the mirror comment to the dashboard page holding the before/after render —
    # CI produces no image, so that page is the only place the render exists. Unset (local dev)
    # → the comment carries no link rather than a link to somebody's localhost.
    app_base_url: str = ""

    # Largest upload accepted on any endpoint that takes a GPML file (issue #16). Every one of
    # them reads the body into memory before parsing it, and both the metadata parse and the
    # renderer walk the whole tree, so an unbounded upload is a way to take the process down —
    # by accident as easily as on purpose, since posting the wrong file does it too.
    #
    # 4 MB is generous for the format: a real pathway is tens to low hundreds of KB, and the
    # largest to come through the live portal so far (88 data nodes, 14 references) is well under
    # a megabyte.
    max_upload_bytes: int = 4 * 1024 * 1024

    # A standing notice shown at the top of every page. Empty → no banner at all.
    #
    # This exists because a deployment can be pointed at a target that cannot actually publish —
    # a sandbox, a fork without the sister-repo credentials — and nothing on screen said so. The
    # submit page promises the database will publish the pathway and assign its WPID, so wherever
    # that promise does not hold, say it here.
    #
    # The prompt was a submission arriving from an unfamiliar account on a deployment that could
    # not publish it. That one turned out to be a colleague testing, so nobody actually lost work
    # — but it was indistinguishable from the real thing from the inside, which is the point:
    # by the time you can tell the difference, the silent failure has already happened.
    #
    # Deliberately free text rather than derived from publish_mode: "can this target publish"
    # depends on credentials held by other repositories, which this app cannot see.
    site_notice: str = ""

    # Per-user OAuth (submitter identity, scaffolding-plan §3). Absent → auth routes 503.
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"
    # public_repo: push branches + open PRs on the public content repo; read:user: identity.
    oauth_scope: str = "public_repo read:user"
    # Signs the session cookie holding the user token. MUST be overridden in production.
    session_secret: str = "dev-insecure-change-me"
    # Set True behind TLS so the session cookie is only sent over HTTPS (issue #4).
    session_https_only: bool = False
    # Encrypts the OAuth token at rest inside the session (issue #4). A Fernet key; if unset a
    # key is derived from session_secret. Set/rotate independently in production.
    token_encryption_key: str | None = None

    @model_validator(mode="after")
    def _pipeline_defaults(self) -> Settings:
        """Make the pipeline-mode defaults coherent, and say so when they are changed.

        Two settings default to values that are simply wrong against a repository that publishes
        through its own Actions, and both were discovered the hard way on the live deployment:

        - ``require_preview_check`` gates approval on ``pr-preview.yml``, a workflow that repo
          does not have, so **every** approval returns 409 until it is turned off by hand.
        - ``submit_identity="user"`` pushes the branch with the submitter's own token, which an
          ordinary submitter cannot do on a repository they do not have write access to.

        The first is silently corrected, because there is no configuration under which gating on
        a workflow the target repo has never run is what anyone meant. The second is only warned
        about: pointing the app at your own fork, where you *can* push, is a legitimate setup and
        is exactly how the sandbox integration was tested.
        """
        if self.publish_mode == "pipeline":
            if self.require_preview_check:
                logging.getLogger("wpsubmit.config").info(
                    "publish_mode=pipeline: disabling require_preview_check, which gates on %s "
                    "— a workflow the target repository is not expected to have",
                    self.preview_workflow_file,
                )
                self.require_preview_check = False
            if self.submit_identity not in ("bot", "fork"):
                logging.getLogger("wpsubmit.config").warning(
                    "publish_mode=pipeline with submit_identity=%s: submissions are pushed with "
                    "the submitter's own token straight at %s, which only works where they have "
                    "write access to it. Set WPSUBMIT_SUBMIT_IDENTITY=fork (the pull request is "
                    "then the submitter's own) or =bot on a shared repository.",
                    self.submit_identity,
                    self.content_repo,
                )
        elif self.adopt_foreign_prs:
            # Corrected rather than warned about. Outside pipeline mode approving *merges*, and a
            # pull request the app did not lay out is not one it can safely merge — the plugin's
            # new-pathway submissions arrive at a title-derived path, and its placeholder
            # submissions at the shared `WP0001` slot every portal submission also writes to.
            # There is no configuration under which merging those onto `main` is what anyone
            # meant.
            logging.getLogger("wpsubmit.config").warning(
                "publish_mode=%s: disabling adopt_foreign_prs. Approving in this mode merges the "
                "pull request, and a pull request the app did not lay out must not be merged "
                "onto %s.",
                self.publish_mode,
                self.content_repo,
            )
            self.adopt_foreign_prs = False
        return self

    @property
    def content_repo_owner(self) -> str:
        return self.content_repo.split("/", 1)[0]

    @property
    def content_repo_name(self) -> str:
        return self.content_repo.split("/", 1)[1]

    @property
    def is_pipeline_mode(self) -> bool:
        return self.publish_mode == "pipeline"

    @property
    def drafts_repo_owner(self) -> str:
        return self.drafts_repo.split("/", 1)[0]

    @property
    def drafts_repo_name(self) -> str:
        return self.drafts_repo.split("/", 1)[1]
