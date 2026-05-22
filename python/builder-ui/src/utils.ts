import { AgentNodeData, PortPosition, SelectionBounds } from './types';

export const GRID_SIZE = 20;

export const snapToGrid = (value: number) => Math.round(value / GRID_SIZE) * GRID_SIZE;

export const getPortCoordinates = (node: AgentNodeData, port: PortPosition) => {
  const { x, y, width, height } = node;
  switch (port) {
    case 'top':    return { x: x + width / 2, y };
    case 'right':  return { x: x + width, y: y + height / 2 };
    case 'bottom': return { x: x + width / 2, y: y + height };
    case 'left':   return { x, y: y + height / 2 };
  }
};

export const generateBezierPath = (
  x1: number, y1: number,
  x2: number, y2: number,
  sourcePort: PortPosition,
  targetPort: PortPosition
) => {
  const curvature = 0.5;
  const dist = Math.sqrt(Math.pow(x2 - x1, 2) + Math.pow(y2 - y1, 2));
  const controlDist = Math.max(dist * curvature, 50);

  const getControlOffset = (port: PortPosition) => {
    switch (port) {
      case 'top':    return { x: 0, y: -controlDist };
      case 'right':  return { x: controlDist, y: 0 };
      case 'bottom': return { x: 0, y: controlDist };
      case 'left':   return { x: -controlDist, y: 0 };
    }
  };

  const cp1 = { x: x1 + getControlOffset(sourcePort).x, y: y1 + getControlOffset(sourcePort).y };
  const cp2 = { x: x2 + getControlOffset(targetPort).x, y: y2 + getControlOffset(targetPort).y };
  return `M ${x1} ${y1} C ${cp1.x} ${cp1.y}, ${cp2.x} ${cp2.y}, ${x2} ${y2}`;
};

export const isNodeIntersectingRect = (node: AgentNodeData, rect: SelectionBounds) => {
  return (
    node.x < rect.x + rect.width &&
    node.x + node.width > rect.x &&
    node.y < rect.y + rect.height &&
    node.y + node.height > rect.y
  );
};

// Code generation is handled by codegen/index.ts
export { generateAgentCode } from './codegen/index';
