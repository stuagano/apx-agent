"""Autonomous morning scan entrypoint.

Runs via Databricks Job at 7 AM daily (configured in databricks.yml).
Executes the full shortage intelligence pipeline and delivers the dual
reports to Slack channels for the sourcing and sales teams.

Entry point: morning-scan (registered in pyproject.toml [project.scripts])
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import httpx
from databricks.sdk import WorkspaceClient

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

SCAN_PROMPT = (
    "Run the daily shortage intelligence scan. "
    "Scan demand clusters for the last 48 hours with a minimum of 2 customers per component. "
    "For each signal found, look up historical patterns, validate against market news, "
    "check DigiKey for live pricing and availability, find alternative parts, "
    "and produce the full sourcing and sales team reports."
)


def run_agent_loop(ws: WorkspaceClient, query: str, max_iterations: int = 20) -> str:
    """Call the shortage intelligence agent in a tool-calling loop.

    Calls the Databricks serving endpoint directly — no HTTP hop to the app
    needed for the scheduled job context.
    """
    host = ws.config.host.rstrip("/")
    token = ws.config.token
    model_name = os.environ.get("AGENT_MODEL", "databricks-claude-sonnet-4-6")
    endpoint = f"{host}/serving-endpoints/{model_name}/invocations"

    # Import tools for dispatch in job context
    from tools import (
        check_vendor_availability,
        find_alternative_parts,
        find_historical_patterns,
        scan_demand_clusters,
        validate_against_market_news,
    )

    tool_dispatch: dict[str, Any] = {
        "scan_demand_clusters": scan_demand_clusters,
        "find_historical_patterns": find_historical_patterns,
        "validate_against_market_news": validate_against_market_news,
        "check_vendor_availability": check_vendor_availability,
        "find_alternative_parts": find_alternative_parts,
    }

    messages: list[dict[str, Any]] = [{"role": "user", "content": query}]

    with httpx.Client(timeout=300) as client:
        for iteration in range(max_iterations):
            resp = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messages": messages,
                    "max_tokens": 8192,
                    "tools": _build_tool_schemas(tool_dispatch),
                },
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
                logger.info("Tool call: %s(%s)", fn_name, fn_args)
                result = _dispatch(fn_name, fn_args, tool_dispatch, ws)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result),
                })

    raise RuntimeError(f"Agent loop exceeded {max_iterations} iterations")


def _dispatch(
    name: str,
    args: dict[str, Any],
    dispatch: dict[str, Any],
    ws: WorkspaceClient,
) -> Any:
    fn = dispatch.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        import inspect
        sig = inspect.signature(fn)
        if "ws" in sig.parameters:
            return fn(**args, ws=ws)
        return fn(**args)
    except Exception as e:
        logger.warning("Tool %s failed: %s", name, e)
        return {"error": str(e)}


def _build_tool_schemas(dispatch: dict[str, Any]) -> list[dict[str, Any]]:
    """Build minimal OpenAI-compatible tool schemas from function signatures."""
    import inspect
    schemas = []
    for name, fn in dispatch.items():
        sig = inspect.signature(fn)
        params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for param_name, param in sig.parameters.items():
            if param_name == "ws":
                continue
            ann = param.annotation
            type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
            if hasattr(ann, "__origin__") and ann.__origin__ is list:
                params["properties"][param_name] = {"type": "array", "items": {"type": "string"}}
            else:
                params["properties"][param_name] = {"type": type_map.get(ann, "string")}
            if param.default is inspect.Parameter.empty:
                params["required"].append(param_name)
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (fn.__doc__ or "").split("\n")[0],
                "parameters": params,
            },
        })
    return schemas


def deliver_to_slack(webhook_url: str, report_section: str, text: str) -> None:
    if not webhook_url:
        logger.info("No Slack webhook for %s — logging report instead:\n%s", report_section, text)
        return
    try:
        resp = httpx.post(webhook_url, json={"text": text}, timeout=10)
        resp.raise_for_status()
        logger.info("Delivered %s report to Slack", report_section)
    except Exception as e:
        logger.error("Slack delivery failed for %s: %s", report_section, e)


def split_reports(full_output: str) -> tuple[str, str]:
    """Split the agent output into sourcing and sales sections."""
    sourcing_marker = "## SOURCING TEAM REPORT"
    sales_marker = "## SALES TEAM REPORT"

    sourcing = ""
    sales = ""

    if sourcing_marker in full_output and sales_marker in full_output:
        sourcing_start = full_output.index(sourcing_marker)
        sales_start = full_output.index(sales_marker)
        sourcing = full_output[sourcing_start:sales_start].strip()
        sales = full_output[sales_start:].strip()
    else:
        sourcing = full_output
        sales = full_output

    return sourcing, sales


def main() -> None:
    ws = WorkspaceClient()
    sourcing_webhook = os.environ.get("SLACK_WEBHOOK_SOURCING", "")
    sales_webhook = os.environ.get("SLACK_WEBHOOK_SALES", "")

    logger.info("Starting morning shortage intelligence scan")

    try:
        result = run_agent_loop(ws, SCAN_PROMPT)
        logger.info("Scan complete — %d chars of output", len(result))

        sourcing_report, sales_report = split_reports(result)
        deliver_to_slack(sourcing_webhook, "sourcing", sourcing_report)
        deliver_to_slack(sales_webhook, "sales", sales_report)

        logger.info("Morning scan complete")

    except Exception as e:
        logger.error("Morning scan failed: %s", e, exc_info=True)
        error_msg = f"*Shortage Intelligence Morning Scan FAILED*\n\nError: {e}\n\nPlease investigate manually."
        deliver_to_slack(sourcing_webhook, "sourcing-error", error_msg)
        deliver_to_slack(sales_webhook, "sales-error", error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
