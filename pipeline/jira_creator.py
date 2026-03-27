import logging

from models.schemas import IssueFixResult, SentryIssue
from services.jira_service import JiraService

logger = logging.getLogger(__name__)


def create_jira_tickets(
    jira: JiraService,
    issue_results: list[IssueFixResult],
    all_issues: list[SentryIssue],
    pr_url: str = "",
) -> list[str]:
    """Create a Jira ticket for each fixed issue. Returns list of ticket URLs."""
    if not jira.is_configured():
        logger.warning("[JIRA_CREATOR] Jira not configured, skipping ticket creation")
        return []

    fixed = [r for r in issue_results if r.status == "fixed"]
    if not fixed:
        return []

    logger.info(f"[JIRA_CREATOR] Creating {len(fixed)} Jira ticket(s)...")
    tickets = []

    for fix in fixed:
        issue = next((i for i in all_issues if i.id == fix.issue_id), None)
        sentry_link = issue.permalink if issue else ""

        description_lines = [
            f"Error: {fix.title}",
            f"Files changed: {', '.join(fix.files_changed) or 'N/A'}",
            f"Fix confidence: {fix.confidence}",
        ]
        if issue and issue.stacktrace:
            description_lines.append(f"\nStacktrace:\n{issue.stacktrace[:500]}")

        ticket_url = jira.create_ticket(
            issue_id=fix.issue_id,
            title=fix.title,
            description="\n".join(description_lines),
            sentry_link=sentry_link,
            pr_url=pr_url,
        )

        if ticket_url:
            fix.jira_ticket = ticket_url
            tickets.append(ticket_url)

    logger.info(f"[JIRA_CREATOR] Created {len(tickets)}/{len(fixed)} tickets")
    return tickets
