"""Chat: one-shot sanitize of historical message content blocks.

Older chat_messages.content rows have extra SDK fields baked in from
``model_dump()`` early in the chat module's life (parsed_output,
cache_control, signature, etc.). ``load_history_for_api`` runs the
``_sanitize_block`` helper on every read of every block to strip
them — fine for forward-compat, but a per-read cost that compounds
on long conversations.

This migration walks every row once and rewrites the cleaned shape
so subsequent reads can skip the sanitize pass entirely (the helper
stays in place defensively, but its work becomes a no-op).

Idempotent: re-applying the sanitizer to already-clean rows is a
no-op since the kept-fields set is the canonical input shape.

Revision ID: a110_chat_history_sanitize
Revises: a109_schedule_jsonb_lists
Create Date: 2026-05-18
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a110_chat_history_sanitize"
down_revision: Union[str, None] = "a109_schedule_jsonb_lists"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirror of `_BLOCK_FIELDS` in app/modules/chat/anthropic_client.py.
# Keep them in sync if the chat module ever adds a new block type.
_BLOCK_FIELDS: dict[str, set[str]] = {
    "text": {"type", "text"},
    "tool_use": {"type", "id", "name", "input"},
    "tool_result": {"type", "tool_use_id", "content", "is_error"},
    "thinking": {"type", "thinking"},
}


def _sanitize_block(block: dict) -> dict:
    btype = block.get("type")
    keep = _BLOCK_FIELDS.get(btype)
    if keep is None:
        return block
    return {k: v for k, v in block.items() if k in keep and v is not None}


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, content FROM chat_messages "
        "WHERE jsonb_typeof(content) = 'array'"
    )).fetchall()

    for row in rows:
        msg_id, content = row[0], row[1]
        if not isinstance(content, list):
            continue
        cleaned = [
            _sanitize_block(b) if isinstance(b, dict) else b
            for b in content
        ]
        if cleaned != content:
            conn.execute(
                sa.text(
                    "UPDATE chat_messages SET content = CAST(:c AS jsonb) "
                    "WHERE id = :i"
                ),
                {"c": json.dumps(cleaned), "i": msg_id},
            )


def downgrade() -> None:
    # No-op — we can't reconstruct the dropped SDK-only fields, and
    # they weren't load-bearing. Future reads still pass through the
    # in-app sanitizer for any post-migration writes.
    pass
