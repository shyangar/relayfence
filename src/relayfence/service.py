"""A single validation/error boundary for every transport."""

import logging
import sqlite3

from pydantic import ValidationError

from .errors import CoordinationError
from .identity import ADMIN, Principal
from .models import OPERATIONS
from .store import Store

logger = logging.getLogger(__name__)


class Service:
    def __init__(self, store: Store):
        self.store = store

    def call(self, operation: str, payload: dict, principal: Principal = ADMIN) -> tuple[dict, int]:
        try:
            if operation not in OPERATIONS:
                raise CoordinationError("unknown_operation", "Unknown operation", status=404)
            request = OPERATIONS[operation][0].model_validate(payload)
            return self.store.execute(operation, request, principal), 200
        except ValidationError as exc:
            error = CoordinationError(
                "invalid_request",
                "Request does not match the operation schema",
                {
                    "fields": [{"path": list(e["loc"]), "type": e["type"]} for e in exc.errors()],
                },
                422,
            )
        except CoordinationError as exc:
            error = exc
        except (ValueError, UnicodeError):
            error = CoordinationError(
                "invalid_request", "Values must be finite JSON encoded as valid UTF-8", status=422
            )
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                error = CoordinationError(
                    "busy", "Database is busy; retry with the SAME request_id", status=503
                )
            else:
                logger.exception("Ledger operation failed")
                error = CoordinationError(
                    "storage_error",
                    "Storage unavailable; inspect server logs and retry with the SAME request_id",
                    status=503,
                )
        except Exception:
            logger.exception("Unexpected ledger failure")
            error = CoordinationError(
                "internal_error", "Server error; retry with the SAME request_id", status=500
            )
        return error.as_dict(), error.status
