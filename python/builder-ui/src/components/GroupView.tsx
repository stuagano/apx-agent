import React from 'react';
import { AgentNodeData } from '../types';

export type ResizeCorner = 'nw' | 'ne' | 'sw' | 'se';

interface GroupViewProps {
  node: AgentNodeData;
  isSelected: boolean;
  isDimmed?: boolean;
  onMouseDown: (e: React.MouseEvent, node: AgentNodeData) => void;
  onContextMenu: (e: React.MouseEvent, node: AgentNodeData) => void;
  onDoubleClick: (e: React.MouseEvent, node: AgentNodeData) => void;
  onResizeStart: (e: React.MouseEvent, node: AgentNodeData, corner: ResizeCorner) => void;
  isEditing: boolean;
  onEditChange: (val: string) => void;
  onEditComplete: () => void;
}

const CORNER_SIZE = 8;
const BORDER_COLOR = '#7c3aed';
const FILL_COLOR = '#ede9fe';
const LABEL_COLOR = '#5b21b6';

export const GroupView: React.FC<GroupViewProps> = ({
  node,
  isSelected,
  isDimmed,
  onMouseDown,
  onContextMenu,
  onDoubleClick,
  onResizeStart,
  isEditing,
  onEditChange,
  onEditComplete,
}) => {
  const { x, y, width, height, label } = node;
  const stroke = isSelected ? '#FF3621' : BORDER_COLOR;
  const strokeWidth = isSelected ? 2 : 1.5;
  const opacity = isDimmed ? 0.3 : 1;

  const corners: { corner: ResizeCorner; cx: number; cy: number; cursor: string }[] = [
    { corner: 'nw', cx: 0,     cy: 0,      cursor: 'nwse-resize' },
    { corner: 'ne', cx: width, cy: 0,      cursor: 'nesw-resize' },
    { corner: 'sw', cx: 0,     cy: height, cursor: 'nesw-resize' },
    { corner: 'se', cx: width, cy: height, cursor: 'nwse-resize' },
  ];

  return (
    <g
      transform={`translate(${x}, ${y})`}
      style={{ opacity, transition: 'opacity 0.2s' }}
      onMouseDown={(e) => onMouseDown(e, node)}
      onContextMenu={(e) => onContextMenu(e, node)}
      onDoubleClick={(e) => onDoubleClick(e, node)}
      className="cursor-move select-none"
    >
      {/* Background fill */}
      <rect
        width={width}
        height={height}
        fill={FILL_COLOR}
        fillOpacity="0.25"
        stroke={stroke}
        strokeWidth={strokeWidth}
        strokeDasharray="8 4"
        rx="10"
      />

      {/* Label bar at top */}
      <rect
        width={width}
        height={24}
        fill={FILL_COLOR}
        fillOpacity="0.6"
        rx="10"
      />
      <rect y={12} width={width} height={12} fill={FILL_COLOR} fillOpacity="0.6" />

      {/* Label text */}
      <foreignObject x="0" y="0" width={width} height={26}>
        <div
          className="flex items-center px-3 py-1"
          style={{ color: LABEL_COLOR }}
        >
          {isEditing ? (
            <input
              autoFocus
              value={label}
              onChange={(e) => onEditChange(e.target.value)}
              onBlur={onEditComplete}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onEditComplete();
                e.stopPropagation();
              }}
              onMouseDown={(e) => e.stopPropagation()}
              className="bg-white/80 rounded border px-1 font-bold text-[11px] leading-tight outline-none w-full"
              style={{ color: LABEL_COLOR, borderColor: BORDER_COLOR }}
            />
          ) : (
            <span className="font-semibold text-[11px] select-none">{label}</span>
          )}
        </div>
      </foreignObject>

      {/* Corner resize handles — only visible when selected */}
      {isSelected && corners.map(({ corner, cx, cy, cursor }) => (
        <rect
          key={corner}
          x={cx - CORNER_SIZE / 2}
          y={cy - CORNER_SIZE / 2}
          width={CORNER_SIZE}
          height={CORNER_SIZE}
          fill="white"
          stroke={stroke}
          strokeWidth={1.5}
          rx="2"
          style={{ cursor }}
          onMouseDown={(e) => { e.stopPropagation(); onResizeStart(e, node, corner); }}
        />
      ))}
    </g>
  );
};
