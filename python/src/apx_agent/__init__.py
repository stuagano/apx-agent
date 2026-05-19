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
    AfterModelHook,
    AfterToolHook,
    AgentCard,
    AgentConfig,
    AgentContext,
    AgentTool,
    BeforeModelHook,
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
from ._eval import app_predict_fn, evaluate

# Genie tool factories
from .genie import genie_query_tool, genie_tool

# Unity Catalog tool factories
from .catalog import (
    catalog_tool,
    lineage_tool,
    schema_tool,
    uc_function_tool,
    uc_function_toolkit,
)

# Platform tool factories
from .vector_search import vector_search_tool
from .sql_tools import sql_tool
from .foundation_model import foundation_model_tool

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

# @tool decorator and UC publishing
from ._tool import ToolMetadata, get_tool_metadata, tool
from ._tool_publish import PublishResult, publish_tools_to_uc

# Session / multi-turn memory
from ._session import (
    InMemorySessionStore,
    Session,
    SessionStore,
    append_turn,
    load_or_create_session,
)
from ._session_delta import DeltaSessionStore
from ._session_lakebase import LakebaseSessionStore

# Databricks Managed MCP integration
from ._managed_mcp import (
    ManagedMCPEndpoint,
    managed_mcp_client_config,
    managed_mcp_urls,
)

# Mosaic AI Supervisor Agent publishing
from ._publish import create_supervisor_agent, publish_to_supervisor

# Local lightweight guards — zero-latency runtime checks
from ._guards import (
    FeatureFlagGuard,
    RateLimit,
    ToolAllowlist,
    ToolDenylist,
    compose,
    prompt_injection_heuristic,
)

# Cost tracking helpers
from ._cost import CostBreakdown, cost_for_agent, cost_for_endpoint

# Scheduled-job / batch invocation — non-interactive entry point
from ._run_once import run_once

# Static lint
from ._lint import LintFinding, Severity, lint_agent

# Hot-swap the LLM endpoint on a deployed agent without re-logging
from ._hot_swap import (
    APX_MODEL_OVERRIDE_ENV,
    HotSwapResult,
    get_active_override,
    hot_swap_model,
)

# Trace exporter
from ._trace_export import ExportResult, export_traces

# Topology visualization
from ._topology import (
    AgentNode,
    Topology,
    TopologyEdge,
    discover_topology,
    render_topology,
)

# Cross-agent evaluation
from ._eval_chain import ChainCaseResult, ChainEvalReport, evaluate_chain

# Canary / A-B deployment helpers
from ._canary import (
    CanaryConfig,
    CanaryReport,
    VersionMetrics,
    analyze_canary,
    deploy_canary,
    get_canary_config,
    promote_canary,
    rollback_canary,
)

# databricks-watchdog integration
from ._watchdog import (
    WatchdogClient,
    WatchdogDecision,
    WatchdogGuard,
    emit_agent_metadata,
    make_mcp_transport,
    make_uc_violation_writer,
    make_watchdog_transport,
    set_uc_tags_for_agent,
)

# MLflow ChatAgent /invocations route mounter (optional — same extras)
from ._invocations import mount_invocations_route

# MLflow tracing helpers (optional — graceful no-op without mlflow)
from ._mlflow_tracing import (
    current_active_span,
    enable_langchain_autolog,
    is_mlflow_available,
    safe_span,
)

# Audit log schema — apx.* span attributes
from ._audit import (
    AuditAttrs,
    hash_for_audit,
    input_keys_summary,
    output_summary,
    set_audit_attrs,
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
    "AfterModelHook",
    "AfterToolHook",
    "BeforeModelHook",
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
    "evaluate",
    # Tool factories
    "genie_query_tool",
    "genie_tool",
    "catalog_tool",
    "lineage_tool",
    "schema_tool",
    "uc_function_tool",
    "uc_function_toolkit",
    "vector_search_tool",
    "sql_tool",
    "foundation_model_tool",
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
    # @tool decorator and UC publishing
    "tool",
    "ToolMetadata",
    "get_tool_metadata",
    "publish_tools_to_uc",
    "PublishResult",
    # Session / multi-turn memory
    "Session",
    "SessionStore",
    "InMemorySessionStore",
    "DeltaSessionStore",
    "LakebaseSessionStore",
    "append_turn",
    "load_or_create_session",
    # Managed MCP
    "ManagedMCPEndpoint",
    "managed_mcp_urls",
    "managed_mcp_client_config",
    # Supervisor publishing
    "create_supervisor_agent",
    "publish_to_supervisor",
    # Local lightweight guards
    "FeatureFlagGuard",
    "RateLimit",
    "ToolAllowlist",
    "ToolDenylist",
    "prompt_injection_heuristic",
    "compose",
    # Cost tracking
    "CostBreakdown",
    "cost_for_agent",
    "cost_for_endpoint",
    # Batch / scheduled-job invocation
    "run_once",
    # Lint
    "LintFinding",
    "Severity",
    "lint_agent",
    # Hot-swap
    "APX_MODEL_OVERRIDE_ENV",
    "HotSwapResult",
    "get_active_override",
    "hot_swap_model",
    # Trace exporter
    "ExportResult",
    "export_traces",
    # Topology
    "AgentNode",
    "Topology",
    "TopologyEdge",
    "discover_topology",
    "render_topology",
    # Cross-agent eval
    "ChainCaseResult",
    "ChainEvalReport",
    "evaluate_chain",
    # Canary / A-B helpers
    "CanaryConfig",
    "CanaryReport",
    "VersionMetrics",
    "analyze_canary",
    "deploy_canary",
    "get_canary_config",
    "promote_canary",
    "rollback_canary",
    # Watchdog integration
    "WatchdogClient",
    "WatchdogDecision",
    "WatchdogGuard",
    "emit_agent_metadata",
    "set_uc_tags_for_agent",
    "make_uc_violation_writer",
    "make_mcp_transport",
    "make_watchdog_transport",
    # MLflow /invocations route mounter
    "mount_invocations_route",
    # MLflow tracing
    "current_active_span",
    "enable_langchain_autolog",
    "is_mlflow_available",
    "safe_span",
    # Audit log schema
    "AuditAttrs",
    "set_audit_attrs",
    "hash_for_audit",
    "input_keys_summary",
    "output_summary",
]
