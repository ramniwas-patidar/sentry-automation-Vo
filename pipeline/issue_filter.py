import json
import logging

from models.schemas import FilteredIssue, SentryIssue
from services.llm_service import get_llm
from services.sentry_service import SentryService

logger = logging.getLogger(__name__)

FILTER_SYSTEM_PROMPT = """You are a senior engineer triaging Sentry issues. For each issue, determine if it is RELEVANT (a real application bug that can be fixed via a code patch) or NOT RELEVANT.

Issues that are NOT RELEVANT include:
- Third-party library errors (not caused by our code)
- Hydration errors (React SSR/CSR mismatch) — unless they clearly point to a specific code bug
- Infrastructure/deployment errors (network timeouts, DNS failures, etc.)
- Browser extension interference
- Bot/crawler-generated errors
- Duplicate or stale issues with no actionable stacktrace

For each issue, return a JSON object with an "issues" key containing an array:
{
  "issues": [
    {
      "issue_id": "12345",
      "is_relevant": true,
      "reason": "Clear null reference in application code at UserService.getProfile",
      "category": "application_bug"
    },
    {
      "issue_id": "67890",
      "is_relevant": false,
      "reason": "React hydration mismatch caused by browser extension modifying DOM",
      "category": "hydration"
    }
  ]
}

Categories: "application_bug", "third_party", "hydration", "infrastructure", "browser_extension", "bot_traffic", "stale", "other"

Return ONLY valid JSON, no markdown fences."""


def filter_issues(
    issues: list[SentryIssue],
    sentry: SentryService,
) -> tuple[list[SentryIssue], list[SentryIssue], list[FilteredIssue]]:
    """Filter issues using LLM. Returns (relevant, filtered_out, filter_details).

    Also updates filtered issues in Sentry (marks as resolved).
    """
    if not issues:
        return [], [], []

    logger.info(f"[FILTER] ── LLM Issue Filtering ──")
    logger.info(f"[FILTER] Filtering {len(issues)} issues...")

    llm = get_llm()

    # Build summaries for LLM
    summaries = []
    for issue in issues:
        s = {
            "issue_id": issue.id,
            "title": issue.title,
            "culprit": issue.culprit,
            "level": issue.level,
            "count": issue.count,
        }
        if issue.stacktrace:
            s["stacktrace_preview"] = issue.stacktrace[:500]
        summaries.append(s)

    user_message = json.dumps(summaries, indent=2)

    # Call LLM
    logger.info(f"[FILTER] Calling LLM for issue classification...")
    try:
        data = llm.chat_json(
            system_prompt=FILTER_SYSTEM_PROMPT,
            user_message=user_message,
            temperature=0,
        )
        logger.info(f"[FILTER] LLM call ← OK")
    except Exception as e:
        logger.error(f"[FILTER] LLM call ← FAILED: {e}")
        raise

    # Parse response
    items = _extract_items(data)
    filter_results = _build_filter_results(items, issues)

    # Split into relevant and filtered
    relevant_ids = {r.issue_id for r in filter_results if r.is_relevant}
    filtered_ids = {r.issue_id for r in filter_results if not r.is_relevant}

    relevant = [i for i in issues if i.id in relevant_ids]
    filtered_out = [i for i in issues if i.id in filtered_ids]

    logger.info(f"[FILTER] Result: {len(relevant)} relevant, {len(filtered_out)} filtered out")

    # Update filtered issues in Sentry
    if filtered_out:
        logger.info(f"[FILTER] Resolving {len(filtered_out)} filtered issues in Sentry...")
        for issue in filtered_out:
            info = next((r for r in filter_results if r.issue_id == issue.id), None)
            reason = info.reason if info else "Not relevant"
            logger.info(f"[FILTER]   Resolving #{issue.id}: {reason[:60]}")
            result = sentry.update_issue_status(issue.id, status="resolved")
            status_text = "resolved" if result.get("status") == "ok" else f"failed: {result.get('error', '')}"
            logger.info(f"[FILTER]   → {status_text}")

    return relevant, filtered_out, filter_results


def _extract_items(data) -> list[dict]:
    """Extract the list of issue classifications from LLM response."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "issues" in data:
            return data["issues"]
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _build_filter_results(items: list[dict], issues: list[SentryIssue]) -> list[FilteredIssue]:
    """Build FilteredIssue objects, handling missing issues."""
    issue_map = {issue.id: issue for issue in issues}
    results = []

    for item in items:
        issue_id = str(item.get("issue_id", ""))
        issue = issue_map.get(issue_id)
        title = issue.title if issue else item.get("title", "Unknown")

        result = FilteredIssue(
            issue_id=issue_id,
            title=title,
            is_relevant=item.get("is_relevant", True),
            reason=item.get("reason", "No reason provided"),
            category=item.get("category", "unknown"),
        )
        results.append(result)

        icon = "✓ RELEVANT" if result.is_relevant else "✗ FILTERED"
        logger.info(f"[FILTER] {icon}: #{issue_id} ({result.category}) — {result.reason[:80]}")

    # Default any missing issues to relevant
    responded_ids = {r.issue_id for r in results}
    for issue in issues:
        if issue.id not in responded_ids:
            logger.warning(f"[FILTER] #{issue.id} not in LLM response, defaulting to relevant")
            results.append(FilteredIssue(
                issue_id=issue.id, title=issue.title,
                is_relevant=True, reason="Not classified by LLM",
                category="unknown",
            ))

    return results
