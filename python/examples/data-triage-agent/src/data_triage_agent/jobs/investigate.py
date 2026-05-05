"""Databricks Job entrypoint for async Jira ticket investigation.

Invoked by the data-triage-investigation job via python_wheel_task.
Ticket fields arrive as sys.argv via python_params from run_now().
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import httpx
from databricks.sdk import WorkspaceClient

from data_triage_agent.jira_client import JiraClient, JiraClientError

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


def _extract_investigation_query(
    summary: str,
    description: str,
    priority: str,
    reporter: str,
) -> str:
    parts = [summary]
    if description:
        parts.append(description)
    if priority:
        parts.append(f"Priority: {priority}")
    if reporter:
        parts.append(f"Reported by: {reporter}")
    return "\n\n".join(parts)


def _format_comment(issue_key: str, investigation_result: str) -> str:
    return f"*Automated investigation for {issue_key}*\n\n{investigation_result}"


def run_investigation(query: str, ws: WorkspaceClient) -> str:
    """Call the FM API in a tool-calling loop and return the final text response."""
    host = ws.config.host.rstrip("/")
    token = ws.config.token
    endpoint = f"{host}/serving-endpoints/databricks-claude-sonnet-4-6/invocations"

    messages: list[dict[str, Any]] = [{"role": "user", "content": query}]

    with httpx.Client(timeout=120) as client:
        for _ in range(20):
            resp = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"messages": messages, "max_tokens": 4096},
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "stop")
            assistant_msg = choice["message"]
            messages.append(assistant_msg)

            if finish_reason != "tool_calls":
                return assistant_msg.get("content") or ""

            tool_calls = assistant_msg.get("tool_calls", [])
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"])
                result = _dispatch_tool(fn_name, fn_args, ws)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result),
                })
    raise RuntimeError("FM API tool-call loop exceeded 20 iterations")


def _dispatch_tool(name: str, args: dict[str, Any], ws: WorkspaceClient) -> Any:
    """Dispatch tool calls available in job context."""
    from data_triage_agent.backend.agent_router import (
        _run_sql,
        get_job_run_history,
        get_job_run_logs,
        get_job_source_paths,
        get_table_lineage,
        find_jobs_for_table,
    )
    from data_triage_agent.backend.pipeline import get_table_info

    dispatch: dict[str, Any] = {
        "run_sql_query": lambda: _run_sql(ws, args["sql"]),
        "get_table_info": lambda: get_table_info(args["table_full_name"], ws),
        "get_table_lineage": lambda: get_table_lineage(args["table_full_name"], ws),
        "find_jobs_for_table": lambda: find_jobs_for_table(args["table_full_name"], ws),
        "get_job_run_history": lambda: get_job_run_history(args["job_id"], ws),
        "get_job_run_logs": lambda: get_job_run_logs(args["run_id"], ws),
        "get_job_source_paths": lambda: get_job_source_paths(args["job_id"], ws),
    }
    fn = dispatch.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn()
    except Exception as e:
        logger.warning("Tool %s failed: %s", name, e)
        return {"error": str(e)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Investigate a Jira data issue ticket")
    parser.add_argument("--issue-key", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--priority", default="")
    parser.add_argument("--reporter", default="")
    args = parser.parse_args()

    ws = WorkspaceClient()
    jira_base_url = os.environ.get("JIRA_BASE_URL", "")
    jira_email = os.environ.get("JIRA_SERVICE_ACCOUNT_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    jira = JiraClient(jira_base_url, jira_email, jira_token)

    query = _extract_investigation_query(
        summary=args.summary,
        description=args.description,
        priority=args.priority,
        reporter=args.reporter,
    )

    try:
        logger.info("Starting investigation for %s", args.issue_key)
        result = run_investigation(query, ws)
        comment_body = _format_comment(args.issue_key, result)
        jira.post_comment(args.issue_key, comment_body)
        logger.info("Posted investigation comment to %s", args.issue_key)
    except Exception as exc:
        logger.error("Investigation failed for %s: %s", args.issue_key, exc)
        try:
            jira.post_comment(
                args.issue_key,
                f"*Automated investigation failed for {args.issue_key}*\n\nError: {exc}\n\nPlease investigate manually.",
            )
        except JiraClientError:
            logger.error("Also failed to post error comment to %s", args.issue_key)
        sys.exit(1)


if __name__ == "__main__":
    main()
