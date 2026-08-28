import { Artifact, Gate } from "../api";

type Step = {
  id: string;
  label: string;
  description: string;
};

const STEPS: Step[] = [
  { id: "org_profile", label: "Organization Profile", description: "Tell us about your mission" },
  { id: "current_systems", label: "Current Systems", description: "What tools do you use today?" },
  { id: "domain_relevance", label: "Domain Analysis", description: "Matching your needs to solutions" },
  { id: "blueprint", label: "Technology Blueprint", description: "Your recommended tech stack" },
];

type Props = {
  artifacts: Artifact[];
  gate: Gate | null;
  onInspect: (a: Artifact) => void;
};

function getActiveStep(artifacts: Artifact[], gate: Gate | null): number {
  const byType = new Map(artifacts.map((a) => [a.type, a]));
  if (byType.has("blueprint")) return 4;
  if (byType.has("domain_relevance")) return 3;
  if (gate && gate.filled.length > 0) return 2;
  if (byType.has("org_profile")) return 1;
  return 0;
}

function getStepStatus(
  index: number,
  activeStep: number,
  artifacts: Artifact[],
  gate: Gate | null,
): "done" | "active" | "upcoming" {
  const byType = new Map(artifacts.map((a) => [a.type, a]));
  const step = STEPS[index];

  if (step.id === "current_systems") {
    if (gate?.complete) return "done";
    if (gate && gate.filled.length > 0) return "active";
    if (activeStep > index) return "done";
    if (activeStep === index) return "active";
    return "upcoming";
  }

  if (byType.has(step.id)) return "done";
  if (activeStep === index) return "active";
  if (activeStep > index) return "done";
  return "upcoming";
}

export function ProgressRail({ artifacts, gate, onInspect }: Props) {
  const activeStep = getActiveStep(artifacts, gate);
  const byType = new Map(artifacts.map((a) => [a.type, a]));

  return (
    <aside className="progress-rail">
      <div className="progress-rail__header">
        <span className="progress-rail__label">Discovery Progress</span>
        <span className="progress-rail__count">
          {STEPS.filter((_, i) => getStepStatus(i, activeStep, artifacts, gate) === "done").length} of {STEPS.length}
        </span>
      </div>

      <ol className="progress-rail__steps">
        {STEPS.map((step, i) => {
          const status = getStepStatus(i, activeStep, artifacts, gate);
          const artifact = byType.get(step.id);

          return (
            <li
              key={step.id}
              className={`progress-step progress-step--${status}`}
              onClick={() => artifact && onInspect(artifact)}
            >
              <div className="progress-step__connector">
                <span className="progress-step__dot" />
                {i < STEPS.length - 1 && <span className="progress-step__line" />}
              </div>
              <div className="progress-step__content">
                <span className="progress-step__label">{step.label}</span>
                <span className="progress-step__desc">
                  {status === "done" ? "Complete" : status === "active" ? "In progress..." : step.description}
                </span>
                {step.id === "current_systems" && gate && status !== "upcoming" && (
                  <div className="progress-step__gate">
                    <div className="progress-step__gate-bar">
                      <div
                        className="progress-step__gate-fill"
                        style={{ width: `${(gate.filled.length / (gate.filled.length + gate.missing.length)) * 100}%` }}
                      />
                    </div>
                    <span className="progress-step__gate-text">
                      {gate.filled.length}/{gate.filled.length + gate.missing.length} discovered
                    </span>
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>

      {gate && gate.missing.length > 0 && activeStep >= 1 && (
        <div className="progress-rail__hint">
          <span className="progress-rail__hint-icon">?</span>
          <span>Still learning about: {gate.missing.slice(0, 2).join(", ")}{gate.missing.length > 2 ? "..." : ""}</span>
        </div>
      )}
    </aside>
  );
}
