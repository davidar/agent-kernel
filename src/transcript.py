"""SDK transcript parsing — extracts metrics from Claude's internal JSONL files.

Reads the SDK's internal transcript format to get real context usage,
compaction history, and cumulative token counts. Used by agent.py
(context warnings during tick) and cli.py (status command).

Couples to SDK internal format (camelCase fields, JSONL structure).
If the SDK changes its transcript format, this module needs updating.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def get_transcript_path(session_id: str) -> Path | None:
    """Find the transcript file for a session.

    The SDK stores transcripts in ~/.claude/projects/{project-path-mangled}/{session_id}.jsonl
    """
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if project_dir.is_dir():
            transcript = project_dir / f"{session_id}.jsonl"
            if transcript.exists():
                return transcript
    return None


def parse_transcript_metrics(session_id: str) -> dict:
    """Extract metrics from the SDK's internal transcript file.

    Returns a dict with:
        transcript_size_kb, context_tokens, total_input_tokens, total_output_tokens,
        total_cache_read, total_cache_create, message_count, compaction_count,
        last_usage, last_compaction (if any).
    """
    transcript = get_transcript_path(session_id)
    if not transcript:
        return {}

    metrics: dict = {
        "transcript_size_kb": transcript.stat().st_size / 1024,
        "context_tokens": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_create": 0,
        "message_count": 0,
        "compaction_count": 0,
        "last_usage": {},
    }

    try:
        with open(transcript) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    metrics["message_count"] += 1

                    # Check for compaction events
                    if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                        metrics["compaction_count"] += 1
                        # SDK uses camelCase: compactMetadata, preTokens
                        compact_meta = entry.get("compactMetadata", {})
                        metrics["last_compaction"] = {
                            "trigger": compact_meta.get("trigger"),
                            "pre_tokens": compact_meta.get("preTokens"),
                        }

                    # Extract usage from assistant messages
                    msg = entry.get("message", {})
                    usage = msg.get("usage", {})
                    if usage:
                        metrics["last_usage"] = usage
                        metrics["total_input_tokens"] += usage.get("input_tokens", 0) or 0
                        metrics["total_output_tokens"] += usage.get("output_tokens", 0) or 0
                        metrics["total_cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
                        metrics["total_cache_create"] += usage.get("cache_creation_input_tokens", 0) or 0

                        # Current context = most recent cache_read (resets after compaction)
                        cache_read = usage.get("cache_read_input_tokens", 0) or 0
                        if cache_read > 0:
                            metrics["context_tokens"] = cache_read

                except json.JSONDecodeError as e:
                    logger.debug("Skipping malformed transcript line: %s", e)
                    continue
    except OSError as e:
        logger.debug("Failed to read transcript: %s", e)

    return metrics
