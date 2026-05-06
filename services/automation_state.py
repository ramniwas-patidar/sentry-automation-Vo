"""Comment-based dedup state machine for Sentry issues.

Sentry comments on each issue are the durable record of what the pipeline
has done. Every bot comment starts with the prefix [sentry-automation] and
carries key=value fields. The newest such comment wins.

States:
    in_progress — pipeline started, no outcome yet
    fixed       — PR opened
    failed      — pipeline tried and gave up

A small in-memory cache short-circuits "already fixed" so the firehose case
(repeating error.created webhooks for the same bug) does not hit Sentry on
every webhook.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from services.sentry_service import SentryService

logger = logging.getLogger(__name__)

PREFIX = "[sentry-automation]"
IN_PROGRESS_TIMEOUT_S = 10 * 60   # 10 min — assume crash if exceeded
FAILED_RETRY_AFTER_S = 60 * 60    # 1 hour — cooldown before retrying a failure

# Process-local LRU cache: { "slug:issue_id": (state_name, fetched_at_unix) }.
# Bounded so memory stays flat over the server's lifetime — at the cap, the
# oldest entry is evicted (re-fetched from Sentry on next webhook for that
# issue, which is fine since cache hits are an optimization, not correctness).
# 10_000 entries × ~200 bytes ≈ 2 MB worst case.
MAX_CACHE_SIZE = 10_000
_cache: dict = {}


class State:
    def __init__(self, name: str, fields: dict, raw_ts: Optional[str] = None):
        self.name = name
        self.fields = fields
        self.raw_ts = raw_ts

    def age_seconds(self) -> float:
        if not self.raw_ts:
            return float("inf")
        try:
            ts = datetime.fromisoformat(self.raw_ts.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            return float("inf")

    @property
    def pr(self) -> Optional[str]:
        return self.fields.get("pr")


def _cache_key(project_slug: str, issue_id: str) -> str:
    return f"{project_slug}:{issue_id}"


def _parse_comment(text: str) -> Optional[State]:
    if not text or not text.startswith(PREFIX):
        return None
    body = text[len(PREFIX):].strip()
    fields = {}
    for token in body.split():
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k] = v
    name = fields.get("state")
    if not name:
        return None
    return State(name=name, fields=fields, raw_ts=fields.get("ts"))


def get_latest_state(sentry: SentryService, issue_id: str) -> Optional[State]:
    """Fetch comments and return the newest [sentry-automation] state."""
    comments = sentry.get_comments(issue_id)
    candidates = []
    for c in comments:
        text = c.get("data", {}).get("text") or c.get("text") or ""
        state = _parse_comment(text)
        if state:
            candidates.append((c.get("dateCreated", ""), state))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def write_state(sentry: SentryService, issue_id: str, name: str, **fields) -> None:
    """Post a [sentry-automation] comment describing the new state."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"state={name}", f"ts={ts}"]
    for k, v in fields.items():
        if v is None:
            continue
        v_str = str(v).replace(" ", "_")  # no spaces inside values
        parts.append(f"{k}={v_str}")
    text = f"{PREFIX} " + " ".join(parts)
    result = sentry.add_comment(issue_id, text)
    if isinstance(result, dict) and result.get("error"):
        logger.warning(f"[STATE] write_state failed for #{issue_id}: {result.get('error')}")


def decide_action(state: Optional[State]) -> str:
    """Return 'process' or 'skip' based on the current state."""
    if state is None:
        return "process"
    if state.name == "regression":
        return "process"   # supersedes any prior 'fixed', forces re-run
    if state.name == "fixed":
        return "skip"
    if state.name == "in_progress":
        return "skip" if state.age_seconds() < IN_PROGRESS_TIMEOUT_S else "process"
    if state.name == "failed":
        return "skip" if state.age_seconds() < FAILED_RETRY_AFTER_S else "process"
    return "process"


def cached_terminal(project_slug: str, issue_id: str) -> Optional[str]:
    """If a terminal-skip state ('fixed') is cached, return it. Else None."""
    entry = _cache.get(_cache_key(project_slug, issue_id))
    if not entry:
        return None
    name, _ = entry
    return name if name == "fixed" else None


def cache_set(project_slug: str, issue_id: str, name: str) -> None:
    """Insert/refresh a cache entry. Bounded to MAX_CACHE_SIZE via LRU eviction."""
    key = _cache_key(project_slug, issue_id)
    if key in _cache:
        del _cache[key]  # re-insert at end so it counts as most-recent
    _cache[key] = (name, time.time())
    # Evict oldest entries (Python 3.7+ dicts preserve insertion order).
    while len(_cache) > MAX_CACHE_SIZE:
        _cache.pop(next(iter(_cache)))


def cache_clear(project_slug: str, issue_id: str) -> None:
    _cache.pop(_cache_key(project_slug, issue_id), None)
