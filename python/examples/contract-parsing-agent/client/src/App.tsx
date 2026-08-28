import { useState, useEffect, useCallback, useRef } from 'react'
import type { Contract } from './types'
import { ContractTable } from './ContractTable'
import { ChatPanel } from './ChatPanel'
import { useChat } from './useChat'
import { useUpload } from './useUpload'
import { DevToolbar } from './DevToolbar'

export default function App() {
  const [contracts, setContracts] = useState<Contract[]>([])
  const [contractsError, setContractsError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<Contract | null>(null)
  const [pendingMessage, setPendingMessage] = useState('')
  const [devEnabled, setDevEnabled] = useState(true)
  const { messages, isLoading, sendMessage, threadId, reset } = useChat()
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleUploadSuccess = useCallback((filename: string, volumePath: string) => {
    const msg = `I just uploaded "${filename}" (volume_path: ${volumePath}). Please extract it and compare the terms to any existing contracts from the same counterparty.`
    setPendingMessage(msg)
  }, [])

  const { status: uploadStatus, upload } = useUpload(handleUploadSuccess)

  const handleUploadClick = useCallback(() => {
    fileInputRef.current?.click()
  }, [])

  const handleFileChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      upload(file)
      e.target.value = ''
    }
  }, [upload])

  useEffect(() => {
    fetch('/api/contracts')
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json() as Promise<Contract[]>
      })
      .then(setContracts)
      .catch((e: Error) => setContractsError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    fetch('/api/dev-ui')
      .then(r => r.ok ? r.json() as Promise<{ enabled: boolean }> : null)
      .then(config => {
        if (config?.enabled === false) setDevEnabled(false)
      })
      .catch(() => undefined)
  }, [])

  const handleSelectContract = useCallback((c: Contract) => {
    setSelected(c)
    const msg = `Tell me about ${c.contract_id} — ${c.counterparty} (${c.contract_type}, expires ${c.expiry_date ?? 'unknown'}) [${Date.now()}]`
    setPendingMessage(msg)
  }, [])

  return (
    <div className="flex h-screen bg-surface text-gray-200 overflow-hidden">
      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".pdf"
        className="hidden"
        onChange={handleFileChange}
      />

      {/* Left panel — contract table */}
      <div className="w-[58%] flex flex-col border-r border-row">
        <div className="px-4 py-3 border-b border-row shrink-0 flex items-center justify-between">
          <h1 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
            Contract Portfolio
          </h1>
          <div className="flex flex-col items-end gap-1">
            <button
              onClick={handleUploadClick}
              disabled={uploadStatus === 'uploading'}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded bg-accent text-white hover:bg-orange-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {uploadStatus === 'uploading' ? (
                <>
                  <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                  </svg>
                  Uploading…
                </>
              ) : (
                <>
                  <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                  </svg>
                  Upload Contract
                </>
              )}
            </button>
            {uploadStatus === 'error' && (
              <span className="text-xs text-red-400">Upload failed — PDF only, max 10 MB</span>
            )}
          </div>
        </div>
        <ContractTable
          contracts={contracts}
          selected={selected}
          onSelect={handleSelectContract}
          error={contractsError}
          loading={loading}
        />
      </div>

      {/* Right panel — agent chat */}
      <div className="w-[42%] flex flex-col bg-panel">
        <div className="px-4 py-3 border-b border-row shrink-0">
          <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
            Agent Chat
          </h2>
        </div>
        <ChatPanel
          messages={messages}
          isLoading={isLoading}
          onSend={sendMessage}
          pendingMessage={pendingMessage}
        />
      </div>
      {devEnabled && (
        <DevToolbar threadId={threadId} onReset={reset} />
      )}
    </div>
  )
}
