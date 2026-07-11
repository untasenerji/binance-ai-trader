"""Backup/restore readiness built on the append-only audit hash chain."""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.persistence.audit import verify_hash_chain
from app.persistence.models import AuditEvent


class BackupNotReady(RuntimeError):
    code = "BACKUP_AUDIT_CHAIN_INVALID"


@dataclass(frozen=True, slots=True)
class BackupManifest:
    created_at_utc: datetime
    audit_event_count: int
    last_record_hash: str | None
    audit_hash_chain_valid: bool


def create_backup_manifest(events: tuple[AuditEvent, ...]) -> BackupManifest:
    is_valid = verify_hash_chain(events)
    if not is_valid:
        raise BackupNotReady("audit hash chain must verify before backup")
    return BackupManifest(
        created_at_utc=datetime.now(UTC),
        audit_event_count=len(events),
        last_record_hash=events[-1].record_hash if events else None,
        audit_hash_chain_valid=True,
    )


def verify_restore(events: tuple[AuditEvent, ...], manifest: BackupManifest) -> bool:
    if not manifest.audit_hash_chain_valid:
        return False
    if len(events) != manifest.audit_event_count:
        return False
    if (events[-1].record_hash if events else None) != manifest.last_record_hash:
        return False
    return verify_hash_chain(events)
