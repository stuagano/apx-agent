# Identity attributes and remote-agent ABAC

Capability record and supported contract for Databricks identity attributes
across APX remote-agent (A2A) delegation. This is the identity-attribute
counterpart to the [native service policy capability
record](service-policies-native-capabilities.md): it freezes what APX claims,
what CI proves, and what remains unverified rather than invented.

## Platform facts (Beta)

Identity attributes are nine IdP-sourced fields that Databricks stores on an
account user: `title`, `userType`, `locality`, `region`, `country`,
`costCenter`, `organization`, `division`, and `department`. Account admins
provision them so Unity Catalog ABAC policies can reference them.

- The feature is in Beta and is enabled from the account console **Previews**
  page.
- Attributes are supported on **users only**. Service principals and groups
  are not supported.
- Values are provisioned from the identity provider (automatic identity
  management or account SCIM 2.1). The platform, not APX, owns provisioning
  and synchronization.
- The attribute control list that governs which attributes may be written is
  configurable only in the account console. There is no API for it.
- Read paths: account admins read any user through account SCIM
  `GET /Users/<id>`; any user can read their own attributes through the
  workspace-level `/api/2.0/account/scim/v2/Me` endpoint (read-only).
- In Beta, UC ABAC policies consume identity attributes for column masking.
  The UC ABAC policy functions that consume identity attributes are delivered
  separately from the identity-attributes preview itself.
- UC evaluates policies for signed-in users against recently synced attribute
  values.

## The APX contract

When an APX `RemoteDatabricksAgent` call crosses an app boundary (A → B):

1. Unity Catalog evaluates identity attributes from the authenticated user
   identity — the caller's OBO credential that APX already forwards after the
   trusted-origin gate. APX must not copy, synthesize, or accept identity
   attributes as request headers. UC reads attributes from the evaluated user,
   not from the wire.
2. The contract applies only to a user-scoped tool or data operation that
   reaches the remote application with a real user credential.
3. The contract does not apply to the remote application's own FMAPI/model
   calls (those run as the callee's service principal, see
   [app-to-app authentication](../multi-agent/a2a.md#app-to-app-authentication)),
   to service principals, or to groups. The platform does not evaluate
   identity attributes for those principals.
4. Attribute values must never appear in trace tags, audit metadata, logs, or
   prompts. APX correlation headers carry only `traceparent` and
   `x-apx-caller` (application identity), never user attribute values.
5. APX adds no `identity_attributes` declaration field and no client-side
   policy evaluator. The `service_policies.abac.tags` surface remains a
   resource-tag selector for service-policy attachment and is unrelated to
   user identity attributes (see [Service policies](service-policies.md#abac-status)).

## Verified now (CI)

`python/tests/test_identity_attribute_abac_ctk.py` proves the wire-hygiene
half of the contract in-process:

- `_obo_headers` forwards exactly `Authorization`,
  `X-Forwarded-Access-Token`, and `X-Forwarded-Host` after the trusted-origin
  gate; attribute-shaped inbound headers are dropped, and credentials are
  withheld entirely from untrusted origins.
- `_correlation_headers` carries no identity-attribute keys or values.
- An end-to-end A → B call whose inbound request carries attribute-shaped
  headers delivers only the OBO credential across the wire to B — the shaped
  headers never cross the boundary.

## Unverified: the live two-user outcome

The full capability proof — an allowed user and a denied user invoking the
same governed operation on the same identity-attribute-protected UC object
through a real A → B path — is **unverified** in the current environment:

- Attribute values are IdP-provisioned and the attribute control list is
  console-only, so a test cannot safely set up opposite-valued controlled
  users programmatically.
- The UC ABAC policy functions that consume identity attributes are delivered
  separately from the identity-attributes preview; their availability is
  account-dependent.
- The installed `databricks-sdk` 0.102.0 exposes no identity-attribute or UC
  ABAC policy client; read-back goes through account SCIM.

`python/tests/test_identity_attribute_abac_live_reality_ctk.py` encodes the
opt-in proof. It skips with an explicit unverified reason unless the operator
provides `APX_ABAC_WORKSPACE_HOST`, `APX_ABAC_ALLOWED_USER_TOKEN`,
`APX_ABAC_DENIED_USER_TOKEN`, and `APX_ABAC_REMOTE_CARD_URL` (plus an
optional `APX_ABAC_QUERY`), and it reports unverified — rather than passing —
when the account preview, attribute values, or policy state cannot be read
back. The proof fails, rather than passing, when both users receive the same
outcome.

Do not replace the skip with an invented fallback: no synthetic attribute
headers, no client-side policy evaluation, and no claim of group or
service-principal support.
