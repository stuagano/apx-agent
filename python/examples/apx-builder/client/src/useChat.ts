import { useState, useCallback, useRef } from 'react'
import type { Message } from './types'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function extractText(data: any): string {
  return data?.output?.find((o: any) => o.type === 'message')?.content?.[0]?.text ?? 'No response.'
}

export function useChat() {
  const [messages, setMessages] = useState<Message[]>([])
  const messagesRef = useRef<Message[]>([])
  const sessionIdRef = useRef<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)

  const _post = useCallback(async (input: { role: string; content: string }[]) => {
    const body: Record<string, unknown> = { input }
    if (sessionIdRef.current) body.session_id = sessionIdRef.current
    const resp = await fetch('/responses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    return resp.json()
  }, [])

  const _addAssistant = useCallback((text: string) => {
    const msg: Message = { id: crypto.randomUUID(), role: 'assistant', content: text }
    setMessages(prev => [...prev, msg])
    messagesRef.current = [...messagesRef.current, msg]
  }, [])

  const sendMessage = useCallback(async (text: string) => {
    const userMsg: Message = { id: crypto.randomUUID(), role: 'user', content: text }
    const next = [...messagesRef.current, userMsg]
    setMessages(next)
    messagesRef.current = next
    setIsLoading(true)
    try {
      const data = await _post(next.map(m => ({ role: m.role, content: m.content })))
      if (data?.session_id) sessionIdRef.current = data.session_id
      _addAssistant(extractText(data))

      if (data?.build_pending === true) {
        // Payload confirmed — immediately trigger Phase 2 without adding a user message.
        // Send only user turns so the server's answer-recording logic stays clean.
        const userOnly = messagesRef.current
          .filter(m => m.role === 'user')
          .map(m => ({ role: m.role, content: m.content }))
        const buildData = await _post(userOnly)
        if (buildData?.session_id) sessionIdRef.current = buildData.session_id
        _addAssistant(extractText(buildData))
      }
    } catch (err) {
      console.error('[apx-builder] sendMessage failed:', err)
      _addAssistant('Something went wrong. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }, [_post, _addAssistant])

  const reset = useCallback(() => {
    setMessages([])
    messagesRef.current = []
    sessionIdRef.current = null
  }, [])

  return { messages, isLoading, sendMessage, reset }
}
