"""Dev UI eval_ui routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    EvalCaseIn,
    EvalCaseResponse,
    EvalDataSaveResponse,
    HTMLResponse,
    JSONResponse,
    JudgeRequest,
    JudgeResponse,
    Request,
    _EVAL_CASES_CACHE,
    _GROUNDING_COLUMNS_CACHE,
    _TRACES_LIST_CACHE,
    _fetch_eval_cases_async,
    _find_evals_path,
    _json,
    _parse_judge_output,
    _refresh_eval_cache,
    _ws_upload_agent_file,
    asyncio,
    os,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/eval/data", response_model=list[EvalCaseResponse])
    async def eval_data_get() -> Any:
        """Read eval cases — served from cache, refreshed in background after TTL.

        First call fetches from MLflow (may take 30-60s on cold warehouse).
        Subsequent calls return immediately from the 5-minute cache; a background
        task refreshes it when stale. Falls back to evals.json if MLflow is
        unavailable.
        """
        from fastapi.responses import JSONResponse

        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID")
        if experiment_id:
            if _EVAL_CASES_CACHE.fresh:
                return JSONResponse(_EVAL_CASES_CACHE.get())

            cached = _EVAL_CASES_CACHE.get()
            if cached is not None and not _EVAL_CASES_CACHE._refreshing:
                _EVAL_CASES_CACHE._refreshing = True
                asyncio.create_task(_refresh_eval_cache(experiment_id))
                return JSONResponse(cached)

            # First load — must wait
            cases = await _fetch_eval_cases_async(experiment_id)
            if cases:
                _EVAL_CASES_CACHE.put(cases)
                return JSONResponse(cases)

        # Fallback: local evals.json
        path = _find_evals_path()
        if path is None or not path.exists():
            return JSONResponse([])
        try:
            return JSONResponse(_json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @router.post("/_apx/eval/data", response_model=EvalDataSaveResponse)
    async def eval_data_post(request: Request, cases: list[EvalCaseIn]) -> Any:
        """Replace persisted eval cases with the request body (a list).

        ``cases: list[EvalCaseIn]`` makes FastAPI validate the body shape: a
        non-list, or an element missing ``question``, now yields ``422`` (this
        replaces the old manual ``isinstance``/400 check). The persisted bytes
        come from re-reading the **raw** body — ``EvalCaseIn`` uses
        ``extra="ignore"``, so dumping the parsed models would drop the UI's
        divergent fields; the cached raw body preserves them verbatim. The
        semantic 503 (``agent_router.py`` not found) and 500 (OS write error)
        stay in the handler as ``JSONResponse`` (bypassing ``response_model``).
        """
        from fastapi.responses import JSONResponse
        # Body already validated as a list[EvalCaseIn]; re-read the cached raw
        # body so unmodelled, divergent case fields persist unchanged.
        body = await request.json()
        path = _find_evals_path()
        if path is None:
            return JSONResponse({"ok": False, "error": "No agent module or project root (pyproject.toml) to hold evals.json"}, status_code=503)
        content = _json.dumps(body, indent=2)
        try:
            path.write_text(content)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        _EVAL_CASES_CACHE.clear()
        await _ws_upload_agent_file(request, path, content)
        # Raw dict (not JSONResponse) so ``response_model`` validates the shape.
        return {"ok": True, "count": len(cases)}

    @router.post("/_apx/eval/judge", response_model=JudgeResponse)
    async def eval_judge(request: Request, judge: JudgeRequest) -> Any:
        """LLM-as-judge scoring for eval cases.

        Body: {question, response, criterion, model?}. A missing required key or
        a wrong type now yields ``422`` via :class:`JudgeRequest`; a present-but-
        blank required field yields ``422`` here in the handler. The judge prompt
        asks the model to reply with PASS/FAIL + a one-sentence reason; we parse
        the verdict deterministically and return {ok, pass, verdict, reason}.
        The semantic 503 (no agent context) is preserved.
        """
        from fastapi.responses import JSONResponse
        import time as _time

        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            return JSONResponse({"ok": False, "error": "Agent not configured"}, status_code=503)

        question = judge.question.strip()
        response = judge.response.strip()
        criterion = judge.criterion.strip()
        if not (question and response and criterion):
            return JSONResponse(
                {"ok": False, "error": "question, response, and criterion are all required"},
                status_code=422,
            )
        model = judge.model or getattr(ctx.config, "model", "")
        if not model:
            return JSONResponse({"ok": False, "error": "No model configured"}, status_code=400)

        try:
            from databricks_openai import AsyncDatabricksOpenAI
        except ImportError as exc:
            return JSONResponse({"ok": False, "error": f"databricks_openai not available: {exc}"}, status_code=500)

        prompt = (
            "You are evaluating an AI agent's response against a criterion. "
            "Reply on a single line in this exact format:\n"
            "VERDICT: PASS|FAIL\n"
            "REASON: <one sentence>\n\n"
            f"Question: {question}\n"
            f"Response: {response}\n"
            f"Criterion: {criterion}\n"
            "Strict pass: response clearly meets the criterion. If unclear or partial, FAIL."
        )

        t0 = _time.monotonic()
        try:
            client = AsyncDatabricksOpenAI()
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
            elapsed = int((_time.monotonic() - t0) * 1000)
            choices = getattr(resp, "choices", None) or []
            text = ""
            if choices:
                msg = getattr(choices[0], "message", None)
                text = ((getattr(msg, "content", None) or "") if msg else "").strip()
            _judge = _parse_judge_output(text)
            # Raw dict (not JSONResponse) so ``response_model`` validates the
            # JudgeResponse shape; ``pass`` serialises via its field alias.
            return {
                "ok": True,
                "pass": _judge.verdict == "PASS",
                "verdict": _judge.verdict,
                "reason": _judge.reason,
                "duration_ms": elapsed,
                "model": model,
            }
        except Exception as exc:  # noqa: BLE001
            elapsed = int((_time.monotonic() - t0) * 1000)
            return JSONResponse({
                "ok": False,
                "error": str(exc),
                "duration_ms": elapsed,
            }, status_code=200)

    # Redirects for old routes
    @router.get("/_apx/eval", response_class=HTMLResponse)
    async def eval_ui(request: Request) -> HTMLResponse:
        """Eval landing page — cases sourced from MLflow quality assessments.

        Serves from the shared _EVAL_CASES_CACHE (same cache as /_apx/eval/data)
        so both the standalone page and the Chat-shell tab stay in sync.
        Falls back to evals.json when MLflow is unavailable.
        """
        from apx_agent._ui_chat import _render_eval_landing
        cases: list[dict[str, Any]] = []
        loaded_path: str | None = None
        load_error: str | None = None

        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID")
        if experiment_id:
            if _EVAL_CASES_CACHE.fresh:
                cases = list(_EVAL_CASES_CACHE.get() or [])
            else:
                cached = _EVAL_CASES_CACHE.get()
                if cached is not None and not _EVAL_CASES_CACHE._refreshing:
                    _EVAL_CASES_CACHE._refreshing = True
                    asyncio.create_task(_refresh_eval_cache(experiment_id))
                    cases = list(cached)
                elif cached is None:
                    cases = await _fetch_eval_cases_async(experiment_id)
                    if cases:
                        _EVAL_CASES_CACHE.put(cases)
                else:
                    cases = list(cached or [])
            if cases:
                loaded_path = f"MLflow experiment {experiment_id} ({len(cases)} labeled traces)"

        if not cases:
            path = _find_evals_path()
            if path is not None and path.exists():
                loaded_path = str(path)
                try:
                    cases = _json.loads(path.read_text())
                    if not isinstance(cases, list):
                        cases = []
                        load_error = f"{path} did not contain a JSON list."
                except (OSError, ValueError) as exc:
                    load_error = f"Could not parse {path}: {exc}"

        native = getattr(request.app.state, "_apx_native_chat", False)
        return HTMLResponse(_render_eval_landing(cases, loaded_path, load_error, native=native))

    @router.post("/_apx/eval/label-start")
    async def eval_label_start(request: Request) -> Any:
        """Start an SME labeling session via `apx-agent label start`."""
        from fastapi.responses import JSONResponse
        from apx_agent import _labeling
        import datetime

        ctx: AgentContext | None = request.app.state.agent_context
        body = await request.json()
        judge_name = (body.get("judge_name") or "").strip()
        if not judge_name:
            return JSONResponse({"ok": False, "error": "judge_name is required"}, status_code=422)
        experiment_id = (os.environ.get("MLFLOW_EXPERIMENT_ID") or "").strip() or None
        if not experiment_id:
            return JSONResponse({"ok": False, "error": "MLFLOW_EXPERIMENT_ID not set"}, status_code=503)
        agent_name = ctx.config.name if ctx else "agent"
        try:
            import mlflow
            mlflow.set_tracking_uri("databricks")
            result = _labeling.start_session(
                experiment_id=experiment_id,
                agent_name=agent_name,
                judge_name=judge_name,
                scale=None, options=None, assignees=[],
                filter_string=None, limit=None, endpoint=None,
                attach_agent=True,
                now=datetime.datetime.now(datetime.timezone.utc),
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return {"ok": True, "run_id": result.run_id, "session_url": result.session_url, "trace_count": result.trace_count}

    @router.post("/_apx/eval/label-align")
    async def eval_label_align(request: Request) -> Any:
        """Run MemAlign directly from all labeled traces — no run_id needed.

        Loads all traces with quality assessments via mlflow.get_trace(),
        runs MemAlignOptimizer, and returns the distilled guidelines.
        """
        from fastapi.responses import JSONResponse

        body = await request.json()
        judge_name = (body.get("judge_name") or "").strip()
        if not judge_name:
            return JSONResponse({"ok": False, "error": "judge_name is required"}, status_code=422)
        experiment_id = (os.environ.get("MLFLOW_EXPERIMENT_ID") or "").strip() or None
        if not experiment_id:
            return JSONResponse({"ok": False, "error": "MLFLOW_EXPERIMENT_ID not set"}, status_code=503)
        try:
            import mlflow
            from mlflow.genai.judges import make_judge
            from mlflow.genai.judges.optimizers import MemAlignOptimizer

            mlflow.set_tracking_uri("databricks")

            # Load traces that have quality assessments
            df = mlflow.search_traces(
                experiment_ids=[experiment_id],
                max_results=200,
                order_by=["attributes.start_time DESC"],
            )
            labeled_ids = [
                str(row["trace_id"]) for _, row in df.iterrows()  # type: ignore[union-attr]
                if any(
                    (a.get("assessment_name") if isinstance(a, dict) else getattr(a, "name", None)) == judge_name
                    for a in (row.get("assessments") or [])
                )
            ]
            if not labeled_ids:
                return JSONResponse({"ok": False, "error": f"No traces with '{judge_name}' assessments found. Rate some traces first."}, status_code=422)

            traces = []
            for tid in labeled_ids:
                try:
                    traces.append(mlflow.get_trace(tid))
                except Exception:
                    pass
            if not traces:
                return JSONResponse({"ok": False, "error": "Could not load trace spans. Try rating more traces."}, status_code=500)

            judge = make_judge(
                name=judge_name,
                instructions=f"Input: {{{{ inputs }}}}\nOutput: {{{{ outputs }}}}\nReturn true if the response is accurate and appropriate, false if it fabricates information or fails to decline out-of-scope requests.",
                feedback_value_type=bool,
                model="databricks:/databricks-claude-sonnet-4-6",
            )
            optimizer = MemAlignOptimizer(
                reflection_lm="databricks:/databricks-claude-sonnet-4-6",
                retrieval_k=min(5, len(traces)),
                embedding_model="databricks:/databricks-gte-large-en",
            )
            aligned: Any = judge.align(traces=traces, optimizer=optimizer)
            memory = aligned._semantic_memory if hasattr(aligned, "_semantic_memory") else None
            guidelines = [g.guideline_text for g in memory or []]
            from apx_agent import _labeling
            run_id = _labeling.log_align_run(
                experiment_id=experiment_id,
                judge_name=judge_name,
                registered_as=judge_name,
                trace_count=len(traces),
                guidelines=guidelines,
                mlflow_api=mlflow,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return {
            "ok": True,
            "registered_as": judge_name,
            "guidelines": guidelines,
            "trace_count": len(traces),
            "run_id": run_id,
        }

    @router.get("/_apx/eval/label-history")
    async def eval_label_history(request: Request) -> Any:
        """List past MemAlign runs (guidelines over time) for the current experiment."""
        experiment_id = (os.environ.get("MLFLOW_EXPERIMENT_ID") or "").strip() or None
        if not experiment_id:
            return JSONResponse({"ok": False, "error": "MLFLOW_EXPERIMENT_ID not set"}, status_code=503)
        judge_name = (request.query_params.get("judge_name") or "").strip() or None
        try:
            import mlflow
            from apx_agent import _labeling

            mlflow.set_tracking_uri("databricks")
            runs = _labeling.list_align_runs(
                experiment_id=experiment_id,
                judge_name=judge_name,
                mlflow_api=mlflow,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return {
            "ok": True,
            "runs": [
                {
                    "run_id": run.run_id,
                    "start_time": run.start_time,
                    "judge_name": run.judge_name,
                    "registered_as": run.registered_as,
                    "trace_count": run.trace_count,
                    "guidelines": run.guidelines,
                    "guideline_count": run.guideline_count,
                }
                for run in runs
            ],
        }

    @router.post("/_apx/eval/cache/bust")
    async def eval_cache_bust() -> Any:
        """Clear both MLflow query caches so the next request re-fetches fresh data.

        Call this after rating new traces to see them immediately in /_apx/eval
        and /_apx/traces without waiting for the TTL to expire.
        """
        _EVAL_CASES_CACHE.clear()
        _TRACES_LIST_CACHE.clear()
        _GROUNDING_COLUMNS_CACHE.clear()
        return {"ok": True, "message": "All caches cleared."}
