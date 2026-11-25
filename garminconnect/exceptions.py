from typing import Any


class GarminConnectConnectionError(Exception):
    """Raised when communication ended in error.

    ``response`` carries the original HTTP response when the error wraps an
    HTTP failure, so callers can inspect status codes and bodies.
    """

    response: Any = None


class GarminConnectNotFoundError(GarminConnectConnectionError):
    """Raised when a requested resource does not exist (HTTP 404).

    Subclasses GarminConnectConnectionError for backwards compatibility, so
    existing ``except GarminConnectConnectionError`` handlers keep working while
    callers can now catch a missing resource specifically (e.g. deleting an
    already-deleted workout).
    """


class GarminConnectConsentRequiredError(GarminConnectConnectionError):
    """Raised when Garmin refuses a request with HTTP 412 Precondition Failed.

    Garmin refuses the request because the account has not granted upload
    consent; this was seen on every account tested. ``reason_code`` is the
    ``reasonCode`` from Garmin's reply: 1 when the account owner declined, 2
    when they never answered, or None when the reply has no such number.
    Grant consent with ``grant_upload_consent()`` (or by agreeing in Garmin
    Connect) and check it with ``has_upload_consent()``.

    Subclasses GarminConnectConnectionError for backwards compatibility, so
    existing ``except GarminConnectConnectionError`` handlers keep working while
    callers can now catch a missing consent specifically.
    """

    reason_code: int | None = None


def _consent_reason_code(response: Any) -> int | None:
    """Return the integer ``reasonCode`` of a 412 reply, or None."""
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("reasonCode")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return None


class GarminConnectTooManyRequestsError(Exception):
    """Raised when rate limit is exceeded."""


class GarminConnectAuthenticationError(Exception):
    """Raised when authentication is failed."""


class GarminConnectInvalidFileFormatError(Exception):
    """Raised when an invalid file format is provided."""
