import { useState, useCallback } from 'react'
import type { Message } from './types'

export function useChat() {
  const [messages, setMessages] = useState<Message[]>([])
  const [isLoading, setIsLoading] = useState(false)

  const sendMessage = useCallback(async (text: string) => {
    const userMsg: Message = { role: 'user', content: text }
    const next = [...messages, userMsg]
    setMessages(next)
    setIsLoading(true)
    try {
      const resp = await fetch('/responses', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          input: next.map(m => ({ role: m.role, content: m.content })),
        }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      const outputText: string =
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        data?.output?.find((o: any) => o.type === 'message')
          ?.content?.[0]?.text ?? 'No response.'
      setMessages(prev => [...prev, { role: 'assistant', content: outputText }])
    } catch {
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: 'Something went wrong. Please try again.' },
      ])
    } finally {
      setIsLoading(false)
    }
  }, [messages])

  const reset = useCallback(() => setMessages([]), [])

  return { messages, isLoading, sendMessage, reset }
}
