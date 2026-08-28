import { useEffect, useState } from 'react'
import {
  deleteSkill,
  deleteThread,
  getConfig,
  getInstructions,
  getPrompt,
  getTools,
  listThreads,
  revertInstructions,
  setInstructions,
  setModel,
  setSkill,
  setToolEnabled,
  type AppKitThread,
  type DevSnapshot,
} from './devApi'

type Tab = 'Config' | 'Instructions' | 'Tools' | 'Sessions' | 'Prompt'

type Props = {
  threadId: string | null
  onReset: () => void
}

const tabs: Tab[] = ['Config', 'Instructions', 'Tools', 'Sessions', 'Prompt']

export function DevToolbar({ threadId, onReset }: Props) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<Tab>('Config')
  const [snapshot, setSnapshot] = useState<DevSnapshot | null>(null)
  const [threads, setThreads] = useState<AppKitThread[]>([])
  const [prompt, setPrompt] = useState('')
  const [model, setModelInput] = useState('')
  const [instructions, setInstructionsInput] = useState('')
  const [skillName, setSkillName] = useState('')
  const [skillDescription, setSkillDescription] = useState('')
  const [skillContent, setSkillContent] = useState('')
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    setStatus('')
    setError('')
    try {
      await action()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Developer request failed.')
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    if (!open) return
    void run(async () => {
      if (tab === 'Config') {
        const next = await getConfig()
        setSnapshot(next)
        setModelInput(next.model)
      } else if (tab === 'Instructions') {
        const next = await getInstructions()
        setSnapshot(next)
        setInstructionsInput(next.instructions)
      } else if (tab === 'Tools') {
        setSnapshot(await getTools())
      } else if (tab === 'Sessions') {
        setThreads((await listThreads()).threads)
      } else {
        setPrompt((await getPrompt()).systemPrompt)
      }
    })
  }, [open, tab])

  const applyModel = () => run(async () => {
    const next = await setModel(model)
    setSnapshot(next)
    setStatus('Model applied to the live AppKit agent.')
  })

  const applyInstructions = () => run(async () => {
    const next = await setInstructions(instructions)
    setSnapshot(next)
    setStatus('Instructions applied; existing AppKit threads should be reset.')
  })

  const revert = () => run(async () => {
    const next = await revertInstructions()
    setSnapshot(next)
    setInstructionsInput(next.instructions)
    setStatus('Instructions reverted to the deployed manifest.')
  })

  const toggleTool = (name: string, enabled: boolean) => run(async () => {
    setSnapshot(await setToolEnabled(name, enabled))
  })

  const addSkill = () => run(async () => {
    const next = await setSkill({ name: skillName, description: skillDescription, content: skillContent })
    setSnapshot(next)
    setSkillName('')
    setSkillDescription('')
    setSkillContent('')
    setStatus('Skill added to the live AppKit agent.')
  })

  const removeSkill = (name: string) => run(async () => {
    setSnapshot(await deleteSkill(name))
    setStatus('Skill removed from the live AppKit agent.')
  })

  const removeThread = (id: string) => run(async () => {
    await deleteThread(id)
    setThreads(current => current.filter(thread => thread.id !== id))
  })

  const resetSession = () => run(async () => {
    if (!threadId) return
    await deleteThread(threadId)
    onReset()
    setThreads(current => current.filter(thread => thread.id !== threadId))
    setStatus('Current AppKit session reset.')
  })

  if (!open) {
    return (
      <button
        type="button"
        aria-label="Dev"
        onClick={() => setOpen(true)}
        className="fixed bottom-4 right-4 z-50 rounded-full border border-row bg-panel px-3 py-2 text-xs font-semibold text-gray-300 shadow-lg hover:text-white"
      >
        ⚙ Dev
      </button>
    )
  }

  return (
    <section
      role="dialog"
      aria-label="Developer tools"
      className="fixed bottom-4 right-4 z-50 flex h-[70vh] w-[520px] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-lg border border-row bg-panel p-3 font-mono text-xs text-gray-200 shadow-2xl"
    >
      <header className="mb-2 flex items-center justify-between">
        <strong className="text-green-400">APX Developer</strong>
        <div className="flex items-center gap-2">
          <button type="button" disabled={!threadId || busy} onClick={() => void resetSession()} className="rounded border border-row px-2 py-1 disabled:opacity-40">
            Reset session
          </button>
          <button type="button" aria-label="Close developer tools" onClick={() => setOpen(false)}>✕</button>
        </div>
      </header>
      <div className="mb-2 flex items-center gap-2">
        <a
          href="/_apx/agent"
          target="_blank"
          rel="noreferrer"
          aria-label="APX console"
          className="rounded border border-blue-500 px-2 py-1 text-blue-300"
        >
          APX console ↗
        </a>
      </div>
      <div role="tablist" className="mb-2 flex border-b border-row">
        {tabs.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            onClick={() => {
              setStatus('')
              setError('')
              setTab(name)
            }}
            className={`px-2 py-1 ${tab === name ? 'bg-row text-green-400' : 'text-gray-400'}`}
          >
            {name}
          </button>
        ))}
      </div>
      <div role="tabpanel" className="min-h-0 flex-1 overflow-auto">
        {tab === 'Config' && (
          <div className="space-y-3">
            <label className="block">Model
              <input aria-label="Model" value={model} onChange={event => setModelInput(event.target.value)} className="mt-1 w-full rounded border border-row bg-surface p-2" />
            </label>
            <button type="button" disabled={busy || !model.trim()} onClick={() => void applyModel()} className="rounded bg-blue-600 px-3 py-2 disabled:opacity-40">Apply model</button>
            <p>Deployed model: {snapshot?.originalModel ?? '—'}</p>
          </div>
        )}
        {tab === 'Instructions' && (
          <div className="space-y-3">
            <label className="block">Agent instructions
              <textarea aria-label="Agent instructions" value={instructions} onChange={event => setInstructionsInput(event.target.value)} rows={10} className="mt-1 w-full rounded border border-row bg-surface p-2" />
            </label>
            <div className="flex gap-2">
              <button type="button" disabled={busy} onClick={() => void applyInstructions()} className="rounded bg-blue-600 px-3 py-2 disabled:opacity-40">Apply instructions</button>
              <button type="button" disabled={busy || !snapshot?.instructionsOverridden} onClick={() => void revert()} className="rounded border border-row px-3 py-2 disabled:opacity-40">Revert instructions</button>
            </div>
          </div>
        )}
        {tab === 'Tools' && (
          <div className="space-y-4">
            <div>
              <h3 className="mb-2 font-semibold text-gray-300">Compiled tools</h3>
              {snapshot?.tools.map(tool => (
                <label key={tool.name} className="mb-2 flex items-start gap-2">
                  <input type="checkbox" aria-label={tool.name} checked={tool.enabled} disabled={busy} onChange={event => void toggleTool(tool.name, event.target.checked)} />
                  <span><strong>{tool.name}</strong><br />{tool.description}</span>
                </label>
              ))}
            </div>
            <div className="space-y-2 border-t border-row pt-3">
              <h3 className="font-semibold text-gray-300">Live markdown skills</h3>
              {snapshot?.skills.map(skill => (
                <div key={skill.name} className="flex items-center justify-between rounded border border-row p-2">
                  <span>skill.{skill.name}</span>
                  <button type="button" disabled={busy} aria-label={`Remove ${skill.name}`} onClick={() => void removeSkill(skill.name)}>Remove</button>
                </div>
              ))}
              <label className="block">Skill name<input aria-label="Skill name" value={skillName} onChange={event => setSkillName(event.target.value)} className="mt-1 w-full rounded border border-row bg-surface p-2" /></label>
              <label className="block">Skill description<input aria-label="Skill description" value={skillDescription} onChange={event => setSkillDescription(event.target.value)} className="mt-1 w-full rounded border border-row bg-surface p-2" /></label>
              <label className="block">Skill content<textarea aria-label="Skill content" value={skillContent} onChange={event => setSkillContent(event.target.value)} rows={6} className="mt-1 w-full rounded border border-row bg-surface p-2" /></label>
              <button type="button" disabled={busy || !skillName || !skillDescription || !skillContent} onClick={() => void addSkill()} className="rounded bg-blue-600 px-3 py-2 disabled:opacity-40">Add skill</button>
            </div>
            <p className="border-t border-row pt-3">New Databricks resources require declaration and redeploy. <a href="/_apx/agent" target="_blank" rel="noreferrer" className="text-blue-300">Open APX console ↗</a></p>
          </div>
        )}
        {tab === 'Sessions' && (
          <div className="space-y-2">
            {threads.length === 0 ? <p>No active sessions.</p> : threads.map(thread => (
              <div key={thread.id} className="flex items-center justify-between rounded border border-row p-2">
                <span>{thread.id}<br /><span>{thread.messages.length} messages</span></span>
                <button type="button" disabled={busy} aria-label={`Delete thread ${thread.id}`} onClick={() => void removeThread(thread.id)}>Delete</button>
              </div>
            ))}
          </div>
        )}
        {tab === 'Prompt' && <pre className="whitespace-pre-wrap rounded bg-surface p-3">{prompt}</pre>}
      </div>
      {(status || error) && <p role="status" className={`mt-2 ${error ? 'text-red-400' : 'text-green-400'}`}>{error || status}</p>}
      <p className="mt-2 text-gray-500">Overrides are process-local and reset on restart or redeploy.</p>
    </section>
  )
}
