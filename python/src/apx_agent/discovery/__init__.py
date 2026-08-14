"""
apx_agent.discovery — a domain-agnostic, durable "tech-to-biz PoV & discovery
guide" workflow on the WorkflowEngine.

Six sequential structured-output steps (priorities → value_matrices → heat_map
→ wow_selection → discovery_guide → three_ws), resumable via the engine's step
cache, fed by a pluggable ResearchProvider, extensible via appended handoff
steps, and rendered to a markdown brief. Vendor-neutral: the only model seam is
the injected Completion callable.
"""
from .render import render_markdown_brief
from .research import Completion, LLMResearchProvider, ResearchProvider
from .schemas import (
    DiscoveryGuide,
    HeatCell,
    HeatMap,
    MalformedStepOutput,
    MatrixRow,
    Priorities,
    ResearchBundle,
    ThreeWs,
    ValueMatrices,
    WowSelection,
)
from .steps import STEP_KEYS
from .workflow import WORKFLOW_NAME, DiscoveryWorkflow, HandoffHandler

__all__ = [
    "DiscoveryWorkflow",
    "HandoffHandler",
    "WORKFLOW_NAME",
    "STEP_KEYS",
    "Completion",
    "ResearchProvider",
    "LLMResearchProvider",
    "ResearchBundle",
    "Priorities",
    "ValueMatrices",
    "MatrixRow",
    "HeatMap",
    "HeatCell",
    "WowSelection",
    "DiscoveryGuide",
    "ThreeWs",
    "MalformedStepOutput",
    "render_markdown_brief",
]
