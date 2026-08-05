# Adversarial audit — discovery & multi-agent (2026-08-04)

Read-only pass after `v0.4.7`. Two parallel explorations of workspace/Apps
discovery and multi-agent / A2A / remote surfaces. Findings below are
**confirmed by code citation** (spot-checked against `main` @ `b9794e9`).

Thesis under attack: *declared, not wired* — and *identity passed through
every hop*. Discovery + multi-agent are where those claims meet Apps SSO,
shared process state, and remote URLs.

## Deliberately NOT auto-fixed — tracked as issues

### D1. Critical — Discover `wire-agent` accepts arbitrary URLs (credentialed SSRF + OBO exfil) — [#610](https://github.com/stuagano/apx-agent/issues/610)

Any signed-in App user can POST `/_apx/discover/wire-agent` with an
attacker URL. Hot-apply fetches the card and later Chat turns forward the
caller’s OBO bearer. Probe SSRF guards (`_validate_probe_url`) are **not**
applied on this path.

- `_dev.py` `discover_wire_agent` — no allowlist
- `_discover_hot.hot_apply_sub_agent` / `_agents` card fetch
- `_remote._trusted_origin` — if configured URL *is* evil, credentials are “trusted”

**Smallest fix:** Reuse `_validate_probe_url` + HTTPS Apps-host allowlist on
wire/hot-apply; `follow_redirects=False`; never forward OBO off allowlist.

### D2. Critical — Shared live-agent mutation (confused deputy) — [#611](https://github.com/stuagano/apx-agent/issues/611)

Discover hot-apply mutates the process-global `AgentContext` + `agent.py`
for **all** App users. Documented as intentional (`dev-ui.md`); any SSO
viewer can plant tools/peers that privileged users later invoke under their
OBO.

**Smallest fix:** Operator-only Discover writes, or per-principal overlays
that do not mutate shared `_tool_fns`.

### D3. High — Discover identity fail-open to App SP — [#612](https://github.com/stuagano/apx-agent/issues/612)

`_ws_prefer_obo` explicitly “never fail-closed” for browse endpoints —
diverges from G2 served-path fail-closed. Without OBO, inventory is App SP
grants while UI implies “this identity.”

**Smallest fix:** In Apps runtime, 401/empty when OBO absent for Discover
reads; SP fallback only for local `apx-agent run`.

### D4. High — `_probe_card` urllib + redirects + Bearer — [#613](https://github.com/stuagano/apx-agent/issues/613)

`_apps_discovery._probe_card` uses `urllib.request.urlopen` (follows
redirects; Authorization may be resent). No private-IP guard. Env
`APX_DISCOVER_APP_URLS` only requires `http` prefix.

**Smallest fix:** `httpx` `follow_redirects=False` + `_validate_probe_url`.

### M1. High — `card.url` trusted-suffix OBO redirect — [#614](https://github.com/stuagano/apx-agent/issues/614)

`_TRUSTED_HOST_SUFFIXES = (".databricksapps.com",)` lets a card retarget
any Apps host and still receive OBO. Codified as intended in
`test_remote.py` (`test_updates_base_url_from_card`).

**Smallest fix:** Same-origin only for `card.url`, or explicit
`from_app_name` allowlisted remap — drop open wildcard.

### M2. High — Client-spoofable `custom_inputs.user_id` — [#615](https://github.com/stuagano/apx-agent/issues/615)

Body `user_id` wins over `X-Forwarded-User` (`_obo.py`). Scopes session /
memory / approval principal without binding to token subject.

**Smallest fix:** In Apps, ignore client `user_id` when proxy headers present.

### M3. High — Config guardrails skip composition roots / not inherited — [#616](https://github.com/stuagano/apx-agent/issues/616)

`apply_config_guardrails` warns and skips on Sequential/Router/Handoff.
`agent_tool` children do not inherit parent `PolicyGate` / allowlists.

**Smallest fix:** Attach gates to every leaf `LlmAgent`; fail loud if root
guardrails cannot apply; document remote leaf policy.

### M4. Medium — A2A `tasks/get` IDOR — [#617](https://github.com/stuagano/apx-agent/issues/617)

`TaskStore` is global by task id; no principal ownership on `tasks/get`.

**Smallest fix:** Bind task → principal; reject mismatched callers.

## Medium / Low (compressed)

| ID | Sev | Summary | Issue |
|----|-----|---------|-------|
| D5 | Med | Docs/UI oversell user-scoped Discover vs SP-seeded setup (residual after #612) — **fixed:** Setup inventory + auto-prefill now read via OBO | [#627](https://github.com/stuagano/apx-agent/issues/627) |
| D6 | Med | Wire-tool plants UC/Genie/VS by client id; no wire-time grant check | [#628](https://github.com/stuagano/apx-agent/issues/628) |
| D7 | Med | Discover inventory GETs open to any OBO App user (recon; residual after #612) | [#629](https://github.com/stuagano/apx-agent/issues/629) |
| M5 | Med | A2A auth assumed at gateway, not in-process | [#631](https://github.com/stuagano/apx-agent/issues/631) |
| M6 | Med | Nested loops / truthy `max_iterations` cost DoS | [#632](https://github.com/stuagano/apx-agent/issues/632) |
| M7 | Med | Docs over-claim “identity every hop” vs callee FMAPI SP | [#633](https://github.com/stuagano/apx-agent/issues/633) |
| M8 | Med | Handoff compile drops specialist `description` | [#634](https://github.com/stuagano/apx-agent/issues/634) |
| D8 | Low | `binding_name` not `isidentifier()` | [#630](https://github.com/stuagano/apx-agent/issues/630) |
| M9 | Low | Empty Router/remote `agent_tool` descriptions | [#635](https://github.com/stuagano/apx-agent/issues/635) |
| M10 | Low | Sub-agent name collision advertises ≠ callable | [#636](https://github.com/stuagano/apx-agent/issues/636) |

## Explicitly reviewed — not defects (caveats)

- HTML `esc` on Discover names; factory RHS uses `repr`
- UC still enforces at tool call time for the *invoking* user
- `_trusted_origin` same-origin / scheme-downgrade for non-suffix hosts
- httpx card fetch in `_fetch_sub_agent_card` defaults `follow_redirects=False`
- Serving endpoints not wireable as tools
- Topology UC discover is read-only tag walk
- OBO hop forwarding when origin is not redirected (multi-hop CTK)
- G2 fail-closed for served tools without OBO
- LoopAgent / HandoffAgent constructor caps
- Long-task continuation (#604) — no local resume-token forgery surface
- Registry publish ownership (#464)

## Test gaps that leave the above unproven

- Wire-agent rejects private IPs / non-Apps hosts
- Discover fail-closed in Apps without OBO
- `_probe_card` redirect must not resend Authorization
- `card.url` to a *different* `*.databricksapps.com` must not get OBO
- Apps `X-Forwarded-User` beats body `user_id`
- Cross-user A2A `tasks/get`
- Root guardrails apply to Sequential / `agent_tool` leaves
- Handoff compile descriptions match `agent.description`

## Suggested patch order

1. Wire-agent + hot-apply URL allowlist / SSRF (D1)
2. `_probe_card` no-redirect + allowlist (D4)
3. Lock `card.url` trusted-suffix (M1)
4. Bind principal to proxy identity (M2)
5. Discover fail-closed in Apps (D3) + restrict who can hot-apply (D2)
6. Guardrails on composition leaves (M3)
7. A2A task ownership (M4)
8. Docs + Handoff description + loop bounds (D5, M7, M8, M6)
