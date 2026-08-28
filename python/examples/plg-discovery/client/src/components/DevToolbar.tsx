import { useEffect, useState, type CSSProperties } from "react";
import {
  devAuthorSkill,
  devDeleteSession,
  devDeleteSkill,
  devGetConfig,
  devGetFullPrompt,
  devGetInstructions,
  devGetToolset,
  devListSessions,
  devPatchConfig,
  devPatchInstructions,
  devResetInstructions,
  devToggleTool,
  type AppKitThread,
  type DevSnapshot,
} from "../api";

type Tab = "Config" | "Instructions" | "Tools" | "Sessions" | "Prompt";
const TABS: Tab[] = ["Config", "Instructions", "Tools", "Sessions", "Prompt"];

export function DevToolbar({ threadId, onReset }: { threadId: string | null; onReset: () => void }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("Config");
  const [snapshot, setSnapshot] = useState<DevSnapshot | null>(null);
  const [threads, setThreads] = useState<AppKitThread[]>([]);
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [instructions, setInstructions] = useState("");
  const [skillName, setSkillName] = useState("");
  const [skillDescription, setSkillDescription] = useState("");
  const [skillContent, setSkillContent] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setStatus("");
    try { await action(); }
    catch (error) { setStatus(error instanceof Error ? error.message : "Developer request failed."); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    if (!open) return;
    void run(async () => {
      if (tab === "Config") {
        const next = await devGetConfig();
        setSnapshot(next);
        setModel(next.model);
      } else if (tab === "Instructions") {
        const next = await devGetInstructions();
        setSnapshot(next);
        setInstructions(next.instructions);
      } else if (tab === "Tools") {
        setSnapshot(await devGetToolset());
      } else if (tab === "Sessions") {
        setThreads((await devListSessions()).threads);
      } else {
        setPrompt((await devGetFullPrompt()).systemPrompt);
      }
    });
  }, [open, tab]);

  async function resetCurrent() {
    if (!threadId) return;
    await run(async () => {
      await devDeleteSession(threadId);
      setThreads((current) => current.filter((thread) => thread.id !== threadId));
      onReset();
      setStatus("Current AppKit session reset.");
    });
  }

  async function removeThread(id: string) {
    await run(async () => {
      await devDeleteSession(id);
      setThreads((current) => current.filter((thread) => thread.id !== id));
      if (id === threadId) onReset();
    });
  }

  if (!open) {
    return <button type="button" aria-label="Dev" onClick={() => setOpen(true)} style={styles.launcher}>⚙ Dev</button>;
  }

  return (
    <section role="dialog" aria-label="Developer tools" style={styles.panel}>
      <header style={styles.header}>
        <strong style={{ color: "#61d982" }}>APX Developer</strong>
        <div style={styles.inline}>
          <button type="button" disabled={!threadId || busy} onClick={() => void resetCurrent()} style={styles.button}>Reset session</button>
          <button type="button" aria-label="Close developer tools" onClick={() => setOpen(false)} style={styles.close}>✕</button>
        </div>
      </header>
      <a href="/_apx/agent" target="_blank" rel="noreferrer" style={styles.link}>APX console ↗</a>
      <div role="tablist" style={styles.tabs}>
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            onClick={() => setTab(name)}
            style={tab === name ? styles.activeTab : styles.tab}
          >{name}</button>
        ))}
      </div>
      <div role="tabpanel" style={styles.content}>
        {tab === "Config" && snapshot && (
          <div>
            <label style={styles.label}>Model
              <input aria-label="Model" value={model} onChange={(event) => setModel(event.target.value)} style={styles.input} />
            </label>
            <button type="button" disabled={busy || !model.trim()} onClick={() => void run(async () => {
              setSnapshot(await devPatchConfig(model.trim()));
              setStatus("Model applied to the live AppKit agent.");
            })} style={styles.primary}>Apply model</button>
            <p>Deployed model: {snapshot.originalModel}</p>
          </div>
        )}
        {tab === "Instructions" && snapshot && (
          <div>
            <label style={styles.label}>Agent instructions
              <textarea aria-label="Agent instructions" rows={10} value={instructions} onChange={(event) => setInstructions(event.target.value)} style={styles.textarea} />
            </label>
            <div style={styles.inline}>
              <button type="button" disabled={busy} onClick={() => void run(async () => {
                setSnapshot(await devPatchInstructions(instructions));
                setStatus("Instructions applied; reset existing AppKit threads to use them.");
              })} style={styles.primary}>Apply instructions</button>
              <button type="button" disabled={busy || !snapshot.instructionsOverridden} onClick={() => void run(async () => {
                const next = await devResetInstructions();
                setSnapshot(next);
                setInstructions(next.instructions);
                setStatus("Instructions reverted to the deployed manifest.");
              })} style={styles.button}>Revert instructions</button>
            </div>
          </div>
        )}
        {tab === "Tools" && snapshot && (
          <div>
            <h3>Compiled tools</h3>
            {snapshot.tools.map((tool) => (
              <label key={tool.name} style={styles.tool}>
                <input type="checkbox" aria-label={tool.name} checked={tool.enabled} disabled={busy} onChange={(event) => void run(async () => {
                  setSnapshot(await devToggleTool(tool.name, event.target.checked));
                })} />
                <span><strong>{tool.name}</strong><br />{tool.description}</span>
              </label>
            ))}
            <h3 style={styles.divider}>Live markdown skills</h3>
            {snapshot.skills.map((skill) => (
              <div key={skill.name} style={styles.row}>
                <span>skill.{skill.name}</span>
                <button type="button" aria-label={`Remove ${skill.name}`} disabled={busy} onClick={() => void run(async () => {
                  setSnapshot(await devDeleteSkill(skill.name));
                })} style={styles.button}>Remove</button>
              </div>
            ))}
            <input aria-label="Skill name" placeholder="Skill name" value={skillName} onChange={(event) => setSkillName(event.target.value)} style={styles.input} />
            <input aria-label="Skill description" placeholder="Skill description" value={skillDescription} onChange={(event) => setSkillDescription(event.target.value)} style={styles.input} />
            <textarea aria-label="Skill content" placeholder="Skill markdown" rows={5} value={skillContent} onChange={(event) => setSkillContent(event.target.value)} style={styles.textarea} />
            <button type="button" disabled={busy || !skillName || !skillDescription || !skillContent} onClick={() => void run(async () => {
              setSnapshot(await devAuthorSkill({ name: skillName, description: skillDescription, content: skillContent }));
              setSkillName(""); setSkillDescription(""); setSkillContent("");
              setStatus("Skill added to the live AppKit agent.");
            })} style={styles.primary}>Add skill</button>
            <p style={styles.divider}>New Databricks resources require declaration and redeploy.</p>
          </div>
        )}
        {tab === "Sessions" && (
          <div>
            {threads.length === 0 ? <p>No active sessions.</p> : threads.map((thread) => (
              <div key={thread.id} style={styles.row}>
                <span>{thread.id}<br />{thread.messages.length} {thread.messages.length === 1 ? "message" : "messages"}</span>
                <button type="button" aria-label={`Delete thread ${thread.id}`} disabled={busy} onClick={() => void removeThread(thread.id)} style={styles.button}>Delete</button>
              </div>
            ))}
          </div>
        )}
        {tab === "Prompt" && <pre style={styles.prompt}>{prompt}</pre>}
      </div>
      {status && <p role="status" style={{ color: "#61d982" }}>{status}</p>}
      <p style={{ color: "#8c93a3" }}>Overrides are process-local and reset on restart or redeploy.</p>
    </section>
  );
}

const styles: Record<string, CSSProperties> = {
  launcher: { position: "fixed", right: 16, bottom: 16, zIndex: 9999, padding: "7px 12px", borderRadius: 7, border: "1px solid #3f4655", background: "#1a1d29", color: "#61d982", font: "600 13px monospace", cursor: "pointer" },
  panel: { position: "fixed", right: 16, bottom: 16, zIndex: 9999, width: 520, maxWidth: "calc(100vw - 32px)", height: "70vh", display: "flex", flexDirection: "column", padding: 12, overflow: "hidden", borderRadius: 8, border: "1px solid #3f4655", background: "#1a1d29", color: "#e7e9ee", font: "12px monospace", boxShadow: "0 8px 32px rgba(0,0,0,.45)" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 },
  inline: { display: "flex", alignItems: "center", gap: 6 },
  close: { color: "#aaa", background: "none", border: 0, cursor: "pointer" },
  link: { color: "#80bfff", marginBottom: 8 },
  tabs: { display: "flex", borderBottom: "1px solid #3f4655", marginBottom: 8 },
  tab: { padding: "6px 8px", border: 0, background: "transparent", color: "#aeb4c0", cursor: "pointer" },
  activeTab: { padding: "6px 8px", border: 0, background: "#303543", color: "#61d982", cursor: "pointer" },
  content: { minHeight: 0, flex: 1, overflow: "auto" },
  label: { display: "block", marginBottom: 10 },
  input: { display: "block", boxSizing: "border-box", width: "100%", marginTop: 4, marginBottom: 6, padding: 7, borderRadius: 4, border: "1px solid #3f4655", background: "#242936", color: "#fff" },
  textarea: { display: "block", boxSizing: "border-box", width: "100%", marginTop: 4, marginBottom: 6, padding: 7, borderRadius: 4, border: "1px solid #3f4655", background: "#242936", color: "#fff", resize: "vertical" },
  button: { padding: "5px 8px", borderRadius: 4, border: "1px solid #4a5264", background: "transparent", color: "#e7e9ee", cursor: "pointer" },
  primary: { padding: "6px 10px", borderRadius: 4, border: 0, background: "#2867c7", color: "white", cursor: "pointer" },
  tool: { display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 8 },
  divider: { borderTop: "1px solid #3f4655", paddingTop: 10, marginTop: 12 },
  row: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, padding: 8, marginBottom: 6, border: "1px solid #3f4655", borderRadius: 4 },
  prompt: { whiteSpace: "pre-wrap", padding: 10, background: "#242936", borderRadius: 4 },
};
