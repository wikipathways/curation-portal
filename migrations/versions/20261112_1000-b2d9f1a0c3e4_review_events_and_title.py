"""review: an audit trail, and the title and organism the queue names a submission by

``review_event`` records who changed a review's status, assignment or checklist answers, and
when. Until now the only trace of a decision was ``decided_by`` on the row, which the next
decision overwrites.

``title`` and ``organism`` are what the submitted GPML declares. The queue needs them to say
which pathway a card is about; the render cache that also holds them is deleted at every
terminal transition. Nullable with no backfill: rows written before this get them on their next
upload or adoption refresh, and the card falls back to the cached metadata meanwhile.

Revision ID: b2d9f1a0c3e4
Revises: a1c8e9f0b2d3
Create Date: 2026-11-12 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2d9f1a0c3e4'
down_revision: str | None = 'a1c8e9f0b2d3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('review', sa.Column('title', sa.String(length=512), nullable=True))
    op.add_column('review', sa.Column('organism', sa.String(length=255), nullable=True))
    op.create_table(
        'review_event',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'pr_number',
            sa.Integer(),
            sa.ForeignKey('review.pr_number', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('actor', sa.String(length=255), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=True),
    )
    op.create_index('ix_review_event_pr_number', 'review_event', ['pr_number'])


def downgrade() -> None:
    op.drop_index('ix_review_event_pr_number', table_name='review_event')
    op.drop_table('review_event')
    op.drop_column('review', 'organism')
    op.drop_column('review', 'title')
