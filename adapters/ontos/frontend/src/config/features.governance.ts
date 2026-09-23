// In features.ts, add:
//   import governanceFeatures from './features.governance'
//   export const features = [...existingFeatures, ...governanceFeatures]

export const governanceFeatures = [
  {
    id: 'governance-violations',
    name: 'Violations',
    path: '/governance',
    description: 'Active policy violations across all crawled resources',
    icon: 'ShieldAlert',
    group: 'govern' as const,
    maturity: 'beta' as const,
  },
  {
    id: 'governance-policies',
    name: 'Governance Policies',
    path: '/governance/policies',
    description: 'Author and manage governance policies evaluated by the Governance Monitoring scanner',
    icon: 'FileText',
    group: 'govern' as const,
    maturity: 'beta' as const,
  },
  {
    id: 'governance-exceptions',
    name: 'Exceptions',
    path: '/governance/exceptions',
    description: 'Review, approve, and revoke policy exceptions',
    icon: 'ShieldOff',
    group: 'govern' as const,
    maturity: 'beta' as const,
  },
  {
    id: 'governance-remediation',
    name: 'Remediation Review',
    path: '/governance/remediation',
    description: 'Review and approve AI-generated remediation proposals',
    icon: 'Wrench',
    group: 'govern' as const,
    maturity: 'beta' as const,
  },
]
