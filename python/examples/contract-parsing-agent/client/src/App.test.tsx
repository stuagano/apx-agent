import { fireEvent, render, screen } from '@testing-library/react'
import App from './App'

beforeAll(() => { Element.prototype.scrollIntoView = vi.fn() })
afterEach(() => vi.unstubAllGlobals())

it('starts a new conversation without deleting managed history', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve({
    ok: true,
    json: async () => String(input) === '/api/invocations'
      ? { status: 'completed', output: { status: 'completed', messages: [{ role: 'assistant', content: 'Agent reply' }] } }
      : [],
  } as Response))
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  fireEvent.change(screen.getByPlaceholderText('Ask about the contracts…'), { target: { value: 'Hello' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
  await screen.findByText('Agent reply')
  fireEvent.click(screen.getByRole('button', { name: 'New conversation' }))
  expect(screen.queryByText('Agent reply')).not.toBeInTheDocument()
  expect(fetchMock.mock.calls.every(([url]) => ['/api/contracts', '/api/invocations'].includes(String(url)))).toBe(true)
})
