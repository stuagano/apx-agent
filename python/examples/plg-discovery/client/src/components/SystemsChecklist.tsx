import { Gate } from "../api";

export function SystemsChecklist({ gate }: { gate: Gate }) {
  return (
    <div className="checklist-card">
      <div className="checklist-title">
        Current Systems
        <span className={`checklist-badge ${gate.complete ? "checklist-badge--complete" : "checklist-badge--pending"}`}>
          {gate.complete ? "Complete" : "Required"}
        </span>
      </div>
      <ul className="checklist-list">
        {gate.filled.map((c) => (
          <li key={c} className="checklist-item checklist-item--filled">
            <span className="checklist-icon checklist-icon--filled">&#10003;</span>
            {c}
          </li>
        ))}
        {gate.missing.map((c) => (
          <li key={c} className="checklist-item checklist-item--missing">
            <span className="checklist-icon checklist-icon--missing" />
            {c}
          </li>
        ))}
      </ul>
    </div>
  );
}
