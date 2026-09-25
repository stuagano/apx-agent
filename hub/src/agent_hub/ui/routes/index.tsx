import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  Button,
  Spinner,
  Typography,
  useDesignSystemTheme,
} from "@databricks/design-system";
import Navbar from "@/components/apx/navbar";
import ChatPanel from "@/components/apx/ChatPanel";
import {
  discoverWorkspaceAgents,
  listAgents,
  type AgentCard,
} from "@/lib/api";

export const Route = createFileRoute("/")({
  component: AgentHub,
});

function useStatusColor(status: string): string {
  const { theme } = useDesignSystemTheme();
  const map: Record<string, string> = {
    live: theme.colors.textValidationSuccess,
    unreachable: theme.colors.textValidationDanger,
    stub: theme.colors.textValidationWarning,
    planned: theme.colors.textSecondary,
  };
  return map[status] ?? theme.colors.textSecondary;
}

function StatusDot({ status }: { status: string }) {
  const color = useStatusColor(status);
  return (
    <span
      style={{
        width: 8,
        height: 8,
        borderRadius: "50%",
        flexShrink: 0,
        background: color,
      }}
    />
  );
}

function AgentListItem({
  agent,
  selected,
  onSelect,
}: {
  agent: AgentCard;
  selected: boolean;
  onSelect: () => void;
}) {
  const { theme } = useDesignSystemTheme();
  const isInvokable = agent.status === "live" && agent.supports_invoke;

  return (
    <button
      type="button"
      onClick={isInvokable ? onSelect : undefined}
      aria-disabled={!isInvokable}
      style={{
        width: "100%",
        textAlign: "left",
        border: selected
          ? `1px solid ${theme.colors.actionDefaultBorderHover}`
          : "1px solid transparent",
        borderRadius: theme.borders.borderRadiusMd,
        padding: theme.spacing.sm,
        background: selected
          ? theme.colors.actionDefaultBackgroundPress
          : "transparent",
        cursor: isInvokable ? "pointer" : "default",
        opacity: isInvokable ? 1 : 0.4,
      }}
      onMouseEnter={(e) => {
        if (isInvokable && !selected)
          e.currentTarget.style.background =
            theme.colors.actionDefaultBackgroundHover;
      }}
      onMouseLeave={(e) => {
        if (!selected) e.currentTarget.style.background = "transparent";
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: theme.spacing.sm,
          marginBottom: 2,
        }}
      >
        <StatusDot status={agent.status} />
        <Typography.Text bold ellipsis>
          {agent.display_name}
        </Typography.Text>
      </div>
      <Typography.Paragraph
        color="secondary"
        withoutMargins
        ellipsis={{ rows: 2 }}
      >
        {agent.description}
      </Typography.Paragraph>
      {agent.tags && agent.tags.length > 0 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: theme.spacing.xs,
            marginTop: theme.spacing.xs,
          }}
        >
          {agent.tags.slice(0, 3).map((tag) => (
            <Typography.Hint key={tag}>#{tag}</Typography.Hint>
          ))}
        </div>
      )}
    </button>
  );
}

function AgentHub() {
  const { theme } = useDesignSystemTheme();
  const queryClient = useQueryClient();
  const { data: agents, isLoading } = useQuery({
    queryKey: ["agents"],
    queryFn: () => listAgents(),
  });

  const discover = useMutation({
    mutationFn: () => discoverWorkspaceAgents(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  // Re-run workspace discovery once on mount so the UI picks up Apps that
  // came online after Hub startup (server already discovers in lifespan).
  useEffect(() => {
    discover.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional one-shot on mount
  }, []);

  const live = agents?.data.filter((a) => a.status === "live") ?? [];
  const other = agents?.data.filter((a) => a.status !== "live") ?? [];

  const [selectedAgent, setSelectedAgent] = useState<AgentCard | null>(null);

  const sectionLabel = (label: string) => (
    <div
      style={{ padding: `${theme.spacing.xs}px ${theme.spacing.sm}px` }}
    >
      <Typography.Text
        color="secondary"
        size="sm"
        bold
        style={{ textTransform: "uppercase", letterSpacing: 0.4 }}
      >
        {label}
      </Typography.Text>
    </div>
  );

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100dvh",
        background: theme.colors.backgroundPrimary,
      }}
    >
      <Navbar />

      <div
        style={{
          display: "flex",
          flex: 1,
          overflow: "hidden",
          minHeight: 0,
        }}
      >
        {/* Left panel — agent list */}
        <div
          style={{
            width: 288,
            flexShrink: 0,
            borderRight: `1px solid ${theme.colors.border}`,
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              padding: theme.spacing.md,
              borderBottom: `1px solid ${theme.colors.border}`,
              flexShrink: 0,
              display: "flex",
              flexDirection: "column",
              gap: theme.spacing.sm,
            }}
          >
            <div>
              <Typography.Title level={4} withoutMargins>
                Agent Hub
              </Typography.Title>
              <Typography.Text color="secondary" size="sm">
                {discover.isPending
                  ? "Discovering workspace agents…"
                  : "Select an agent to chat"}
              </Typography.Text>
            </div>
            <Button
              componentId="refresh-discovery"
              size="small"
              onClick={() => discover.mutate()}
              loading={discover.isPending}
              block
            >
              {discover.isPending ? "Refreshing…" : "Refresh discovery"}
            </Button>
            {discover.isError && (
              <Typography.Text color="error" size="sm">
                Discovery failed — check Hub logs.
              </Typography.Text>
            )}
          </div>

          <div
            style={{
              flex: 1,
              overflowY: "auto",
              padding: theme.spacing.sm,
              minHeight: 0,
            }}
          >
            {isLoading ? (
              <div
                style={{
                  display: "flex",
                  justifyContent: "center",
                  padding: theme.spacing.lg,
                }}
              >
                <Spinner />
              </div>
            ) : (
              <>
                {live.length > 0 && (
                  <div style={{ marginBottom: theme.spacing.xs }}>
                    {sectionLabel("Live")}
                    {live.map((a) => (
                      <AgentListItem
                        key={a.id}
                        agent={a}
                        selected={selectedAgent?.id === a.id}
                        onSelect={() => setSelectedAgent(a)}
                      />
                    ))}
                  </div>
                )}
                {other.length > 0 && (
                  <div>
                    {sectionLabel("Other")}
                    {other.map((a) => (
                      <AgentListItem
                        key={a.id}
                        agent={a}
                        selected={selectedAgent?.id === a.id}
                        onSelect={() => setSelectedAgent(a)}
                      />
                    ))}
                  </div>
                )}
                {!live.length && !other.length && (
                  <div style={{ padding: theme.spacing.md }}>
                    <Typography.Text color="secondary" size="sm">
                      {discover.isPending
                        ? "Scanning Databricks Apps for A2A cards…"
                        : "No agents found yet. Deploy an Apps agent with /.well-known/agent.json, or use Refresh."}
                    </Typography.Text>
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        {/* Right panel — chat */}
        <div style={{ flex: 1, overflow: "hidden", minWidth: 0 }}>
          {selectedAgent ? (
            <ChatPanel key={selectedAgent.id} agent={selectedAgent} />
          ) : (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                height: "100%",
                gap: theme.spacing.sm,
                textAlign: "center",
                padding: theme.spacing.md,
              }}
            >
              <Typography.Title level={3} withoutMargins>
                Select an agent to start chatting
              </Typography.Title>
              <Typography.Text color="secondary">
                Choose from the list on the left
              </Typography.Text>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
