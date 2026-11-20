"""untracked_dismissal: curators hide open pull requests from the untracked panel

Additive: a new table, nothing else touched.

Revision ID: e5b2d3c4f6a7
Revises: d4a1c2b3e5f6
Create Date: 2026-11-20 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5b2d3c4f6a7'
down_revision: str | None = 'd4a1c2b3e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'untracked_dismissal',
        sa.Column('repo', sa.String(length=255), nullable=False),
        sa.Column('pr_number', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('head_sha', sa.String(length=64), nullable=False),
        sa.Column('dismissed_by', sa.String(length=255), nullable=False),
        sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('repo', 'pr_number'),
    )


def downgrade() -> None:
    op.drop_table('untracked_dismissal')
