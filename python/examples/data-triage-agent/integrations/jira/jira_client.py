from __future__ import annotations

import httpx


class JiraClientError(Exception):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (email, api_token)

    def post_comment(self, issue_key: str, body: str) -> None:
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/comment"
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": body}],
                    }
                ],
            }
        }
        with httpx.Client() as client:
            resp = client.post(url, json=payload, auth=self._auth)
        if not resp.is_success:
            raise JiraClientError(f"Jira API {resp.status_code}: {resp.text[:200]}")
