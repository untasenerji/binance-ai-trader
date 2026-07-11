"""Persistence failure is a hard gate for new entry creation."""

from dataclasses import dataclass


class PersistenceUnavailable(RuntimeError):
    code = "DATABASE_AUDIT_FAILURE"


@dataclass(slots=True)
class PersistenceCircuitBreaker:
    halted_reason: str | None = None

    @property
    def new_entries_allowed(self) -> bool:
        return self.halted_reason is None

    def record_write_failure(self, error: Exception) -> None:
        self.halted_reason = f"{PersistenceUnavailable.code}: {type(error).__name__}"

    def require_new_entries_allowed(self) -> None:
        if not self.new_entries_allowed:
            raise PersistenceUnavailable(self.halted_reason)

    def reset_after_successful_reconciliation(self) -> None:
        self.halted_reason = None
