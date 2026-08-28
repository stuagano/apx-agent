import { useState, useRef, useEffect } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Msg = { role: string; content: string };

function ThinkingIndicator() {
  return (
    <div className="chat-bubble chat-bubble--assistant chat-thinking">
      <div className="thinking-content">
        <span className="thinking-label">Thinking</span>
        <span className="thinking-dots">
          <span className="thinking-dot" />
          <span className="thinking-dot" />
          <span className="thinking-dot" />
        </span>
      </div>
    </div>
  );
}

export function ChatPanel(
  { messages, onSend, busy, collapsed }: { messages: Msg[]; onSend: (t: string) => void; busy: boolean; collapsed?: boolean },
) {
  const [value, setValue] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [messages, busy]);

  function handleSubmit() {
    onSend(value);
    setValue("");
  }

  if (collapsed) {
    return (
      <div className="chat-panel chat-panel--collapsed">
        <div className="chat-collapsed-hint">
          <span className="chat-collapsed-icon">💬</span>
          <span>Chat will become available after you provide your organization info above</span>
        </div>
      </div>
    );
  }

  return (
    <div className="chat-panel">
      <div className="chat-messages">
        {messages.map((m, i) => (
          <div
            key={i}
            className={`chat-bubble chat-bubble--${m.role === "user" ? "user" : "assistant"}`}
          >
            {m.role === "assistant" ? (
              <Markdown remarkPlugins={[remarkGfm]}>{m.content ?? ""}</Markdown>
            ) : (
              m.content
            )}
          </div>
        ))}
        {busy && <ThinkingIndicator />}
        <div ref={messagesEndRef} />
      </div>
      <div className="chat-input-row">
        <input
          className="chat-input"
          placeholder="Tell us about your organization..."
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && handleSubmit()}
        />
        <button
          className="chat-send-btn"
          disabled={busy}
          onClick={handleSubmit}
        >
          {busy ? "Thinking..." : "Send"}
        </button>
      </div>
    </div>
  );
}
