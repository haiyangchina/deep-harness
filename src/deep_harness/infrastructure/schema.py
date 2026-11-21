"""New business schema, separate from legacy Harness and graph checkpoints."""

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()
profile_bindings = Table(
    "profile_bindings",
    metadata,
    Column("profile_id", String, primary_key=True),
    Column("workspace", Text, nullable=False),
)
sessions = Table(
    "sessions",
    metadata,
    Column("id", String, primary_key=True),
    Column("profile_id", String, nullable=False),
    Column("principal", String, nullable=False),
    Column("request_id", String, nullable=False),
    Column("payload_hash", String, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("tools", JSON, nullable=False),
    Column("engine", String, nullable=False),
    Column("harness", String, nullable=False, default="general"),
    Column("permission_mode", String, nullable=False, default="legacy"),
    Column("permission_revision", Integer, nullable=False, default=0),
    UniqueConstraint("profile_id", "principal", "request_id"),
)
admissions = Table(
    "admissions",
    metadata,
    Column("ordinal", Integer, primary_key=True, autoincrement=True),
    Column("id", String, nullable=False, unique=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("principal", String, nullable=False),
    Column("submission_id", String, nullable=False),
    Column("payload_hash", String, nullable=False),
    Column("text", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("generation", Integer, nullable=False, default=0),
    Column("owner", String),
    Column("result", Text),
    UniqueConstraint("session_id", "principal", "submission_id"),
)
delegations = Table(
    "delegations",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("admission_id", String, nullable=False),
    Column("parent_tool_call_id", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("mode", String, nullable=False, default="sync"),
    Column("task_hash", String, nullable=False),
    Column("status", String, nullable=False),
    Column("result", Text),
    Column("child_session_id", ForeignKey("sessions.id")),
    Column("child_admission_id", String),
    Column("stop_requested", Integer, nullable=False, default=0),
    UniqueConstraint("admission_id", "parent_tool_call_id"),
)
delegation_tool_receipts = Table(
    "delegation_tool_receipts",
    metadata,
    Column("id", String, primary_key=True),
    Column("delegation_id", ForeignKey("delegations.id"), nullable=False),
    Column("name", String, nullable=False),
    Column("arguments_hash", String, nullable=False),
    Column("result", Text, nullable=False),
)
messages = Table(
    "messages",
    metadata,
    Column("ordinal", Integer, primary_key=True, autoincrement=True),
    Column("id", String, nullable=False, unique=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("admission_id", String, nullable=False),
    Column("payload", JSON, nullable=False),
)
events = Table(
    "events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("type", String, nullable=False),
    Column("payload", JSON, nullable=False),
)
effects = Table(
    "effects",
    metadata,
    Column("id", String, primary_key=True),
    Column("admission_id", String, nullable=False),
    Column("proposal_hash", String, nullable=False),
    Column("status", String, nullable=False),
    Column("result", Text),
)
interactions = Table(
    "interactions",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("admission_id", String, nullable=False),
    Column("proposal_hash", String, nullable=False),
    Column("proposal", JSON, nullable=False),
    Column("decision", String),
)
tool_attempts = Table(
    "tool_attempts",
    metadata,
    Column("admission_id", ForeignKey("admissions.id"), primary_key=True),
    Column("call_id", String, primary_key=True),
)
approval_waits = Table(
    "approval_waits",
    metadata,
    Column("admission_id", ForeignKey("admissions.id"), primary_key=True),
    Column("generation", Integer, nullable=False),
    Column("interaction_ids", JSON, nullable=False),
)
approval_revocations = Table(
    "approval_revocations",
    metadata,
    Column("admission_id", String, primary_key=True),
)
model_receipts = Table(
    "model_receipts",
    metadata,
    Column("id", String, primary_key=True),
    Column("admission_id", String, nullable=False),
    Column("request_hash", String, nullable=False),
    Column("response", JSON, nullable=False),
)
session_policies = Table(
    "session_policies",
    metadata,
    Column("session_id", ForeignKey("sessions.id"), primary_key=True),
    Column("fingerprint", String, nullable=False),
    Column("snapshot", JSON, nullable=False),
)
session_branches = Table(
    "session_branches",
    metadata,
    Column("session_id", ForeignKey("sessions.id"), primary_key=True),
    Column("parent_id", ForeignKey("sessions.id"), nullable=False),
    Column("copied_admissions", JSON, nullable=False),
)
resolutions = Table(
    "resolutions",
    metadata,
    Column("admission_id", ForeignKey("admissions.id"), primary_key=True),
    Column("request_hash", String, nullable=False),
    Column("evidence", Text, nullable=False),
    Column("effects", JSON, nullable=False),
)
context_heads = Table(
    "context_heads",
    metadata,
    Column("session_id", ForeignKey("sessions.id"), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("dropped_ids", JSON, nullable=False),
    Column("summary", Text, nullable=False),
    Column("summary_version", Integer, nullable=False, server_default="1"),
)
context_archives = Table(
    "context_archives",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("messages", JSON, nullable=False),
    Column("summary", Text, nullable=False),
)
skills = Table(
    "skills",
    metadata,
    Column("id", String, primary_key=True),
    Column("profile_id", String, nullable=False),
    Column("name", String, nullable=False),
    Column("description", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("active", Integer, nullable=False),
    Column("content_hash", String, nullable=False),
    UniqueConstraint("profile_id", "name", "version"),
)
memories = Table(
    "memories",
    metadata,
    Column("id", String, primary_key=True),
    Column("profile_id", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("source", String, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("active", Integer, nullable=False),
    Column("content_hash", String, nullable=False),
)
curator_runs = Table(
    "curator_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("profile_id", String, nullable=False),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("history_hash", String, nullable=False),
    Column("model_hash", String, nullable=False),
    Column("usage", JSON),
    Column("created_at", Text, nullable=False),
)
memory_candidates = Table(
    "memory_candidates",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", ForeignKey("curator_runs.id"), nullable=False),
    Column("profile_id", String, nullable=False),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", Text, nullable=False),
)
