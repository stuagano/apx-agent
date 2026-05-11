import { useState } from 'react'
import type { Scenario } from './types'

const SCENARIOS: Scenario[] = [
  {
    label: 'Jane Smith',
    subLabel: '123 Maple Ave · DEN-001234',
    tag: 'EXACT',
    tagColor: 'green',
    name: 'Jane Smith',
    address: '123 Maple Ave Denver CO',
    accountNumber: 'DEN-001234',
  },
  {
    label: 'John Smith',
    subLabel: '123 Maple Ave · same address',
    tag: 'FAMILIAL',
    tagColor: 'yellow',
    name: 'John Smith',
    address: '123 Maple Ave Denver CO',
    accountNumber: '',
  },
  {
    label: 'Liz Rodriguez',
    subLabel: '456 Oak St · Elizabeth on record',
    tag: 'NICKNAME',
    tagColor: 'blue',
    name: 'Liz Rodriguez',
    address: '456 Oak Street Aurora CO',
    accountNumber: '',
  },
  {
    label: "M. O'Brien",
    subLabel: '567 Birch Ln · initials → SQL',
    tag: 'SQL PATH',
    tagColor: 'purple',
    name: "M. O'Brien",
    address: '567 Birch Lane Westminster CO',
    accountNumber: '',
  },
]

const TAG_CLASSES: Record<Scenario['tagColor'], string> = {
  green:  'bg-green-900/50 text-green-400',
  yellow: 'bg-amber-900/50 text-amber-400',
  blue:   'bg-blue-900/50 text-blue-400',
  purple: 'bg-purple-900/50 text-purple-400',
}

interface Props {
  onRun: (name: string, address: string, accountNumber: string) => void
  isRunning: boolean
}

export function InputPanel({ onRun, isRunning }: Props) {
  const [name, setName] = useState('')
  const [address, setAddress] = useState('')
  const [accountNumber, setAccountNumber] = useState('')
  const [activeIdx, setActiveIdx] = useState<number | null>(null)

  function selectScenario(idx: number) {
    const s = SCENARIOS[idx]
    setActiveIdx(idx)
    setName(s.name)
    setAddress(s.address)
    setAccountNumber(s.accountNumber)
    onRun(s.name, s.address, s.accountNumber)
  }

  function handleRun() {
    if (name.trim()) onRun(name.trim(), address.trim(), accountNumber.trim())
  }

  return (
    <div className="flex flex-col gap-3 p-3.5 overflow-y-auto">
      <div className="flex flex-col gap-2">
        {SCENARIOS.map((s, i) => (
          <button
            key={i}
            onClick={() => selectScenario(i)}
            disabled={isRunning}
            className={`text-left rounded-md border px-3 py-2 transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
              activeIdx === i
                ? 'border-accent bg-orange-950/30'
                : 'border-row bg-panel hover:bg-row'
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-gray-200">{s.label}</span>
              <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${TAG_CLASSES[s.tagColor]}`}>
                {s.tag}
              </span>
            </div>
            <div className="text-[11px] text-gray-500 mt-0.5">{s.subLabel}</div>
          </button>
        ))}
      </div>

      <div className="border-t border-row pt-3">
        <div className="text-[10px] uppercase tracking-widest text-gray-600 mb-2">or enter manually</div>
        <div className="flex flex-col gap-2">
          <div className="flex flex-col gap-1">
            <label className="text-[10px] text-gray-500">Applicant Name</label>
            <input
              type="text"
              value={name}
              onChange={e => { setName(e.target.value); setActiveIdx(null) }}
              className="bg-surface border border-row rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-accent"
              placeholder="Jane Smith"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[10px] text-gray-500">Service Address</label>
            <input
              type="text"
              value={address}
              onChange={e => { setAddress(e.target.value); setActiveIdx(null) }}
              className="bg-surface border border-row rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-accent"
              placeholder="123 Maple Ave Denver CO"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[10px] text-gray-500">Account Number</label>
            <input
              type="text"
              value={accountNumber}
              onChange={e => { setAccountNumber(e.target.value); setActiveIdx(null) }}
              className="bg-surface border border-row rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-accent"
              placeholder="DEN-001234"
            />
          </div>
          <button
            onClick={handleRun}
            disabled={isRunning || !name.trim()}
            className="mt-1 rounded-md bg-accent text-white py-2 text-sm font-semibold hover:bg-orange-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {isRunning ? 'Running…' : 'Run Match →'}
          </button>
        </div>
      </div>
    </div>
  )
}
