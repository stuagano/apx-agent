// In app.tsx, add inside the /governance route children:
//   import governanceRoutes from './routes.governance'
//   children: [...existingGovernanceChildren, ...governanceRoutes]

import GovernanceDashboard      from '@/views/GovernanceDashboard'
import GovernanceResourceDetail from '@/views/GovernanceResourceDetail'
import GovernancePolicies       from '@/views/GovernancePolicies'
import GovernanceExceptions     from '@/views/GovernanceExceptions'
import RemediationDashboard   from '@/views/RemediationDashboard'
import RemediationInbox       from '@/views/RemediationInbox'

const governanceRoutes = [
  { path: 'violations',                      element: <GovernanceDashboard /> },
  { path: 'governance/resources/:resourceId', element: <GovernanceResourceDetail /> },
  { path: 'policies',                       element: <GovernancePolicies /> },
  { path: 'exceptions',                     element: <GovernanceExceptions /> },
  { path: 'remediation',                    element: <RemediationDashboard /> },
  { path: 'remediation/inbox',              element: <RemediationInbox /> },
]

export default governanceRoutes
