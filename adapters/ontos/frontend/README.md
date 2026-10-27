# Governance — Frontend Integration

These React components integrate the Governance UI into an Ontos
(databrickslabs/ontos) fork. They consume the governance REST API mounted
by the backend package (`apx-ontos-governance`).

## Integration Steps

### 1. Copy files into your Ontos fork

```bash
cp -r frontend/src/* /path/to/ontos-fork/src/frontend/src/
```

This adds files — it does not overwrite any existing Ontos files.

### 2. Register features (one line)

In `config/features.ts`:

```typescript
import { governanceFeatures } from './features.governance'
export const features = [...existingFeatures, ...governanceFeatures]
```

### 3. Register routes (one line)

In `app.tsx`, inside the `/governance` route children:

```typescript
import governanceRoutes from './routes.governance'
children: [...existingGovernanceChildren, ...governanceRoutes]
```

### 4. Register translations (one line)

In `i18n/index.ts`:

```typescript
import governance from './en/governance.json'
// Add 'governance' to your i18n resource bundle
```

## Views

| File | Route | Purpose |
|------|-------|---------|
| `GovernanceDashboard.tsx` | `/governance/violations` | Violation summary cards + filterable table |
| `GovernanceResourceDetail.tsx` | `/governance/resources/:id` | Classifications / violations / exceptions tabs |
| `GovernancePolicies.tsx` | `/governance/policies` | Policy list with YAML-origin lock, create/edit dialog |
| `GovernanceExceptions.tsx` | `/governance/exceptions` | Active/expired toggle, revoke, bulk-revoke-expired |
| `RemediationDashboard.tsx` | `/governance/remediation` | Proposal funnel, agent effectiveness, reviewer load |
| `RemediationInbox.tsx` | `/governance/remediation/inbox` | Review queue: approve / reject / reassign proposals |

## Dependencies

These components use Ontos's existing UI primitives:

- `@/components/ui/*` — Card, Badge, Button, DataTable, Dialog, etc.
- `@/hooks/use-api` — API client hook
- `@/hooks/use-toast` — Toast notification hook
- `@/stores/breadcrumb-store` — Breadcrumb navigation
- `react-i18next` — Translations
- `lucide-react` — Icons

No additional npm dependencies are required.
