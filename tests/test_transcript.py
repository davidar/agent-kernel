"""Tests for SDK transcript parsing (src/transcript.py)."""

import json

import pytest

from src.transcript import parse_transcript_metrics, get_transcript_path


@pytest.fixture
def mock_claude_dir(tmp_path, monkeypatch):
    """Set up a fake ~/.claude/projects/ hierarchy for transcript tests."""
    projects_dir = tmp_path / ".claude" / "projects"
    project = projects_dir / "test-project"
    project.mkdir(parents=True)
    monkeypatch.setattr("src.transcript.CLAUDE_PROJECTS_DIR", projects_dir)
    return project


class TestGetTranscriptPath:
    def test_finds_transcript(self, mock_claude_dir):
        session_id = "abc-123-def"
        transcript = mock_claude_dir / f"{session_id}.jsonl"
        transcript.write_text("")
        result = get_transcript_path(session_id)
        assert result is not None
        assert result == transcript

    def test_missing_transcript_returns_none(self, mock_claude_dir):
        result = get_transcript_path("nonexistent-session")
        assert result is None

    def test_searches_multiple_projects(self, tmp_path, monkeypatch):
        projects_dir = tmp_path / ".claude" / "projects"
        proj_a = projects_dir / "project-a"
        proj_b = projects_dir / "project-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        monkeypatch.setattr("src.transcript.CLAUDE_PROJECTS_DIR", projects_dir)

        session_id = "session-in-b"
        (proj_b / f"{session_id}.jsonl").write_text("")
        result = get_transcript_path(session_id)
        assert result is not None
        assert "project-b" in str(result)


def _write_transcript(path, entries):
    """Write a list of dicts as JSONL to a file."""
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


class TestParseTranscriptMetrics:
    def test_empty_transcript(self, mock_claude_dir):
        session_id = "empty-session"
        _write_transcript(mock_claude_dir / f"{session_id}.jsonl", [])
        metrics = parse_transcript_metrics(session_id)
        assert metrics["message_count"] == 0
        assert metrics["context_tokens"] == 0
        assert metrics["total_input_tokens"] == 0
        assert metrics["total_output_tokens"] == 0
        assert metrics["compaction_count"] == 0

    def test_usage_accumulation(self, mock_claude_dir):
        session_id = "usage-session"
        entries = [
            {
                "message": {
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_read_input_tokens": 800,
                        "cache_creation_input_tokens": 200,
                    }
                }
            },
            {
                "message": {
                    "usage": {
                        "input_tokens": 1200,
                        "output_tokens": 600,
                        "cache_read_input_tokens": 1500,
                        "cache_creation_input_tokens": 0,
                    }
                }
            },
        ]
        _write_transcript(mock_claude_dir / f"{session_id}.jsonl", entries)
        metrics = parse_transcript_metrics(session_id)
        assert metrics["total_input_tokens"] == 2200
        assert metrics["total_output_tokens"] == 1100
        assert metrics["total_cache_read"] == 2300
        assert metrics["total_cache_create"] == 200
        # context_tokens = most recent cache_read
        assert metrics["context_tokens"] == 1500

    def test_compaction_counted(self, mock_claude_dir):
        session_id = "compact-session"
        entries = [
            {"message": {"usage": {"input_tokens": 100, "output_tokens": 50}}},
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "auto", "preTokens": 180000},
            },
            {
                "message": {
                    "usage": {
                        "input_tokens": 500,
                        "output_tokens": 200,
                        "cache_read_input_tokens": 20000,
                    }
                }
            },
        ]
        _write_transcript(mock_claude_dir / f"{session_id}.jsonl", entries)
        metrics = parse_transcript_metrics(session_id)
        assert metrics["compaction_count"] == 1
        assert metrics["last_compaction"]["trigger"] == "auto"
        assert metrics["last_compaction"]["pre_tokens"] == 180000
        # Context should reflect post-compaction cache_read
        assert metrics["context_tokens"] == 20000

    def test_missing_session_returns_empty(self, mock_claude_dir):
        metrics = parse_transcript_metrics("nonexistent-session-id")
        assert metrics == {}

    def test_malformed_lines_skipped(self, mock_claude_dir):
        session_id = "malformed-session"
        path = mock_claude_dir / f"{session_id}.jsonl"
        with open(path, "w") as f:
            f.write('{"message": {"usage": {"input_tokens": 100, "output_tokens": 50}}}\n')
            f.write("this is not json\n")
            f.write('{"message": {"usage": {"input_tokens": 200, "output_tokens": 100}}}\n')
        metrics = parse_transcript_metrics(session_id)
        assert metrics["message_count"] == 2  # skipped the bad line
        assert metrics["total_input_tokens"] == 300
        assert metrics["total_output_tokens"] == 150

    def test_null_usage_values_handled(self, mock_claude_dir):
        """Usage fields can be null (None in JSON) — should be treated as 0."""
        session_id = "null-usage"
        entries = [
            {
                "message": {
                    "usage": {
                        "input_tokens": None,
                        "output_tokens": 100,
                        "cache_read_input_tokens": None,
                        "cache_creation_input_tokens": None,
                    }
                }
            },
        ]
        _write_transcript(mock_claude_dir / f"{session_id}.jsonl", entries)
        metrics = parse_transcript_metrics(session_id)
        assert metrics["total_input_tokens"] == 0
        assert metrics["total_output_tokens"] == 100
        assert metrics["total_cache_read"] == 0

    def test_transcript_size_reported(self, mock_claude_dir):
        session_id = "size-session"
        entries = [{"message": {"usage": {"input_tokens": 100, "output_tokens": 50}}}]
        _write_transcript(mock_claude_dir / f"{session_id}.jsonl", entries)
        metrics = parse_transcript_metrics(session_id)
        assert metrics["transcript_size_kb"] > 0

    def test_messages_without_usage_counted(self, mock_claude_dir):
        """Non-assistant messages (e.g. system, user) count but have no usage."""
        session_id = "mixed-session"
        entries = [
            {"type": "user", "content": "hello"},
            {"message": {"usage": {"input_tokens": 100, "output_tokens": 50}}},
            {"type": "system", "subtype": "init", "data": {}},
        ]
        _write_transcript(mock_claude_dir / f"{session_id}.jsonl", entries)
        metrics = parse_transcript_metrics(session_id)
        assert metrics["message_count"] == 3
        assert metrics["total_input_tokens"] == 100
