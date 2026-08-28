import { useCallback, useEffect, useState } from "react";
import { buildOnboardingPrompt, mergeArtifacts, streamChat, type Artifact, type Gate } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { OnboardingPanel } from "./components/OnboardingPanel";
import { ProgressRail } from "./components/ProgressRail";
import { ArtifactInspector } from "./components/ArtifactInspector";
import { BlueprintView } from "./components/BlueprintView";
import { DevToolbar } from "./components/DevToolbar";
import dbForGoodLogo from "./assets/databricks-for-good.png";

export default function App() {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<{ role: string; content: string }[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [gate, setGate] = useState<Gate | null>(null);
  const [inspect, setInspect] = useState<Artifact | null>(null);
  const [busy, setBusy] = useState(false);
  const [seeded, setSeeded] = useState(false);
  const [devEnabled, setDevEnabled] = useState(true);

  useEffect(() => {
    fetch("/api/dev-ui")
      .then((response) => response.ok ? response.json() : null)
      .then((value) => { if (value?.enabled === false) setDevEnabled(false); })
      .catch(() => undefined);
  }, []);

  const handleReset = useCallback(() => {
    setThreadId(null);
    setMessages([]);
    setArtifacts([]);
    setGate(null);
    setInspect(null);
    setSeeded(false);
  }, []);

  async function runTurn(message: string, displayMessage: string) {
    setBusy(true);
    setMessages((current) => [
      ...current,
      { role: "user", content: displayMessage },
      { role: "assistant", content: "" },
    ]);
    try {
      const result = await streamChat({
        message,
        threadId,
        onText: (content) => setMessages((current) => [
          ...current.slice(0, -1),
          { role: "assistant", content },
        ]),
      });
      setThreadId(result.threadId);
      setMessages((current) => [
        ...current.slice(0, -1),
        { role: "assistant", content: result.reply || "No response." },
        ...(result.artifactError ? [{ role: "assistant", content: `Structured result rejected: ${result.artifactError}` }] : []),
      ]);
      const merged = mergeArtifacts(artifacts, result.artifacts);
      setArtifacts(merged.artifacts);
      setGate(merged.gate);
    } catch (error) {
      setMessages((current) => [
        ...current.slice(0, -1),
        { role: "assistant", content: error instanceof Error ? `Discovery failed: ${error.message}` : "Discovery failed." },
      ]);
    } finally {
      setBusy(false);
    }
  }

  async function onSend(text: string) {
    if (text.trim()) await runTurn(text, text);
  }

  async function onOnboardingSubmit(url: string, files: File[]) {
    setSeeded(true);
    const summary = url ? `Provided website: ${url}` : "";
    const fileNote = files.length > 0 ? `Uploaded ${files.length} document(s)` : "";
    const userMsg = [summary, fileNote].filter(Boolean).join(". ");
    await runTurn(await buildOnboardingPrompt(url, files), userMsg);
  }

  const showBlueprint = artifacts.find((a) => a.type === "blueprint");

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header__brand">
          <img className="app-header__logo" src={dbForGoodLogo} alt="Databricks for Good" />
          <p>Transform your operations with Data and AI</p>
        </div>
      </header>

      <div className="app-layout">
        <ProgressRail artifacts={artifacts} gate={gate} onInspect={setInspect} />

        <main className="app-main">
          <OnboardingPanel
            hasMessages={messages.length > 0}
            onSubmit={onOnboardingSubmit}
            busy={busy}
          />
          <ChatPanel messages={messages} onSend={onSend} busy={busy} collapsed={!seeded} />
          {showBlueprint && <BlueprintView artifact={showBlueprint} />}
        </main>
      </div>

      <ArtifactInspector artifact={inspect} onClose={() => setInspect(null)} />
      {devEnabled && <DevToolbar threadId={threadId} onReset={handleReset} />}
    </div>
  );
}
