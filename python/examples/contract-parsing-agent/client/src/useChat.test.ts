import { renderHook, act } from '@testing-library/react'
import { useChat } from './useChat'

describe('useChat', () => {
  it('appends user and assistant messages on successful send', async () => {
    const mockResponse = {
      thread_id: 'thread-123',
      output: [
        {
          type: 'message',
          content: [{ type: 'output_text', text: 'Hello from agent!' }],
        },
      ],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockResponse),
    } as unknown as Response))

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('Hi')
    })

    expect(result.current.messages).toHaveLength(2)
    expect(result.current.messages[0]).toEqual({ role: 'user', content: 'Hi' })
    expect(result.current.messages[1]).toEqual({ role: 'assistant', content: 'Hello from agent!' })
    expect(result.current.isLoading).toBe(false)
    expect(result.current.threadId).toBe('thread-123')

    act(() => result.current.reset())
    expect(result.current.messages).toEqual([])
    expect(result.current.threadId).toBeNull()
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledWith(
      '/invocations',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: expect.stringContaining('"role":"user"'),
      })
    )
  })

  it('appends error message when fetch fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network error')))

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('Hi')
    })

    expect(result.current.messages[1]).toEqual({
      role: 'assistant',
      content: 'Sorry, something went wrong.',
    })
    expect(result.current.isLoading).toBe(false)
  })

  it('appends error message when response is not ok', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
    } as unknown as Response))

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('Hi')
    })

    expect(result.current.messages[1].content).toBe('Sorry, something went wrong.')
  })
})
