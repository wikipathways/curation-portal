"""review: where a review came from, and which repository its PR number belongs to

Four columns, for one feature and one long-standing defect.

The feature is adoption: the portal now builds a review around a pull request somebody else
opened — the PathVisio plugin above all, whose own README records that a pull request opened
straight against the GitHub API "has no path into that database". Such a row needs ``origin`` (an
adopted submitter has no portal session, cannot re-upload, and answers a change request by pushing
a commit), ``head_sha`` (so a ``synchronize`` event can tell a real new commit from a base-branch
move, instead of re-rendering and re-posting the mirror comment on every push to ``main``), and
``pathway_paths`` (a plugin submission for a *new* pathway arrives at a title-derived path such as
``pathways/testing_new_pathway/``, so there is no WPID to name it by — and a pull request touching
several pathways has to be flagged rather than half-published).

The defect is older and measured: ``pr_number`` is the primary key, a pull request number is
unique only within one repository, and nothing recorded which repository. Repointing
``content_repo`` at the 2026-08-21 cutover therefore rebound seven open rows onto strangers' pull
requests that happened to share their numbers — three of them still actionable, where approving
would have labelled an unrelated pull request and dispatched the publish workflow against it.
``base_repo`` records the answer. NULL means "whatever ``content_repo`` was", which is the
implicit meaning every older row already carries, so there is no backfill.

``origin`` takes a server default rather than a data migration for the same reason: every row
written before this migration *is* a portal submission.

Revision ID: a1c8e9f0b2d3
Revises: f6a2c3e4d5b7
Create Date: 2026-08-21 19:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1c8e9f0b2d3'
down_revision: str | None = 'f6a2c3e4d5b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'review',
        sa.Column(
            'origin',
            sa.String(length=16),
            nullable=False,
            server_default='portal',
        ),
    )
    op.add_column('review', sa.Column('base_repo', sa.String(length=255), nullable=True))
    op.add_column('review', sa.Column('head_sha', sa.String(length=64), nullable=True))
    op.add_column('review', sa.Column('pathway_paths', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('review', 'pathway_paths')
    op.drop_column('review', 'head_sha')
    op.drop_column('review', 'base_repo')
    op.drop_column('review', 'origin')
