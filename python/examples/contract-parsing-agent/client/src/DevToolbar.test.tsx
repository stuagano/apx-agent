import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { DevToolbar } from './DevToolbar'

const baseSnapshot = {
  agentName: 'contract-parsing-agent',
  model: 'databricks-claude-sonnet-4-6',
  originalModel: 'databricks-claude-sonnet-4-6',
  instructions: 'Analyze contracts carefully.',
  instructionsOverridden: false,
  tools: [
    {
      name: 'query_portfolio',
      description: 'Query contract rows.',
      enabled: true,
      annotations: { effect: 'read' },
    },
  ],
  skills: [] as Array<{ name: string; description: string; content: string }>,
  systemPrompt: 'Base AppKit prompt\n\nAnalyze contracts carefully.',
  overridesEphemeral: true,
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response
}

function installFetch() {
  let snapshot = structuredClone(baseSnapshot)
  let threads = [
    {
      id: 'thread-123',
      userId: 'alice',
      messages: [
        { id: 'one', role: 'user', content: 'hello', createdAt: '2026-08-28T12:00:00Z' },
        { id: 'two', role: 'assistant', content: 'hi', createdAt: '2026-08-28T12:00:01Z' },
      ],
      createdAt: '2026-08-28T12:00:00Z',
      updatedAt: '2026-08-28T12:00:01Z',
    },
  ]
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    if (url === '/api/dev/config' && method === 'PATCH') {
      snapshot.model = JSON.parse(String(init?.body)).model
    }
    if (url === '/api/dev/instructions' && method === 'PATCH') {
      snapshot.instructions = JSON.parse(String(init?.body)).instructions
      snapshot.instructionsOverridden = true
    }
    if (url === '/api/dev/instructions' && method === 'DELETE') {
      snapshot.instructions = baseSnapshot.instructions
      snapshot.instructionsOverridden = false
    }
    if (url === '/api/dev/tools/query_portfolio' && method === 'PATCH') {
      snapshot.tools[0].enabled = JSON.parse(String(init?.body)).enabled
    }
    if (url === '/api/dev/skills/pricing_policy' && method === 'PUT') {
      snapshot.skills = [{ name: 'pricing_policy', ...JSON.parse(String(init?.body)) }]
    }
    if (url === '/api/dev/skills/pricing_policy' && method === 'DELETE') {
      snapshot.skills = []
    }
    if (url.startsWith('/api/agents/threads/') && method === 'DELETE') {
      threads = threads.filter(thread => !url.endsWith(thread.id))
      return jsonResponse({ deleted: true })
    }
    if (url === '/api/agents/threads') return jsonResponse({ threads })
    if (url === '/api/dev/prompt') return jsonResponse({ systemPrompt: snapshot.systemPrompt })
    return jsonResponse(snapshot)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('DevToolbar', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('loads config and applies a model to the live runtime', async () => {
    const fetchMock = installFetch()
    render(<DevToolbar threadId={null} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }))

    const model = await screen.findByLabelText('Model')
    expect(model).toHaveValue('databricks-claude-sonnet-4-6')
    fireEvent.change(model, { target: { value: 'databricks-claude-opus-4-7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply model' }))

    await screen.findByText('Model applied to the live AppKit agent.')
    expect(fetchMock).toHaveBeenCalledWith('/api/dev/config', expect.objectContaining({
      method: 'PATCH',
      body: JSON.stringify({ model: 'databricks-claude-opus-4-7' }),
    }))
  })

  it('applies and reverts live instructions', async () => {
    const fetchMock = installFetch()
    render(<DevToolbar threadId={null} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Instructions' }))

    const instructions = await screen.findByLabelText('Agent instructions')
    fireEvent.change(instructions, { target: { value: 'Use terse answers.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply instructions' }))
    await screen.findByText('Instructions applied; existing AppKit threads should be reset.')
    fireEvent.click(screen.getByRole('button', { name: 'Revert instructions' }))
    await screen.findByText('Instructions reverted to the deployed manifest.')

    expect(fetchMock).toHaveBeenCalledWith('/api/dev/instructions', expect.objectContaining({
      method: 'DELETE',
    }))
  })

  it('toggles compiled tools and authors a callable markdown skill', async () => {
    const fetchMock = installFetch()
    render(<DevToolbar threadId={null} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Tools' }))

    const toggle = await screen.findByRole('checkbox', { name: 'query_portfolio' })
    fireEvent.click(toggle)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/dev/tools/query_portfolio',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ enabled: false }) }),
    ))

    fireEvent.change(screen.getByLabelText('Skill name'), { target: { value: 'pricing_policy' } })
    fireEvent.change(screen.getByLabelText('Skill description'), { target: { value: 'Load policy.' } })
    fireEvent.change(screen.getByLabelText('Skill content'), { target: { value: '# Policy' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add skill' }))
    expect(await screen.findByText('skill.pricing_policy')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove pricing_policy' }))
    await screen.findByText('Skill removed from the live AppKit agent.')

    expect(screen.getByText('New Databricks resources require declaration and redeploy.')).toBeInTheDocument()
  })

  it('shows and deletes the requesting user’s real AppKit sessions', async () => {
    const fetchMock = installFetch()
    render(<DevToolbar threadId={null} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Sessions' }))

    expect(await screen.findByText('2 messages')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete thread thread-123' }))
    await screen.findByText('No active sessions.')
    expect(fetchMock).toHaveBeenCalledWith('/api/agents/threads/thread-123', { method: 'DELETE' })
  })

  it('inspects the effective system prompt and resets the current thread', async () => {
    installFetch()
    const onReset = vi.fn()
    render(<DevToolbar threadId="thread-123" onReset={onReset} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }))

    fireEvent.click(screen.getByRole('tab', { name: 'Prompt' }))
    expect(await screen.findByText(/Base AppKit prompt/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reset session' }))
    await screen.findByText('Current AppKit session reset.')
    expect(onReset).toHaveBeenCalledOnce()
  })

  it('closes back to the floating launcher', async () => {
    installFetch()
    render(<DevToolbar threadId={null} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }))
    await screen.findByLabelText('Model')
    fireEvent.click(screen.getByRole('button', { name: 'Close developer tools' }))
    expect(screen.queryByRole('dialog', { name: 'Developer tools' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dev' })).toBeInTheDocument()
  })
})
