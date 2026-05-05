import type { Contract } from './types'

interface Props {
  contracts: Contract[]
  selected: Contract | null
  onSelect: (c: Contract) => void
  error: string | null
  loading: boolean
}

export function ContractTable({ contracts, selected, onSelect, error, loading }: Props) {
  if (loading) {
    return (
      <div className="flex-1 overflow-auto p-4">
        <p className="text-gray-500 text-sm animate-pulse">Loading contracts…</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex-1 overflow-auto p-4">
        <p className="text-red-400 text-sm">Failed to load contracts: {error}</p>
      </div>
    )
  }

  if (contracts.length === 0) {
    return (
      <div className="flex-1 overflow-auto p-4">
        <p className="text-gray-500 text-sm">No contracts found.</p>
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-auto">
      <table className="w-full text-sm text-left border-collapse">
        <thead className="sticky top-0 z-10 bg-panel text-gray-300 text-xs uppercase">
          <tr>
            <th className="px-3 py-2 font-medium">ID</th>
            <th className="px-3 py-2 font-medium">Counterparty</th>
            <th className="px-3 py-2 font-medium">Type</th>
            <th className="px-3 py-2 font-medium">Effective</th>
            <th className="px-3 py-2 font-medium">Expires</th>
            <th className="px-3 py-2 font-medium">Yrs</th>
            <th className="px-3 py-2 font-medium">Auto-renew</th>
          </tr>
        </thead>
        <tbody>
          {contracts.map(c => (
            <tr
              key={c.contract_id}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(c)}
              onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && onSelect(c)}
              aria-selected={selected?.contract_id === c.contract_id}
              className={`cursor-pointer border-b border-row transition-colors hover:bg-[#334d72] ${
                selected?.contract_id === c.contract_id ? 'bg-selected ring-1 ring-accent ring-inset' : ''
              }`}
            >
              <td className="px-3 py-2 font-mono text-xs text-gray-400">{c.contract_id}</td>
              <td className="px-3 py-2 text-gray-100">{c.counterparty}</td>
              <td className="px-3 py-2 text-gray-300">{c.contract_type}</td>
              <td className="px-3 py-2 text-gray-400">{c.effective_date ?? '—'}</td>
              <td className="px-3 py-2 text-gray-400">{c.expiry_date ?? '—'}</td>
              <td className="px-3 py-2 text-gray-400 text-center">{c.term_years ?? '—'}</td>
              <td className="px-3 py-2">
                {c.auto_renewal == null ? (
                  <span className="text-gray-500">—</span>
                ) : c.auto_renewal ? (
                  <span className="bg-green-900 text-green-300 text-xs px-2 py-0.5 rounded">
                    Yes
                  </span>
                ) : (
                  <span className="bg-row text-gray-400 text-xs px-2 py-0.5 rounded">No</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
