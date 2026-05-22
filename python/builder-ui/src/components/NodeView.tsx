import React from 'react';
import { AgentNodeData, AgentNodeType, PortPosition } from '../types';
import { NODE_COLORS } from '../constants';
import { Bot, Cpu, Search, Wrench, Database, GitBranch } from 'lucide-react';

interface NodeViewProps {
  node: AgentNodeData;
  isSelected: boolean;
  isEditing: boolean;
  isDimmed?: boolean;
  onMouseDown: (e: React.MouseEvent, node: AgentNodeData) => void;
  onContextMenu: (e: React.MouseEvent, node: AgentNodeData) => void;
  onPortMouseDown: (e: React.MouseEvent, node: AgentNodeData, port: PortPosition) => void;
  onDoubleClick: (e: React.MouseEvent, node: AgentNodeData) => void;
  onEditChange: (val: string) => void;
  onEditComplete: () => void;
}

const NodeIcon = ({ type, color }: { type: AgentNodeType; color: string }) => {
  const props = { size: 16, color };
  switch (type) {
    case 'supervisor': return <Bot {...props} />;
    case 'router': return <GitBranch {...props} />;
    case 'llm': return <Cpu {...props} />;
    case 'vector_search': return <Search {...props} />;
    case 'uc_function': return <Wrench {...props} />;
    case 'group': return null;
    case 'lakebase': return <Database {...props} />;
  }
};

const getNodeSubtitle = (node: AgentNodeData): string => {
  switch (node.type) {
    case 'llm': {
      const cfg = node.config as { model: string };
      return cfg.model.replace('databricks-', '');
    }
    case 'vector_search': {
      const cfg = node.config as { indexName: string };
      return cfg.indexName.split('.').pop() ?? cfg.indexName;
    }
    case 'uc_function': {
      const cfg = node.config as { catalog: string; schema: string; functionName: string };
      return `${cfg.catalog}.${cfg.schema}`;
    }
    case 'supervisor': {
      const cfg = node.config as { description: string };
      return cfg.description ? cfg.description.slice(0, 40) : 'manages workers';
    }
    case 'router': {
      const cfg = node.config as { description: string };
      return cfg.description ? cfg.description.slice(0, 40) : 'routes to one agent';
    }
    case 'group': return '';
    case 'lakebase': {
      const cfg = node.config as { instanceName: string };
      return cfg.instanceName || 'set instance name';
    }
  }
};

const getNodeBadge = (node: AgentNodeData): string | null => {
  switch (node.type) {
    case 'llm': {
      const cfg = node.config as { temperature: number; maxIterations: number };
      return `temp ${cfg.temperature} · max ${cfg.maxIterations ?? 10} iter`;
    }
    case 'vector_search': {
      const cfg = node.config as { numResults: number };
      return `top-${cfg.numResults}`;
    }
    case 'uc_function': {
      const cfg = node.config as { functionName: string };
      return cfg.functionName;
    }
    case 'supervisor': {
      const cfg = node.config as { maxIterations: number };
      return `max ${cfg.maxIterations ?? 10} routing rounds`;
    }
    case 'router':
      return 'conditional dispatch';
    case 'group':
      return null;
    case 'lakebase':
      return 'postgres';
  }
};

export const NodeView: React.FC<NodeViewProps> = ({
  node,
  isSelected,
  isEditing,
  isDimmed,
  onMouseDown,
  onContextMenu,
  onPortMouseDown,
  onDoubleClick,
  onEditChange,
  onEditComplete,
}) => {
  const { x, y, width, height, type, label } = node;
  const colors = NODE_COLORS[type];

  const opacity = isDimmed ? 0.3 : 1;
  const stroke = isSelected ? '#FF3621' : colors.borderColor;
  const strokeWidth = isSelected ? 2 : 1.5;

  const subtitle = getNodeSubtitle(node);
  const badge = getNodeBadge(node);

  return (
    <g
      transform={`translate(${x}, ${y})`}
      style={{ opacity, transition: 'opacity 0.2s' }}
      onMouseDown={(e) => onMouseDown(e, node)}
      onContextMenu={(e) => onContextMenu(e, node)}
      onDoubleClick={(e) => onDoubleClick(e, node)}
      className="cursor-move select-none"
    >
      {/* Drop shadow */}
      <rect x={2} y={2} width={width} height={height} fill="#000" fillOpacity="0.05" rx="8" />

      {/* Main body */}
      <rect width={width} height={height} fill="white" stroke={stroke} strokeWidth={strokeWidth} rx="8" />

      {/* Colored header band */}
      <rect width={width} height={30} fill={colors.headerBg} rx="8" />
      <rect y={16} width={width} height={14} fill={colors.headerBg} />

      {/* Content via foreignObject */}
      <foreignObject x="0" y="0" width={width} height={height}>
        <div className="h-full w-full flex flex-col">
          {/* Header */}
          <div
            className="flex items-center gap-2 px-3 py-2 rounded-t"
            style={{ backgroundColor: colors.headerBg, minHeight: 30 }}
          >
            <div className="flex-shrink-0">
              <NodeIcon type={type} color={colors.borderColor} />
            </div>
            <div className="flex-1 min-w-0">
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
                  className="w-full bg-white/80 rounded border px-1 font-bold text-[11px] leading-tight outline-none"
                  style={{ color: colors.headerText, borderColor: colors.borderColor }}
                />
              ) : (
                <div
                  className="font-bold text-[11px] leading-tight truncate"
                  style={{ color: colors.headerText }}
                  title={label}
                >
                  {label}
                </div>
              )}
            </div>
          </div>

          {/* Body */}
          <div className="flex-1 px-3 py-2 flex flex-col gap-1.5">
            {subtitle && (
              <div className="text-[#6b8897] text-[9px] truncate font-medium" title={subtitle}>
                {subtitle}
              </div>
            )}
            {badge && (
              <div
                className="inline-flex items-center self-start text-[8.5px] font-mono px-2 py-0.5 rounded border"
                style={{ backgroundColor: `${colors.borderColor}15`, borderColor: `${colors.borderColor}40`, color: colors.borderColor }}
              >
                {badge}
              </div>
            )}
          </div>
        </div>
      </foreignObject>

      {/* Connection ports — appear on hover via CSS opacity trick */}
      {(['top', 'right', 'bottom', 'left'] as PortPosition[]).map((port) => {
        let px = 0, py = 0;
        if (port === 'top') { px = width / 2; py = 0; }
        else if (port === 'right') { px = width; py = height / 2; }
        else if (port === 'bottom') { px = width / 2; py = height; }
        else { px = 0; py = height / 2; }

        return (
          <g
            key={port}
            onMouseDown={(e) => { e.stopPropagation(); onPortMouseDown(e, node, port); }}
            className="opacity-0 hover:opacity-100 transition-opacity duration-150"
          >
            <circle
              cx={px} cy={py} r={6}
              fill="white"
              stroke={colors.borderColor}
              strokeWidth={1.5}
              className="cursor-crosshair"
            />
            <circle cx={px} cy={py} r={3} fill={colors.borderColor} />
          </g>
        );
      })}
    </g>
  );
};
