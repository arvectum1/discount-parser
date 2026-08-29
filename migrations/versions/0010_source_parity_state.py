"""Add durable DP Engine live source parity state.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "source_parity_state" in inspector.get_table_names():
        return
    op.create_table(
        "source_parity_state",
        sa.Column("source_key", sa.String(length=120), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="observing"),
        sa.Column("parity_observed_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parity_pass_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parity_failure_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_pass_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clean_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generic_direct_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("emergency_fallback_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("source_key"),
        sa.CheckConstraint("mode IN ('observing','generic_primary')", name="ck_source_parity_state_mode"),
        sa.CheckConstraint("parity_observed_pages >= 0", name="ck_source_parity_observed_nonnegative"),
        sa.CheckConstraint("parity_pass_pages >= 0", name="ck_source_parity_pass_nonnegative"),
        sa.CheckConstraint("parity_failure_pages >= 0", name="ck_source_parity_failure_nonnegative"),
        sa.CheckConstraint("consecutive_pass_pages >= 0", name="ck_source_parity_consecutive_nonnegative"),
        sa.CheckConstraint("clean_runs >= 0", name="ck_source_parity_clean_runs_nonnegative"),
        sa.CheckConstraint("generic_direct_pages >= 0", name="ck_source_parity_direct_nonnegative"),
        sa.CheckConstraint("emergency_fallback_pages >= 0", name="ck_source_parity_emergency_nonnegative"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "source_parity_state" in inspector.get_table_names():
        op.drop_table("source_parity_state")
