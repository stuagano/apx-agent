import type { Step } from './types'

interface Props {
  step: Step
}

const STEPS: { key: Step; label: string }[] = [
  { key: 'normalize', label: 'Normalize' },
  { key: 'search',    label: 'Search' },
  { key: 'evaluate',  label: 'Evaluate' },
  { key: 'decide',    label: 'Decide' },
]

const ORDER: Step[] = ['idle', 'normalize', 'search', 'evaluate', 'decide', 'done']

function stepIndex(s: Step): number {
  return ORDER.indexOf(s)
}

export function StepProgress({ step }: Props) {
  const currentIdx = stepIndex(step)

  return (
    <div className="flex items-center gap-1">
      {STEPS.map((s, i) => {
        const idx = stepIndex(s.key)
        const isDone = currentIdx > idx || step === 'done'
        const isActive = step === s.key
        return (
          <div key={s.key} className="flex items-center gap-1">
            {i > 0 && (
              <span className={`text-xs mx-1 ${isDone ? 'text-gray-500' : 'text-gray-700'}`}>›</span>
            )}
            <div className="flex items-center gap-1.5">
              <div
                className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                  isDone
                    ? 'bg-green-400'
                    : isActive
                    ? 'bg-accent animate-pulse'
                    : 'bg-gray-700'
                }`}
              />
              <span
                className={`text-xs font-medium ${
                  isDone
                    ? 'text-green-400'
                    : isActive
                    ? 'text-accent'
                    : 'text-gray-600'
                }`}
              >
                {s.label}
              </span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
