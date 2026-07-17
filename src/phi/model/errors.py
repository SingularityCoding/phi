from __future__ import annotations


class ModelError(Exception):
    """Base class for failures at the Model boundary."""


class ModelHTTPError(ModelError):
    """A non-success response or HTTP transport failure."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Model HTTP request failed with status {status_code}: {body}")


class ModelProtocolError(ModelError):
    """A successful transport response that violates the Model protocol."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ModelTimeoutError(ModelError):
    """A Model request exceeded its configured timeout."""

    def __init__(self) -> None:
        super().__init__("Model request timed out")
