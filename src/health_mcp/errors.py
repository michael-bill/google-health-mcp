"""Safe error vocabulary for application and transport boundaries."""


class HealthError(Exception):
    """Only fixed, non-sensitive error codes may cross the MCP boundary."""

    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(code)

    def as_dict(self):
        return {"code": self.code, "http_status": self.status}
