"""Stable, actionable errors; internal exceptions never cross the wire."""


class CoordinationError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = status

    def as_dict(self) -> dict:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }
