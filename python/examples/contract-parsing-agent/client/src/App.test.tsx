import { render, screen, waitFor } from '@testing-library/react'
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

  it('is visible by default and opens the APX console', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('unavailable'))))
    render(<App />)

    const link = await screen.findByRole('link', { name: 'Dev' })
    expect(link).toHaveAttribute('href', '/_apx/agent')
  })

  it('is absent when the server explicitly disables it', async () => {
    mockAppFetch(false)
    render(<App />)

    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/dev-ui'))
    expect(screen.queryByRole('link', { name: 'Dev' })).not.toBeInTheDocument()
  })
})
