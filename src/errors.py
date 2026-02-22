"""Error detection and classification for the agent tick loop.

Consolidates three detection layers (SDK bug workaround) into a single
stateful detector. When the SDK properly sets message.error for all errors,
the string-matching fallback can be removed and this module simplified.

See: https://github.com/anthropics/claude-agent-sdk-python/issues/472
"""

from dataclasses import dataclass


@dataclass
class ErrorInfo:
    """Structured error classification."""

    fatal: bool  # Unrecoverable — requires pause/manual intervention
    category: str  # "rate_limit", "prompt_too_long", "overloaded", "timeout", "unknown"
    text: str  # The error text for logging
    detection_method: str  # How it was detected (for removal tracking)


# Phrases from the Anthropic API indicating unrecoverable errors
_FATAL_PHRASES = [
    "prompt is too long",
    "context_length_exceeded",
    "input is too long",
]

# Phrases indicating rate limiting / quota exhaustion
_RATE_LIMIT_PHRASES = [
    "you've hit your limit",
    "you have hit your limit",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "billing_error",
    "overloaded",
    "529",
]


class ErrorDetector:
    """Stateful error detector for a single tick.

    Tracks whether an error has been detected so later layers don't
    re-classify an already-caught error.
    """

    def __init__(self):
        self._error: ErrorInfo | None = None

    def reset(self):
        """Clear error state for retry."""
        self._error = None

    @property
    def error(self) -> ErrorInfo | None:
        """The detected error, if any."""
        return self._error

    @property
    def is_fatal(self) -> bool:
        return self._error is not None and self._error.fatal

    def check_message_error(self, error_field: str | None) -> ErrorInfo | None:
        """Layer 1: Check AssistantMessage.error field (the proper way).

        The SDK defines error as a Literal["authentication_failed", "billing_error",
        "rate_limit", "invalid_request", "server_error", "unknown"].
        """
        if self._error or not error_field:
            return None
        if error_field == "invalid_request":
            self._error = ErrorInfo(
                fatal=True, category="invalid_request", text=error_field, detection_method="message.error"
            )
        else:
            self._error = ErrorInfo(
                fatal=False, category=error_field, text=error_field, detection_method="message.error"
            )
        return self._error

    def check_text_content(self, text: str) -> ErrorInfo | None:
        """Layer 2: String matching fallback (SDK bug workaround).

        Due to SDK bug #472, errors arrive as text content without
        message.error being set. Uses exact/near-exact matches to
        minimize false positives.
        """
        if self._error:
            return None
        text_lower = text.lower()

        if any(phrase in text_lower for phrase in _FATAL_PHRASES):
            self._error = ErrorInfo(
                fatal=True, category="prompt_too_long", text=text[:200], detection_method="string_match_fallback"
            )
            return self._error

        if any(phrase in text_lower for phrase in _RATE_LIMIT_PHRASES):
            self._error = ErrorInfo(
                fatal=False, category="rate_limit", text=text[:200], detection_method="string_match_fallback"
            )
            return self._error

        return None

    def check_result_error(self, is_error: bool, result_text: str) -> ErrorInfo | None:
        """Layer 3: Check ResultMessage.is_error flag."""
        if self._error or not is_error:
            return None
        result_lower = result_text.lower()
        if "prompt" in result_lower and "long" in result_lower:
            self._error = ErrorInfo(
                fatal=True,
                category="prompt_too_long",
                text=result_text[:200],
                detection_method="result_message.is_error",
            )
        else:
            self._error = ErrorInfo(
                fatal=False, category="unknown", text=result_text[:200], detection_method="result_message.is_error"
            )
        return self._error

    @staticmethod
    def classify_exception(error: Exception) -> ErrorInfo:
        """Classify an exception raised during the tick.

        Returns structured info for logging/pause decisions.
        """
        error_msg = str(error)
        error_lower = error_msg.lower()

        # Fatal: unrecoverable, needs manual intervention
        if any(p in error_lower for p in ["prompt", "context_length", "input is too long"]) and (
            "long" in error_lower or "exceeded" in error_lower
        ):
            return ErrorInfo(fatal=True, category="prompt_too_long", text=error_msg, detection_method="exception")

        # Transient categories
        if "overload" in error_lower or "529" in error_lower:
            return ErrorInfo(fatal=False, category="overloaded", text=error_msg, detection_method="exception")
        if "rate" in error_lower or "429" in error_lower or "limit" in error_lower:
            return ErrorInfo(fatal=False, category="rate_limit", text=error_msg, detection_method="exception")
        if "timeout" in error_lower:
            return ErrorInfo(fatal=False, category="timeout", text=error_msg, detection_method="exception")

        return ErrorInfo(fatal=False, category="unknown", text=error_msg, detection_method="exception")
