"""Where a submission's branch lives, and whose credentials put it there.

Three arrangements, and the difference between them is one repository name:

- **user** — the submitter pushes a branch to the content repo with their own token. The app's
  default, and correct wherever the submitter has write access: their own fork, the demo.
- **bot** — the GitHub App pushes the branch instead, because an ordinary contributor has no
  write access to a shared content repo. Authorship survives on the commit, but the *pull
  request* is the bot's: the bot is who GitHub notifies, who can edit the description, and who
  closes it. Every submission appears to come from one account.
- **fork** — the submitter's own fork holds the branch and the pull request is cross-repository,
  which is how contributing to a repo you cannot write to normally works. 36 of the last 53 pull
  requests on the content repo are already this shape, so it is the *ordinary* mode there and the
  bot-pushes-a-branch model is the unusual one (issue #22).

Fork mode asks nothing new of the submitter. The app already holds their OAuth token and already
requests ``public_repo``, which GitHub defines as read/write to code on public repositories —
that covers creating a fork of one and pushing to it. So this exercises capability the app has
rather than acquiring any, which is what makes it a smaller change than it first looks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.github import CredentialsRejected, GitHubClient

logger = logging.getLogger("wpsubmit.submit.targets")


@dataclass(frozen=True)
class WriteTarget:
    """Resolved once per submission, before anything is written.

    ``branch_repo`` is where the branch and the file go; ``content_repo`` is where the pull
    request is opened and where the base branch is read from. They are the same repository in
    every mode except fork.
    """

    client: GitHubClient
    branch_repo: str
    content_repo: str
    #: Which identity this is, for logs and for the review record. Not behaviour.
    identity: str = "user"

    @property
    def is_cross_repo(self) -> bool:
        return self.branch_repo != self.content_repo

    @property
    def base_repo(self) -> str:
        """Which repository a new branch's base commit is read from.

        **The branch repository, which in fork mode is the submitter's fork, not the content
        repo.** This reverses what the app did until 2026-08-05, and the reason is not a
        preference: GitHub refuses to point a ref at a commit the repository does not itself hold.
        A commit merely readable through the shared fork network answers **404** — as distinct
        from 422 ``Object does not exist`` for a commit outside the network, and 201 for the
        repository's own. So cutting a fork's branch from the *content* repo's head is only
        possible while the fork happens to be level with it, which is true for a few minutes after
        the fork is created and false ever after. That is the whole of issue #29, and it presented
        as a permission problem for a day because 404 is also what GitHub says to a write you may
        not make.

        The fork's own head is native by definition, so this works on any topology and needs no
        sync to have succeeded first. ``ensure_fork`` still fast-forwards where it can, which keeps
        the base recent; this makes the write possible when it could not.

        A stale base is safe here in a way it would not be in a normal codebase, and that is a
        property of *this* repository rather than a general one: GPML is never line-merged and
        derived files are regenerated, so the uploaded file replaces its predecessor wholesale
        rather than being reconciled against it. GitHub computes the pull request's diff against
        the merge base, so an old base does not drag unrelated files into the change. What a stale
        base cannot protect against is two people editing one pathway, and that is what the
        check-out lock is for.
        """
        return self.branch_repo

    @property
    def head_repo(self) -> str | None:
        """What ``Review.head_repo`` should record — None meaning the content repo itself."""
        return self.branch_repo if self.is_cross_repo else None

    def head(self, branch: str) -> str:
        """The ``head`` a pull request must declare.

        GitHub spells a cross-repository head ``owner:branch``, naming only the *owner* — a fork
        keeps its parent's name, so the name is redundant there and including it does not resolve.
        """
        if not self.is_cross_repo:
            return branch
        return f"{self.branch_repo.split('/', 1)[0]}:{branch}"


def same_repo_target(
    client: GitHubClient, content_repo: str, *, identity: str = "user"
) -> WriteTarget:
    """The arrangement every mode but fork uses: branch and pull request in one repository."""
    return WriteTarget(
        client=client, branch_repo=content_repo, content_repo=content_repo, identity=identity
    )


def resolve_write_target(
    *,
    identity: str,
    user_client: GitHubClient,
    bot_client: GitHubClient | None,
    content_repo: str,
    submitter: str,
    base_branch: str = "main",
) -> WriteTarget:
    """Pick the write target for one submission, falling back when fork mode cannot be had.

    **The fallback is deliberately placed before the first write, and nowhere else.** Everything
    that realistically stops fork mode happens here — an organisation that forbids forking, a fork
    that has not finished being created, GitHub being down. Catching those costs nothing, because
    not one byte has been written yet, and the submission proceeds as a bot push exactly as it
    does today.

    **A revoked authorisation is the exception** and is re-raised rather than absorbed. This
    docstring used to list it among the things the fallback covers, which was the wrong call: it
    is not transient, so the fallback would keep reattributing that person's every submission to
    the bot, indefinitely and silently, with nothing ever telling them to sign in again (#28).

    Retrying *after* a write has begun is deliberately not done. A branch half-created on the
    fork and then re-attempted against the content repo is a different repository holding a
    different partial state, and reconciling that is a new class of bug in exchange for a case
    that is already vanishingly rare by this point. Such a failure surfaces to the submitter as
    an error, which is what it does today too. The debris — an unused branch on the submitter's
    own fork — is theirs and harmless.
    """
    if identity == "fork":
        # GitHub refuses to fork a repository into the account that already owns it, so for the
        # owner fork mode can only ever fail over to the bot — and that would be a worse pull
        # request than the one they can open directly, since they have push access by definition.
        # This is not hypothetical: the live deployment targets `marvinm2/sandbox-wp-db` and
        # `marvinm2` is the person testing it, so without this branch every submission he makes
        # would take the fallback path and none would exercise what fork mode does.
        if content_repo.split("/", 1)[0].casefold() == submitter.casefold():
            logger.info(
                "%s owns %s, so no fork is needed: pushing with their own token",
                submitter,
                content_repo,
            )
            return same_repo_target(user_client, content_repo, identity="user")
        try:
            fork = user_client.ensure_fork(content_repo)
        except CredentialsRejected:
            # Not a reason to fall back. Every other failure here is transient or environmental,
            # and a bot-authored pull request is a fair trade for not losing the upload — but a
            # revoked authorisation is neither, and quietly reattributing someone's contribution
            # to the bot is precisely what fork mode exists to stop (issue #22). It surfaces as a
            # 401 telling them to sign in again, which is a thing they can actually act on, and
            # then their next attempt is theirs (issue #28).
            raise
        except Exception as exc:  # noqa: BLE001 - any failure here means "use the bot instead"
            if bot_client is None:
                # Nothing to fall back to. Pushing to the content repo as the submitter is what
                # `user` mode does, and on a shared repo it will fail at create_branch with a
                # 403 that names the real problem better than anything invented here.
                logger.warning(
                    "fork mode unavailable for %s (%s) and no bot identity is configured; "
                    "falling back to the submitter's own token against %s",
                    submitter,
                    exc,
                    content_repo,
                )
                return same_repo_target(user_client, content_repo, identity="user")
            logger.warning(
                "fork mode unavailable for %s (%s); falling back to the bot identity",
                submitter,
                exc,
            )
            return same_repo_target(bot_client, content_repo, identity="bot")
        # A fork that has commits of its own cannot be branched from without carrying them into
        # the pull request: the branch is cut from the fork's head (see ``base_repo``), and the
        # pull request's diff runs from the merge base. Five of 26 production forks had diverged
        # (measured 2026-11-11). The bot pushes to the content repo instead; a failed comparison
        # proceeds, because the file-list guard at approval still catches the result.
        try:
            ahead = user_client.fork_ahead_by(content_repo, fork, base_branch)
        except CredentialsRejected:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("could not compare %s with %s", fork, content_repo, exc_info=True)
            ahead = 0
        if ahead and bot_client is not None:
            logger.warning(
                "%s is %d commit(s) ahead of %s; pushing %s's submission as the bot so the pull "
                "request carries only their pathway",
                fork,
                ahead,
                content_repo,
                submitter,
            )
            return same_repo_target(bot_client, content_repo, identity="bot")
        logger.info("submission by %s will be pushed to their fork %s", submitter, fork)
        return WriteTarget(
            client=user_client,
            branch_repo=fork,
            content_repo=content_repo,
            identity="fork",
        )

    if identity == "bot":
        if bot_client is None:
            raise BotIdentityUnavailable(
                "submit_identity=bot, but the GitHub App (bot) identity is not configured. "
                "Submitters cannot push to the target repo without it."
            )
        return same_repo_target(bot_client, content_repo, identity="bot")

    return same_repo_target(user_client, content_repo, identity="user")


def bot_fallback_target(
    bot_client: GitHubClient | None, content_repo: str, *, submitter: str, reason: str
) -> WriteTarget | None:
    """The target to retry under when a fork write was refused, or None if there is none.

    Called only where the refusal came from ``create_branch``, the first mutating call in every
    write path — so nothing has been created and retrying under another identity cannot leave two
    repositories holding halves of one submission. That is the same boundary
    ``resolve_write_target`` draws, reached a step later: the fork resolved fine and the *push*
    was refused, which is what a lapsed authorisation looks like (2026-08-04).

    Degrading to a bot-authored pull request is worse than the submitter's own, and much better
    than a 502 on work they have already done.
    """
    if bot_client is None:
        return None
    logger.warning(
        "fork write refused for %s (%s); retrying as the bot", submitter, reason
    )
    return same_repo_target(bot_client, content_repo, identity="bot")


class BotIdentityUnavailable(RuntimeError):
    """``submit_identity=bot`` with no GitHub App configured.

    A deployment error rather than anything the submitter did, which is why it surfaces as a 503.
    """
