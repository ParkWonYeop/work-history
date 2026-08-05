"""Add generated report storage and scoped device purposes.

Revision ID: 0002_generated_reports
Revises: 0001_initial
"""

import sqlalchemy as sa
from alembic import op

from work_history.models import GeneratedReport, GeneratedReportVersion

revision = "0002_generated_reports"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("ingest_devices")}
    if "purpose" not in columns:
        op.add_column(
            "ingest_devices",
            sa.Column(
                "purpose",
                sa.String(length=32),
                nullable=True,
                server_default="gitlab_ingest",
            ),
        )
        op.execute(
            sa.text("UPDATE ingest_devices SET purpose = 'gitlab_ingest' WHERE purpose IS NULL")
        )
        op.alter_column("ingest_devices", "purpose", nullable=False)

    GeneratedReport.__table__.create(bind=bind, checkfirst=True)
    GeneratedReportVersion.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    GeneratedReportVersion.__table__.drop(bind=bind, checkfirst=True)
    GeneratedReport.__table__.drop(bind=bind, checkfirst=True)
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("ingest_devices")}
    if "purpose" in columns:
        op.drop_column("ingest_devices", "purpose")
