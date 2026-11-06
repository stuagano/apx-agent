"""Register the Explain My Bill agent as a Unity Catalog function.

Running this script creates a Python UDF in Unity Catalog that wraps the
deployed APX app's /invocations endpoint. Any workspace that can see the
catalog can then call the agent with EXECUTE permission — no MCP setup needed.

This is the "agent catalog" pattern:
  APX app   = runtime  (does the work)
  UC function = catalog (discovery + permission boundary)

Usage
─────
    python catalog/register_agent.py

Or run the SQL directly in a notebook / SQL editor.

Prerequisites
─────────────
1. Deploy the app:
       databricks bundle deploy && databricks bundle run mcp-explain-my-bill

2. Create a service principal in the target workspace with CAN_USE on the app,
   then store its PAT in Databricks secrets:
       databricks secrets create-scope agent-catalog
       databricks secrets put-secret agent-catalog explain-my-bill-token --string-value <PAT>

3. Set environment variables (or edit the defaults below):
       export DEMO_CATALOG=my_catalog
       export DEMO_SCHEMA=agents
       export APP_URL=https://mcp-explain-my-bill-<id>.aws.databricksapps.com
       export SECRET_SCOPE=agent-catalog
       export SECRET_KEY=explain-my-bill-token
"""

import os

from databricks.sdk import WorkspaceClient

# Defaults are the placeholder values from step 3 of the module docstring
# (and the secret scope/key created in step 2); export the env vars to override.
DEFAULT_CATALOG = "my_catalog"
DEFAULT_SCHEMA = "agents"
DEFAULT_APP_URL = "https://mcp-explain-my-bill-<id>.aws.databricksapps.com"
DEFAULT_SECRET_SCOPE = "agent-catalog"
DEFAULT_SECRET_KEY = "explain-my-bill-token"
# No warehouse is assumed: an unset WAREHOUSE_ID reaches the Statement
# Execution API as an empty id and fails there with Databricks' own error.
UNSET_WAREHOUSE_ID = ""

CATALOG = os.environ.get("DEMO_CATALOG", DEFAULT_CATALOG)
SCHEMA = os.environ.get("DEMO_SCHEMA", DEFAULT_SCHEMA)
APP_URL = os.environ.get("APP_URL", DEFAULT_APP_URL)
SECRET_SCOPE = os.environ.get("SECRET_SCOPE", DEFAULT_SECRET_SCOPE)
SECRET_KEY = os.environ.get("SECRET_KEY", DEFAULT_SECRET_KEY)

# ---------------------------------------------------------------------------
# UC function SQL
# ---------------------------------------------------------------------------

CREATE_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.ask_explain_my_bill(
  question STRING
    COMMENT 'Natural language question about energy billing (e.g. "Why was CUST-0001''s March bill $129?")'
)
RETURNS STRING
LANGUAGE PYTHON
COMMENT 'Ask the Explain My Bill AI agent a billing question.
The agent looks up customer profiles, AMI smart meter data, billing history,
and rate schedules from Unity Catalog, then synthesizes a clear explanation.
App: {APP_URL}
Source: examples/explain-my-bill-agent in databricks-solutions/apx'
AS $$
import urllib.request
import json

def ask_explain_my_bill(question):
    token = dbutils.secrets.get(scope="{SECRET_SCOPE}", key="{SECRET_KEY}")
    url = "{APP_URL}/invocations"

    payload = json.dumps({{
        "messages": [{{"role": "user", "content": question}}]
    }}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={{
            "Content-Type": "application/json",
            "Authorization": f"Bearer {{token}}",
        }},
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())

    choices = result.get("choices", [])
    if choices:
        return choices[0].get("message", {{}}).get("content", "")
    return str(result)
$$
"""

GRANT_SQL = f"""
-- Share with analysts in your organization.
-- Replace with a specific group, user, or service principal as needed.
GRANT EXECUTE ON FUNCTION {CATALOG}.{SCHEMA}.ask_explain_my_bill
  TO `account users`;
"""

EXAMPLE_QUERY = f"""
-- Call from any workspace with EXECUTE permission
SELECT {CATALOG}.{SCHEMA}.ask_explain_my_bill(
  'Why was CUST-0001''s March bill higher than February?'
);
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ws = WorkspaceClient()

    print(f"Registering UC function: {CATALOG}.{SCHEMA}.ask_explain_my_bill")
    ws.statement_execution.execute_statement(
        statement=CREATE_FUNCTION_SQL,
        warehouse_id=os.environ.get("WAREHOUSE_ID", UNSET_WAREHOUSE_ID),
    )
    print("  ✓ Function created")

    print("Granting EXECUTE to account users")
    ws.statement_execution.execute_statement(
        statement=GRANT_SQL,
        warehouse_id=os.environ.get("WAREHOUSE_ID", UNSET_WAREHOUSE_ID),
    )
    print("  ✓ Permission granted")

    print(f"\nAgent registered. Example query:\n{EXAMPLE_QUERY}")


if __name__ == "__main__":
    main()
