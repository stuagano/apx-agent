"""MLflow model contract over APX's existing Responses execution handlers."""

from __future__ import annotations

from typing import Any, Generator
from threading import Lock

from mlflow.pyfunc.model import ResponsesAgent
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse, ResponsesAgentStreamEvent

from ._agents import BaseAgent
from ._responses_agent import CompiledResponsesAgent, compile_to_responses_agent


class ApxResponsesAgent(ResponsesAgent):
    """A saveable MLflow model with runtime handlers initialized on first use.

    Handler caches contain process-local stores and must not be pickled into a
    model artifact. Explicit caller-provided bindings retain their own normal
    serialization requirements; use model-from-code for external resources.
    """

    def __init__(self, agent: BaseAgent, *, model: str, checkpointer: Any = None,
                 conversation_store: Any = None) -> None:
        self.agent = agent
        self.model = model
        self.checkpointer = checkpointer
        self.conversation_store = conversation_store
        self._handlers: CompiledResponsesAgent | None = None
        self._compile_lock = Lock()

    def __getstate__(self) -> dict[str, Any]:
        state = {**self.__dict__, "_handlers": None}
        state.pop("_compile_lock")
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        for name, value in state.items():
            setattr(self, name, value)
        self._compile_lock = Lock()

    def _compiled(self) -> CompiledResponsesAgent:
        with self._compile_lock:
            if self._handlers is None:
                self._handlers = compile_to_responses_agent(
                    self.agent, model=self.model, checkpointer=self.checkpointer,
                    conversation_store=self.conversation_store,
                )
            return self._handlers

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        return self._compiled().non_streaming(request)

    def predict_stream(self, request: ResponsesAgentRequest) -> Generator[ResponsesAgentStreamEvent, None, None]:
        yield from self._compiled().streaming(request)
