"""DHCP lease ingest + building population module.

Three tables:
  - dhcp_lease_cache    — point-in-time mirror of the Windows DHCP server.
                          Full replace each ingest cycle.
  - oui_registry         — IEEE OUI prefix → manufacturer + device class hint.
                          Seeded with a small starter set here; full IEEE
                          refresh is a Phase-4 monthly job.
  - population_snapshots — insert-only time series of building × device class
                          counts. Cleanup worker drops rows older than the
                          configured retention window.

Permission `network.population.view` is admin-only at first — DHCP hostnames
can contain student names, so the data is treated as PII-adjacent.

Revision ID: a053_dhcp_population
Revises: a052_inventory_storage_location
Create Date: 2026-04-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a053_dhcp_population"
down_revision: Union[str, None] = "a052_inventory_storage_location"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── dhcp_lease_cache ─────────────────────────────────────────────────
    op.create_table(
        "dhcp_lease_cache",
        sa.Column("mac", sa.String(17), primary_key=True),  # lowercase, colon-separated
        sa.Column("ip", postgresql.INET),
        sa.Column("hostname", sa.Text),                      # may contain PII — never log at INFO
        sa.Column("vendor_class", sa.Text),                  # DHCP option 60
        sa.Column("lease_expires", sa.DateTime(timezone=True)),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dhcp_lease_cache_cached_at", "dhcp_lease_cache", ["cached_at"])

    # ── population_snapshots ─────────────────────────────────────────────
    op.create_table(
        "population_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("building", sa.Text, nullable=False),
        sa.Column("device_class", sa.Text, nullable=False),
        sa.Column("confidence", sa.Text, nullable=False),    # 'high' | 'medium' | 'low'
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_population_snapshots_building_at",
        "population_snapshots",
        ["building", sa.text("snapshot_at DESC")],
    )
    op.create_index(
        "ix_population_snapshots_at",
        "population_snapshots",
        ["snapshot_at"],
    )

    # ── oui_registry ─────────────────────────────────────────────────────
    op.create_table(
        "oui_registry",
        sa.Column("prefix", sa.CHAR(6), primary_key=True),   # uppercase, no separators e.g. A4C3F0
        sa.Column("manufacturer", sa.Text, nullable=False),
        sa.Column("device_hint", sa.Text),                    # 'mobile' | 'laptop' | 'infrastructure' | 'printer' | NULL
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Starter OUI seed ─────────────────────────────────────────────────
    # A small curated set covering the manufacturers we care about. The
    # Phase-4 update_oui_registry job pulls the full IEEE registry monthly
    # for completeness; this seed makes the system useful immediately
    # without that job running. Prefixes are uppercase, no separators.
    seed = [
        # Apple — both consumer (mobile) and Mac-style (laptop in education)
        ("000393", "Apple", "mobile"),
        ("3C0754", "Apple", "mobile"),
        ("A4C361", "Apple", "mobile"),
        ("F0DBE2", "Apple", "mobile"),
        ("DC2B61", "Apple", "mobile"),
        ("A8667F", "Apple", "mobile"),
        ("E0C97A", "Apple", "mobile"),
        ("8866A5", "Apple", "mobile"),
        ("404E36", "Apple", "mobile"),
        ("1C36BB", "Apple", "mobile"),
        # Samsung mobile
        ("0023D7", "Samsung Electronics", "mobile"),
        ("E8508B", "Samsung Electronics", "mobile"),
        ("8C77F4", "Samsung Electronics", "mobile"),
        ("D0176A", "Samsung Electronics", "mobile"),
        ("F40F24", "Samsung Electronics", "mobile"),
        # Google mobile / Pixel
        ("38FFB6", "Google", "mobile"),
        ("F4F5D8", "Google", "mobile"),
        ("F8C3CC", "Google", "mobile"),
        # OnePlus, Motorola
        ("64A2F9", "OnePlus", "mobile"),
        ("D03794", "Motorola Mobility", "mobile"),
        ("002717", "Motorola Mobility", "mobile"),
        # Dell laptops
        ("0014FE", "Dell", "laptop"),
        ("3417EB", "Dell", "laptop"),
        ("F8DB88", "Dell", "laptop"),
        ("AC162D", "Dell", "laptop"),
        ("E4434B", "Dell", "laptop"),
        ("84A93E", "Dell", "laptop"),
        ("18FB7B", "Dell", "laptop"),
        ("D89EF3", "Dell", "laptop"),
        ("509A4C", "Dell", "laptop"),
        # HP laptops
        ("3C52A1", "Hewlett Packard", "laptop"),
        ("9CB6D0", "Hewlett Packard", "laptop"),
        ("A4B197", "Hewlett Packard", "laptop"),
        ("3457D1", "Hewlett Packard", "laptop"),
        ("EC9A74", "Hewlett Packard", "laptop"),
        ("8851FB", "Hewlett Packard", "laptop"),
        # Lenovo laptops
        ("0023AE", "Lenovo", "laptop"),
        ("AC2B6E", "Lenovo", "laptop"),
        ("00059A", "Lenovo", "laptop"),
        ("F0DEF1", "Lenovo", "laptop"),
        ("E8B1FC", "Lenovo", "laptop"),
        # Acer
        ("00012E", "Acer", "laptop"),
        ("00807F", "Acer", "laptop"),
        # Intel Corporate (managed-fleet wifi cards, often laptops)
        ("ACDE48", "Intel Corporate", "laptop"),
        ("D8FC93", "Intel Corporate", "laptop"),
        ("3C58C2", "Intel Corporate", "laptop"),
        ("F8E43B", "Intel Corporate", "laptop"),
        # Aruba — INFRASTRUCTURE (excluded from population)
        ("000B86", "Aruba Networks", "infrastructure"),
        ("18643F", "Aruba Networks", "infrastructure"),
        ("9C1C12", "Aruba Networks", "infrastructure"),
        ("D8C7C8", "Aruba Networks", "infrastructure"),
        ("245A4C", "Aruba Networks", "infrastructure"),
        # Cisco — infrastructure
        ("000142", "Cisco Systems", "infrastructure"),
        ("000C30", "Cisco Systems", "infrastructure"),
        ("001B0D", "Cisco Systems", "infrastructure"),
        ("F4CFE2", "Cisco Systems", "infrastructure"),
        # TP-Link, Ubiquiti, Netgear — infrastructure
        ("002566", "TP-Link", "infrastructure"),
        ("EC086B", "TP-Link", "infrastructure"),
        ("002722", "Ubiquiti Networks", "infrastructure"),
        ("FCECDA", "Ubiquiti Networks", "infrastructure"),
        ("00146C", "Netgear", "infrastructure"),
        # Printers
        ("000074", "Canon", "printer"),
        ("9CAEDB", "Canon", "printer"),
        ("0080A3", "Epson", "printer"),
        ("002485", "Xerox", "printer"),
        ("0000F0", "Ricoh", "printer"),
        ("0080A0", "Brother Industries", "printer"),
    ]
    for prefix, mfr, hint in seed:
        op.execute(
            sa.text(
                "INSERT INTO oui_registry (prefix, manufacturer, device_hint) "
                "VALUES (:p, :m, :h) ON CONFLICT (prefix) DO NOTHING"
            ).bindparams(p=prefix, m=mfr, h=hint)
        )

    # ── Permission ───────────────────────────────────────────────────────
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES ('network.population.view',
                'View building population snapshots and DHCP lease cache (admin-gated until use cases broaden)',
                false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name = 'admin' AND p.action = 'network.population.view'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE action = 'network.population.view')")
    op.execute("DELETE FROM permissions WHERE action = 'network.population.view'")
    op.drop_index("ix_population_snapshots_at", table_name="population_snapshots")
    op.drop_index("ix_population_snapshots_building_at", table_name="population_snapshots")
    op.drop_index("ix_dhcp_lease_cache_cached_at", table_name="dhcp_lease_cache")
    op.drop_table("oui_registry")
    op.drop_table("population_snapshots")
    op.drop_table("dhcp_lease_cache")
