import { useState, useEffect, useRef } from 'react'
import type { Message } from './types'

interface Props {
  messages: Message[]
  isLoading: boolean
  onSend: (text: string) => void
  pendingMessage: string
}

export function ChatPanel({ messages, isLoading, onSend, pendingMessage }: Props) {
  const [input, setInput] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const lastSentRef = useRef('')

  // Auto-send when a new pendingMessage arrives from a row click
  useEffect(() => {
    if (pendingMessage && pendingMessage !== lastSentRef.current) {
      lastSentRef.current = pendingMessage
      onSend(pendingMessage)
    }
  }, [pendingMessage, onSend])

  // Scroll to bottom whenever messages update
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || isLoading) return
    setInput('')
    onSend(text)
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-auto p-4 space-y-3">
        {messages.length === 0 && (
          <p className="text-gray-500 text-sm text-center mt-8">
            Select a contract from the table, or type a question below.
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
                m.role === 'user' ? 'bg-accent text-white' : 'bg-row text-gray-200'
              }`}
            >
              {m.content}
            </div>
          </div>
        ))}
        {isLoading && (
          <div className="flex justify-start">
            <div className="bg-row text-gray-400 rounded-lg px-3 py-2 text-sm animate-pulse">
              Thinking…
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <form onSubmit={handleSubmit} className="flex gap-2 p-3 border-t border-row">
        <input
          type="text"
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Ask about the contracts…"
          disabled={isLoading}
          className="flex-1 bg-surface border border-row rounded px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-accent disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={isLoading || !input.trim()}
          className="bg-accent hover:bg-orange-600 disabled:opacity-40 text-white px-4 py-2 rounded text-sm font-medium transition-colors"
        >
          Send
        </button>
      </form>
    </div>
  )
}
