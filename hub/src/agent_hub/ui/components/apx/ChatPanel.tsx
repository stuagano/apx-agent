import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Link } from "@tanstack/react-router";
import { Send, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import type { AgentCard } from "@/lib/api";

interface Message {
  id: number;
  role: "user" | "agent";
  text: string;
  error?: boolean;
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

  const suggestedPrompts = useMemo(
    () => agent.tools.slice(0, 2).map((t) => t.description),
    [agent.tools]
  );

  const handleSend = useCallback(async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    setInput("");
    setMessages((prev) => [...prev, { id: nextId.current++, role: "user", text: trimmed }]);
    setLoading(true);
    try {
      const res = await fetch(`/api/agents/${agent.id}/invoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: trimmed }),
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body}`);
      }
      const data = await res.json();
      const responseText = data.output_text ?? JSON.stringify(data, null, 2);
      setMessages((prev) => [...prev, { id: nextId.current++, role: "agent", text: responseText }]);
    } catch (e: unknown) {
      setMessages((prev) => [
        ...prev,
        { id: nextId.current++, role: "agent", text: e instanceof Error ? e.message : String(e), error: true },
      ]);
    } finally {
      setLoading(false);
    }
  }, [agent.id, loading]);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-3 border-b bg-card/50 shrink-0">
        <div className="w-2 h-2 rounded-full bg-emerald-400 shrink-0" />
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-sm">{agent.display_name}</span>
          <span className="text-muted-foreground text-xs ml-2 hidden sm:inline truncate">
            {agent.description}
          </span>
        </div>
        <Link
          to="/agents/$agentId"
          params={{ agentId: agent.id }}
          className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 shrink-0 transition-colors"
        >
          details <ExternalLink size={11} />
        </Link>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3 min-h-0">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-4 text-center">
            <div className="text-3xl select-none">🔍</div>
            <p className="text-sm text-muted-foreground">Ask {agent.display_name} something</p>
            {suggestedPrompts.length > 0 && (
              <div className="flex flex-wrap gap-2 justify-center max-w-md">
                {suggestedPrompts.map((prompt, i) => (
                  <button
                    key={i}
                    onClick={() => setInput(prompt)}
                    className="text-xs border rounded-md px-3 py-1.5 text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.id}
              className={cn("flex", msg.role === "user" ? "justify-end" : "justify-start")}
            >
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
                {msg.text}
              </div>
            </div>
          ))
        )}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-muted rounded-xl px-4 py-2.5 text-sm text-muted-foreground animate-pulse">
              Thinking…
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
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
