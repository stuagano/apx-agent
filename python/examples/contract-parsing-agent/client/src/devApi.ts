export type DevTool = {
  name: string
  description: string
  enabled: boolean
  annotations: { effect: string }
}

export type DevSkill = {
  name: string
  description: string
  content: string
}

export type DevSnapshot = {
  agentName: string
  model: string
  originalModel: string
  instructions: string
  instructionsOverridden: boolean
  tools: DevTool[]
  skills: DevSkill[]
  systemPrompt: string
  overridesEphemeral: true
}

export type AppKitThread = {
  id: string
  messages: unknown[]
  createdAt: string
  updatedAt: string
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) throw new Error(`dev request failed: ${response.status}`)
  return response.json() as Promise<T>
}

const body = (value: unknown): Pick<RequestInit, 'headers' | 'body'> => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(value),
})

export const getConfig = () => json<DevSnapshot>('/api/dev/config')
export const setModel = (model: string) =>
  json<DevSnapshot>('/api/dev/config', { method: 'PATCH', ...body({ model }) })
export const getInstructions = () => json<DevSnapshot>('/api/dev/instructions')
export const setInstructions = (instructions: string) =>
  json<DevSnapshot>('/api/dev/instructions', { method: 'PATCH', ...body({ instructions }) })
export const revertInstructions = () =>
  json<DevSnapshot>('/api/dev/instructions', { method: 'DELETE' })
export const getTools = () => json<DevSnapshot>('/api/dev/tools')
export const setToolEnabled = (name: string, enabled: boolean) =>
  json<DevSnapshot>(`/api/dev/tools/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    ...body({ enabled }),
  })
export const setSkill = (skill: DevSkill) =>
  json<DevSnapshot>(`/api/dev/skills/${encodeURIComponent(skill.name)}`, {
    method: 'PUT',
    ...body({ description: skill.description, content: skill.content }),
  })
export const deleteSkill = (name: string) =>
  json<DevSnapshot>(`/api/dev/skills/${encodeURIComponent(name)}`, { method: 'DELETE' })
export const getPrompt = () => json<{ systemPrompt: string }>('/api/dev/prompt')
export const listThreads = () => json<{ threads: AppKitThread[] }>('/api/agents/threads')
export const deleteThread = (id: string) =>
  json<{ deleted: boolean }>(`/api/agents/threads/${encodeURIComponent(id)}`, { method: 'DELETE' })
