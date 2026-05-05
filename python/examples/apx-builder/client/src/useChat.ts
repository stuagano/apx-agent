import { useState, useCallback, useRef } from 'react'
import type { Message } from './types'

export function useChat() {
  const [messages, setMessages] = useState<Message[]>([])
  const messagesRef = useRef<Message[]>([])
  const [isLoading, setIsLoading] = useState(false)

  const sendMessage = useCallback(async (text: string) => {
    const userMsg: Message = { id: crypto.randomUUID(), role: 'user', content: text }
    const next = [...messagesRef.current, userMsg]
    setMessages(next)
    messagesRef.current = next
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
      const assistantMsg: Message = { id: crypto.randomUUID(), role: 'assistant', content: outputText }
      setMessages(prev => [...prev, assistantMsg])
      messagesRef.current = [...messagesRef.current, assistantMsg]
    } catch (err) {
      console.error('[apx-builder] sendMessage failed:', err)
      const errMsg: Message = { id: crypto.randomUUID(), role: 'assistant', content: 'Something went wrong. Please try again.' }
      setMessages(prev => [...prev, errMsg])
      messagesRef.current = [...messagesRef.current, errMsg]
    } finally {
      setIsLoading(false)
    }
  }, [])  // stable — no dependencies

  const reset = useCallback(() => {
    setMessages([])
    messagesRef.current = []
  }, [])

  return { messages, isLoading, sendMessage, reset }
}
