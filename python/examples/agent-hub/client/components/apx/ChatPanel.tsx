import { useState, useEffect, useRef, useCallback } from "react";
import { Link } from "@tanstack/react-router";
import { Send, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import type { AgentCard } from "@/lib/api";

interface Message {
  id: number;
  role: "user" | "agent";
  text: string;
  error?: boolean;
  streaming?: boolean;
}

export default function ChatPanel({ agent }: { agent: AgentCard }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const nextId = useRef<number>(0);

  useEffect(() => {
    setMessages([]);
    setInput("");
  }, [agent.id]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = useCallback(async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    setInput("");
    setMessages((prev) => [...prev, { id: nextId.current++, role: "user", text: trimmed }]);
    setLoading(true);

    const msgId = nextId.current++;
    setMessages((prev) => [...prev, { id: msgId, role: "agent", text: "", streaming: true }]);

    try {
      const res = await fetch(`/api/agents/${agent.id}/invoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({ input: trimmed }),
      });

      if (!res.ok || !res.body) {
        throw new Error(`${res.status}: ${await res.text()}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";

        for (const block of events) {
          const lines = block.split("\n");
          const eventType = lines.find((l) => l.startsWith("event:"))?.slice(6).trim();
          const dataLine = lines.find((l) => l.startsWith("data:"))?.slice(5).trim();
          if (!dataLine) continue;

          if (eventType === "output_text.delta") {
            const { text: chunk } = JSON.parse(dataLine) as { text: string };
            setMessages((prev) =>
              prev.map((m) => (m.id === msgId ? { ...m, text: m.text + chunk } : m))
            );
          } else if (eventType === "error") {
            let errText = dataLine;
            try { errText = (JSON.parse(dataLine) as { error?: string }).error ?? dataLine; } catch {}
            setMessages((prev) =>
              prev.map((m) => (m.id === msgId ? { ...m, text: errText, error: true, streaming: false } : m))
            );
          }
        }
      }
      setMessages((prev) => prev.map((m) => (m.id === msgId ? { ...m, streaming: false } : m)));
    } catch (e: unknown) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === msgId
            ? { ...m, text: e instanceof Error ? e.message : String(e), error: true, streaming: false }
            : m
        )
      );
    } finally {
      setLoading(false);
    }
  }, [agent.id, loading]);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-3 px-5 py-3 border-b bg-card/50 shrink-0">
        <div className="w-2 h-2 rounded-full bg-emerald-400 shrink-0" />
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-sm">{agent.display_name}</span>
          <span className="text-muted-foreground text-xs ml-2 hidden sm:inline truncate">
            {agent.description}
          </span>
        </div>
        <a
          href={`${agent.url}/_apx/agent`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 shrink-0 transition-colors"
        >
          full UI <ExternalLink size={11} />
        </a>
        <Link
          to="/agents/$agentId"
          params={{ agentId: agent.id }}
          className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 shrink-0 transition-colors"
        >
          details <ExternalLink size={11} />
        </Link>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3 min-h-0">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-4 text-center">
            <div className="text-3xl select-none">🔍</div>
            <p className="text-sm text-muted-foreground">Ask {agent.display_name} something</p>
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className={cn("flex", msg.role === "user" ? "justify-end" : "justify-start")}>
              <div
                className={cn(
                  "max-w-[80%] rounded-xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words",
                  msg.role === "user"
                    ? "bg-primary text-primary-foreground"
                    : msg.error
                    ? "bg-destructive/10 text-destructive-foreground border border-destructive/20"
                    : "bg-muted text-foreground"
                )}
              >
                {msg.text || (msg.streaming ? <span className="animate-pulse text-muted-foreground">Thinking…</span> : null)}
              </div>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t px-4 py-3 flex gap-2 items-center bg-card/50 shrink-0">
        <input
          type="text"
          value={input}
          aria-label={`Ask ${agent.display_name}`}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend(input)}
          placeholder={`Ask ${agent.display_name}…`}
          disabled={loading}
          className="flex-1 bg-background border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
        />
        <button
          aria-label="Send message"
          onClick={() => handleSend(input)}
          disabled={loading || !input.trim()}
          className={cn(
            "p-2 rounded-lg transition-colors shrink-0",
            "bg-primary text-primary-foreground hover:bg-primary/90",
            "disabled:opacity-50 disabled:cursor-not-allowed"
          )}
        >
          <Send size={16} />
        </button>
      </div>
    </div>
  );
}
