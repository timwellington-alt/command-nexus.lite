"""Inventory categories — add `group_name` + seed Facilities/Classroom set.

Adds a `group_name` column so categories can be organized by domain
(Technology / Facilities / Classroom / General). Backfills the existing
16 categories with sensible groups, then inserts ~33 new categories
covering CAD-derived facility equipment (HVAC, electrical, plumbing,
fire & life safety, security) plus instructional AV gear we know we
need from past inventory scans.

Idempotent: ON CONFLICT DO NOTHING on the unique name index.

Revision ID: a077_cat_groups
Revises: a076_circuit_items
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a077_cat_groups"
down_revision: Union[str, None] = "a076_circuit_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (name, group, sort_order, default_is_loanable, default_is_capitalized)
NEW_CATEGORIES = [
    # ── Technology additions ────────────────────────────────────────
    ("Patch Panel",          "Technology",  120, False, True),
    ("Data Port / Wall Plate","Technology", 121, False, False),

    # ── Facilities — HVAC ───────────────────────────────────────────
    ("Air Handler (AHU)",    "Facilities",  200, False, True),
    ("Rooftop Unit (RTU)",   "Facilities",  201, False, True),
    ("Boiler",               "Facilities",  202, False, True),
    ("Chiller",              "Facilities",  203, False, True),
    ("Air Conditioner",      "Facilities",  204, False, True),
    ("Condensing Unit",      "Facilities",  205, False, True),
    ("Fan Coil Unit",        "Facilities",  206, False, True),
    ("Cabinet Unit Heater",  "Facilities",  207, False, False),
    ("Unit Heater",          "Facilities",  208, False, False),
    ("Exhaust Fan",          "Facilities",  209, False, False),
    ("Return Fan",           "Facilities",  210, False, False),
    ("HVAC Pump",            "Facilities",  211, False, False),

    # ── Facilities — Electrical ─────────────────────────────────────
    ("Electrical Panel",     "Facilities",  300, False, True),
    ("Main Switchboard",     "Facilities",  301, False, True),
    ("Transformer",          "Facilities",  302, False, True),
    ("Generator",            "Facilities",  303, False, True),
    ("Disconnect Switch",    "Facilities",  304, False, False),

    # ── Facilities — Plumbing ───────────────────────────────────────
    ("Water Heater",         "Facilities",  400, False, True),
    ("Plumbing Fixture",     "Facilities",  401, False, False),
    ("Backflow Preventer",   "Facilities",  402, False, True),

    # ── Facilities — Fire & Life Safety ─────────────────────────────
    ("Fire Alarm Panel",     "Facilities",  500, False, True),
    ("Smoke Detector",       "Facilities",  501, False, False),
    ("Heat Detector",        "Facilities",  502, False, False),
    ("Pull Station",         "Facilities",  503, False, False),
    ("Fire Extinguisher",    "Facilities",  504, False, False),
    ("Sprinkler Head",       "Facilities",  505, False, False),
    ("Strobe / Horn",        "Facilities",  506, False, False),

    # ── Facilities — Security ──────────────────────────────────────
    ("Surveillance Camera",  "Facilities",  550, False, True),
    ("Card Reader",          "Facilities",  551, False, False),
    ("Door Strike",          "Facilities",  552, False, False),

    # ── Classroom (instructional AV) ───────────────────────────────
    ("Document Camera",      "Classroom",   600, True,  True),
    ("Projector Lamp / Bulb","Classroom",   601, False, False),
    ("Mount / Bracket",      "Classroom",   602, False, False),
    ("Speaker",              "Classroom",   603, False, False),
    ("Microphone",           "Classroom",   604, True,  False),
    ("Display / TV",         "Classroom",   605, False, True),
]

# Existing categories → group assignments. Keeps sort_order as-is.
EXISTING_GROUP_MAP = {
    "Switch":       "Technology",
    "Access Point": "Technology",
    "Server":       "Technology",
    "Workstation":  "Technology",
    "Laptop":       "Technology",
    "Monitor":      "Technology",
    "Projector":    "Classroom",   # instructional AV per Tim
    "UPS":          "Technology",
    "Printer":      "Technology",
    "Mouse":        "Technology",
    "Phone":        "Technology",
    "Cable":        "Technology",
    "Adapter":      "Technology",
    "Spare Part":   "General",
    "Consumable":   "General",
    "Tool":         "General",
}


def upgrade() -> None:
    op.add_column(
        "inventory_categories",
        sa.Column("group_name", sa.String(50)),
    )
    op.create_index(
        "ix_inventory_categories_group", "inventory_categories", ["group_name"],
    )

    # Backfill existing categories with their domain group
    for name, grp in EXISTING_GROUP_MAP.items():
        op.execute(
            sa.text(
                "UPDATE inventory_categories SET group_name = :grp WHERE name = :name"
            ).bindparams(grp=grp, name=name)
        )

    # Insert new categories. ON CONFLICT lets re-runs be safe.
    for name, grp, sort_order, loanable, capitalized in NEW_CATEGORIES:
        op.execute(
            sa.text(
                "INSERT INTO inventory_categories "
                "(name, group_name, sort_order, default_is_loanable, default_is_capitalized, archived) "
                "VALUES (:name, :grp, :so, :ln, :cap, false) "
                "ON CONFLICT (name) DO UPDATE SET "
                "  group_name = EXCLUDED.group_name, sort_order = EXCLUDED.sort_order"
            ).bindparams(name=name, grp=grp, so=sort_order, ln=loanable, cap=capitalized)
        )


def downgrade() -> None:
    # Best-effort: remove the new categories and drop the column. Existing
    # rows that were re-grouped don't get reverted (we don't track prior
    # values) but their data is intact.
    names = ", ".join(["'" + n.replace("'", "''") + "'" for n, *_ in NEW_CATEGORIES])
    op.execute(f"DELETE FROM inventory_categories WHERE name IN ({names})")
    op.drop_index("ix_inventory_categories_group", table_name="inventory_categories")
    op.drop_column("inventory_categories", "group_name")
