"""upload_sessions.title: keep the optional title of POST uploads until finalize (DC-022).

The M02 contract lets `POST uploads` carry an optional `title` for a NEW document, but the 0003 schema had
nowhere to keep it between session creation and finalize (the document row doesn't exist until finalize), so a
custom title was validated and then dropped. A nullable column with the same shape rule as `documents.title`
fixes that; NULL means "derive the title from the filename". The app role's table-level INSERT grant already
covers the new column, and the forward-only state trigger only watches state/failure_code/finalized_at.

Revision ID: 0004_upload_session_title
Revises: 0003_documents
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_upload_session_title"
down_revision: str | None = "0003_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE upload_sessions ADD COLUMN title text")
    op.execute(
        "ALTER TABLE upload_sessions ADD CONSTRAINT ck_upload_sessions_title CHECK ("
        "title IS NULL OR (length(title) BETWEEN 1 AND 255 AND title !~ '^[[:space:]]|[[:space:]]$'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE upload_sessions DROP CONSTRAINT ck_upload_sessions_title")
    op.execute("ALTER TABLE upload_sessions DROP COLUMN title")
