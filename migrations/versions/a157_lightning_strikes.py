"""Persist Blitzortung lightning strikes for 30-day retention.

Prior state: the subscriber held only a 1-hour in-memory rolling
deque, so post-mortem correlations against strikes older than an
hour weren't possible (see the 2026-07-21 example gate + 192.0.2.1
PoE cascade — we had to fall back to KPMH METARs). This table
captures every in-radius strike with structured columns plus the
raw JSONB blob so we can correlate weeks later + add fields to the
schema without re-instrumenting the ingest.

Retention: the ``prune_lightning_strikes`` ARQ job deletes rows
older than 30 days nightly. Plain DELETE (not partitioning) — at
Ohio's lightning volume (max ~200k strikes / peak-summer 30d ≈
40 MB with the raw blob), partition maintenance would cost more
than it saves.

Revision ID: a157_lightning_strikes
Revises: a156_service_scoped_attachments
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a157_lightning_strikes"
down_revision = "a156_service_scoped_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lightning_strikes",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # UTC timestamp of the strike itself (from Blitzortung's ns-precision
        # `time` field). Distinct from ``ingested_at`` — a strike can be
        # queued in the buffer for a few seconds before we write it.
        sa.Column("struck_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lon", sa.Float, nullable=False),
        # Distance from the configured lightning-subscriber center at ingest
        # time. Materialized here so radius filters don't re-haversine.
        sa.Column("distance_mi", sa.Float, nullable=False),
        # Optional fields Blitzortung sometimes includes — kept nullable so
        # missing values don't force a fake sentinel.
        sa.Column("alt_m", sa.Integer),
        sa.Column("polarity", sa.SmallInteger),
        sa.Column("multiplicity", sa.SmallInteger),
        sa.Column("detector_count", sa.SmallInteger),
        # Raw payload verbatim for schema flexibility — fields Blitzortung
        # adds later (mds, mcg, region, etc.) stay queryable without a
        # migration. Cheap: JSONB compresses well and each record is small.
        sa.Column("raw", postgresql.JSONB),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    # struck_at DESC — every read is either "recent strikes" or a time
    # range. DESC index makes the common "last N strikes" query index-only.
    op.execute(
        "CREATE INDEX ix_lightning_strikes_struck_at "
        "ON lightning_strikes (struck_at DESC)"
    )


def downgrade() -> None:
    op.drop_table("lightning_strikes")
