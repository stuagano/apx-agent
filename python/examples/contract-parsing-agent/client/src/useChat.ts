import { useState, useCallback } from 'react'
import type { Message } from './types'

export function useChat() {
  const [messages, setMessages] = useState<Message[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [threadId, setThreadId] = useState<string | null>(null)

  const reset = useCallback(() => {
    setMessages([])
    setThreadId(null)
  }, [])

  const sendMessage = useCallback(async (text: string) => {
    const userMsg: Message = { role: 'user', content: text }
    setMessages(prev => [...prev, userMsg])
    setIsLoading(true)
    try {
      const sessionId = threadId ?? crypto.randomUUID()
      setThreadId(sessionId)
      const resp = await fetch('/api/invocations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: crypto.randomUUID(),
          session_id: sessionId,
          input: { messages: [userMsg] },
        }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      if (data.status !== 'completed' || data.output?.status !== 'completed') {
        throw new Error('Agent invocation did not complete')
      }
      const outputText = data.output.messages
        .filter((message: Message) => message.role === 'assistant')
        .map((message: Message) => message.content).join('\n')
      if (!outputText) throw new Error('Agent returned no response')
      setMessages(prev => [...prev, { role: 'assistant', content: outputText }])
    } catch {
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: 'Sorry, something went wrong.' },
      ])
    } finally {
      setIsLoading(false)
    }
  }, [threadId])

  return { messages, isLoading, sendMessage, threadId, reset }
}
