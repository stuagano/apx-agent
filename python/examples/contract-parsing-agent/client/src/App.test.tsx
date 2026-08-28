import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

function mockAppFetch(devEnabled: boolean) {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const body = url === '/api/dev-ui' ? { enabled: devEnabled } : []
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(body),
    } as Response)
  }))
}

describe('developer launcher', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('is visible by default and opens the five-tab inline panel', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('unavailable'))))
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: 'Dev' }))

    expect(screen.getByRole('dialog', { name: 'Developer tools' })).toBeInTheDocument()
    for (const tab of ['Config', 'Instructions', 'Tools', 'Sessions', 'Prompt']) {
      expect(screen.getByRole('tab', { name: tab })).toBeInTheDocument()
    }
    expect(screen.getByRole('link', { name: 'APX console' })).toHaveAttribute(
      'href',
      '/_apx/agent',
    )
    expect(await screen.findByRole('status')).toHaveTextContent('unavailable')
  })

  it('is absent when the server explicitly disables it', async () => {
    mockAppFetch(false)
    render(<App />)

    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/dev-ui'))
    expect(screen.queryByRole('link', { name: 'Dev' })).not.toBeInTheDocument()
  })

  it('resets the active AppKit thread only after deletion succeeds', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/invocations') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            thread_id: 'thread-123',
            output: [{ type: 'message', content: [{ text: 'Agent reply' }] }],
          }),
        } as Response)
      }
      if (url === '/api/dev-ui') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ enabled: true }) } as Response)
      }
      if (url === '/api/dev/config') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            model: 'databricks-claude-sonnet-4-6',
            originalModel: 'databricks-claude-sonnet-4-6',
          }),
        } as Response)
      }
      if (url === '/api/agents/threads/thread-123' && init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ deleted: true }) } as Response)
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve([]) } as Response)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)

    fireEvent.change(screen.getByPlaceholderText('Ask about the contracts…'), {
      target: { value: 'Hello' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await screen.findByText('Agent reply')
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }))
    await screen.findByText('Deployed model: databricks-claude-sonnet-4-6')
    fireEvent.click(screen.getByRole('button', { name: 'Reset session' }))

    expect(await screen.findByText('Current AppKit session reset.')).toBeInTheDocument()
    expect(screen.getByText('Select a contract from the table, or type a question below.')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/api/agents/threads/thread-123', { method: 'DELETE' })
  })
})
