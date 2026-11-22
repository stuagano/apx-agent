"""Dev UI traces routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    AgentContext,
    Any,
    HTMLResponse,
    JSONResponse,
    LastRouteResponse,
    LatencyTrendResponse,
    Request,
    ToolFailRateResponse,
    TraceDetailResponse,
    TraceDiffResponse,
    TraceRow,
    _TRACES_LIST_CACHE,
    _fetch_traces_list_async,
    _last_route_payload,
    _latency_trend,
    _load_optimize_eval_rows,
    _refresh_traces_cache,
    _render_trace_detail,
    _render_trace_diff,
    _render_traces_list,
    _serialize_trace_spans,
    _tool_fail_rates,
    _trace_diff_side,
    asyncio,
    os,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/traces", response_model=list[TraceRow])
    async def traces_list_ui(request: Request) -> Any:
        import os
        ctx: AgentContext | None = getattr(request.app.state, "agent_context", None)
        agent_name = ctx.config.name if ctx else None
        fmt = request.query_params.get("fmt")
        max_results = int(request.query_params.get("max", "50"))

        # HTML path: return the skeleton immediately — rows load via JS fetch
        # to /_apx/traces?fmt=json. Avoids a 30-60s cold-warehouse hang that
        # makes the browser tab unresponsive on deployed Apps. (#718 class issue)
        if fmt != "json":
            return HTMLResponse(_render_traces_list(None, agent_name))

        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID")

        # Stale-while-revalidate: serve from cache; background-refresh when stale.
        if _TRACES_LIST_CACHE.fresh:
            rows = list(_TRACES_LIST_CACHE.get())
        else:
            cached_rows = _TRACES_LIST_CACHE.get()
            if cached_rows is not None and not _TRACES_LIST_CACHE._refreshing:
                _TRACES_LIST_CACHE._refreshing = True
                asyncio.create_task(_refresh_traces_cache(experiment_id, max_results))
                rows = list(cached_rows)
            elif cached_rows is None:
                rows = await _fetch_traces_list_async(experiment_id, max_results)
                _TRACES_LIST_CACHE.put(rows)
            else:
                rows = list(cached_rows or [])

        seen_ids: set[str] = {r["trace_id"] for r in rows}
        # Merge ring-buffer traces not yet committed to the tracking store.
        try:
            from apx_agent._trace_store import list_recent as _ts_list_recent
            for tid in _ts_list_recent(max_results):
                if tid not in seen_ids:
                    rows.insert(0, {
                        "trace_id": tid,
                        "state": "OK",
                        "request_time_ms": None,
                        "duration_ms": None,
                        "request_preview": "",
                        "response_preview": "",
                    })
                    seen_ids.add(tid)
        except Exception:
            pass
        rows = rows[:max_results]
        return rows


    @router.get("/_apx/traces/last-route", response_model=LastRouteResponse)
    async def traces_last_route(request: Request) -> Any:
        """Map the latest Chat turn onto topology node/edge ids for live highlight.

        Registered before ``/_apx/traces/{trace_id:path}`` so ``last-route`` is
        not captured as a trace id.
        """
        ctx: AgentContext | None = getattr(request.app.state, "agent_context", None)
        if ctx is None:
            return JSONResponse(
                {"error": "Agent context not available"}, status_code=503
            )

        return _last_route_payload(ctx)

    @router.get("/_apx/traces/latency", response_model=LatencyTrendResponse)
    async def traces_latency_trend(request: Request) -> Any:
        """p50/p95 + last-20 ``duration_ms`` for the Chat landing sparkline.

        Registered before ``/_apx/traces/{trace_id:path}`` so ``latency`` is
        not captured as a trace id. Reuses ``_TRACES_LIST_CACHE`` — no second
        MLflow query, no third TTL cache.
        """
        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID")
        if not experiment_id:
            return JSONResponse(
                {"error": "MLFLOW_EXPERIMENT_ID not set"}, status_code=503
            )

        if _TRACES_LIST_CACHE.fresh:
            rows = list(_TRACES_LIST_CACHE.get())
        else:
            cached_rows = _TRACES_LIST_CACHE.get()
            if cached_rows is not None and not _TRACES_LIST_CACHE._refreshing:
                _TRACES_LIST_CACHE._refreshing = True
                asyncio.create_task(_refresh_traces_cache(experiment_id, 50))
                rows = list(cached_rows)
            elif cached_rows is None:
                # Fetch the traces-list default (50), not the sparkline
                # window — putting 20 rows here would starve GET /_apx/traces.
                rows = await _fetch_traces_list_async(experiment_id, 50)
                _TRACES_LIST_CACHE.put(rows)
            else:
                rows = list(cached_rows)

        return _latency_trend(rows)

    @router.get("/_apx/traces/tool-fails", response_model=ToolFailRateResponse)
    async def traces_tool_fails(request: Request) -> Any:
        """Per-tool fail rate from the in-process span ring buffer.

        Registered before ``/_apx/traces/{trace_id:path}`` so ``tool-fails``
        is not captured as a trace id. Reads ``_trace_store`` only -- no
        MLflow, no list-cache write.
        """
        from apx_agent._trace_store import get as _ts_get, list_recent as _ts_list

        spans_by_trace: list[list[dict[str, Any]]] = []
        for tid in _ts_list():
            spans = _ts_get(tid)
            if spans is None:
                continue
            spans_by_trace.append(spans)
        return _tool_fail_rates(spans_by_trace)

    @router.get("/_apx/traces/diff", response_model=TraceDiffResponse)
    async def traces_diff(request: Request) -> Any:
        """Side-by-side request / response / judge for two trace ids.

        Registered before ``/_apx/traces/{trace_id:path}`` so ``diff`` is not
        captured as a trace id. Ring buffer + evals.json only — no
        ``mlflow.get_trace``.
        """
        from fastapi.responses import JSONResponse
        from apx_agent._trace_store import get as _ts_get

        left_id = request.query_params.get("a")
        right_id = request.query_params.get("b")
        fmt = request.query_params.get("fmt")
        if not left_id or not right_id:
            if fmt == "json":
                return JSONResponse(
                    {"error": "query params a and b (trace ids) are required"},
                    status_code=400,
                )
            empty = {
                "trace_id": "",
                "found": False,
                "question": None,
                "response": None,
                "judge_verdict": None,
                "judge_reason": None,
            }
            return HTMLResponse(_render_trace_diff(empty, empty))

        eval_rows = _load_optimize_eval_rows()
        payload = {
            "left": _trace_diff_side(left_id, _ts_get(left_id), eval_rows),
            "right": _trace_diff_side(right_id, _ts_get(right_id), eval_rows),
        }
        if fmt == "json":
            return payload
        return HTMLResponse(_render_trace_diff(payload["left"], payload["right"]))

    @router.get("/_apx/traces/{trace_id:path}", response_model=TraceDetailResponse)
    async def trace_detail_ui(trace_id: str, request: Request) -> Any:
        from fastapi.responses import JSONResponse
        from apx_agent._trace_store import get as _ts_get, put as _ts_put

        fmt = request.query_params.get("fmt")

        # 1) Buffer hit — serve from the in-process ring buffer (captured at
        #    root-span-end, no blob fetch). This is the FEVM/private-link path:
        #    recent traces are served from memory and never touch get_trace.
        buffered = _ts_get(trace_id)
        if buffered is not None:
            if fmt == "json":
                return {"trace_id": trace_id, "spans": buffered}
            return HTMLResponse(_render_trace_detail(trace_id, buffered, None))

        # 2) Buffer miss — fall through to mlflow.get_trace under a worker-thread
        #    TIMEOUT so a blocked blob fetch fails fast instead of hanging. The
        #    executor is NOT used as a context manager: its __exit__ would
        #    shutdown(wait=True) and re-join the still-hung worker, undoing the
        #    timeout. On timeout we abandon the orphaned thread.
        import concurrent.futures as _futures

        _GET_TRACE_TIMEOUT_S = 5.0
        unavailable = (
            "span data unavailable on this workspace (artifact-storage egress "
            "blocked) — recent traces are served from memory"
        )

        def _fetch() -> Any:
            import mlflow as _mlflow
            return _mlflow.get_trace(trace_id)

        executor = _futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = executor.submit(_fetch)
            try:
                trace = fut.result(timeout=_GET_TRACE_TIMEOUT_S)
            except _futures.TimeoutError:
                executor.shutdown(wait=False, cancel_futures=True)
                if fmt == "json":
                    return JSONResponse({"error": unavailable}, status_code=200)
                return HTMLResponse(_render_trace_detail(trace_id, None, unavailable))
            except Exception as exc:
                executor.shutdown(wait=False, cancel_futures=True)
                if fmt == "json":
                    return JSONResponse({"error": str(exc)}, status_code=404)
                return HTMLResponse(_render_trace_detail(trace_id, None, str(exc)))
        finally:
            # Non-blocking shutdown — never wait on a possibly-hung worker.
            executor.shutdown(wait=False)

        if trace is None:
            if fmt == "json":
                return JSONResponse({"error": "not found"}, status_code=404)
            return HTMLResponse(_render_trace_detail(trace_id, None, "Trace not found"))

        # 3) Success — serve AND opportunistically populate the buffer so the
        #    next view (and the FEVM path) hits memory.
        span_dicts = _serialize_trace_spans(trace)
        if span_dicts:
            _ts_put(trace_id, span_dicts)
        if fmt == "json":
            return {"trace_id": trace_id, "spans": span_dicts}
        return HTMLResponse(_render_trace_detail(trace_id, span_dicts, None))
