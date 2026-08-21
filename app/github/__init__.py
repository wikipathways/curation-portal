"""GitHub client abstraction used by the submission/curation flows.

The abstraction exists so the flows are unit-testable without a network (``FakeGitHubClient``)
and so the two identities from scaffolding-plan §3 — per-user OAuth for the push/PR, the
GitHub App for privileged actions — can be swapped in behind the same interface.
"""

from app.github.client import (
    BranchAlreadyExists,
    CredentialsRejected,
    FakeGitHubClient,
    GitHubClient,
    GitHubError,
    HttpGitHubClient,
    PrFile,
    PullRequest,
    PullRequestDetail,
    WorkflowRun,
    WriteDenied,
)

__all__ = [
    "BranchAlreadyExists",
    "CredentialsRejected",
    "WriteDenied",
    "FakeGitHubClient",
    "GitHubClient",
    "GitHubError",
    "HttpGitHubClient",
    "PrFile",
    "PullRequest",
    "PullRequestDetail",
    "WorkflowRun",
]
