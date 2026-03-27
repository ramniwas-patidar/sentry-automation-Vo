import logging

import requests

logger = logging.getLogger(__name__)


class JiraService:
    """Jira REST API client for creating tickets."""

    def __init__(self, domain: str = "", email: str = "", api_token: str = "",
                 project_key: str = "", issue_type: str = "Bug"):
        self.domain = domain
        self.email = email
        self.api_token = api_token
        self.project_key = project_key
        self.issue_type = issue_type

    def is_configured(self) -> bool:
        return bool(
            self.domain
            and self.api_token
            and self.api_token != "your_jira_api_token"
            and self.project_key
        )

    def create_ticket(
        self,
        issue_id: str,
        title: str,
        description: str,
        sentry_link: str = "",
        pr_url: str = "",
    ) -> str:
        if not self.is_configured():
            logger.warning("[JIRA] Skipped — credentials not configured")
            return ""

        url = f"https://{self.domain}/rest/api/3/issue"

        description_parts = [
            f"*Sentry Issue:* #{issue_id}",
            f"*Error:* {title}",
            "",
            description,
            "",
        ]
        if sentry_link:
            description_parts.append(f"*Sentry Link:* {sentry_link}")
        if pr_url:
            description_parts.append(f"*Pull Request:* {pr_url}")

        description_text = "\n".join(description_parts)

        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": f"[Sentry #{issue_id}] {title[:200]}",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": description_text}
                            ],
                        }
                    ],
                },
                "issuetype": {"name": self.issue_type},
            }
        }

        logger.info(f"[JIRA] Creating ticket for #{issue_id}: {title[:80]}")

        try:
            response = requests.post(
                url, json=payload,
                auth=(self.email, self.api_token),
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if not response.ok:
                logger.error(f"[JIRA] Failed: {response.text[:500]}")
                return ""

            data = response.json()
            ticket_key = data.get("key", "")
            ticket_url = f"https://{self.domain}/browse/{ticket_key}"
            logger.info(f"[JIRA] ✓ {ticket_key} → {ticket_url}")
            return ticket_url

        except requests.exceptions.RequestException as e:
            logger.error(f"[JIRA] Exception: {e}")
            return ""
