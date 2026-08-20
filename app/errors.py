"""The single error taxonomy. Every non-2xx response in the service is an ApiError."""

from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={"error": {"code": self.code, "message": self.message}},
            headers=self.headers,
        )


def unauthorized(msg="Missing or invalid bearer token.") -> ApiError:
    return ApiError(401, "unauthorized", msg)


def payload_too_large(msg="Payload exceeds 1048576 bytes.") -> ApiError:
    return ApiError(413, "payload_too_large", msg)


def invalid_json(msg="Request body is not valid JSON.") -> ApiError:
    return ApiError(400, "invalid_json", msg)


def invalid_diff(msg="Field 'diff' is missing, empty, or not a parseable unified diff.") -> ApiError:
    return ApiError(422, "invalid_diff", msg)


def idempotency_conflict(msg="Idempotency-Key was already used with a different body.") -> ApiError:
    return ApiError(409, "idempotency_conflict", msg)


def not_found(msg="Resource not found.") -> ApiError:
    return ApiError(404, "not_found", msg)


def rate_limited(retry_after: int) -> ApiError:
    return ApiError(
        429,
        "rate_limited",
        "Too many submissions. Retry after the interval in the Retry-After header.",
        headers={"Retry-After": str(retry_after)},
    )


def internal(msg="Internal server error.") -> ApiError:
    return ApiError(500, "internal", msg)
