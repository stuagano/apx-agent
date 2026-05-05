import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ExternalLink,
  Wrench,
  CheckCircle2,
  Clock,
  AlertCircle,
  Globe,
  Server,
  Send,
} from "lucide-react";
import Navbar from "@/components/apx/navbar";
import { getAgent, type AgentCard } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useState } from "react";

export const Route = createFileRoute("/agents/$agentId")({
  component: AgentDetail,
});

function StatusBadge({ status }: { status: string }) {
  const config: Record<string, { label: string; icon: typeof CheckCircle2; className: string }> = {
    live: { label: "Live", icon: CheckCircle2, className: "text-emerald-400 bg-emerald-400/10 border-emerald-400/20" },
    stub: { label: "Stub", icon: Clock, className: "text-amber-400 bg-amber-400/10 border-amber-400/20" },
    unreachable: { label: "Unreachable", icon: AlertCircle, className: "text-red-400 bg-red-400/10 border-red-400/20" },
  };
  const c = config[status] ?? config.stub;
  const Icon = c.icon;
  return (
    <span className={cn("inline-flex items-center gap-1.5 px-3 py-1 rounded-full border text-sm font-medium", c.className)}>
      <Icon size={14} />
      {c.label}
    </span>
  );
}

function TryItPanel({ agent }: { agent: AgentCard }) {
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
      setResponse(data.output_text ?? JSON.stringify(data, null, 2));
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
        <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-4">Try It</h2>
        <div className="rounded-xl border bg-card p-5 text-sm text-muted-foreground">
          This agent uses the full AppKit UI.{" "}
          <a href={`${agent.url}/_apx/agent`} target="_blank" rel="noopener noreferrer" className="text-foreground underline underline-offset-2 hover:no-underline inline-flex items-center gap-1">
            Open agent UI <ExternalLink size={12} />
          </a>
        </div>
      </section>
    );
  }

  return (
    <section>
      <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-4">
        Try It
      </h2>
      <div className="rounded-xl border bg-card p-5">
        <div className="flex gap-2 mb-4">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSend()}
            placeholder="Ask this agent something..."
            className="flex-1 px-3 py-2 rounded-lg border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <button
            onClick={handleSend}
            disabled={loading || !input.trim()}
            className={cn(
              "px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-2 transition-colors",
              "bg-primary text-primary-foreground hover:bg-primary/90",
              "disabled:opacity-50 disabled:cursor-not-allowed"
            )}
          >
            <Send size={14} />
            {loading ? "Sending..." : "Send"}
          </button>
        </div>
        {error && (
          <div className="p-3 rounded-lg border border-destructive/30 bg-destructive/10 text-sm text-destructive-foreground mb-3">
            {error}
          </div>
        )}
        {response && (
          <div className="p-4 rounded-lg bg-muted/50 text-sm whitespace-pre-wrap font-mono leading-relaxed max-h-96 overflow-y-auto">
            {response}
          </div>
        )}
      </div>
    </section>
  );
}

function AgentDetail() {
  const { agentId } = Route.useParams();
  const { data, isLoading, error } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => getAgent({ agent_id: agentId }),
  });

  const agent = data?.data;

  return (
    <div className="min-h-screen flex flex-col bg-background">
      <Navbar />

      <main className="flex-1 max-w-3xl mx-auto w-full px-4 py-10">
        {/* Back link */}
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors mb-8"
        >
          <ArrowLeft size={14} />
          All Agents
        </Link>

        {isLoading && (
          <div className="space-y-4 animate-pulse">
            <div className="h-8 bg-muted rounded w-2/5" />
            <div className="h-4 bg-muted rounded w-4/5" />
            <div className="h-4 bg-muted rounded w-3/5" />
          </div>
        )}

        {error && (
          <div className="p-4 rounded-lg border border-destructive/30 bg-destructive/10 text-sm text-destructive-foreground">
            Failed to load agent: {String(error)}
          </div>
        )}

        {agent && (
          <div className="space-y-8">
            {/* Header */}
            <div>
              <div className="flex items-center gap-3 mb-3">
                <h1 className="text-2xl font-bold tracking-tight">{agent.display_name}</h1>
                <StatusBadge status={agent.status} />
              </div>
              <p className="text-muted-foreground leading-relaxed">{agent.description}</p>

              {/* Meta */}
              <div className="flex flex-wrap gap-4 mt-4 text-sm text-muted-foreground">
                {agent.url && (
                  <a
                    href={agent.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 hover:text-foreground transition-colors"
                  >
                    <Globe size={14} />
                    {new URL(agent.url).hostname.split("-7474")[0]}
                    <ExternalLink size={12} />
                  </a>
                )}
                {agent.url && agent.status === "live" && (
                  <a
                    href={`${agent.url}/_apx/agent`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border/60 bg-muted/50 hover:bg-accent hover:text-foreground transition-colors text-xs font-medium"
                  >
                    Open full agent UI
                    <ExternalLink size={11} />
                  </a>
                )}
                {agent.mcp_endpoint && (
                  <span className="inline-flex items-center gap-1.5">
                    <Server size={14} />
                    MCP enabled
                  </span>
                )}
                {agent.workstream && (
                  <span className="inline-flex items-center gap-1.5">
                    workstream: {agent.workstream}
                  </span>
                )}
              </div>

              {/* Tags */}
              {agent.tags && agent.tags.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-4">
                  {agent.tags.map((tag) => (
                    <span key={tag} className="px-2.5 py-0.5 rounded-full bg-muted text-muted-foreground text-xs">
                      #{tag}
                    </span>
                  ))}
                </div>
              )}
            </div>

            {/* Tools */}
            <section>
              <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-4 flex items-center gap-2">
                <Wrench size={14} />
                Tools ({agent.tools.length})
              </h2>
              {agent.tools.length === 0 ? (
                <p className="text-sm text-muted-foreground italic">No tools registered.</p>
              ) : (
                <div className="grid gap-2">
                  {agent.tools.map((tool) => (
                    <div
                      key={tool.name}
                      className="flex items-start gap-3 p-3 rounded-lg border bg-card hover:border-border/80 transition-colors"
                    >
                      <code className="shrink-0 px-2 py-0.5 rounded bg-muted text-xs font-mono">
                        {tool.name}
                      </code>
                      <span className="text-sm text-muted-foreground">{tool.description}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* Try It */}
            <TryItPanel agent={agent} />

            {/* Connection Info */}
            {agent.url && agent.status === "live" && (
              <section>
                <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-4">
                  Connect
                </h2>
                <div className="rounded-xl border bg-card p-5 space-y-3">
                  <div>
                    <span className="text-xs text-muted-foreground font-medium">Responses API</span>
                    <code className="block mt-1 text-sm font-mono bg-muted px-3 py-2 rounded-lg break-all">
                      POST {agent.url}/responses
                    </code>
                  </div>
                  <div>
                    <span className="text-xs text-muted-foreground font-medium">A2A Discovery</span>
                    <code className="block mt-1 text-sm font-mono bg-muted px-3 py-2 rounded-lg break-all">
                      GET {agent.url}/.well-known/agent.json
                    </code>
                  </div>
                  {agent.mcp_endpoint && (
                    <div>
                      <span className="text-xs text-muted-foreground font-medium">MCP Server</span>
                      <code className="block mt-1 text-sm font-mono bg-muted px-3 py-2 rounded-lg break-all">
                        POST {agent.mcp_endpoint}
                      </code>
                    </div>
                  )}
                </div>
              </section>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
