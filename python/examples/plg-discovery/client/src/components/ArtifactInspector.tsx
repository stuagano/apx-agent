import { Artifact } from "../api";

export function ArtifactInspector(
  { artifact, onClose }: { artifact: Artifact | null; onClose: () => void },
) {
  if (!artifact) return null;
  return (
    <div className="inspector-overlay" onClick={onClose}>
      <div className="inspector-panel" onClick={(e) => e.stopPropagation()}>
        <div className="inspector-header">
          <span>{artifact.type.replace(/_/g, " ")}</span>
          <button className="inspector-close" onClick={onClose}>Close</button>
        </div>
        <div className="inspector-body">
          <pre>{JSON.stringify(artifact, null, 2)}</pre>
        </div>
      </div>
    </div>
  );
}
