import React from 'react';
import { EdgeData, AgentNodeData } from '../types';
import { generateBezierPath, getPortCoordinates } from '../utils';

interface EdgeViewProps {
  edge: EdgeData;
  sourceNode: AgentNodeData;
  targetNode: AgentNodeData;
  isSelected: boolean;
  onSelect: (e: React.MouseEvent, edge: EdgeData) => void;
  isDimmed?: boolean;
}

export const EdgeView: React.FC<EdgeViewProps> = ({
  edge,
  sourceNode,
  targetNode,
  isSelected,
  onSelect,
  isDimmed,
}) => {
  if (!sourceNode || !targetNode) return null;

  const start = getPortCoordinates(sourceNode, edge.sourcePort);
  const end = getPortCoordinates(targetNode, edge.targetPort);
  const pathD = generateBezierPath(start.x, start.y, end.x, end.y, edge.sourcePort, edge.targetPort);

  const opacity = isDimmed ? 0.2 : 1;
  const strokeColor = isSelected ? '#FF3621' : '#475569';
  const strokeWidth = isSelected ? 2 : 1.5;

  return (
    <g onClick={(e) => onSelect(e, edge)} style={{ opacity, transition: 'opacity 0.2s' }}>
      {/* Invisible thick path for easier click selection */}
      <path d={pathD} stroke="transparent" strokeWidth="20" fill="none" className="cursor-pointer" />

      <defs>
        <marker
          id={`arrow-${edge.id}`}
          markerWidth="12"
          markerHeight="12"
          refX="9"
          refY="6"
          orient="auto"
          markerUnits="userSpaceOnUse"
        >
          <path d="M2,2 L10,6 L2,10 L4,6 Z" fill={strokeColor} />
        </marker>
      </defs>

      {/* Visible path with arrowhead */}
      <path
        d={pathD}
        stroke={strokeColor}
        strokeWidth={strokeWidth}
        fill="none"
        markerEnd={`url(#arrow-${edge.id})`}
      />
    </g>
  );
};
