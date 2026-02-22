"""Tests for error detection and classification."""

import pytest

from src.errors import ErrorDetector, _FATAL_PHRASES, _RATE_LIMIT_PHRASES


class TestCheckMessageError:
    def test_rate_limit(self):
        d = ErrorDetector()
        err = d.check_message_error("rate_limit")
        assert err is not None
        assert not err.fatal
        assert err.category == "rate_limit"
        assert err.detection_method == "message.error"

    def test_invalid_request_fatal(self):
        d = ErrorDetector()
        err = d.check_message_error("invalid_request")
        assert err is not None
        assert err.fatal
        assert err.category == "invalid_request"

    def test_none_error(self):
        d = ErrorDetector()
        assert d.check_message_error(None) is None
        assert d.error is None

    def test_only_first_classified(self):
        d = ErrorDetector()
        first = d.check_message_error("rate_limit")
        assert first is not None
        second = d.check_message_error("server_error")
        assert second is None
        assert d.error is first


class TestCheckTextContent:
    @pytest.mark.parametrize("phrase", _FATAL_PHRASES)
    def test_fatal_phrases(self, phrase):
        d = ErrorDetector()
        err = d.check_text_content(f"Error: {phrase} for this request")
        assert err is not None
        assert err.fatal
        assert err.detection_method == "string_match_fallback"

    @pytest.mark.parametrize("phrase", _RATE_LIMIT_PHRASES)
    def test_rate_limit_phrases(self, phrase):
        d = ErrorDetector()
        err = d.check_text_content(f"Error: {phrase}")
        assert err is not None
        assert not err.fatal

    def test_no_match(self):
        d = ErrorDetector()
        assert d.check_text_content("Everything is fine.") is None

    def test_skipped_if_error_exists(self):
        d = ErrorDetector()
        d.check_message_error("rate_limit")
        assert d.check_text_content("prompt is too long") is None


class TestCheckResultError:
    def test_prompt_too_long_fatal(self):
        d = ErrorDetector()
        err = d.check_result_error(True, "The prompt is too long for this model")
        assert err is not None
        assert err.fatal
        assert err.category == "prompt_too_long"

    def test_unknown_error_nonfatal(self):
        d = ErrorDetector()
        err = d.check_result_error(True, "Something went wrong")
        assert err is not None
        assert not err.fatal
        assert err.category == "unknown"

    def test_no_error_flag(self):
        d = ErrorDetector()
        assert d.check_result_error(False, "prompt is too long") is None


class TestClassifyException:
    def test_prompt_too_long(self):
        err = ErrorDetector.classify_exception(Exception("The prompt is too long"))
        assert err.fatal
        assert err.category == "prompt_too_long"

    def test_overloaded(self):
        err = ErrorDetector.classify_exception(Exception("API overloaded (529)"))
        assert not err.fatal
        assert err.category == "overloaded"

    def test_rate_limit(self):
        err = ErrorDetector.classify_exception(Exception("rate limit exceeded"))
        assert not err.fatal
        assert err.category == "rate_limit"

    def test_429(self):
        err = ErrorDetector.classify_exception(Exception("HTTP 429"))
        assert not err.fatal
        assert err.category == "rate_limit"

    def test_timeout(self):
        err = ErrorDetector.classify_exception(Exception("Connection timeout"))
        assert not err.fatal
        assert err.category == "timeout"

    def test_unknown(self):
        err = ErrorDetector.classify_exception(Exception("Something unexpected"))
        assert not err.fatal
        assert err.category == "unknown"


class TestDetectorState:
    def test_reset_clears_error(self):
        d = ErrorDetector()
        d.check_message_error("rate_limit")
        assert d.error is not None
        d.reset()
        assert d.error is None
        # Can classify again after reset
        err = d.check_message_error("server_error")
        assert err is not None

    def test_is_fatal_property(self):
        d = ErrorDetector()
        assert not d.is_fatal
        d.check_message_error("invalid_request")
        assert d.is_fatal
