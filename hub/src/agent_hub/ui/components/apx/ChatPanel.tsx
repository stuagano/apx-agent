import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Link } from "@tanstack/react-router";
import {
  Button,
  Input,
  Typography,
  NewWindowIcon,
  SendIcon,
  useDesignSystemTheme,
} from "@databricks/design-system";
import type { AgentCard } from "@/lib/api";
import { extractResponseText } from "@/lib/response-text";
import { renderIcon } from "@/components/apx/Icon";

interface Message {
  id: number;
  role: "user" | "agent";
  text: string;
  error?: boolean;
}

export default function ChatPanel({ agent }: { agent: AgentCard }) {
  const { theme } = useDesignSystemTheme();
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
    [agent.tools],
  );

  const handleSend = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || loading) return;
      setInput("");
      setMessages((prev) => [
        ...prev,
        { id: nextId.current++, role: "user", text: trimmed },
      ]);
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
        const responseText =
          extractResponseText(data) ?? "No assistant text returned.";
        setMessages((prev) => [
          ...prev,
          { id: nextId.current++, role: "agent", text: responseText },
        ]);
      } catch (e: unknown) {
        setMessages((prev) => [
          ...prev,
          {
            id: nextId.current++,
            role: "agent",
            text: e instanceof Error ? e.message : String(e),
            error: true,
          },
        ]);
      } finally {
        setLoading(false);
      }
    },
    [agent.id, loading],
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: theme.spacing.sm,
          padding: `${theme.spacing.sm}px ${theme.spacing.md}px`,
          borderBottom: `1px solid ${theme.colors.border}`,
          background: theme.colors.backgroundSecondary,
          flexShrink: 0,
        }}
      >
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: theme.colors.textValidationSuccess,
            flexShrink: 0,
          }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <Typography.Text bold>{agent.display_name}</Typography.Text>{" "}
          <Typography.Text color="secondary" size="sm">
            {agent.description}
          </Typography.Text>
        </div>
        {agent.url && (
          <Typography.Link
            componentId="chat-a2a-card"
            href={`${agent.url}/.well-known/agent.json`}
            openInNewTab
            title="A2A discovery card (/.well-known/agent.json)"
          >
            A2A card
          </Typography.Link>
        )}
        <Link
          to="/agents/$agentId"
          params={{ agentId: agent.id }}
          style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
        >
          <Typography.Text color="secondary" size="sm">
            details
          </Typography.Text>
          {renderIcon(NewWindowIcon, { color: theme.colors.textSecondary })}
        </Link>
      </div>

      {/* Messages */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: `${theme.spacing.md}px`,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          gap: theme.spacing.sm,
        }}
      >
        {messages.length === 0 ? (
          <div
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: theme.spacing.md,
              textAlign: "center",
            }}
          >
            <Typography.Text color="secondary">
              Ask {agent.display_name} something
            </Typography.Text>
            {suggestedPrompts.length > 0 && (
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: theme.spacing.sm,
                  justifyContent: "center",
                  maxWidth: 448,
                }}
              >
                {suggestedPrompts.map((prompt, i) => (
                  <Button
                    key={i}
                    componentId={`chat-suggest-${i}`}
                    size="small"
                    onClick={() => setInput(prompt)}
                  >
                    {prompt}
                  </Button>
                ))}
              </div>
            )}
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.id}
              style={{
                display: "flex",
                justifyContent: msg.role === "user" ? "flex-end" : "flex-start",
              }}
            >
              <div
                style={{
                  maxWidth: "80%",
                  borderRadius: theme.borders.borderRadiusLg,
                  padding: `${theme.spacing.sm}px ${theme.spacing.md}px`,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  background:
                    msg.role === "user"
                      ? theme.colors.actionPrimaryBackgroundDefault
                      : msg.error
                        ? theme.colors.backgroundDanger
                        : theme.colors.backgroundSecondary,
                  color:
                    msg.role === "user"
                      ? theme.colors.actionPrimaryTextDefault
                      : msg.error
                        ? theme.colors.textValidationDanger
                        : theme.colors.textPrimary,
                }}
              >
                {msg.text}
              </div>
            </div>
          ))
        )}
        {loading && (
          <div style={{ display: "flex", justifyContent: "flex-start" }}>
            <div
              style={{
                borderRadius: theme.borders.borderRadiusLg,
                padding: `${theme.spacing.sm}px ${theme.spacing.md}px`,
                background: theme.colors.backgroundSecondary,
              }}
            >
              <Typography.Text color="secondary">Thinking…</Typography.Text>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div
        style={{
          borderTop: `1px solid ${theme.colors.border}`,
          padding: `${theme.spacing.sm}px ${theme.spacing.md}px`,
          display: "flex",
          gap: theme.spacing.sm,
          alignItems: "center",
          background: theme.colors.backgroundSecondary,
          flexShrink: 0,
        }}
      >
        <Input
          componentId="chat-input"
          value={input}
          aria-label={`Ask ${agent.display_name}`}
          onChange={(e) => setInput(e.target.value)}
          onPressEnter={() => handleSend(input)}
          placeholder={`Ask ${agent.display_name}…`}
          disabled={loading}
          allowClear
        />
        <Button
          componentId="chat-send"
          type="primary"
          aria-label="Send message"
          icon={renderIcon(SendIcon)}
          onClick={() => handleSend(input)}
          disabled={loading || !input.trim()}
        />
      </div>
    </div>
  );
}
