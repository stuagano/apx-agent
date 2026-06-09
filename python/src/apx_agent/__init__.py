"""apx-agent — declarative agent DSL that compiles to LangGraph + MLflow."""

# Agent types
from ._agents import (
    BaseAgent,
    HandoffAgent,
    KeywordRouter,
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

# DataAgent — LlmAgent specialized for governed UC data access
from .data_agent import DataAgent, DataTemplate
from .coworker import CoworkerAgent, CoworkerTemplate

# Template protocol, registry, and decorator
from ._template import Template, TemplateInfo, TemplateRegistry, template, template_registry

# Models
from ._models import (
    AfterAgentCallback,
    AfterModelHook,
    AfterToolHook,
    AgentCard,
    AgentConfig,
    AgentContext,
    AgentTool,
    BeforeAgentCallback,
    BeforeModelHook,
    BeforeToolHook,
    ExampleBackendConfig,
    GuardrailsConfig,
    InputGuardrailFn,
    MemoryBackendConfig,
    Message,
    OnModelErrorCallback,
    OnToolErrorCallback,
    OutputGuardrailFn,
    SessionBackendConfig,
)

# FastAPI dependency injection
from ._defaults import Dependencies

# SQL utilities
from ._sql import decode_statement, get_warehouse_id, run_sql

# Provider compatibility layer — get_llm() factory + named subclasses
from ._llm import ChatDatabricksGptReasoning, get_llm

# App factory and setup
from ._wiring import create_app, finalize_agent, mount_mcp_endpoints, setup_agent
from ._readyz import mount_readyz
from ._tool_config import ToolConfigError, load_config_tools, merge_config_tools

# Eval bridge
from ._eval import (
    app_predict_fn,
    endpoint_predict_fn,
    eval_against_endpoint,
    evaluate,
)

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
from .http_tools import http_tool, openapi_tool
from .mcp_consume import mcp_tool, mcp_toolkit
from .foundation_model import foundation_model_tool
from .jobs_tools import (
    jobs_for_table_tool,
    jobs_history_tool,
    jobs_logs_tool,
    jobs_source_paths_tool,
    jobs_tools,
)

# LangGraph compiler (optional — requires the ``langgraph`` extra)
from ._compile import CompileContext, compile_to_langgraph

# Executor protocol + event types (harness abstraction seam)
from ._executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)

# LangGraph executor implementation
from ._langgraph_executor import LangGraphExecutor

# MLflow ChatAgent wrapper (optional — requires the ``langgraph`` and ``eval`` extras)
from ._chat_agent import chat_agent_for, compile_to_chat_agent, log_agent

# Databricks Apps ResponsesAgent compile target (optional — same extras)
from ._responses_agent import compile_to_responses_agent

# Unified OBO header extraction (both runtimes use this)
from ._obo import extract_obo_headers, make_obo_workspace_client

# Resource declaration — auto-derive MLflow resources from the agent tree
from ._resources import (
    ResourceSpec,
    attach_resources,
    collect_resource_specs,
    mlflow_resources_for,
    resources_to_databricks_yml,
)

# @tool decorator and UC publishing
from ._tool import ToolMetadata, get_tool_metadata, tool
from ._tool_factory import build_tool, resolve_description
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

# Durable memory + few-shot examples
from ._memory import (
    EmbeddingFn,
    InMemoryMemoryStore,
    Memory,
    MemoryFilter,
    MemoryStore,
    RecallOptions,
    RecallResult,
    cosine_similarity,
)
from ._example import (
    Example,
    ExampleFilter,
    ExampleResult,
    ExampleStore,
    FindSimilarOptions,
    InMemoryExampleStore,
)
from ._memory_lakebase import LakebaseMemoryStore
from ._example_lakebase import LakebaseExampleStore
from ._memory_delta import DeltaMemoryStore, VectorSearchLike
from ._example_delta import DeltaExampleStore

# Agent-facing memory / example tools (LLM-callable wrappers around the stores)
from ._memory_tools import make_memory_tools
from ._example_tools import make_example_tools

# Prompt-assembly helpers — stitch memory + example recall into a system prompt
from ._prompt_assembly import (
    assemble_context,
    assemble_example_context,
    assemble_memory_context,
)

# Example mining — extract few-shot examples from session history
from ._example_mining import (
    MineResult,
    Turn as ExampleMiningTurn,
    mine_examples,
    pair_turns,
)

# Memory consolidation — LLM-summarize older memories into a rollup row
from ._memory_consolidate import (
    ConsolidateResult,
    consolidate_memories,
)

# Databricks Managed MCP integration
from ._managed_mcp import (
    ManagedMCPEndpoint,
    managed_mcp_client_config,
    managed_mcp_urls,
)

# Mosaic AI Supervisor Agent publishing
from ._publish import create_supervisor_agent, publish_to_registry, publish_to_supervisor, publish_tools_to_registry, publish_standalone_tools_to_registry

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

# Hot-swap analog for the Apps target — re-deploys with a different bundle var
from ._hot_swap_apps import (
    DEFAULT_LLM_VAR_NAME,
    AppsHotSwapResult,
    hot_swap_apps,
    read_var_default,
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

# Canary / A-B deployment helpers (Model Serving target)
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

# Canary helpers for the Databricks Apps target — multi-target bundle flow
from ._canary_apps import (
    TRACE_APP_NAME_TAG,
    AppsCanaryConfig,
    AppsCanaryReport,
    AppsPromoteResult,
    AppsVersionMetrics,
    add_canary_target_to_yml,
    analyze_canary_app,
    canary_app_name,
    canary_target_name,
    deploy_canary_app,
    promote_canary_app,
    remove_canary_target_from_yml,
    rollback_canary_app,
    sanitize_version,
    write_databricks_yml,
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

# MLflow ChatAgent /invocations + ResponsesAgent /responses route mounters
from ._invocations import mount_invocations_route, mount_responses_route

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

# Bootstrap helpers for Apps-target quickstart
from .bootstrap import init_apps_experiment

__all__ = [
    # Agent types
    "Agent",
    "BaseAgent",
    "CoworkerAgent",
    "CoworkerTemplate",
    "DataAgent",
    "DataTemplate",
    "HandoffAgent",
    "KeywordRouter",
    "LlmAgent",
    "LoopAgent",
    "ParallelAgent",
    "RouterAgent",
    "SequentialAgent",
    "RemoteDatabricksAgent",
    "agent_tool",
    # Template protocol + registry
    "Template",
    "TemplateInfo",
    "TemplateRegistry",
    "template",
    "template_registry",
    # Models
    "AfterAgentCallback",
    "AfterModelHook",
    "AfterToolHook",
    "AgentCard",
    "AgentConfig",
    "AgentContext",
    "AgentTool",
    "BeforeAgentCallback",
    "BeforeModelHook",
    "BeforeToolHook",
    "ExampleBackendConfig",
    "GuardrailsConfig",
    "InputGuardrailFn",
    "MemoryBackendConfig",
    "Message",
    "OnModelErrorCallback",
    "OnToolErrorCallback",
    "OutputGuardrailFn",
    "SessionBackendConfig",
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
    "finalize_agent",
    "mount_mcp_endpoints",
    "mount_readyz",
    "setup_agent",
    # Declarative tool config
    "ToolConfigError",
    "load_config_tools",
    "merge_config_tools",
    # Eval
    "app_predict_fn",
    "endpoint_predict_fn",
    "eval_against_endpoint",
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
    "http_tool",
    "openapi_tool",
    "mcp_tool",
    "mcp_toolkit",
    "foundation_model_tool",
    "jobs_for_table_tool",
    "jobs_history_tool",
    "jobs_logs_tool",
    "jobs_source_paths_tool",
    "jobs_tools",
    # LangGraph compiler
    "CompileContext",
    "compile_to_langgraph",
    # MLflow ChatAgent wrapper
    "chat_agent_for",
    "compile_to_chat_agent",
    "log_agent",
    # Databricks Apps ResponsesAgent compile target
    "compile_to_responses_agent",
    # Unified OBO header extraction
    "extract_obo_headers",
    "make_obo_workspace_client",
    # Resource declaration
    "ResourceSpec",
    "attach_resources",
    "collect_resource_specs",
    "mlflow_resources_for",
    "resources_to_databricks_yml",
    # @tool decorator and UC publishing
    "tool",
    "build_tool",
    "resolve_description",
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
    # Durable memory + examples
    "EmbeddingFn",
    "InMemoryMemoryStore",
    "Memory",
    "MemoryFilter",
    "MemoryStore",
    "RecallOptions",
    "RecallResult",
    "cosine_similarity",
    "Example",
    "ExampleFilter",
    "ExampleResult",
    "ExampleStore",
    "FindSimilarOptions",
    "InMemoryExampleStore",
    "LakebaseMemoryStore",
    "LakebaseExampleStore",
    "DeltaMemoryStore",
    "DeltaExampleStore",
    "VectorSearchLike",
    # Agent-facing memory / example tools
    "make_memory_tools",
    "make_example_tools",
    # Prompt-assembly helpers
    "assemble_context",
    "assemble_example_context",
    "assemble_memory_context",
    # Example mining
    "ExampleMiningTurn",
    "MineResult",
    "mine_examples",
    "pair_turns",
    # Memory consolidation
    "ConsolidateResult",
    "consolidate_memories",
    # Managed MCP
    "ManagedMCPEndpoint",
    "managed_mcp_urls",
    "managed_mcp_client_config",
    # Supervisor publishing
    "create_supervisor_agent",
    "publish_to_registry",
    "publish_to_supervisor",
    "publish_tools_to_registry",
    "publish_standalone_tools_to_registry",
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
    # Hot-swap (Apps target)
    "AppsHotSwapResult",
    "DEFAULT_LLM_VAR_NAME",
    "hot_swap_apps",
    "read_var_default",
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
    # Canary / A-B helpers (Model Serving)
    "CanaryConfig",
    "CanaryReport",
    "VersionMetrics",
    "analyze_canary",
    "deploy_canary",
    "get_canary_config",
    "promote_canary",
    "rollback_canary",
    # Canary helpers (Databricks Apps)
    "AppsCanaryConfig",
    "AppsCanaryReport",
    "AppsPromoteResult",
    "AppsVersionMetrics",
    "TRACE_APP_NAME_TAG",
    "add_canary_target_to_yml",
    "analyze_canary_app",
    "canary_app_name",
    "canary_target_name",
    "deploy_canary_app",
    "promote_canary_app",
    "remove_canary_target_from_yml",
    "rollback_canary_app",
    "sanitize_version",
    "write_databricks_yml",
    # Watchdog integration
    "WatchdogClient",
    "WatchdogDecision",
    "WatchdogGuard",
    "emit_agent_metadata",
    "set_uc_tags_for_agent",
    "make_uc_violation_writer",
    "make_mcp_transport",
    "make_watchdog_transport",
    # MLflow route mounters
    "mount_invocations_route",
    "mount_responses_route",
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
    # Bootstrap
    "init_apps_experiment",
]
