"""review: the base version an update was made against

``base_blob`` is the blob sha of the pathway's GPML on the default branch when an update was
uploaded or adopted (``absent`` when the file was not there). Most production curation is direct
commits to ``main``, and an approved update replaces the file wholesale, so an update made against
an older version would silently undo a direct edit. Approval compares the two and refuses when
they differ. Nullable with no backfill: older rows are not guarded.

Revision ID: c3e0a2b1d4f5
Revises: b2d9f1a0c3e4
Create Date: 2026-11-12 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3e0a2b1d4f5'
down_revision: str | None = 'b2d9f1a0c3e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('review', sa.Column('base_blob', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('review', 'base_blob')
