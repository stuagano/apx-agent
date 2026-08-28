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
})
