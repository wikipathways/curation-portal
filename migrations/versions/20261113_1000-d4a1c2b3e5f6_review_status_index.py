"""review: an index on status

Every queue tab, the tab counts, the background loop's due-review scan and the API list filter
the review table by status. Small today; a production queue keeps every closed and published
review, so the scan would otherwise grow with the database's history rather than its live work.
Additive and built online-safe for a table this size.

Revision ID: d4a1c2b3e5f6
Revises: c3e0a2b1d4f5
Create Date: 2026-11-13 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4a1c2b3e5f6'
down_revision: str | None = 'c3e0a2b1d4f5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index('ix_review_status', 'review', ['status'])


def downgrade() -> None:
    op.drop_index('ix_review_status', table_name='review')
