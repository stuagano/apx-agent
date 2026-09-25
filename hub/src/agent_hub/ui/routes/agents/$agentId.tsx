import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Alert,
  ArrowLeftIcon,
  Button,
  Input,
  Spinner,
  Tag,
  Typography,
  useDesignSystemTheme,
} from "@databricks/design-system";
import Navbar from "@/components/apx/navbar";
import { getAgent, type AgentCard } from "@/lib/api";
import { extractResponseText } from "@/lib/response-text";
import { renderIcon } from "@/components/apx/Icon";

export const Route = createFileRoute("/agents/$agentId")({
  component: AgentDetail,
});

function safeHost(url: string): string {
  // Guard new URL() — a malformed agent.url must not throw and blank the view.
  try {
    return new URL(url).hostname.split("-7474")[0];
  } catch {
    return url;
  }
}

type TagColor = "lime" | "lemon" | "coral" | "charcoal";

function StatusBadge({ status }: { status: string | undefined }) {
  const config: Record<string, { label: string; color: TagColor }> = {
    live: { label: "Live", color: "lime" },
    stub: { label: "Stub", color: "lemon" },
    unreachable: { label: "Unreachable", color: "coral" },
  };
  const c = (status && config[status]) || config.stub;
  return (
    <Tag color={c.color} componentId={`status-${status}`}>
      {c.label}
    </Tag>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div style={{ marginBottom: theme.spacing.md }}>
      <Typography.Text
        color="secondary"
        size="sm"
        bold
        style={{ textTransform: "uppercase", letterSpacing: 0.4 }}
      >
        {children}
      </Typography.Text>
    </div>
  );
}

function Panel({ children }: { children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <div
      style={{
        border: `1px solid ${theme.colors.border}`,
        borderRadius: theme.borders.borderRadiusLg,
        background: theme.colors.backgroundSecondary,
        padding: theme.spacing.md,
      }}
    >
      {children}
    </div>
  );
}

function CodeBlock({ children }: { children: React.ReactNode }) {
  const { theme } = useDesignSystemTheme();
  return (
    <code
      style={{
        display: "block",
        fontFamily: "monospace",
        fontSize: 13,
        background: theme.colors.backgroundPrimary,
        border: `1px solid ${theme.colors.border}`,
        borderRadius: theme.borders.borderRadiusMd,
        padding: `${theme.spacing.sm}px ${theme.spacing.md}px`,
        wordBreak: "break-all",
        marginTop: theme.spacing.xs,
      }}
    >
      {children}
    </code>
  );
}

function TryItPanel({ agent }: { agent: AgentCard }) {
  const { theme } = useDesignSystemTheme();
  const [input, setInput] = useState("");
  const [response, setResponse] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSend = async () => {
    if (!input.trim()) return;
    setLoading(true);
    setError(null);
    setResponse(null);
    try {
      const res = await fetch(`/api/agents/${agent.id}/invoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: input.trim() }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`${res.status}: ${text}`);
      }
      const data = await res.json();
      setResponse(extractResponseText(data) ?? "No assistant text returned.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  if (!agent.url || agent.status !== "live") return null;

  if (!agent.supports_invoke) {
    return (
      <section>
        <SectionTitle>Try It</SectionTitle>
        <Panel>
          <Typography.Text color="secondary">
            This agent uses the full AppKit UI.{" "}
          </Typography.Text>
          <Typography.Link
            componentId="tryit-open-ui"
            href={`${agent.url}/_apx/agent`}
            openInNewTab
          >
            Open agent UI
          </Typography.Link>
        </Panel>
      </section>
    );
  }

  return (
    <section>
      <SectionTitle>Try It</SectionTitle>
      <Panel>
        <div style={{ display: "flex", gap: theme.spacing.sm }}>
          <Input
            componentId="tryit-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPressEnter={handleSend}
            placeholder="Ask this agent something..."
            allowClear
          />
          <Button
            componentId="tryit-send"
            type="primary"
            onClick={handleSend}
            loading={loading}
            disabled={!input.trim()}
          >
            {loading ? "Sending..." : "Send"}
          </Button>
        </div>
        {error && (
          <div style={{ marginTop: theme.spacing.md }}>
            <Alert
              componentId="tryit-error"
              type="error"
              message={error}
              closable={false}
            />
          </div>
        )}
        {response && (
          <div
            style={{
              marginTop: theme.spacing.md,
              padding: theme.spacing.md,
              borderRadius: theme.borders.borderRadiusMd,
              background: theme.colors.backgroundPrimary,
              fontFamily: "monospace",
              fontSize: 13,
              whiteSpace: "pre-wrap",
              lineHeight: 1.6,
              maxHeight: 384,
              overflowY: "auto",
            }}
          >
            {response}
          </div>
        )}
      </Panel>
    </section>
  );
}

function AgentDetail() {
  const { theme } = useDesignSystemTheme();
  const { agentId } = Route.useParams();
  const { data, isLoading, error } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => getAgent({ agent_id: agentId }),
  });

  const agent = data?.data;

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        background: theme.colors.backgroundPrimary,
      }}
    >
      <Navbar />

      <main
        style={{
          flex: 1,
          maxWidth: 768,
          margin: "0 auto",
          width: "100%",
          padding: `${theme.spacing.lg}px ${theme.spacing.md}px`,
        }}
      >
        <Link
          to="/"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: theme.spacing.xs,
            marginBottom: theme.spacing.lg,
          }}
        >
          {renderIcon(ArrowLeftIcon, { color: theme.colors.textSecondary })}
          <Typography.Text color="secondary" size="sm">
            All Agents
          </Typography.Text>
        </Link>

        {isLoading && (
          <div style={{ display: "flex", justifyContent: "center" }}>
            <Spinner />
          </div>
        )}

        {error && (
          <Alert
            componentId="agent-load-error"
            type="error"
            message={`Failed to load agent: ${String(error)}`}
            closable={false}
          />
        )}

        {agent && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: theme.spacing.lg,
            }}
          >
            {/* Header */}
            <div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: theme.spacing.sm,
                  marginBottom: theme.spacing.sm,
                }}
              >
                <Typography.Title level={2} withoutMargins>
                  {agent.display_name}
                </Typography.Title>
                <StatusBadge status={agent.status} />
              </div>
              <Typography.Paragraph color="secondary" withoutMargins>
                {agent.description}
              </Typography.Paragraph>

              {/* Meta */}
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: theme.spacing.md,
                  marginTop: theme.spacing.md,
                  alignItems: "center",
                }}
              >
                {agent.url && (
                  <Typography.Link
                    componentId="agent-host-link"
                    href={agent.url}
                    openInNewTab
                  >
                    {safeHost(agent.url)}
                  </Typography.Link>
                )}
                {agent.url && agent.status === "live" && (
                  <Typography.Link
                    componentId="agent-open-ui"
                    href={`${agent.url}/_apx/agent`}
                    openInNewTab
                  >
                    Open full agent UI
                  </Typography.Link>
                )}
                {agent.mcp_endpoint && (
                  <Typography.Text color="secondary" size="sm">
                    MCP enabled
                  </Typography.Text>
                )}
              </div>

              {/* Tags */}
              {agent.tags && agent.tags.length > 0 && (
                <div
                  style={{
                    display: "flex",
                    flexWrap: "wrap",
                    gap: theme.spacing.xs,
                    marginTop: theme.spacing.md,
                  }}
                >
                  {agent.tags.map((tag) => (
                    <Tag key={tag} color="charcoal" componentId={`tag-${tag}`}>
                      #{tag}
                    </Tag>
                  ))}
                </div>
              )}
            </div>

            {/* Tools */}
            <section>
              <SectionTitle>Tools ({agent.tools.length})</SectionTitle>
              {agent.tools.length === 0 ? (
                <Typography.Text color="secondary">
                  No tools registered.
                </Typography.Text>
              ) : (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: theme.spacing.sm,
                  }}
                >
                  {agent.tools.map((tool) => (
                    <Panel key={tool.name}>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "flex-start",
                          gap: theme.spacing.sm,
                        }}
                      >
                        <code
                          style={{
                            flexShrink: 0,
                            fontFamily: "monospace",
                            fontSize: 12,
                            background: theme.colors.backgroundPrimary,
                            borderRadius: theme.borders.borderRadiusSm,
                            padding: `2px ${theme.spacing.xs}px`,
                          }}
                        >
                          {tool.name}
                        </code>
                        <Typography.Text color="secondary" size="sm">
                          {tool.description}
                        </Typography.Text>
                      </div>
                    </Panel>
                  ))}
                </div>
              )}
            </section>

            {/* Try It */}
            <TryItPanel agent={agent} />

            {/* Connection Info */}
            {agent.url && agent.status === "live" && (
              <section>
                <SectionTitle>Connect</SectionTitle>
                <Panel>
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: theme.spacing.md,
                    }}
                  >
                    <div>
                      <Typography.Text color="secondary" size="sm" bold>
                        Responses API
                      </Typography.Text>
                      <CodeBlock>POST {agent.url}/responses</CodeBlock>
                    </div>
                    <div>
                      <Typography.Text color="secondary" size="sm" bold>
                        A2A Discovery
                      </Typography.Text>
                      <Typography.Link
                        componentId="connect-a2a"
                        href={`${agent.url}/.well-known/agent.json`}
                        openInNewTab
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          marginTop: theme.spacing.xs,
                        }}
                      >
                        GET {agent.url}/.well-known/agent.json
                      </Typography.Link>
                    </div>
                    {agent.mcp_endpoint && (
                      <div>
                        <Typography.Text color="secondary" size="sm" bold>
                          MCP Server
                        </Typography.Text>
                        <CodeBlock>POST {agent.mcp_endpoint}</CodeBlock>
                      </div>
                    )}
                  </div>
                </Panel>
              </section>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
