import { Artifact } from "../api";

type Line = {
  domain: string;
  current_system: string | null;
  decision: string;
  target: string | null;
  justification: string;
};

function decisionClass(decision: string): string {
  const d = decision.toLowerCase();
  if (d === "keep") return "blueprint-decision--keep";
  if (d === "replace") return "blueprint-decision--replace";
  return "blueprint-decision--adopt";
}

export function BlueprintView({ artifact }: { artifact: Artifact }) {
  const lines = (artifact.lines as Line[]) ?? [];
  return (
    <div className="blueprint-card">
      <div className="blueprint-header">
        <h3>Technology Blueprint</h3>
      </div>
      <table className="blueprint-table">
        <thead>
          <tr>
            <th>Domain</th>
            <th>Current</th>
            <th>Decision</th>
            <th>Recommended</th>
            <th>Rationale</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((l, i) => (
            <tr key={i}>
              <td>{l.domain}</td>
              <td>{l.current_system ?? "—"}</td>
              <td>
                <span className={`blueprint-decision ${decisionClass(l.decision)}`}>
                  {l.decision}
                </span>
              </td>
              <td>{l.target ?? "—"}</td>
              <td>{l.justification}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
