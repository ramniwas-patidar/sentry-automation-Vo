import logging

from models.schemas import SentryIssue
from services.sentry_service import SentryService

logger = logging.getLogger(__name__)


def fetch_all_issues(
    sentry: SentryService,
    query: str = "is:unresolved",
    issue_id: str = None,
) -> list[SentryIssue]:
    """Fetch all issues and enrich each with stacktrace details.

    If issue_id is provided, fetches only that single issue.
    Otherwise fetches all issues matching the query.
    Deduplicates issues with identical titles to avoid redundant processing.
    """
    if issue_id:
        logger.info(f"[FETCHER] Fetching specific issue: {issue_id}")
        details = sentry.get_issue_details(issue_id)
        if "error" in details:
            raise ValueError(f"Failed to fetch issue {issue_id}: {details['error']}")
        logger.info(f"[FETCHER] Issue fetched: {details['title'][:80]}")
        return [SentryIssue(**details)]

    logger.info(f"[FETCHER] Fetching all issues with query: {query}")
    issues = []
    cursor = None
    page = 1

    while True:
        result = sentry.get_issues(query=query, cursor=cursor)
        if "error" in result:
            raise ValueError(f"Failed to fetch issues: {result['error']}")

        page_issues = result.get("issues", [])
        issues.extend(page_issues)
        logger.info(f"[FETCHER] Page {page}: {len(page_issues)} issues (total so far: {len(issues)})")

        cursor = result.get("next_cursor")
        if not cursor or not page_issues:
            break
        page += 1

    logger.info(f"[FETCHER] Found {len(issues)} total issues across {page} page(s)")
    if not issues:
        raise ValueError(f"No issues found for query: {query}")

    # Deduplicate by title — keep the one with highest count
    unique = _deduplicate_issues(issues)
    if len(unique) < len(issues):
        logger.info(f"[FETCHER] Deduplicated: {len(issues)} → {len(unique)} unique issues")

    # Enrich each issue with stacktrace details
    enriched = []
    for i, issue in enumerate(unique):
        iid = issue["id"]
        logger.info(f"[FETCHER] Enriching issue {i+1}/{len(unique)}: {iid} - {issue['title'][:60]}")
        details = sentry.get_issue_details(iid)
        if "error" in details:
            logger.warning(f"[FETCHER] Failed to enrich {iid}: {details['error']}, skipping")
            continue
        enriched.append(SentryIssue(**details))
        logger.info(
            f"[FETCHER]   stacktrace={'yes' if details.get('stacktrace') else 'no'}, "
            f"filename={details.get('filename')}"
        )

    logger.info(f"[FETCHER] Enriched {len(enriched)}/{len(unique)} issues")
    return enriched


def _deduplicate_issues(issues: list[dict]) -> list[dict]:
    """Remove duplicate issues with the same title, keeping the one with highest count."""
    seen = {}
    for issue in issues:
        title = issue.get("title", "")
        count = int(issue.get("count", 0) or 0)
        if title not in seen or count > int(seen[title].get("count", 0) or 0):
            seen[title] = issue
    return list(seen.values())
