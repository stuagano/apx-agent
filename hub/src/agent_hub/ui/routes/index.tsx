import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import Navbar from "@/components/apx/navbar";
import ChatPanel from "@/components/apx/ChatPanel";
import { listAgents, type AgentCard } from "@/lib/api";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/")({
  component: AgentHub,
});

function StatusDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    live: "bg-emerald-400",
    unreachable: "bg-red-400",
    stub: "bg-amber-400",
    planned: "bg-slate-400",
  };
  return (
    <div className={cn("w-2 h-2 rounded-full shrink-0", colors[status] ?? "bg-slate-400")} />
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
  const isInvokable = agent.status === "live" && agent.supports_invoke;

  return (
    <button
      type="button"
      onClick={isInvokable ? onSelect : undefined}
      aria-disabled={!isInvokable}
      className={cn(
        "w-full text-left rounded-lg px-3 py-3 transition-colors",
        isInvokable ? "cursor-pointer hover:bg-accent/50" : "cursor-default opacity-40",
        selected && "bg-primary/10 border border-primary/30"
      )}
    >
      <div className="flex items-center gap-2 mb-1">
        <StatusDot status={agent.status} />
        <span
          className={cn(
            "text-sm font-medium truncate",
            selected ? "text-primary" : "text-foreground"
          )}
        >
          {agent.display_name}
        </span>
      </div>
      <p className="text-xs text-muted-foreground line-clamp-2 pl-4">{agent.description}</p>
      {agent.tags && agent.tags.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5 pl-4">
          {agent.tags.slice(0, 3).map((tag) => (
            <span key={tag} className="text-[10px] text-muted-foreground">
              #{tag}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}

function AgentHub() {
  const { data: agents, isLoading } = useQuery({
    queryKey: ["agents"],
    queryFn: listAgents,
  });

  const live = agents?.data.filter((a) => a.status === "live") ?? [];
  const other = agents?.data.filter((a) => a.status !== "live") ?? [];

  const [selectedAgent, setSelectedAgent] = useState<AgentCard | null>(null);

  return (
    <div className="flex flex-col bg-background" style={{ height: "100dvh" }}>
      <Navbar />

      <div className="flex flex-1 overflow-hidden min-h-0">
        {/* Left panel — agent list */}
        <div className="w-72 shrink-0 border-r flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b shrink-0">
            <h1 className="font-semibold text-sm">Agent Hub</h1>
            <p className="text-xs text-muted-foreground mt-0.5">Select an agent to chat</p>
          </div>

          <div className="flex-1 overflow-y-auto p-2 min-h-0">
            {isLoading ? (
              <div className="space-y-2 p-1">
                {[1, 2, 3].map((i) => (
                  <div key={i} className="h-16 bg-muted rounded-lg animate-pulse" />
                ))}
              </div>
            ) : (
              <>
                {live.length > 0 && (
                  <div className="mb-1">
                    <p className="px-3 py-1.5 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
                      Live
                    </p>
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
                    <p className="px-3 py-1.5 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
                      In Development
                    </p>
                    {other.map((a) => (
                      <AgentListItem
                        key={a.id}
                        agent={a}
                        selected={false}
                        onSelect={() => {}}
                      />
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        {/* Right panel — chat */}
        <div className="flex-1 overflow-hidden min-w-0">
          {selectedAgent ? (
            <ChatPanel key={selectedAgent.id} agent={selectedAgent} />
          ) : (
            <div className="flex flex-col items-center justify-center h-full gap-3 text-center px-4">
              <div className="text-4xl select-none">⬡</div>
              <p className="text-sm font-medium">Select an agent to start chatting</p>
              <p className="text-xs text-muted-foreground">Choose from the list on the left</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
