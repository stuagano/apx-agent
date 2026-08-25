// Slim Chat dock for Topology — send a turn without leaving the graph.
// Uses the same `/responses` streaming contract as `/_apx/agent` Chat.
// On completion, calls `onTurnComplete` so the amber last-turn highlight refreshes.

import { useCallback, useEffect, useRef, useState } from "react";

export interface ChatDockProps {
  onTurnComplete?: () => void;
  collapsed?: boolean;
  onToggle?: () => void;
  /** A workflow question supplied by the topology rail. */
  starterQuestion?: string | null;
  /** Changes for each requested run, including retries of the same question. */
  runRequestId?: number;
  /** Reports whether a workflow-started request reached a completed response. */
  onRunQuestion?: (requestId: number, completed: boolean) => void;
}

interface ToolStep {
  id: string;
  name: string;
  phase: "running" | "done" | "error";
}

interface Msg {
  role: "user" | "assistant";
  text: string;
  tools?: ToolStep[];
}

function threadId(): string {
  try {
    const key = "apxTopoChatThread";
    let id = sessionStorage.getItem(key);
    if (!id) {
      id = `topo-${Date.now().toString(36)}`;
      sessionStorage.setItem(key, id);
    }
    return id;
  } catch {
    return `topo-${Date.now().toString(36)}`;
  }
}

async function streamChat(
  history: Array<{ role: string; content: string }>,
  onDelta: (full: string) => void,
  onTool: (step: ToolStep) => void,
): Promise<string> {
  const res = await fetch("/responses", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-return-trace-id": "true",
    },
    body: JSON.stringify({
      input: history,
      stream: true,
      custom_inputs: { thread_id: threadId() },
    }),
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${await res.text()}`);
  }
  if (!res.body) throw new Error("No response body");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let full = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() || "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      try {
        const payload = JSON.parse(line.slice(6)) as Record<string, unknown>;
        const ptype = String(payload.type || "");
        if (ptype === "response.output_text.delta" && typeof payload.delta === "string") {
          full += payload.delta;
          onDelta(full);
        } else if (ptype === "response.output_item.done") {
          const item = (payload.item || {}) as Record<string, unknown>;
          if (item.type === "message" && Array.isArray(item.content)) {
            for (const part of item.content as Array<Record<string, unknown>>) {
              if (part.type === "output_text" && typeof part.text === "string") {
                full += part.text;
                onDelta(full);
              }
            }
          } else if (item.type === "function_call") {
            onTool({
              id: String(item.call_id || item.id || item.name || "tool"),
              name: String(item.name || "tool"),
              phase: "running",
            });
          } else if (item.type === "function_call_output") {
            const outStr =
              typeof item.output === "string"
                ? item.output
                : JSON.stringify(item.output || "");
            const isErr = /"error"|\berror\b/i.test(outStr);
            onTool({
              id: String(item.call_id || item.id || "tool"),
              name: String(item.name || "tool"),
              phase: isErr ? "error" : "done",
            });
          }
        } else if (ptype === "response.completed" && !full) {
          const response = payload.response as { output?: unknown[] } | undefined;
          const out = response?.output;
          if (Array.isArray(out)) {
            for (const item of out as Array<Record<string, unknown>>) {
              if (item.type === "message" && Array.isArray(item.content)) {
                for (const part of item.content as Array<Record<string, unknown>>) {
                  if (part.type === "output_text" && typeof part.text === "string") {
                    full += part.text;
                  }
                }
              }
            }
            if (full) onDelta(full);
          }
        }
      } catch {
        /* ignore bad SSE lines */
      }
    }
  }
  return full;
}

export function ChatDock(props: ChatDockProps) {
  const {
    onTurnComplete,
    collapsed,
    onToggle,
    starterQuestion,
    runRequestId,
    onRunQuestion,
  } = props;
  const [messages, setMessages] = useState<Msg[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [history, setHistory] = useState<Array<{ role: string; content: string }>>(
    [],
  );
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const lastRunRequest = useRef<number | undefined>(undefined);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  const send = useCallback(async (question: string, fromWorkflow = false) => {
    const text = question.trim();
    if (!text || sending) return;
    setDraft("");
    const nextHistory = [...history, { role: "user", content: text }];
    setHistory(nextHistory);
    setMessages((m) => [...m, { role: "user", text }, { role: "assistant", text: "", tools: [] }]);
    setSending(true);

    try {
      const full = await streamChat(
        nextHistory,
        (partial) => {
          setMessages((m) => {
            const copy = [...m];
            const last = copy[copy.length - 1];
            if (last?.role === "assistant") {
              copy[copy.length - 1] = { ...last, text: partial };
            }
            return copy;
          });
        },
        (step) => {
          setMessages((m) => {
            const copy = [...m];
            const last = copy[copy.length - 1];
            if (last?.role !== "assistant") return m;
            const tools = [...(last.tools || [])];
            const idx = tools.findIndex((t) => t.id === step.id);
            if (idx >= 0) tools[idx] = { ...tools[idx]!, ...step, name: step.name || tools[idx]!.name };
            else tools.push(step);
            copy[copy.length - 1] = { ...last, tools };
            return copy;
          });
        },
      );
      setHistory((h) => [...h, { role: "assistant", content: full }]);
      if (fromWorkflow && runRequestId !== undefined) onRunQuestion?.(runRequestId, true);
      // Give the ring buffer a beat to capture the trace, then highlight.
      window.setTimeout(() => onTurnComplete?.(), 400);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setMessages((m) => {
        const copy = [...m];
        const last = copy[copy.length - 1];
        if (last?.role === "assistant") {
          copy[copy.length - 1] = { ...last, text: `Error: ${msg}` };
        }
        return copy;
      });
      if (fromWorkflow && runRequestId !== undefined) onRunQuestion?.(runRequestId, false);
    } finally {
      setSending(false);
    }
  }, [history, onRunQuestion, onTurnComplete, runRequestId, sending]);

  useEffect(() => {
    if (
      runRequestId === undefined ||
      !starterQuestion ||
      lastRunRequest.current === runRequestId
    ) {
      return;
    }
    lastRunRequest.current = runRequestId;
    void send(starterQuestion, true);
  }, [runRequestId, send, starterQuestion]);

  if (collapsed) {
    return (
      <div className="apx-chat-dock collapsed">
        <button type="button" className="apx-btn" onClick={onToggle}>
          Chat
        </button>
      </div>
    );
  }

  return (
    <div className="apx-chat-dock">
      <div className="apx-chat-dock-head">
        <div>
          <div className="apx-chat-dock-title">Chat</div>
          <div className="apx-chat-dock-sub">Try a turn — route lights up on the graph</div>
        </div>
        {onToggle && (
          <button type="button" className="apx-nav-refresh" onClick={onToggle}>
            Hide
          </button>
        )}
      </div>

      <div className="apx-chat-dock-msgs">
        {messages.length === 0 && (
          <div className="apx-chat-dock-empty">
            Ask something that uses a tool or peer (e.g. “how many trips?”).
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`apx-chat-msg ${m.role}`}>
            {m.tools && m.tools.length > 0 && (
              <div className="apx-chat-tools">
                {m.tools.map((t) => (
                  <span key={t.id} className={`apx-chat-tool ${t.phase}`}>
                    {t.name}
                    {t.phase === "running" ? "…" : t.phase === "error" ? " !" : ""}
                  </span>
                ))}
              </div>
            )}
            <div className="apx-chat-bubble">{m.text || (sending && i === messages.length - 1 ? "…" : "")}</div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <form
        className="apx-chat-dock-form"
        onSubmit={(e) => {
          e.preventDefault();
          void send(draft);
        }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={sending ? "Waiting…" : "Message the agent"}
          disabled={sending}
          spellCheck={false}
        />
        <button type="submit" className="apx-btn" disabled={sending || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
