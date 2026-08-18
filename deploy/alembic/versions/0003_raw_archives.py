"""Add permanent raw-record archive ledger.

Revision ID: 0003_raw_archives
Revises: 0002_generated_reports
"""

from alembic import op

from work_history.models import RawArchiveBatch, RawArchiveEntry

revision = "0003_raw_archives"
down_revision = "0002_generated_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    RawArchiveBatch.__table__.create(bind=bind, checkfirst=True)
    RawArchiveEntry.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    RawArchiveEntry.__table__.drop(bind=bind, checkfirst=True)
    RawArchiveBatch.__table__.drop(bind=bind, checkfirst=True)
