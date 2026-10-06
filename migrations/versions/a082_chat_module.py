"""Chat module — chat.admin permission + conversation/message/tool_call tables.

Tech-admin chat agent with tool surface for cross-integration queries.
Admin-only by design — not for general staff use.

Schema:
  chat_conversations   one per user-initiated chat thread
  chat_messages        one per turn (user / assistant / tool_result), content as JSONB
                       blocks (Anthropic's tool_use shape)
  chat_tool_calls      per-call audit row tied to the assistant message that issued it

Revision ID: a082_chat_module
Revises: a081_item_room_fk
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a082_chat_module"
down_revision: Union[str, None] = "a081_item_room_fk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Permission ─────────────────────────────────────────────────
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES ('chat.admin', 'Use the tech-admin chat agent', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name = 'admin' AND p.action = 'chat.admin'
        ON CONFLICT DO NOTHING
    """)

    # ── Conversations ──────────────────────────────────────────────
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default="New conversation"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_chat_conv_user_recent", "chat_conversations", ["user_id", "last_message_at"])
    op.create_index("ix_chat_conv_created_at", "chat_conversations", ["created_at"])

    # ── Messages ───────────────────────────────────────────────────
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(),
                  sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_chat_msg_conv", "chat_messages", ["conversation_id", "id"])

    # ── Tool calls (audit) ─────────────────────────────────────────
    op.create_table(
        "chat_tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(),
                  sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=True),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("is_error", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_chat_tool_msg", "chat_tool_calls", ["message_id"])
    op.create_index("ix_chat_tool_audit", "chat_tool_calls", ["tool_name", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_tool_audit", table_name="chat_tool_calls")
    op.drop_index("ix_chat_tool_msg", table_name="chat_tool_calls")
    op.drop_table("chat_tool_calls")
    op.drop_index("ix_chat_msg_conv", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_conv_created_at", table_name="chat_conversations")
    op.drop_index("ix_chat_conv_user_recent", table_name="chat_conversations")
    op.drop_table("chat_conversations")
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (SELECT id FROM permissions WHERE action = 'chat.admin')
    """)
    op.execute("DELETE FROM permissions WHERE action = 'chat.admin'")
