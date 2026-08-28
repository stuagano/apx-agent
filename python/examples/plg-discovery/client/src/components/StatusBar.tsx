import { Artifact } from "../api";

const NODES = [
  { type: "org_profile", label: "Profile" },
  { type: "domain_relevance", label: "Domains" },
  { type: "blueprint", label: "Blueprint" },
];

export function StatusBar(
  { artifacts, onInspect }: { artifacts: Artifact[]; onInspect: (a: Artifact) => void },
) {
  const byType = new Map(artifacts.map((a) => [a.type, a]));
  return (
    <div className="status-bar">
      {NODES.map((n) => {
        const done = byType.get(n.type);
        return (
          <button
            key={n.type}
            className={`status-node ${done ? "status-node--done" : ""}`}
            onClick={() => done && onInspect(done)}
          >
            <span className={`status-dot ${done ? "status-dot--done" : ""}`} />
            {n.label}
          </button>
        );
      })}
    </div>
  );
}
