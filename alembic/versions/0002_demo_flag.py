"""demo flag: label seeded rows in the schema, not by convention

Adds `is_demo` to all six ledger tables so `app.cli.seed_demo` can populate a
local UI without ever producing a row that could be mistaken for real revenue.
`app.demo` reads it; `app.admin` lists it; the dashboard banners on it.

One deviation from what `alembic revision --autogenerate` emitted, and it is
load-bearing: **`server_default=sa.false()`**. Autogenerate produced a bare
`nullable=False`, which is fine against an empty local SQLite file and fails on
Postgres the moment a table has a row -- `ADD COLUMN ... NOT NULL` with no
default cannot fill the rows that are already there, and Render's database will
not be empty when this runs.

The matching `server_default` is declared on the model too. `alembic/env.py`
runs with `compare_server_default=True`, so a default present in the database
but absent from the model reports as permanent schema drift; `alembic check`
after this migration is clean only because both sides agree. Verified by running
`alembic upgrade head && alembic check && alembic downgrade base` on SQLite.

Revision ID: 0002_demo_flag
Revises: 0001_initial_ledger
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_demo_flag"
down_revision: str | None = "0001_initial_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: table -> index name, in dependency-free order (adding a column needs none).
TABLES: tuple[tuple[str, str], ...] = (
    ("author", "ix_author_is_demo"),
    ("tool", "ix_tool_is_demo"),
    ("pay_session", "ix_pay_session_is_demo"),
    ("call", "ix_call_is_demo"),
    ("batch", "ix_batch_is_demo"),
    ("receipt", "ix_receipt_is_demo"),
)


def upgrade() -> None:
    for table, index in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "is_demo",
                    sa.Boolean(),
                    nullable=False,
                    # Backfills existing rows to "real". A row that predates the
                    # flag was written by the gateway, not by the seeder, so
                    # false is both the safe default and the true one.
                    server_default=sa.false(),
                )
            )
            batch_op.create_index(batch_op.f(index), ["is_demo"], unique=False)


def downgrade() -> None:
    for table, index in reversed(TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(batch_op.f(index))
            batch_op.drop_column("is_demo")
