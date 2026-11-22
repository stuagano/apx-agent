"""Shared native invocation client for the two existing chat renderers."""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError


def native_input_router(agent: Any) -> APIRouter:
    """Discover and validate the server declaration without executing a workflow."""
    from ._native_input import input_leaf, native_data_part

    schema = getattr(input_leaf(agent), "_input_schema", None)
    router = APIRouter()

    @router.get("/api/input-schema", include_in_schema=False)
    async def input_schema() -> Any:
        return schema.model_json_schema() if schema is not None else None

    @router.post("/api/input-validation", include_in_schema=False)
    async def validate_input(request: Request) -> Any:
        if schema is None:
            raise HTTPException(422, "This agent accepts text input")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Declared input exceeds 64 KiB")
        try:
            schema.model_validate_json(body, strict=True)
            return native_data_part(schema, json.loads(body))
        except ValidationError as exc:
            # Locations and messages help correction without reflecting submitted values.
            detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False)[:16])
            raise HTTPException(422, detail[:2048]) from None
        except (ValueError, TypeError):
            raise HTTPException(422, "Provide a JSON object matching the declared schema") from None

    return router

_VENDOR_JS = Path(__file__).parent / "_static" / "vendor" / "native-chat.js"
# Shipped script text. test_native_chat_transport executes apxPriorTurns from it.
NATIVE_CHAT_SCRIPT = _VENDOR_JS.read_text(encoding="utf-8")

NATIVE_CHAT_STYLE = r"""<style>
.native-execution-card {border: 1px solid var(--apx-border); border-radius: 8px; padding: 12px; margin: 8px 0; overflow-wrap: anywhere;}
.native-step-contract, .native-step-handoff {color: var(--apx-muted); margin-top: 4px;}
/* Consecutive steps read as a chain. */
.native-execution-card + .native-execution-card {position: relative; margin-top: 22px;}
.native-execution-card + .native-execution-card::before {content: '↓'; position: absolute; top: -21px; left: 16px; color: var(--apx-muted);}
.native-input-schema > summary {display: inline-block; border: 1px solid var(--apx-border); border-radius: 999px; padding: 2px 10px; cursor: pointer;}
.native-input-form {display: grid; gap: 6px; margin: 0 0 8px; padding: 8px 12px; border: 1px solid var(--apx-border); border-radius: 8px;}
.native-input-form .native-field {display: grid; grid-template-columns: minmax(8em, 14em) 1fr; gap: 2px 8px; align-items: center;}
.native-input-form small {grid-column: 2; color: var(--apx-muted);}
.native-input-form .native-required, .native-input-form-notice {color: var(--apx-muted);}
.native-input-form[disabled] .native-field {opacity: .5;}
.native-event, .native-output, .native-input-schema pre {white-space: pre-wrap; overflow-wrap: anywhere; max-height: 320px; overflow: auto;}
.native-validation {color: var(--apx-err);}
.native-status-row {display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin: 12px 0;}
.native-result-button {border: 1px solid var(--apx-border); border-radius: 6px; padding: 7px 14px; font: inherit; font-weight: 600; cursor: pointer;}
.native-result-button:disabled {opacity: .5; cursor: default;}
.native-result-panel {border: 1px solid var(--apx-border); border-radius: 8px; padding: 16px; margin-bottom: 12px; overflow-wrap: anywhere; min-width: 0;}
.native-result-panel h3 {margin: 0 0 12px;}
.native-result-panel h4 {margin: 16px 0 8px;}
.native-result-panel .native-tree-table {display: block; max-width: 100%; overflow-x: auto;}
.native-result-answer {white-space: pre-wrap;}
.native-result-packet {margin: 12px 0;}
@media (max-width: 700px) {
  .main:has(.native-status-row) {flex-direction: column; overflow-y: auto;}
  .main:has(.native-status-row) .chat-panel {flex: 1 0 70vh;}
  .main:has(.native-status-row) .right-panel {width: 100%; min-width: 0; max-width: none; flex: 0 0 40vh;}
}
</style>
"""

def native_chat_script(*, root: bool) -> str:
    """Bind to existing page elements and safe message renderers.

    The client is a static asset. Root chat serves it from this app (the dev
    UI may be off). The dev chat page uses the vendor route.
    """
    src = "/assets/chat/native-chat.js" if root else "/_apx/vendor/native-chat.js"
    bindings = (
        "form, input, send: sendBtn, container: log, bubble, render: renderMarkdown"
        if root else
        "form, input: inputEl, send: sendBtn, container: chat, bubble: addMsg, render: renderAssistantInto"
    )
    return (
        NATIVE_CHAT_STYLE
        + '<script src="' + src + '"></script>'
        + "<script>apxNativeChat({" + bindings + "});</script>"
    )
