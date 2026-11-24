"""
Agent Bricks - Manage Knowledge Assistants and Supervisor Agents.

This module provides a unified interface for managing Agent Bricks resources:
- Knowledge Assistants (KA): Document-based Q&A systems
- Supervisor Agents (MAS): Multi-agent orchestration

Genie spaces are managed as code with `apx-agent resources genie-space pull|plan|push`.
"""

from .manager import AgentBricksManager, TileExampleQueue, get_tile_example_queue
from .models import (
    # Enums
    EndpointStatus,
    Permission,
    TileType,
    # Data classes
    KAIds,
    MASIds,
    # TypedDicts
    BaseAgentDict,
    EvaluationRunDict,
    KnowledgeAssistantDict,
    KnowledgeAssistantExampleDict,
    KnowledgeAssistantListExamplesResponseDict,
    KnowledgeAssistantResponseDict,
    KnowledgeAssistantStatusDict,
    KnowledgeSourceDict,
    ListEvaluationRunsResponseDict,
    MultiAgentSupervisorDict,
    MultiAgentSupervisorExampleDict,
    MultiAgentSupervisorListExamplesResponseDict,
    MultiAgentSupervisorResponseDict,
    MultiAgentSupervisorStatusDict,
    TileDict,
)

__all__ = [
    # Main class
    "AgentBricksManager",
    # Background queue
    "TileExampleQueue",
    "get_tile_example_queue",
    # Enums
    "EndpointStatus",
    "Permission",
    "TileType",
    # Data classes
    "KAIds",
    "MASIds",
    # TypedDicts
    "BaseAgentDict",
    "EvaluationRunDict",
    "KnowledgeAssistantDict",
    "KnowledgeAssistantExampleDict",
    "KnowledgeAssistantListExamplesResponseDict",
    "KnowledgeAssistantResponseDict",
    "KnowledgeAssistantStatusDict",
    "KnowledgeSourceDict",
    "ListEvaluationRunsResponseDict",
    "MultiAgentSupervisorDict",
    "MultiAgentSupervisorExampleDict",
    "MultiAgentSupervisorListExamplesResponseDict",
    "MultiAgentSupervisorResponseDict",
    "MultiAgentSupervisorStatusDict",
    "TileDict",
]
