"""apx-agent — declarative agent DSL that compiles to LangGraph + MLflow."""

# Agent types
from ._agents import (
    BaseAgent,
    HandoffAgent,
    LlmAgent,
    LoopAgent,
    ParallelAgent,
    RouterAgent,
    SequentialAgent,
)
from ._remote import RemoteDatabricksAgent

# Canonical short name — Agent is the DSL-facing alias for LlmAgent
Agent = LlmAgent

# Agent-as-tool composition primitive
from ._agent_tool import agent_tool

# Models
from ._models import (
    AfterToolHook,
    AgentCard,
    AgentConfig,
    AgentContext,
    AgentTool,
    BeforeToolHook,
    InputGuardrailFn,
    Message,
    OutputGuardrailFn,
)

# FastAPI dependency injection
from ._defaults import Dependencies

# SQL utilities
from ._sql import decode_statement, get_warehouse_id, run_sql

# Provider compatibility layer — get_llm() factory + named subclasses
from ._llm import ChatDatabricksGptReasoning, get_llm

# App factory and setup
from ._wiring import create_app, setup_agent

# Eval bridge
from ._eval import app_predict_fn

# Genie tool factory
from .genie import genie_tool

# Unity Catalog tool factories
from .catalog import catalog_tool, lineage_tool, schema_tool, uc_function_tool

# LangGraph compiler (optional — requires the ``langgraph`` extra)
from ._compile import CompileContext, compile_to_langgraph

# MLflow ChatAgent wrapper (optional — requires the ``langgraph`` and ``eval`` extras)
from ._chat_agent import chat_agent_for, compile_to_chat_agent, log_agent

# Resource declaration — auto-derive MLflow resources from the agent tree
from ._resources import (
    ResourceSpec,
    attach_resources,
    collect_resource_specs,
    mlflow_resources_for,
)

# MLflow ChatAgent /invocations route mounter (optional — same extras)
from ._invocations import mount_invocations_route

# MLflow tracing helpers (optional — graceful no-op without mlflow)
from ._mlflow_tracing import (
    enable_langchain_autolog,
    is_mlflow_available,
    safe_span,
)

__all__ = [
    # Agent types
    "Agent",
    "BaseAgent",
    "HandoffAgent",
    "LlmAgent",
    "LoopAgent",
    "ParallelAgent",
    "RouterAgent",
    "SequentialAgent",
    "RemoteDatabricksAgent",
    "agent_tool",
    # Models
    "AgentCard",
    "AgentConfig",
    "AgentContext",
    "AgentTool",
    "AfterToolHook",
    "BeforeToolHook",
    "InputGuardrailFn",
    "Message",
    "OutputGuardrailFn",
    # Dependencies
    "Dependencies",
    # SQL utilities
    "decode_statement",
    "get_warehouse_id",
    "run_sql",
    # Provider compat
    "ChatDatabricksGptReasoning",
    "get_llm",
    # App factory
    "create_app",
    "setup_agent",
    # Eval
    "app_predict_fn",
    # Tool factories
    "genie_tool",
    "catalog_tool",
    "lineage_tool",
    "schema_tool",
    "uc_function_tool",
    # LangGraph compiler
    "CompileContext",
    "compile_to_langgraph",
    # MLflow ChatAgent wrapper
    "chat_agent_for",
    "compile_to_chat_agent",
    "log_agent",
    # Resource declaration
    "ResourceSpec",
    "attach_resources",
    "collect_resource_specs",
    "mlflow_resources_for",
    # MLflow /invocations route mounter
    "mount_invocations_route",
    # MLflow tracing
    "enable_langchain_autolog",
    "is_mlflow_available",
    "safe_span",
]
