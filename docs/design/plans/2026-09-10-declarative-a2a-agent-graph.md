# Declarative A2A agent graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a named APX capability leaf work as a required internal A2A stage in an existing declared graph, with OBO identity and one real MLflow trace tree across the hop. All normal ingress routes execute the same declared root; generated and customer-facing material shows logical agent names, never transport details.

**Architecture:** Preserve the existing public graph language: `Agent` / `DataAgent` / `CoworkerAgent` leaves plus `RouterAgent`, `KeywordRouter`, `SequentialAgent`, `ParallelAgent`, `LoopAgent`, `HandoffAgent`, and `agent_tool`. A small `AgentConfig.bindings` map resolves a logical leaf name to an A2A card URL during `finalize_agent()`. The resolved map is private state on the root and flows through the existing `CompileContext`; `_compile_any()` emits one private remote node for a bound leaf. `RemoteDatabricksAgent` remains the shared internal transport and compatibility API. Shared request-tracing scopes continue MLflow context at every inbound protocol boundary, including the lifetime of an SSE generator.

**Tech Stack:** Python 3.11+, FastAPI, LangGraph, MLflow distributed tracing, Databricks Apps OBO forwarding, `httpx.ASGITransport`, pytest, Ctk reality tests, `uv --frozen`.

**Spec:** [Declarative role pipelines](../declarative-role-pipelines.md)

## Global Constraints

- Do not add a public `Pipeline`, `AgentRole`, coordinator class, second compiler, or endpoint-specific workflow layer.
- The declared root graph is authoritative for `/invocations`, `/responses`, A2A `message/send`, and root chat. A direct answer is a terminal branch in that graph, not a separate coordinator endpoint.
- Normal generated source, topology, progress, and UI labels use only logical names such as `router`, `data`, and `pricing`. They never show remote card URLs, OBO headers, credentials, HTTP/A2A protocol, or `RemoteDatabricksAgent`.
- Preserve public `RemoteDatabricksAgent`, `agent_tool(remote)`, and `sub_agents=[url]` for compatibility. Named binding is the normal deterministic graph path, not a breaking replacement.
- Bindings resolve at finalization and fail closed for blank, unknown, ambiguous, or unsafe values. A required stage must raise rather than degrade into optional-tool error text.
- Use the existing remote transport for trusted-origin checks, OBO forwarding, SDK-versus-HTTP choice, and MLflow header injection. Do not copy that logic into the compiler.
- A complete trace promise needs both distributed context and one shared configured MLflow experiment/trace location for every independently deployed app in the composed workflow. The experiment stays governed deployment configuration, never a caller-controlled header.
- First release supports bound leaves inside `SequentialAgent`, `ParallelAgent`, `RouterAgent`, and `KeywordRouter`. Reject a bound leaf used directly as a `LoopAgent` body or `HandoffAgent` peer: these require a later versioned A2A control-result protocol for `finish_loop` / `transfer_to_*`.
- Use neutral fictional names everywhere. Never put a private customer name/data in docs, examples, generated text, labels, or plan prose.
- Do not select a Databricks profile, deploy, mutate a workspace, or add dependencies. Local in-process proof is sufficient for this slice.
- Run focused checks from `python/` with `uv run --frozen pytest` and the explicit test nodes named in each task. Run `make check` only when implementation is complete. Do not skip or weaken tests.

## File map

- Create: `python/tests/test_remote_leaf_bindings.py` — binding validation and topology opacity.
- Create: `python/tests/test_declared_a2a_binding_reality_ctk.py` — in-process standard-ingress proof of declared routing, OBO, one shared trace, and logical labels.
- Modify: `python/src/apx_agent/_models.py` — minimal `bindings: dict[str, str]` on `AgentConfig`.
- Modify: `python/src/apx_agent/_wiring.py` — resolve and attach private bindings in `finalize_agent()`.
- Modify: `python/src/apx_agent/_remote.py` — one header-based private invocation path; outbound MLflow header injection.
- Modify: `python/src/apx_agent/_agent_tool.py` — remove its fake FastAPI request shim by using the private transport path.
- Modify: `python/src/apx_agent/_compile.py` — private binding map in `CompileContext` and one bound-leaf compiler node.
- Modify: `python/src/apx_agent/_invocations.py`, `python/src/apx_agent/_a2a.py`, and `python/src/apx_agent/_responses_agent.py` — inbound context continuation, including stream lifetime.
- Modify: relevant existing compiler, remote, MLflow, invocation, A2A, and project-generator tests.
- Modify after proof: `docs/agents/composition.md`, `docs/multi-agent/overview.md`, `docs/multi-agent/a2a.md`, and `docs/running/tracing.md`.

---

### Task 1: Declare a private name-to-A2A binding at the common finalization seam

**Files:**
- Modify: `python/src/apx_agent/_models.py`
- Modify: `python/src/apx_agent/_wiring.py`
- Modify: `python/src/apx_agent/_remote.py`
- Create: `python/tests/test_remote_leaf_bindings.py`

**Interfaces:**
- Consumes: `AgentConfig`, `finalize_agent()`, `resolve_env_var()`, existing composition child fields, and `build_topology()`.
- Produces: private root attribute `_apx_remote_leaf_bindings: Mapping[str, _RemoteLeafBinding]`. No public binding model or compiler parameter.

- [ ] **Step 1: Write failing startup and opacity tests.**

Create a logical graph with neutral names:

~~~python
pricing = Agent(name="pricing", description="Produces an approved price.")
root = SequentialAgent([Agent(name="data"), pricing], name="review")
config = AgentConfig(
    name="research-assistant",
    bindings={"pricing": "$PRICING_APP_URL"},
)
~~~

With `PRICING_APP_URL` set to a card URL, assert that `finalize_agent(root, config)` creates a mapping containing the resolved URL while leaving `pricing._name == "pricing"`. Call finalization twice and assert idempotence. Add failures for an unknown key, an unset/blank reference, duplicate logical leaf names, and a malformed card URL. Keep a local in-process URL valid for the ASGI proof.

Add direct graph-boundary cases:

~~~python
with pytest.raises(ValueError, match="remote loop completion requires an A2A control protocol"):
    finalize_agent(LoopAgent(pricing), config)

with pytest.raises(ValueError, match="remote handoff requires an A2A control protocol"):
    finalize_agent(HandoffAgent(agents=[triage, pricing]), config)
~~~

After valid finalization, assert `build_topology(root)` contains `pricing` but neither the card URL nor a transport implementation name.

- [ ] **Step 2: Prove the test initially fails.**

Run:

~~~bash
cd python && uv run --frozen pytest tests/test_remote_leaf_bindings.py -q
~~~

Expected: `AgentConfig` has no binding field and no finalization wiring.

- [ ] **Step 3: Add only the data needed for named bindings.**

Add to `AgentConfig` beside `sub_agents`:

~~~python
bindings: dict[str, str] = Field(default_factory=dict)
"""Logical leaf name to remote A2A card URL or `$ENV_VAR` reference."""
~~~

Define the following non-exported record in `_remote.py`:

~~~python
@dataclass(frozen=True)
class _RemoteLeafBinding:
    logical_name: str
    card_url: str
~~~

Do not add a public role enum, transport configuration hierarchy, or replacement leaf class.

- [ ] **Step 4: Resolve mappings once in `finalize_agent()`.**

Add a private `_apply_remote_leaf_bindings(root, bindings)` helper to `_wiring.py`, invoked after the regular config has been applied and before cards/graphs are compiled. It must:

1. Walk the actual existing shapes only: `SequentialAgent._agents`, `ParallelAgent._agents`, `RouterAgent._routes`, `KeywordRouter._branches` / `_default`, `LoopAgent._inner`, and `HandoffAgent._agents`.
2. Collect named capability leaves and their immediate control position.
3. Resolve references through the existing environment helper and reject missing/blank values.
4. Require exactly one matching logical leaf per binding key.
5. Reject a loop body or handoff peer before serving, with an explicit control-protocol error.
6. Store a private immutable mapping on the root without replacing logical leaf objects.

The implementation must preserve names/descriptions used by existing router and handoff compilers.

- [ ] **Step 5: Re-run and commit.**

Run:

~~~bash
cd python && uv run --frozen pytest tests/test_remote_leaf_bindings.py -q
~~~

Expected: PASS; startup rejects unsupported placements before an app route exists.

Commit:

~~~bash
git add python/src/apx_agent/_models.py python/src/apx_agent/_wiring.py \
  python/src/apx_agent/_remote.py python/tests/test_remote_leaf_bindings.py
git commit -m "feat: bind declared agent leaves to A2A"
~~~

---

### Task 2: Compile bound logical leaves through the one existing graph dispatcher

**Files:**
- Modify: `python/src/apx_agent/_compile.py`
- Modify: `python/tests/test_compile.py`
- Modify: `python/tests/test_compile_advanced_agents.py`

**Interfaces:**
- Consumes: `CompileContext.headers`, `_RemoteLeafBinding`, current LangChain/APX message conversion helpers.
- Produces: `_compile_bound_remote_leaf(logical_leaf, binding, ctx)` and a defaulted private binding map in `CompileContext`.

- [ ] **Step 1: Add failing compiler behavior tests.**

Patch only the private remote transport method; do not make a network call. Parameterize the already generic composition paths:

~~~python
@pytest.mark.parametrize("root_factory", [
    lambda remote: SequentialAgent([remote], name="sequence"),
    lambda remote: ParallelAgent([Agent(name="local"), remote]),
    lambda remote: RouterAgent(agents=[remote]),
    lambda remote: KeywordRouter(
        branches=[("price", remote, ["price"])],
        default=Agent(name="local"),
    ),
])
def test_bound_leaf_compiles_as_its_logical_name(monkeypatch, root_factory):
    pricing = Agent(name="pricing", description="Returns an approved price.")
    root = root_factory(pricing)
    root._apx_remote_leaf_bindings = {"pricing": _binding("pricing")}
    monkeypatch.setattr(
        RemoteDatabricksAgent,
        "_run_with_incoming_headers",
        _returning("approved"),
    )

    graph = compile_to_langgraph(root, ws=None, model="test-model")
    result = graph.invoke({"messages": [HumanMessage(content="need a price")]})

    assert _last_text(result) == "approved"
    assert "pricing" in _graph_node_names(graph)
    assert "RemoteDatabricksAgent" not in _graph_node_names(graph)
~~~

Add a transport exception test that asserts `graph.invoke()` raises. Do not accept the optional dynamic-tool `sub-agent unreachable` string on this path.

- [ ] **Step 2: Confirm current compiler behavior fails.**

Run:

~~~bash
cd python && uv run --frozen pytest \
  tests/test_compile.py tests/test_compile_advanced_agents.py -q
~~~

Expected: the standard compiler has no bound-leaf dispatch.

- [ ] **Step 3: Carry private binding state through `CompileContext`.**

Add a defaulted field:

~~~python
remote_leaf_bindings: Mapping[str, _RemoteLeafBinding] = field(default_factory=dict)
~~~

At the existing `CompileContext` construction in `compile_to_langgraph()`, set:

~~~python
remote_leaf_bindings=getattr(agent, "_apx_remote_leaf_bindings", {}),
~~~

Keep `compile_to_langgraph()` public arguments unchanged. Update any direct internal `CompileContext(...)` construction only as required by the new default/type check.

- [ ] **Step 4: Add one private compiler node before normal `LlmAgent` dispatch.**

At the start of `_compile_any()`:

~~~python
binding = ctx.remote_leaf_bindings.get(getattr(agent, "_name", None))
if binding is not None:
    return _compile_bound_remote_leaf(agent, binding, ctx)
~~~

The private node must convert accumulated LangChain messages with existing helpers, construct/use `RemoteDatabricksAgent` internally from `binding.card_url`, call its private header-based method with `ctx.headers`, and return exactly:

~~~python
{"messages": [AIMessage(content=result)]}
~~~

Let required remote errors propagate. Do not create a new message serializer, graph family, or per-composition adapter: `SequentialAgent`, `ParallelAgent`, `RouterAgent`, and `KeywordRouter` already recurse through `_compile_any()`.

- [ ] **Step 5: Re-run and commit.**

Run:

~~~bash
cd python && uv run --frozen pytest \
  tests/test_compile.py tests/test_compile_advanced_agents.py \
  tests/test_remote_leaf_bindings.py -q
~~~

Commit:

~~~bash
git add python/src/apx_agent/_compile.py \
  python/tests/test_compile.py python/tests/test_compile_advanced_agents.py
git commit -m "feat: compile named A2A agent bindings"
~~~

---

### Task 3: Make existing remote transport own OBO and outbound MLflow propagation

**Files:**
- Modify: `python/src/apx_agent/_remote.py`
- Modify: `python/src/apx_agent/_agent_tool.py`
- Modify: `python/tests/test_remote.py`
- Modify: `python/tests/test_mlflow_tracing.py`

**Interfaces:**
- Consumes: public `RemoteDatabricksAgent.run()` / `stream()`, `_obo_headers()`, `_correlation_headers()`, `inject_tracing_headers()`, and `remote_agent_tool()`.
- Produces: private `_run_with_incoming_headers(messages, incoming_headers)`. The public `run()` delegates to it.

- [ ] **Step 1: Write failing transport tests.**

Patch SDK and HTTP invocation methods. Assert both receive the same correlation + MLflow headers and the HTTP path preserves a local opaque OBO sentinel without logging it. Assert `remote_agent_tool()` calls the private method directly and no longer needs a fake `Request` / `SimpleNamespace` shim.

Extend distributed-tracing tests with a real MLflow primitive proof:

~~~python
with mlflow.start_span("sender") as sender:
    headers = inject_tracing_headers({})

with continue_trace_from_headers(headers):
    with safe_span("receiver") as receiver:
        assert receiver.trace_id == sender.trace_id
        assert receiver.parent_id == sender.span_id
~~~

The sender must be closed before the receiver context starts; this prevents ambient in-process context from making a broken propagation test pass.

- [ ] **Step 2: Run focused tests.**

~~~bash
cd python && uv run --frozen pytest \
  tests/test_remote.py tests/test_mlflow_tracing.py -q
~~~

Expected: the current transport has split request/header handling and emits only legacy correlation fields.

- [ ] **Step 3: Use one private header-based transport method.**

Refactor public `run(messages, request)` to delegate to:

~~~python
async def _run_with_incoming_headers(
    self,
    messages: list[Message],
    incoming_headers: Mapping[str, str],
) -> str:
    ...
~~~

That method owns current initialization, long-task handling, SDK choice, HTTP fallback, and trusted-origin OBO behavior. Refactor the private `_obo_headers()` helper to accept a header mapping rather than a FastAPI `Request`; public `run()` and `stream()` pass `request.headers`, so their public signatures remain unchanged. Keep `_correlation_headers()` as the one outbound call site. The merged upstream helper commit already provides `inject_tracing_headers()`; wire it here rather than adding another propagation utility. After constructing its legacy fallback fields call:

~~~python
inject_tracing_headers(headers)
~~~

Then read/stamp the actual emitted `traceparent` and retain the current caller label. The same returned headers feed SDK, direct HTTP, fallback `/invocations`, and stream paths.

In `_agent_tool.py`, pass its existing `forwarded` mapping to the private method. Preserve the dynamic-tool failure-to-text behavior; it is intentionally different from a declared required stage.

- [ ] **Step 4: Re-run and commit.**

~~~bash
cd python && uv run --frozen pytest \
  tests/test_remote.py tests/test_mlflow_tracing.py \
  tests/test_compile.py tests/test_compile_advanced_agents.py -q
~~~

Commit:

~~~bash
git add python/src/apx_agent/_remote.py python/src/apx_agent/_agent_tool.py \
  python/tests/test_remote.py python/tests/test_mlflow_tracing.py
git commit -m "feat: propagate trace context across remote agents"
~~~

---

### Task 4: Continue trace context at every ingress, including the complete stream lifetime

**Files:**
- Modify: `python/src/apx_agent/_invocations.py`
- Modify: `python/src/apx_agent/_a2a.py`
- Modify: `python/src/apx_agent/_responses_agent.py`
- Modify: `python/tests/test_invocations_route.py`
- Modify: `python/tests/test_a2a.py`
- Modify: `python/tests/test_mlflow_tracing.py`

**Interfaces:**
- Consumes: `continue_trace_from_headers()`, `safe_span()`, `stamp_caller_correlation()`, `make_async_stream()`, and the existing ResponsesAgent request-header accessor.
- Produces: a single private request-span helper and a stream wrapper that keeps context alive until consumption finishes.

- [ ] **Step 1: Add failing ingress and streaming tests.**

For real sender MLflow headers created under a sender span then closed, invoke:

- `POST /invocations`
- `POST /responses`
- A2A `POST /` with JSON-RPC `message/send`

Observe the inbound `safe_span` and assert direct MLflow parentage, not tag equality:

~~~python
assert inbound.trace_id == sender.trace_id
assert inbound.parent_id == sender.span_id
~~~

For both HTTP streams, make `predict_stream` / the Responses stream open `mlflow.start_span("stream-body")` immediately before its first yielded event. Exhaust the response and assert:

~~~python
assert stream_body.trace_id == request_span.trace_id
assert stream_body.parent_id == request_span.span_id
~~~

Retain no-header tests. A2A has no streaming implementation today, so do not invent one for this task.

- [ ] **Step 2: Confirm tests fail with the current tag-only behavior.**

~~~bash
cd python && uv run --frozen pytest \
  tests/test_invocations_route.py tests/test_a2a.py tests/test_mlflow_tracing.py -q
~~~

Expected: spans are separate roots, and a returned `StreamingResponse` closes its route context before its worker iterator captures context.

- [ ] **Step 3: Add one private inbound span scope.**

The merged upstream helper commit already provides `continue_trace_from_headers()` but does not install it at any ingress. In `_invocations.py`, add a private context manager that installs that existing helper before creating the request span:

~~~python
@contextmanager
def _inbound_request_span(headers, *, name, attributes):
    with continue_trace_from_headers(headers):
        with safe_span(name, span_type="CHAIN", attributes=dict(attributes)) as span:
            stamp_caller_correlation(span, headers)
            yield
~~~

Use it around non-stream `/invocations` and `/responses` execution. `asyncio.to_thread()` copies the active context, so retain the existing worker strategy.

For streams, do not scope tracing around the immediate `StreamingResponse` return. Wrap the async iterator itself:

~~~python
def _traced_stream(*, headers, name, attributes, stream_factory):
    async def iterator():
        with _inbound_request_span(headers, name=name, attributes=attributes):
            async for item in stream_factory():
                yield item
    return iterator()
~~~

Pass a copied header mapping and the existing `make_async_stream(...)(None)` iterator to this wrapper. It keeps both the continuation and request span alive until the SSE body ends.

Import and use the same private scope in `_a2a.py` around its existing `safe_span` work. In `_responses_agent.py`, retrieve its existing request headers and wrap both non-streaming and streaming agent execution; this supported direct/legacy path must not remain a trace root.

- [ ] **Step 4: Re-run and commit.**

~~~bash
cd python && uv run --frozen pytest \
  tests/test_invocations_route.py tests/test_a2a.py tests/test_mlflow_tracing.py -q
~~~

Commit:

~~~bash
git add python/src/apx_agent/_invocations.py python/src/apx_agent/_a2a.py \
  python/src/apx_agent/_responses_agent.py \
  python/tests/test_invocations_route.py python/tests/test_a2a.py \
  python/tests/test_mlflow_tracing.py
git commit -m "feat: continue MLflow traces at agent ingress"
~~~

---

### Task 5: Generate logical declarations and prove the full graph through standard ingress

**Files:**
- Modify: `python/src/apx_agent/_project_gen.py`
- Modify: `python/tests/test_project_gen.py`
- Create: `python/tests/test_declared_a2a_binding_reality_ctk.py`
- Modify: `python/tests/test_cross_agent_delegation_reality_ctk.py`

**Interfaces:**
- Consumes: generated `pyproject.toml`, normal logical leaf constructors, the three supported ingress protocols, shared MLflow experiment configuration, and `httpx.ASGITransport`.
- Produces: public authoring output with logical leaves only and one black-box Ctk proof of actual A2A parentage.

- [ ] **Step 1: Add failing generated-output tests.**

Give a neutral graph a binding:

~~~toml
[tool.apx.agent.bindings]
pricing = "$PRICING_APP_URL"
~~~

Assert generated `agent.py` contains `Agent(name="pricing", ...)`, contains no `RemoteDatabricksAgent` import, and contains neither the environment reference nor a URL. Keep the existing explicit legacy `type: remote` generator test as a compatibility assertion.

- [ ] **Step 2: Add the single high-value reality proof.**

Build on the existing two-App ASGI Ctk harness, not a second test server. The root graph is:

~~~text
RouterAgent
├── direct local data leaf
└── SequentialAgent(local review -> named pricing leaf -> internal A2A app)
~~~

Use a recording fake model and `httpx.ASGITransport`. For sender headers, open an MLflow sender span, get injected headers, then close the sender before making the receiver call. This prevents in-process context inheritance from masking a broken implementation.

Assert:

1. Direct route makes zero remote calls.
2. Review route invokes exactly the declared `pricing` stage once.
3. OBO reaches the remote tool boundary but not response text, topology, or labels.
4. `/invocations`, `/responses`, and A2A `message/send` all execute the same declared root and produce the expected result.
5. Remote request span has the caller request span as parent in the same trace.
6. Both apps are configured with the same local test experiment/trace location and the trace can be read back as one experiment trace.
7. Topology, card metadata, and progress show logical names only.

Use `ctk.claim_vs_reality` around the black-box assertion. Replace the existing tag-join success condition in `test_cross_agent_delegation_reality_ctk.py` with a parentage assertion, while retaining tag checks as audit compatibility coverage.

- [ ] **Step 3: Implement only generation serialization and re-run.**

Update `_build_pyproject()` to emit `[tool.apx.agent.bindings]`. Keep `_render_leaf()` on `agent` / `data` / `coworker` constructors and preserve `_render_remote_leaf()` as explicit legacy compatibility.

Run:

~~~bash
cd python && uv run --frozen pytest \
  tests/test_project_gen.py tests/test_declared_a2a_binding_reality_ctk.py \
  tests/test_cross_agent_delegation_reality_ctk.py -q
~~~

Expected: PASS with no network or workspace requirement.

- [ ] **Step 4: Commit the public path and proof.**

~~~bash
git add python/src/apx_agent/_project_gen.py python/tests/test_project_gen.py \
  python/tests/test_declared_a2a_binding_reality_ctk.py \
  python/tests/test_cross_agent_delegation_reality_ctk.py
git commit -m "test: prove declared A2A graph behavior"
~~~

---

### Task 6: Document the verified public model and run the real gate

**Files:**
- Modify: `docs/agents/composition.md`
- Modify: `docs/multi-agent/overview.md`
- Modify: `docs/multi-agent/a2a.md`
- Modify: `docs/running/tracing.md`

- [ ] **Step 1: Replace transport-first deterministic examples with the declared binding path.**

Document this neutral example:

~~~toml
[tool.apx.agent]
experiment = "/Shared/research-assistant"

[tool.apx.agent.bindings]
pricing = "$PRICING_APP_URL"
~~~

~~~python
data = Agent(name="data", description="Looks up governed account facts.")
pricing = Agent(name="pricing", description="Produces an approved price.")
review = SequentialAgent([data, pricing], name="review")
root = RouterAgent(agents=[data, review])
~~~

Explain only verified behavior: the graph is logical, transport is resolved internally, direct answers belong to the root graph, `agent_tool` is discretionary delegation, and legacy remote APIs are advanced compatibility paths. State the loop/handoff binding boundary plainly. Explain that every independently deployed app must use the shared configured MLflow experiment/trace location for a single persisted experiment trace.

- [ ] **Step 2: Run source hygiene and all gates.**

~~~bash
cd python && uv run --frozen pytest tests/test_public_example_hygiene.py -q
git diff --check
cd typescript && npm ci && npm run build
cd .. && make check
cd python && uv run --frozen pytest
~~~

Expected: the hygiene test passes and all relevant focused tests and repository gates pass. If an unrelated failure occurs, report its command/output separately and do not change unrelated code.

- [ ] **Step 3: Commit verified documentation.**

~~~bash
git add docs/agents/composition.md docs/multi-agent/overview.md \
  docs/multi-agent/a2a.md docs/running/tracing.md
git commit -m "docs: explain declared A2A agent graphs"
~~~

Report the local reality proof, exact gate results, shared-experiment requirement, and the deliberate loop/handoff boundary. Do not claim deployment or customer validation not performed.
