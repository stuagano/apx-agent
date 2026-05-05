import { useState, useRef, useEffect } from 'react'
import type { Message } from './types'

interface Props {
  messages: Message[]
  isLoading: boolean
  onSend: (text: string) => void
  onReset: () => void
}

export function ChatPanel({ messages, isLoading, onSend, onReset }: Props) {
  const [input, setInput] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || isLoading) return
    setInput('')
    onSend(text)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', maxWidth: 720, margin: '0 auto', padding: '0 16px' }}>
      <div style={{ padding: '16px 0', borderBottom: '1px solid #eee', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Agent Builder</h1>
        {messages.length > 0 && (
          <button onClick={onReset} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#888', fontSize: 13 }}>
            New agent
          </button>
        )}
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '24px 0' }}>
        {messages.length === 0 && (
          <p style={{ color: '#888', textAlign: 'center', marginTop: 80 }}>
            Tell me what you want your agent to do.
          </p>
        )}
        {messages.map((m) => (
          <div key={m.id} style={{ marginBottom: 16, textAlign: m.role === 'user' ? 'right' : 'left' }}>
            <span style={{
              display: 'inline-block',
              padding: '8px 14px',
              borderRadius: 12,
              background: m.role === 'user' ? '#0066ff' : '#f0f0f0',
              color: m.role === 'user' ? '#fff' : '#000',
              maxWidth: '80%',
              whiteSpace: 'pre-wrap',
              fontSize: 14,
              lineHeight: 1.5,
            }}>
              {m.content}
            </span>
          </div>
        ))}
        {isLoading && (
          <div style={{ color: '#888', fontSize: 13, padding: '4px 0' }}>Thinking...</div>
        )}
        <div ref={bottomRef} />
      </div>

      <form onSubmit={handleSubmit} style={{ padding: '16px 0', borderTop: '1px solid #eee', display: 'flex', gap: 8 }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Describe your agent..."
          disabled={isLoading}
          autoFocus
          style={{ flex: 1, padding: '8px 12px', borderRadius: 8, border: '1px solid #ddd', fontSize: 14 }}
        />
        <button
          type="submit"
          disabled={isLoading || !input.trim()}
          style={{ padding: '8px 16px', background: '#0066ff', color: '#fff', border: 'none', borderRadius: 8, cursor: 'pointer', fontSize: 14 }}
        >
          Send
        </button>
      </form>
    </div>
  )
}
