import logging

import requests

logger = logging.getLogger(__name__)


class SentryService:
    """Sentry API client — fetch issues, get details, update status."""

    def __init__(self, token: str, org: str, project: str, base_url: str = "https://sentry.io/api/0"):
        self.token = token
        self.org = org
        self.project = project
        self.base_url = base_url

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def verify_token(self) -> dict:
        url = f"{self.base_url}/organizations/{self.org}/"
        logger.info(f"[SENTRY] verify_token → {url}")
        try:
            response = requests.get(url, headers=self._headers(), timeout=15)
            logger.info(f"[SENTRY] verify_token ← status={response.status_code}")
            if not response.ok:
                logger.error(f"[SENTRY] verify_token ← FAILED: {response.status_code} {response.reason}")
                return {"error": f"{response.status_code} {response.reason}", "detail": response.text}
            slug = response.json().get("slug")
            logger.info(f"[SENTRY] verify_token ← OK (org={slug})")
            return {"status": "ok", "organization": slug}
        except requests.exceptions.RequestException as e:
            logger.error(f"[SENTRY] verify_token ← EXCEPTION: {e}")
            return {"error": str(e)}

    def get_issues(self, query: str = "is:unresolved", cursor: str = None) -> dict:
        try:
            params = {"query": f"project:{self.project} {query}"}
            if cursor:
                params["cursor"] = cursor

            url = f"{self.base_url}/organizations/{self.org}/issues/"
            logger.info(f"[SENTRY] get_issues → {url}")
            logger.info(f"[SENTRY] params: {params}")

            response = requests.get(url, headers=self._headers(), params=params, timeout=15)
            logger.info(f"[SENTRY] get_issues ← status={response.status_code}")

            if not response.ok:
                return {"error": f"{response.status_code} {response.reason}", "detail": response.text}

            issues = response.json()
            next_cursor = None
            link_header = response.headers.get("Link", "")
            if 'rel="next"; results="true"' in link_header:
                for part in link_header.split(","):
                    if 'rel="next"' in part and 'results="true"' in part:
                        next_cursor = part.split("cursor=")[1].split("&")[0].strip('">')
                        break

            return {
                "issues": [
                    {
                        "id": issue["id"],
                        "title": issue["title"],
                        "culprit": issue.get("culprit"),
                        "level": issue.get("level"),
                        "status": issue.get("status"),
                        "count": issue.get("count"),
                        "first_seen": issue.get("firstSeen"),
                        "last_seen": issue.get("lastSeen"),
                        "permalink": issue.get("permalink"),
                    }
                    for issue in issues
                ],
                "next_cursor": next_cursor,
            }
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}

    def get_issue_details(self, issue_id: str) -> dict:
        try:
            url = f"{self.base_url}/issues/{issue_id}/"
            logger.info(f"[SENTRY] get_issue_details → {url}")
            response = requests.get(url, headers=self._headers(), timeout=15)
            logger.info(f"[SENTRY] get_issue_details ← status={response.status_code}")

            if not response.ok:
                return {"error": f"{response.status_code} {response.reason}", "detail": response.text}
            issue_data = response.json()

            # Get latest event for stacktrace
            event_url = f"{self.base_url}/issues/{issue_id}/events/latest/"
            logger.info(f"[SENTRY] get_latest_event → {event_url}")
            event_response = requests.get(event_url, headers=self._headers(), timeout=15)
            logger.info(f"[SENTRY] get_latest_event ← status={event_response.status_code}")

            stacktrace = None
            filename = None
            if event_response.ok:
                event = event_response.json()
                entries = event.get("entries", [])
                for entry in entries:
                    if entry.get("type") == "exception":
                        exceptions = entry.get("data", {}).get("values", [])
                        frames_text = []
                        for exc in exceptions:
                            exc_type = exc.get("type", "")
                            exc_value = str(exc.get("value", "") or "")
                            frames_text.append(f"{exc_type}: {exc_value}")
                            frames = (exc.get("stacktrace") or {}).get("frames", [])
                            for frame in frames:
                                abs_path = frame.get("absPath") or frame.get("filename", "")
                                lineno = frame.get("lineNo", "?")
                                func = frame.get("function", "?")
                                context_line = frame.get("context_line", "").strip() if frame.get("context_line") else ""
                                frames_text.append(f'  File "{abs_path}", line {lineno}, in {func}')
                                if context_line:
                                    frames_text.append(f"    {context_line}")
                                if frame.get("inApp"):
                                    filename = abs_path
                        stacktrace = "\n".join(frames_text)
            else:
                logger.warning(f"[SENTRY] Could not fetch latest event: {event_response.status_code}")

            return {
                "id": str(issue_data["id"]),
                "title": issue_data.get("title", ""),
                "culprit": issue_data.get("culprit"),
                "level": issue_data.get("level"),
                "status": issue_data.get("status"),
                "count": issue_data.get("count"),
                "first_seen": issue_data.get("firstSeen"),
                "last_seen": issue_data.get("lastSeen"),
                "permalink": issue_data.get("permalink"),
                "stacktrace": stacktrace,
                "filename": filename,
            }
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}

    def update_issue_status(self, issue_id: str, status: str = "resolved") -> dict:
        try:
            url = f"{self.base_url}/issues/{issue_id}/"
            logger.info(f"[SENTRY] update_issue_status → {url} (status={status})")
            response = requests.put(
                url, headers={**self._headers(), "Content-Type": "application/json"},
                json={"status": status}, timeout=15,
            )
            logger.info(f"[SENTRY] update_issue_status ← status={response.status_code}")
            if not response.ok:
                logger.error(f"[SENTRY] update_issue_status ← error: {response.text[:300]}")
                return {"error": f"{response.status_code} {response.reason}"}
            return {"status": "ok"}
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}
