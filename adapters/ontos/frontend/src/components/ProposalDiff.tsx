import { useTranslation } from 'react-i18next'

interface ProposalDiffProps {
  preState: Record<string, string | null>
  proposedState: Record<string, string | null>
}

export default function ProposalDiff({ preState, proposedState }: ProposalDiffProps) {
  const { t } = useTranslation(['governance'])

  const allKeys = Array.from(
    new Set([...Object.keys(preState), ...Object.keys(proposedState)])
  ).sort()

  return (
    <div className="grid grid-cols-2 gap-4">
      {/* Before */}
      <div className="rounded-lg border border-red-200 dark:border-red-900/40 bg-red-50/50 dark:bg-red-950/20 p-4">
        <div className="text-xs font-medium uppercase text-red-600 dark:text-red-400 mb-3">
          {t('governance:current_state')}
        </div>
        <div className="space-y-2 font-mono text-sm">
          {allKeys.map((key) => {
            const val = preState[key]
            const changed = val !== proposedState[key]
            return (
              <div key={key} className={changed ? 'text-red-700 dark:text-red-300' : 'text-muted-foreground'}>
                <span className="text-muted-foreground">{key}:</span>{' '}
                {val ?? <span className="italic text-red-400">(not set)</span>}
              </div>
            )
          })}
        </div>
      </div>

      {/* After */}
      <div className="rounded-lg border border-green-200 dark:border-green-900/40 bg-green-50/50 dark:bg-green-950/20 p-4">
        <div className="text-xs font-medium uppercase text-green-600 dark:text-green-400 mb-3">
          {t('governance:proposed_state')}
        </div>
        <div className="space-y-2 font-mono text-sm">
          {allKeys.map((key) => {
            const val = proposedState[key]
            const changed = val !== preState[key]
            return (
              <div key={key} className={changed ? 'text-green-700 dark:text-green-300 font-medium' : 'text-muted-foreground'}>
                <span className="text-muted-foreground">{key}:</span>{' '}
                {val ?? <span className="italic text-muted-foreground">(removed)</span>}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
