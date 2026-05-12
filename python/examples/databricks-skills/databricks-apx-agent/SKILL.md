---
name: databricks-apx-agent
description: "Build custom Python agents using the apx-agent SDK (ClaudeAgentOptions, ClaudeSDKClient). Use this skill — NOT MLflow ResponsesAgent or mlflow.pyfunc — when the user asks to build an agent, chatbot, or AI assistant that should run as a Python script or FastAPI service rather than a Databricks Model Serving endpoint."
---

# apx-agent SDK

Build custom AI agents in Python using the apx-agent SDK. The SDK runs a Claude subprocess with configurable tools, MCP servers, and session state.

**Use this skill when:** the user asks to "build an agent", "make me an agent", "create a chatbot", or "write an AI assistant."

**Do NOT use:** MLflow `ResponsesAgent`, `mlflow.pyfunc`, `mlflow.models.set_model`, or `agents.deploy()`. Those are for deploying to Databricks Model Serving. The apx-agent SDK runs agents locally or inside a FastAPI service.

## Installation

```bash
pip install claude-agent-sdk
```

The builder app's `.venv` already has it installed.

## Core Pattern — One-Shot Query

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

options = ClaudeAgentOptions(
    allowed_tools=["Read", "Write", "Bash"],
    permission_mode="bypassPermissions",
)

async def run(prompt: str):
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            print(msg)
```

## Core Pattern — Streaming with Session Resumption

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

session_id = None  # persist this across calls to resume the conversation

async def chat(prompt: str):
    global session_id

    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Write"],
        permission_mode="bypassPermissions",
        resume=session_id,              # resume previous conversation
        include_partial_messages=True,  # token-by-token streaming
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id  # save for next call
```

## Connecting to an MCP Server (SSE)

When the agent needs Databricks tools, connect it to the running `databricks-mcp-server` via SSE.
The builder app starts this server at `http://localhost:8080/sse`.

```python
import os
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import McpSSEServerConfig

# The databricks-mcp-server must already be running:
#   python run_server.py --transport sse --port 8080
mcp_config = McpSSEServerConfig(
    type="sse",
    url=os.environ.get("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse"),
)

options = ClaudeAgentOptions(
    allowed_tools=[
        "mcp__databricks__execute_sql",
        "mcp__databricks__ask_genie",
        "mcp__databricks__ask_genie_followup",
        "mcp__databricks__list_warehouses",
        # add other mcp__databricks__<tool_name> tools as needed
    ],
    permission_mode="bypassPermissions",
    mcp_servers={"databricks": mcp_config},
)
```

## Example: Multi-Genie Agent

An agent that routes questions to two Genie Spaces and synthesizes the answers.

```python
"""multi_genie_agent.py — queries two Genie Spaces, synthesizes answers."""

import asyncio
import os
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    McpSSEServerConfig, AssistantMessage, ResultMessage, TextBlock
)

SPACE_1_ID = "YOUR_FIRST_SPACE_ID"   # replace with actual space IDs
SPACE_2_ID = "YOUR_SECOND_SPACE_ID"

SYSTEM_PROMPT = f"""You have access to two Genie Spaces via the Databricks MCP tools:

- Space 1 (compliance data): space_id = {SPACE_1_ID}
- Space 2 (activity/cost data): space_id = {SPACE_2_ID}

When answering questions:
1. Decide which space(s) are relevant.
2. Call ask_genie for each relevant space.
3. For follow-up questions, use ask_genie_followup with the conversation_id from the previous answer.
4. Synthesize the results into a single clear answer.
"""

mcp_config = McpSSEServerConfig(
    type="sse",
    url=os.environ.get("DATABRICKS_MCP_SERVER_URL", "http://localhost:8080/sse"),
)

session_id = None

async def ask(prompt: str) -> str:
    global session_id

    options = ClaudeAgentOptions(
        allowed_tools=[
            "mcp__databricks__ask_genie",
            "mcp__databricks__ask_genie_followup",
        ],
        permission_mode="bypassPermissions",
        mcp_servers={"databricks": mcp_config},
        system_prompt=SYSTEM_PROMPT,
        resume=session_id,
    )

    response_parts = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id

    return "".join(response_parts)


async def main():
    while True:
        prompt = input("\nYou: ").strip()
        if not prompt:
            continue
        answer = await ask(prompt)
        print(f"\nAgent: {answer}")


if __name__ == "__main__":
    asyncio.run(main())
```

**Run it:**
```bash
DATABRICKS_MCP_SERVER_URL=http://localhost:8080/sse python multi_genie_agent.py
```

## Key Options Reference

| Option | Type | Purpose |
|--------|------|---------|
| `allowed_tools` | `list[str]` | Built-in tools (`Read`, `Write`, `Bash`) and MCP tools (`mcp__<server>__<tool>`) |
| `permission_mode` | `str` | `"bypassPermissions"` to auto-approve all tools |
| `mcp_servers` | `dict` | Map of server name → `McpSSEServerConfig` |
| `system_prompt` | `str` | Override the default system prompt |
| `resume` | `str \| None` | Session ID from a previous `ResultMessage` to continue that conversation |
| `include_partial_messages` | `bool` | `True` for token-by-token streaming |
| `cwd` | `str` | Working directory for file tools |
| `env` | `dict` | Extra environment variables for the Claude subprocess |

## Message Types

```python
from claude_agent_sdk.types import (
    AssistantMessage,   # Claude's response — contains TextBlock, ToolUseBlock, ThinkingBlock
    UserMessage,        # Tool results sent back to Claude
    ResultMessage,      # Final message — has session_id, duration_ms, total_cost_usd
    SystemMessage,      # System events
    StreamEvent,        # Token-level events when include_partial_messages=True
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
)
```

## Built-in Tools

| Tool | What it does |
|------|-------------|
| `Read` | Read a local file |
| `Write` | Write a local file |
| `Edit` | Edit a local file |
| `Bash` | Run shell commands |
| `Glob` | Find files by pattern |
| `Grep` | Search file contents |
| `Skill` | Load a Claude Code skill |

## What NOT to Do

```python
# ❌ WRONG — this is for Databricks Model Serving deployment
import mlflow
from databricks.agents import ResponsesAgent
mlflow.models.set_model(my_agent)
mlflow.pyfunc.log_model(...)

# ✅ RIGHT — use ClaudeSDKClient
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
async with ClaudeSDKClient(options=options) as client:
    await client.query(prompt)
```

Use MLflow model serving only if the explicit goal is deploying to a `/invocations` REST endpoint. For everything else — scripts, FastAPI services, notebooks, local tools — use `ClaudeSDKClient`.
