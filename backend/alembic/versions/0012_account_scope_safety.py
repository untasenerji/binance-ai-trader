"""Add V1 account fencing, durable envelope heads, and resumable safety upgrades.

Revision ID: 0012_account_scope_safety
Revises: 0011_forward_invariants
Create Date: 2026-07-16
"""

import hashlib
import json
import os
from collections import deque
from collections.abc import Callable, Mapping, Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_account_scope_safety"
down_revision: str | None = "0011_forward_invariants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ACCOUNT_ID = "v1-primary"
_MARKERS = "migration_execution_markers"
_POLICY_TABLES = (
    "durable_actual_risk_policies",
    "durable_actual_risk_policy_versions",
)
_IMMUTABLE_TABLES = (
    "durable_portfolio_envelope_heads",
    "durable_portfolio_envelope_supersessions",
    "durable_evidence_quarantines",
    "durable_evidence_quarantine_resolutions",
)


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _create_markers(bind: sa.Connection) -> None:
    if _table_exists(bind, _MARKERS):
        return
    op.create_table(
        _MARKERS,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("migration_revision", sa.String(length=64), nullable=False),
        sa.Column("checkpoint", sa.String(length=64), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("migration_revision", "checkpoint", name="uq_migration_checkpoint"),
    )


def _checkpoint_completed(bind: sa.Connection, checkpoint: str) -> bool:
    marker = sa.Table(_MARKERS, sa.MetaData(), autoload_with=bind)
    existing = bind.scalar(
        sa.select(sa.func.count())
        .select_from(marker)
        .where(
            marker.c.migration_revision == revision,
            marker.c.checkpoint == checkpoint,
        )
    )
    return bool(existing)


def _run_checkpoint(
    bind: sa.Connection,
    checkpoint: str,
    operation: Callable[[], None],
) -> None:
    """Run idempotent work before marking it complete for SQLite DDL retry safety."""
    if _checkpoint_completed(bind, checkpoint):
        return
    operation()
    if os.environ.get("UTA_0012_FAIL_AFTER") == checkpoint:
        raise RuntimeError(f"0012 checkpoint failure injected after {checkpoint}")
    marker = sa.Table(_MARKERS, sa.MetaData(), autoload_with=bind)
    bind.execute(marker.insert().values(migration_revision=revision, checkpoint=checkpoint))


def _add_account_columns(bind: sa.Connection) -> None:
    for table_name in (
        "durable_order_intents",
        "durable_intent_fills",
        "durable_intent_absence_observations",
        "durable_actual_risk_policies",
        "durable_actual_risk_policy_versions",
        "durable_account_portfolio_envelopes",
        "durable_entry_authorization_grants",
        "durable_entry_authorization_revocations",
        "durable_simulated_protections",
        "durable_actual_risk_states",
        "durable_risk_reduction_requirements",
    ):
        if not _table_exists(bind, table_name) or "account_id" in _column_names(bind, table_name):
            continue
        op.add_column(
            table_name,
            sa.Column(
                "account_id",
                sa.String(length=128),
                nullable=False,
                server_default=_ACCOUNT_ID,
            ),
        )
        op.create_index(f"ix_{table_name}_account_id", table_name, ["account_id"])
    if _table_exists(bind, "durable_entry_authorization_grants"):
        for column_name in (
            "failure_epoch",
            "recovery_epoch",
            "envelope_version",
            "grant_generation",
        ):
            if column_name not in _column_names(bind, "durable_entry_authorization_grants"):
                op.add_column(
                    "durable_entry_authorization_grants",
                    sa.Column(column_name, sa.Integer(), nullable=False, server_default="0"),
                )


def _create_safety_tables(bind: sa.Connection) -> None:
    if not _table_exists(bind, "durable_portfolio_envelope_heads"):
        op.create_table(
            "durable_portfolio_envelope_heads",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("account_scope", sa.String(length=128), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("verified_account_equity_usdt", sa.String(length=64), nullable=False),
            sa.Column("bot_equity_cap_usdt", sa.String(length=64), nullable=False),
            sa.Column("required_reserve_usdt", sa.String(length=64), nullable=False),
            sa.Column("max_total_exposure_usdt", sa.String(length=64), nullable=False),
            sa.Column("symbol_exposure_caps_usdt", sa.JSON(), nullable=False),
            sa.Column("max_required_margin_usdt", sa.String(length=64), nullable=False),
            sa.Column("daily_remaining_risk_usdt", sa.String(length=64), nullable=False),
            sa.Column("weekly_remaining_risk_usdt", sa.String(length=64), nullable=False),
            sa.Column("open_position_count", sa.Integer(), nullable=False),
            sa.Column("pending_order_count", sa.Integer(), nullable=False),
            sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
            sa.Column("exposure_slices", sa.JSON(), nullable=False),
            sa.Column("envelope_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("fingerprint_effective_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "effective_from",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column("superseded_by", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_id", "version", name="uq_portfolio_envelope_head_version"),
            sa.UniqueConstraint("envelope_fingerprint"),
        )
        op.create_index(
            "ix_durable_portfolio_envelope_heads_account_id",
            "durable_portfolio_envelope_heads",
            ["account_id"],
        )
    if not _table_exists(bind, "durable_portfolio_envelope_supersessions"):
        op.create_table(
            "durable_portfolio_envelope_supersessions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("superseded_version", sa.Integer(), nullable=False),
            sa.Column("superseded_by", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "account_id", "superseded_version", name="uq_envelope_supersession"
            ),
        )
        op.create_index(
            "ix_durable_portfolio_envelope_supersessions_account_id",
            "durable_portfolio_envelope_supersessions",
            ["account_id"],
        )
    if not _table_exists(bind, "durable_account_safety_states"):
        op.create_table(
            "durable_account_safety_states",
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("failure_epoch", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("recovery_epoch", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("envelope_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("envelope_fingerprint", sa.String(length=64), nullable=True),
            sa.Column("grant_generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("recovery_required", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("account_id"),
        )
    if not _table_exists(bind, "durable_account_scope_locks"):
        op.create_table(
            "durable_account_scope_locks",
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("lock_generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("account_id"),
        )
    if not _table_exists(bind, "durable_evidence_quarantines"):
        op.create_table(
            "durable_evidence_quarantines",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("quarantine_id", sa.String(length=128), nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("economic_key", sa.String(length=256), nullable=False),
            sa.Column("client_order_id", sa.String(length=128), nullable=False),
            sa.Column("query_reference", sa.String(length=128), nullable=True),
            sa.Column("provenance_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("reason", sa.String(length=128), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("quarantine_id"),
            sa.UniqueConstraint(
                "account_id",
                "economic_key",
                "provenance_fingerprint",
                name="uq_evidence_quarantine_identity",
            ),
        )
        for column_name in ("quarantine_id", "account_id", "economic_key", "client_order_id"):
            op.create_index(
                f"ix_durable_evidence_quarantines_{column_name}",
                "durable_evidence_quarantines",
                [column_name],
            )
    if not _table_exists(bind, "durable_evidence_quarantine_resolutions"):
        op.create_table(
            "durable_evidence_quarantine_resolutions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("resolution_id", sa.String(length=128), nullable=False),
            sa.Column("quarantine_id", sa.String(length=128), nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("operator_id", sa.String(length=128), nullable=False),
            sa.Column("verified_evidence_source", sa.String(length=64), nullable=False),
            sa.Column("verified_query_reference", sa.String(length=128), nullable=False),
            sa.Column("verified_evidence_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["quarantine_id"], ["durable_evidence_quarantines.quarantine_id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("resolution_id"),
            sa.UniqueConstraint("quarantine_id", name="uq_evidence_quarantine_resolution"),
        )
        op.create_index(
            "ix_durable_evidence_quarantine_resolutions_quarantine_id",
            "durable_evidence_quarantine_resolutions",
            ["quarantine_id"],
        )
        op.create_index(
            "ix_durable_evidence_quarantine_resolutions_account_id",
            "durable_evidence_quarantine_resolutions",
            ["account_id"],
        )


def _ensure_account_safety_rows(
    bind: sa.Connection,
    safety: sa.Table,
    locks: sa.Table,
    *,
    account_id: str,
    envelope_version: int = 0,
    envelope_fingerprint: str | None = None,
) -> None:
    if account_id != _ACCOUNT_ID:
        raise RuntimeError("V1_SECOND_ACCOUNT_UNSUPPORTED")
    current = (
        bind.execute(sa.select(safety).where(safety.c.account_id == account_id)).mappings().first()
    )
    if current is None:
        bind.execute(
            safety.insert().values(
                account_id=account_id,
                envelope_version=envelope_version,
                envelope_fingerprint=envelope_fingerprint,
                recovery_required=True,
            )
        )
    elif envelope_version > int(current["envelope_version"]):
        bind.execute(
            safety.update()
            .where(safety.c.account_id == account_id)
            .values(
                envelope_version=envelope_version,
                envelope_fingerprint=envelope_fingerprint,
                grant_generation=int(current["grant_generation"]) + 1,
                recovery_required=True,
            )
        )
    if not bind.scalar(
        sa.select(sa.func.count()).select_from(locks).where(locks.c.account_id == account_id)
    ):
        bind.execute(locks.insert().values(account_id=account_id, lock_generation=0))


def _backfill_heads(bind: sa.Connection) -> None:
    heads = sa.Table("durable_portfolio_envelope_heads", sa.MetaData(), autoload_with=bind)
    safety = sa.Table("durable_account_safety_states", sa.MetaData(), autoload_with=bind)
    locks = sa.Table("durable_account_scope_locks", sa.MetaData(), autoload_with=bind)
    _ensure_account_safety_rows(bind, safety, locks, account_id=_ACCOUNT_ID)
    if not _table_exists(bind, "durable_account_portfolio_envelopes"):
        return
    source = sa.Table("durable_account_portfolio_envelopes", sa.MetaData(), autoload_with=bind)
    rows = tuple(
        bind.execute(
            sa.select(source).order_by(
                source.c.account_id.asc(), source.c.version.asc(), source.c.id.asc()
            )
        ).mappings()
    )
    latest: dict[str, Mapping[str, object]] = {}
    for row in rows:
        account_id = str(row.get("account_id") or _ACCOUNT_ID)
        if account_id != _ACCOUNT_ID:
            raise RuntimeError("V1_SECOND_ACCOUNT_UNSUPPORTED")
        existing = bind.scalar(
            sa.select(sa.func.count())
            .select_from(heads)
            .where(heads.c.account_id == account_id, heads.c.version == row["version"])
        )
        if not existing:
            bind.execute(
                heads.insert().values(
                    account_id=account_id,
                    account_scope=str(row["account_scope"]),
                    version=int(row["version"]),
                    verified_account_equity_usdt=str(row["verified_account_equity_usdt"]),
                    bot_equity_cap_usdt=str(row["bot_equity_cap_usdt"]),
                    required_reserve_usdt=str(row["required_reserve_usdt"]),
                    max_total_exposure_usdt=str(row["max_total_exposure_usdt"]),
                    symbol_exposure_caps_usdt={"*": str(row["max_symbol_exposure_usdt"])},
                    max_required_margin_usdt=str(row["max_required_margin_usdt"]),
                    daily_remaining_risk_usdt=str(row["daily_remaining_risk_usdt"]),
                    weekly_remaining_risk_usdt=str(row["weekly_remaining_risk_usdt"]),
                    open_position_count=int(row["open_position_count"]),
                    pending_order_count=int(row["pending_order_count"]),
                    reconciliation_required=bool(row["reconciliation_required"]),
                    exposure_slices=row["exposure_slices"],
                    envelope_fingerprint=str(row["envelope_fingerprint"]),
                    fingerprint_effective_from=None,
                    effective_from=row["created_at"],
                    superseded_by=None,
                )
            )
        latest[account_id] = row
    for account_id, row in latest.items():
        _ensure_account_safety_rows(
            bind,
            safety,
            locks,
            account_id=account_id,
            envelope_version=int(row["version"]),
            envelope_fingerprint=str(row["envelope_fingerprint"]),
        )


def _legacy_quarantine_fingerprint(evidence: Mapping[str, object]) -> str:
    canonical = json.dumps(
        evidence,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _backfill_migration_quarantines(bind: sa.Connection) -> None:
    """Turn all historical migration quarantines into active durable deny records."""
    if not _table_exists(bind, "migration_quarantine_records"):
        return
    source = sa.Table("migration_quarantine_records", sa.MetaData(), autoload_with=bind)
    target = sa.Table("durable_evidence_quarantines", sa.MetaData(), autoload_with=bind)
    rows = tuple(
        bind.execute(
            sa.select(source)
            .where(source.c.reconciliation_required.is_(True))
            .order_by(source.c.id)
        ).mappings()
    )
    for row in rows:
        raw_evidence = row["evidence"]
        if isinstance(raw_evidence, str):
            try:
                evidence = json.loads(raw_evidence)
            except json.JSONDecodeError as error:
                raise RuntimeError("migration quarantine evidence is not JSON") from error
        else:
            evidence = raw_evidence
        if not isinstance(evidence, Mapping):
            raise RuntimeError("migration quarantine evidence is not structured")
        account_id = str(evidence.get("account_id") or _ACCOUNT_ID)
        client_order_id = evidence.get("client_order_id")
        economic_key = evidence.get("economic_key")
        query_reference = evidence.get("query_reference")
        if (
            account_id != _ACCOUNT_ID
            or not isinstance(client_order_id, str)
            or not client_order_id
            or not isinstance(economic_key, str)
            or not economic_key
            or query_reference is not None
            and not isinstance(query_reference, str)
        ):
            raise RuntimeError("migration quarantine cannot be safely scoped to V1 account")
        provenance = evidence.get("provenance_fingerprint")
        if not isinstance(provenance, str) or len(provenance) != 64:
            provenance = _legacy_quarantine_fingerprint(evidence)
        existing = bind.scalar(
            sa.select(sa.func.count())
            .select_from(target)
            .where(
                target.c.account_id == account_id,
                target.c.economic_key == economic_key,
                target.c.provenance_fingerprint == provenance,
            )
        )
        if existing:
            continue
        bind.execute(
            target.insert().values(
                quarantine_id=f"migration-quarantine-{row['id']}",
                account_id=account_id,
                economic_key=economic_key,
                client_order_id=client_order_id,
                query_reference=query_reference,
                provenance_fingerprint=provenance,
                reason=f"MIGRATION_{row['reason']}"[:128],
                evidence=dict(evidence),
            )
        )


def _drop_guard(bind: sa.Connection, table_name: str) -> None:
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_{operation}"))
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_mutation ON {table_name}")
        )


def _install_guard(bind: sa.Connection, table_name: str) -> None:
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER IF NOT EXISTS prevent_{table_name}_{operation} "
                    f"BEFORE {operation.upper()} ON {table_name} "
                    f"BEGIN SELECT RAISE(ABORT, '{table_name} are append-only'); END"
                )
            )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION reject_immutable_evidence_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'immutable evidence is append-only'; "
                "RETURN NULL; END; $$"
            )
        )
        trigger_name = f"prevent_{table_name}_mutation"
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
        bind.execute(
            sa.text(
                f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON {table_name} FOR EACH STATEMENT "
                "EXECUTE FUNCTION reject_immutable_evidence_mutation()"
            )
        )


def _install_all_guards(bind: sa.Connection) -> None:
    for table_name in _IMMUTABLE_TABLES:
        _install_guard(bind, table_name)


def _role_graph_violation(bind: sa.Connection) -> str | None:
    runtime_oid = bind.scalar(sa.text("SELECT oid FROM pg_roles WHERE rolname = 'uta_runtime'"))
    if runtime_oid is None:
        raise RuntimeError("uta_runtime role is missing")
    role_rows = tuple(
        bind.execute(
            sa.text("SELECT oid, rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles")
        ).mappings()
    )
    roles = {int(row["oid"]): row for row in role_rows}
    members: dict[int, list[Mapping[str, object]]] = {}
    for row in bind.execute(
        sa.text(
            "SELECT member, roleid, inherit_option, set_option, admin_option FROM pg_auth_members"
        )
    ).mappings():
        members.setdefault(int(row["member"]), []).append(row)
    owner_oids = {
        int(value)
        for value in bind.scalars(
            sa.text(
                "SELECT relowner FROM pg_class WHERE relname = ANY(:tables) "
                "AND relkind IN ('r', 'p')"
            ),
            {"tables": list(_POLICY_TABLES)},
        )
    }
    # Every edge in an inherited/SET path must permit that mode. ADMIN is
    # deliberately fail-closed once it becomes usable through a settable role:
    # it could otherwise be used to rewrite membership after this migration.
    queue: deque[tuple[int, tuple[int, ...], bool, bool, bool]] = deque(
        [(int(runtime_oid), (int(runtime_oid),), True, True, False)]
    )
    seen: set[tuple[int, bool, bool, bool]] = set()
    while queue:
        role_id, path, inherited, settable, adminable = queue.popleft()
        for edge in members.get(role_id, []):
            next_role = int(edge["roleid"])
            if next_role in path:
                continue
            next_inherited = inherited and bool(edge["inherit_option"])
            next_settable = settable and bool(edge["set_option"])
            next_adminable = adminable or (settable and bool(edge["admin_option"]))
            state = (next_role, next_inherited, next_settable, next_adminable)
            if state in seen:
                continue
            seen.add(state)
            role = roles.get(next_role)
            if role is None:
                continue
            name = str(role["rolname"])
            write_capable = (
                name in {"uta_policy_config", "pg_write_all_data", "pg_database_owner"}
                or next_role in owner_oids
                or bool(role["rolsuper"])
                or bool(role["rolcreaterole"])
                or bool(role["rolcreatedb"])
            )
            if write_capable and (next_inherited or next_settable or next_adminable):
                return name
            queue.append(
                (next_role, (*path, next_role), next_inherited, next_settable, next_adminable)
            )

    # PostgreSQL owns the final semantics for transitive membership. Ask it as
    # well so INHERIT TRUE / SET FALSE and server-version-specific membership
    # details cannot be accidentally approximated by the graph traversal.
    for role_id, role in roles.items():
        name = str(role["rolname"])
        write_capable = (
            name in {"uta_policy_config", "pg_write_all_data", "pg_database_owner"}
            or role_id in owner_oids
            or bool(role["rolsuper"])
            or bool(role["rolcreaterole"])
            or bool(role["rolcreatedb"])
        )
        if not write_capable:
            continue
        for privilege in ("USAGE", "SET", "MEMBER"):
            if bind.scalar(
                sa.text("SELECT pg_has_role('uta_runtime', :role_name, :privilege)"),
                {"role_name": name, "privilege": privilege},
            ):
                return name
    return None


def _runtime_probe_is_denied(bind: sa.Connection, statement: str) -> bool:
    """Run a no-side-effect probe under the actual runtime role identity."""
    bind.execute(sa.text("SAVEPOINT uta_runtime_privilege_probe"))
    try:
        bind.execute(sa.text(statement))
    except sa.exc.DBAPIError:
        bind.execute(sa.text("ROLLBACK TO SAVEPOINT uta_runtime_privilege_probe"))
        bind.execute(sa.text("RELEASE SAVEPOINT uta_runtime_privilege_probe"))
        return True
    bind.execute(sa.text("ROLLBACK TO SAVEPOINT uta_runtime_privilege_probe"))
    bind.execute(sa.text("RELEASE SAVEPOINT uta_runtime_privilege_probe"))
    return False


def _verify_runtime_session_privileges(bind: sa.Connection) -> None:
    """Verify effective privileges using PostgreSQL, not only catalog metadata."""
    bind.execute(sa.text("SET LOCAL ROLE uta_runtime"))
    try:
        current_user = bind.scalar(sa.text("SELECT current_user"))
        if current_user != "uta_runtime":
            raise RuntimeError("unable to enter uta_runtime role for privilege verification")
        for table_name in _POLICY_TABLES:
            if not bind.scalar(
                sa.text("SELECT has_table_privilege(current_user, :table_name, 'SELECT')"),
                {"table_name": table_name},
            ):
                raise RuntimeError("uta_runtime lost required policy table SELECT privilege")
            bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 0"))
            for operation in (
                f"UPDATE {table_name} SET plan_id = plan_id WHERE FALSE",
                f"DELETE FROM {table_name} WHERE FALSE",
                f"TRUNCATE TABLE {table_name}",
            ):
                if not _runtime_probe_is_denied(bind, operation):
                    raise RuntimeError("uta_runtime effective policy-table write was not denied")
        if not _runtime_probe_is_denied(
            bind,
            "CREATE TEMPORARY TABLE uta_runtime_privilege_probe (id integer)",
        ):
            raise RuntimeError("uta_runtime can create temporary shadow tables")
        if not _runtime_probe_is_denied(
            bind,
            "CREATE TABLE public.uta_runtime_privilege_probe (id integer)",
        ):
            raise RuntimeError("uta_runtime can create schema objects")
    finally:
        bind.execute(sa.text("RESET ROLE"))


def _harden_postgresql_roles(bind: sa.Connection) -> None:
    if bind.dialect.name != "postgresql":
        return
    violation = _role_graph_violation(bind)
    if violation is not None:
        raise RuntimeError(f"uta_runtime effective role graph reaches write role: {violation}")
    bind.execute(
        sa.text(
            "ALTER ROLE uta_runtime WITH NOSUPERUSER NOCREATEROLE NOCREATEDB "
            "NOREPLICATION NOBYPASSRLS NOINHERIT"
        )
    )
    bind.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    bind.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM uta_runtime"))
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database()); "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM uta_runtime', "
            "current_database()); "
            "END $$"
        )
    )
    for table_name in (*_POLICY_TABLES, *_IMMUTABLE_TABLES):
        bind.execute(sa.text(f"ALTER TABLE {table_name} OWNER TO uta_policy_config"))
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM PUBLIC"))
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM uta_runtime"))
        bind.execute(sa.text(f"GRANT SELECT ON TABLE {table_name} TO uta_runtime"))
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            allowed = bind.scalar(
                sa.text("SELECT has_table_privilege('uta_runtime', :table_name, :privilege)"),
                {"table_name": table_name, "privilege": privilege},
            )
            if allowed:
                raise RuntimeError("uta_runtime retains policy table write privilege")
        if not bind.scalar(
            sa.text("SELECT has_table_privilege('uta_runtime', :table_name, 'SELECT')"),
            {"table_name": table_name},
        ):
            raise RuntimeError("uta_runtime lost required policy table SELECT privilege")
    _verify_runtime_session_privileges(bind)


def _verify(bind: sa.Connection) -> None:
    required = {
        "durable_portfolio_envelope_heads",
        "durable_account_safety_states",
        "durable_account_scope_locks",
        "durable_evidence_quarantines",
        "durable_evidence_quarantine_resolutions",
        _MARKERS,
    }
    missing = sorted(table for table in required if not _table_exists(bind, table))
    if missing:
        raise RuntimeError("0012 required tables missing: " + ", ".join(missing))
    if bind.dialect.name == "sqlite":
        trigger_names = {
            str(name)
            for name in bind.scalars(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        for table_name in _IMMUTABLE_TABLES:
            for operation in ("update", "delete"):
                if f"prevent_{table_name}_{operation}" not in trigger_names:
                    raise RuntimeError(f"0012 append-only guard missing for {table_name}")


def upgrade() -> None:
    bind = op.get_bind()
    _create_markers(bind)
    _run_checkpoint(bind, "account_columns", lambda: _add_account_columns(bind))
    _run_checkpoint(bind, "heads", lambda: _create_safety_tables(bind))
    _run_checkpoint(bind, "backfill", lambda: _backfill_heads(bind))
    _run_checkpoint(
        bind,
        "quarantine_backfill",
        lambda: _backfill_migration_quarantines(bind),
    )
    _run_checkpoint(
        bind,
        "guards",
        lambda: _install_all_guards(bind),
    )
    _run_checkpoint(bind, "roles", lambda: _harden_postgresql_roles(bind))
    _verify(bind)


def downgrade() -> None:
    raise RuntimeError("0012 account safety migration is forward-only")
