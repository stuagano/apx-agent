import React, { useState, useRef, useCallback, useEffect } from 'react';
import {
  AgentNodeData,
  AgentNodeType,
  EdgeData,
  ProjectSettings,
  ViewportTransform,
  ToolType,
  PortPosition,
  SelectionBounds,
} from '../types';
import {
  GRID_SIZE,
  INITIAL_NODES,
  INITIAL_EDGES,
  DEFAULT_NODE_SIZE,
  DEFAULT_CONFIGS,
  DEFAULT_PROJECT_SETTINGS,
} from '../constants';
import {
  snapToGrid,
  getPortCoordinates,
  generateBezierPath,
  isNodeIntersectingRect,
  generateAgentCode,
} from '../utils';
import { buildBundleConfig, downloadProjectZip, generateCICDWorkflow } from '../codegen/project';
import { NodeView } from '../components/NodeView';
import { GroupView, ResizeCorner } from '../components/GroupView';
import { EdgeView } from '../components/EdgeView';
import {
  Header,
  Sidebar,
  RightPanel,
  ContextMenu,
  CodeExportModal,
  ProjectSettingsModal,
  DatabricksAuth,
} from '../components/Controls';
import { DATABRICKS_MODELS } from '../constants';

// ── Types ────────────────────────────────────────────────────────────────────

interface ConnectingState {
  sourceNodeId: string;
  sourcePort: PortPosition;
  currX: number;
  currY: number;
}

interface PanState {
  startClientX: number;
  startClientY: number;
  startVpX: number;
  startVpY: number;
}

interface DragState {
  canvasStartX: number;
  canvasStartY: number;
  startPositions: Map<string, { x: number; y: number }>;
}

interface CtxMenu {
  x: number;
  y: number;
  nodeId: string | null;
}

interface ResizeState {
  nodeId: string;
  corner: ResizeCorner;
  canvasStartX: number;
  canvasStartY: number;
  startNodeX: number;
  startNodeY: number;
  startWidth: number;
  startHeight: number;
}

// ── Canvas background dot grid ────────────────────────────────────────────────

const DotGrid = () => (
  <defs>
    <pattern id="dotgrid" x="0" y="0" width={GRID_SIZE} height={GRID_SIZE} patternUnits="userSpaceOnUse">
      <circle cx={GRID_SIZE / 2} cy={GRID_SIZE / 2} r="0.8" fill="#94a3b8" fillOpacity="0.35" />
    </pattern>
  </defs>
);

// ── Main Component ────────────────────────────────────────────────────────────

export function AgentEditor() {
  // ── State ──────────────────────────────────────────────────────────────────
  const [nodes, setNodes] = useState<AgentNodeData[]>(INITIAL_NODES);
  const [edges, setEdges] = useState<EdgeData[]>(INITIAL_EDGES);
  const [viewport, setViewport] = useState<ViewportTransform>({ x: 80, y: 60, zoom: 1 });

  const [selectedNodeIds, setSelectedNodeIds] = useState<Set<string>>(new Set());
  const [selectedEdgeIds, setSelectedEdgeIds] = useState<Set<string>>(new Set());

  const [tool, setTool] = useState<ToolType>('select');
  const [agentName, setAgentName] = useState('Customer Support Agent');

  // Interaction flags (refs to avoid stale closures)
  const isPanningRef = useRef(false);
  const panStateRef = useRef<PanState | null>(null);

  const isDraggingRef = useRef(false);
  const dragStateRef = useRef<DragState | null>(null);
  const resizingRef = useRef<ResizeState | null>(null);

  const selectionBoxRef = useRef<SelectionBounds | null>(null);
  const selectionStartRef = useRef<{ x: number; y: number } | null>(null);

  // React state for visual feedback
  const [selectionBox, setSelectionBox] = useState<SelectionBounds | null>(null);
  const [connecting, setConnecting] = useState<ConnectingState | null>(null);
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<CtxMenu | null>(null);
  const [showCodeExport, setShowCodeExport] = useState(false);
  const [isDownloadingZip, setIsDownloadingZip] = useState(false);
  const [auth, setAuth] = useState<DatabricksAuth | null>(null);
  const [projectSettings, setProjectSettings] = useState<ProjectSettings>(DEFAULT_PROJECT_SETTINGS);
  const [showSettings, setShowSettings] = useState(false);

  // Undo / redo
  const historyRef = useRef<{ nodes: AgentNodeData[]; edges: EdgeData[] }[]>([]);
  const historyIndexRef = useRef(-1);

  const canvasRef = useRef<SVGSVGElement>(null);

  // ── History ────────────────────────────────────────────────────────────────

  const saveToHistory = useCallback(() => {
    setNodes(currentNodes => {
      setEdges(currentEdges => {
        const newState = { nodes: [...currentNodes], edges: [...currentEdges] };
        const slice = historyRef.current.slice(0, historyIndexRef.current + 1);
        slice.push(newState);
        if (slice.length > 20) slice.shift();
        historyRef.current = slice;
        historyIndexRef.current = slice.length - 1;
        return currentEdges;
      });
      return currentNodes;
    });
  }, []);

  // Initialise history on first render
  useEffect(() => {
    if (historyRef.current.length === 0) {
      historyRef.current = [{ nodes: [...INITIAL_NODES], edges: [...INITIAL_EDGES] }];
      historyIndexRef.current = 0;
    }
  }, []);

  const undo = useCallback(() => {
    if (historyIndexRef.current <= 0) return;
    historyIndexRef.current--;
    const { nodes: n, edges: e } = historyRef.current[historyIndexRef.current];
    setNodes([...n]);
    setEdges([...e]);
    setSelectedNodeIds(new Set());
    setSelectedEdgeIds(new Set());
  }, []);

  const redo = useCallback(() => {
    if (historyIndexRef.current >= historyRef.current.length - 1) return;
    historyIndexRef.current++;
    const { nodes: n, edges: e } = historyRef.current[historyIndexRef.current];
    setNodes([...n]);
    setEdges([...e]);
  }, []);

  const canUndo = historyIndexRef.current > 0;
  const canRedo = historyIndexRef.current < historyRef.current.length - 1;

  // ── Coordinate helpers ─────────────────────────────────────────────────────

  const screenToCanvas = useCallback(
    (screenX: number, screenY: number) => {
      const rect = canvasRef.current!.getBoundingClientRect();
      return {
        x: (screenX - rect.left - viewport.x) / viewport.zoom,
        y: (screenY - rect.top - viewport.y) / viewport.zoom,
      };
    },
    [viewport]
  );

  // ── Keyboard shortcuts ─────────────────────────────────────────────────────

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const meta = e.metaKey || e.ctrlKey;

      const active = document.activeElement;
      const isTyping = active instanceof HTMLInputElement
        || active instanceof HTMLTextAreaElement
        || (active as HTMLElement)?.isContentEditable;

      if ((e.key === 'Delete' || e.key === 'Backspace') && editingNodeId === null && !isTyping) {
        if (selectedNodeIds.size > 0) {
          const ids = new Set(selectedNodeIds);
          setNodes(prev => prev.filter(n => !ids.has(n.id)));
          setEdges(prev => prev.filter(ed => !ids.has(ed.source) && !ids.has(ed.target)));
          setSelectedNodeIds(new Set());
          // history saved below
        }
        if (selectedEdgeIds.size > 0) {
          const ids = new Set(selectedEdgeIds);
          setEdges(prev => prev.filter(ed => !ids.has(ed.id)));
          setSelectedEdgeIds(new Set());
        }
      }
      if (meta && e.key === 'z') { e.preventDefault(); undo(); }
      if (meta && (e.key === 'y' || (e.shiftKey && e.key === 'z'))) { e.preventDefault(); redo(); }
      if (e.key === 'v') setTool('select');
      if (e.key === 'h') setTool('hand');
      if (e.key === 'c' && !meta) setTool('connect');
      if (e.key === 'Escape') {
        setConnecting(null);
        setContextMenu(null);
        setSelectedNodeIds(new Set());
        setSelectedEdgeIds(new Set());
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedNodeIds, selectedEdgeIds, editingNodeId, undo, redo]);

  // ── Close context menu on outside click ───────────────────────────────────

  useEffect(() => {
    const close = () => setContextMenu(null);
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, []);

  // ── Canvas mouse events ────────────────────────────────────────────────────

  const handleCanvasMouseDown = (e: React.MouseEvent<SVGSVGElement>) => {
    const target = e.target as Element;
    const isBackground =
      target === canvasRef.current ||
      target.tagName === 'rect' && target.getAttribute('fill') === 'url(#dotgrid)';

    if (!isBackground) return;

    setContextMenu(null);

    if (tool === 'hand' || e.button === 1) {
      isPanningRef.current = true;
      panStateRef.current = {
        startClientX: e.clientX,
        startClientY: e.clientY,
        startVpX: viewport.x,
        startVpY: viewport.y,
      };
      return;
    }

    if (tool === 'select') {
      setSelectedNodeIds(new Set());
      setSelectedEdgeIds(new Set());
      const canvas = screenToCanvas(e.clientX, e.clientY);
      selectionStartRef.current = canvas;
      selectionBoxRef.current = { x: canvas.x, y: canvas.y, width: 0, height: 0 };
      setSelectionBox({ x: canvas.x, y: canvas.y, width: 0, height: 0 });
    }
  };

  const handleCanvasMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    // Resize group
    if (resizingRef.current) {
      const rs = resizingRef.current;
      const canvas = screenToCanvas(e.clientX, e.clientY);
      const dx = canvas.x - rs.canvasStartX;
      const dy = canvas.y - rs.canvasStartY;
      const MIN_W = 100, MIN_H = 80;
      let newX = rs.startNodeX, newY = rs.startNodeY;
      let newW = rs.startWidth, newH = rs.startHeight;

      if (rs.corner === 'se') {
        newW = Math.max(MIN_W, snapToGrid(rs.startWidth + dx));
        newH = Math.max(MIN_H, snapToGrid(rs.startHeight + dy));
      } else if (rs.corner === 'sw') {
        newW = Math.max(MIN_W, snapToGrid(rs.startWidth - dx));
        newH = Math.max(MIN_H, snapToGrid(rs.startHeight + dy));
        newX = rs.startNodeX + rs.startWidth - newW;
      } else if (rs.corner === 'ne') {
        newW = Math.max(MIN_W, snapToGrid(rs.startWidth + dx));
        newH = Math.max(MIN_H, snapToGrid(rs.startHeight - dy));
        newY = rs.startNodeY + rs.startHeight - newH;
      } else {
        // nw
        newW = Math.max(MIN_W, snapToGrid(rs.startWidth - dx));
        newH = Math.max(MIN_H, snapToGrid(rs.startHeight - dy));
        newX = rs.startNodeX + rs.startWidth - newW;
        newY = rs.startNodeY + rs.startHeight - newH;
      }

      setNodes(prev => prev.map(n =>
        n.id === rs.nodeId ? { ...n, x: newX, y: newY, width: newW, height: newH } : n
      ));
      return;
    }

    // Pan
    if (isPanningRef.current && panStateRef.current) {
      const { startClientX, startClientY, startVpX, startVpY } = panStateRef.current;
      setViewport(prev => ({
        ...prev,
        x: startVpX + (e.clientX - startClientX),
        y: startVpY + (e.clientY - startClientY),
      }));
      return;
    }

    // Drag nodes
    if (isDraggingRef.current && dragStateRef.current) {
      const { canvasStartX, canvasStartY, startPositions } = dragStateRef.current;
      const canvas = screenToCanvas(e.clientX, e.clientY);
      const dx = canvas.x - canvasStartX;
      const dy = canvas.y - canvasStartY;

      setNodes(prev =>
        prev.map(n => {
          if (!startPositions.has(n.id) || n.locked) return n;
          const sp = startPositions.get(n.id)!;
          return { ...n, x: snapToGrid(sp.x + dx), y: snapToGrid(sp.y + dy) };
        })
      );
      return;
    }

    // Draw connection line
    if (connecting) {
      const canvas = screenToCanvas(e.clientX, e.clientY);
      setConnecting(prev => (prev ? { ...prev, currX: canvas.x, currY: canvas.y } : null));
      return;
    }

    // Marquee selection
    if (selectionStartRef.current) {
      const canvas = screenToCanvas(e.clientX, e.clientY);
      const start = selectionStartRef.current;
      const box = {
        x: Math.min(canvas.x, start.x),
        y: Math.min(canvas.y, start.y),
        width: Math.abs(canvas.x - start.x),
        height: Math.abs(canvas.y - start.y),
      };
      selectionBoxRef.current = box;
      setSelectionBox({ ...box });
    }
  };

  const handleCanvasMouseUp = (e: React.MouseEvent<SVGSVGElement>) => {
    // Stop resizing
    if (resizingRef.current) {
      resizingRef.current = null;
      saveToHistory();
      return;
    }

    // Stop panning
    if (isPanningRef.current) {
      isPanningRef.current = false;
      panStateRef.current = null;
      return;
    }

    // Stop dragging
    if (isDraggingRef.current) {
      isDraggingRef.current = false;
      dragStateRef.current = null;
      saveToHistory();
      return;
    }

    // Finish connection
    if (connecting) {
      const canvas = screenToCanvas(e.clientX, e.clientY);
      let connected = false;

      for (const node of nodes) {
        if (node.id === connecting.sourceNodeId) continue;
        if (node.type === 'group') continue;
        for (const port of ['top', 'right', 'bottom', 'left'] as PortPosition[]) {
          const pos = getPortCoordinates(node, port);
          const dist = Math.sqrt((canvas.x - pos.x) ** 2 + (canvas.y - pos.y) ** 2);
          if (dist < 14) {
            // Check for duplicate
            const dup = edges.some(
              ex =>
                ex.source === connecting.sourceNodeId &&
                ex.target === node.id &&
                ex.sourcePort === connecting.sourcePort &&
                ex.targetPort === port
            );
            if (!dup) {
              const newEdge: EdgeData = {
                id: `e-${Date.now()}`,
                source: connecting.sourceNodeId,
                target: node.id,
                sourcePort: connecting.sourcePort,
                targetPort: port,
              };
              setEdges(prev => [...prev, newEdge]);
              saveToHistory();
            }
            connected = true;
            break;
          }
        }
        if (connected) break;
      }

      setConnecting(null);
      return;
    }

    // Marquee selection
    if (selectionStartRef.current && selectionBoxRef.current) {
      const box = selectionBoxRef.current;
      if (box.width > 5 || box.height > 5) {
        const ids = new Set(
          nodes.filter(n => isNodeIntersectingRect(n, box)).map(n => n.id)
        );
        setSelectedNodeIds(ids);
      }
      selectionStartRef.current = null;
      selectionBoxRef.current = null;
      setSelectionBox(null);
    }
  };

  const ZOOM_STEP = 0.15;
  const zoomIn  = () => setViewport(v => ({ ...v, zoom: Math.min(4, +(v.zoom + ZOOM_STEP).toFixed(2)) }));
  const zoomOut = () => setViewport(v => ({ ...v, zoom: Math.max(0.2, +(v.zoom - ZOOM_STEP).toFixed(2)) }));
  const zoomReset = () => setViewport(v => ({ ...v, zoom: 1 }));

  // ── Node events ────────────────────────────────────────────────────────────

  const handleNodeMouseDown = useCallback(
    (e: React.MouseEvent, node: AgentNodeData) => {
      if (tool === 'hand') return;
      if (tool === 'connect') return;

      e.stopPropagation();
      setContextMenu(null);

      // Build new selection
      let newSelection: Set<string>;
      if (e.shiftKey) {
        newSelection = new Set(selectedNodeIds);
        if (newSelection.has(node.id)) newSelection.delete(node.id);
        else newSelection.add(node.id);
      } else {
        newSelection = selectedNodeIds.has(node.id) ? new Set(selectedNodeIds) : new Set([node.id]);
      }
      setSelectedNodeIds(newSelection);
      setSelectedEdgeIds(new Set());

      // Start drag — capture positions for all selected nodes
      const canvas = screenToCanvas(e.clientX, e.clientY);
      const startPositions = new Map<string, { x: number; y: number }>();
      // Include the clicked node even if not yet in state
      const dragIds = newSelection.has(node.id) ? [...newSelection] : [...newSelection, node.id];
      for (const id of dragIds) {
        const n = nodes.find(nn => nn.id === id);
        if (n) startPositions.set(id, { x: n.x, y: n.y });
      }

      isDraggingRef.current = true;
      dragStateRef.current = {
        canvasStartX: canvas.x,
        canvasStartY: canvas.y,
        startPositions,
      };
    },
    [tool, selectedNodeIds, nodes, screenToCanvas]
  );

  const handleNodeContextMenu = useCallback((e: React.MouseEvent, node: AgentNodeData) => {
    e.preventDefault();
    e.stopPropagation();
    setSelectedNodeIds(new Set([node.id]));
    setContextMenu({ x: e.clientX, y: e.clientY, nodeId: node.id });
  }, []);

  const handlePortMouseDown = useCallback(
    (e: React.MouseEvent, node: AgentNodeData, port: PortPosition) => {
      e.stopPropagation();
      const pos = getPortCoordinates(node, port);
      setConnecting({ sourceNodeId: node.id, sourcePort: port, currX: pos.x, currY: pos.y });
    },
    []
  );

  const handleNodeDoubleClick = useCallback((_e: React.MouseEvent, node: AgentNodeData) => {
    setEditingNodeId(node.id);
  }, []);

  const handleEditChange = useCallback(
    (val: string) => {
      if (!editingNodeId) return;
      setNodes(prev => prev.map(n => (n.id === editingNodeId ? { ...n, label: val } : n)));
    },
    [editingNodeId]
  );

  const handleEditComplete = useCallback(() => {
    if (editingNodeId) saveToHistory();
    setEditingNodeId(null);
  }, [editingNodeId, saveToHistory]);

  const handleGroupResizeStart = useCallback(
    (e: React.MouseEvent, node: AgentNodeData, corner: ResizeCorner) => {
      const canvas = screenToCanvas(e.clientX, e.clientY);
      resizingRef.current = {
        nodeId: node.id,
        corner,
        canvasStartX: canvas.x,
        canvasStartY: canvas.y,
        startNodeX: node.x,
        startNodeY: node.y,
        startWidth: node.width,
        startHeight: node.height,
      };
    },
    [screenToCanvas]
  );

  const handleEdgeSelect = useCallback((e: React.MouseEvent, edge: EdgeData) => {
    e.stopPropagation();
    setSelectedEdgeIds(prev => {
      const next = new Set(prev);
      if (next.has(edge.id)) next.delete(edge.id);
      else next.add(edge.id);
      return next;
    });
    setSelectedNodeIds(new Set());
  }, []);

  // ── Drag-drop from sidebar ────────────────────────────────────────────────

  const handleDragOver = (e: React.DragEvent<SVGSVGElement>) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  };

  const handleDrop = useCallback(
    (e: React.DragEvent<SVGSVGElement>) => {
      e.preventDefault();
      const raw = e.dataTransfer.getData('application/agent-builder');
      if (!raw) return;

      const { type, label } = JSON.parse(raw) as { type: AgentNodeType; label: string };
      const size = DEFAULT_NODE_SIZE[type];
      const canvas = screenToCanvas(e.clientX, e.clientY);

      const newNode: AgentNodeData = {
        id: `${type}-${Date.now()}`,
        type,
        label,
        config: { ...DEFAULT_CONFIGS[type] },
        x: snapToGrid(canvas.x - size.width / 2),
        y: snapToGrid(canvas.y - size.height / 2),
        width: size.width,
        height: size.height,
      };

      setNodes(prev => [...prev, newNode]);
      setSelectedNodeIds(new Set([newNode.id]));
      setSelectedEdgeIds(new Set());

      // Save to history after state update
      setTimeout(() => {
        historyRef.current = historyRef.current.slice(0, historyIndexRef.current + 1);
        historyRef.current.push({
          nodes: [...nodes, newNode],
          edges: [...edges],
        });
        historyIndexRef.current = historyRef.current.length - 1;
      }, 0);
    },
    [screenToCanvas, nodes, edges]
  );

  const handleSidebarDragStart = (e: React.DragEvent, type: AgentNodeType, label: string) => {
    e.dataTransfer.setData('application/agent-builder', JSON.stringify({ type, label }));
    e.dataTransfer.effectAllowed = 'copy';
  };

  // ── Node CRUD helpers ─────────────────────────────────────────────────────

  const deleteNode = useCallback(
    (id: string) => {
      setNodes(prev => prev.filter(n => n.id !== id));
      setEdges(prev => prev.filter(ed => ed.source !== id && ed.target !== id));
      setSelectedNodeIds(prev => { const s = new Set(prev); s.delete(id); return s; });
      saveToHistory();
    },
    [saveToHistory]
  );

  const duplicateNode = useCallback(
    (node: AgentNodeData) => {
      const newNode: AgentNodeData = {
        ...node,
        id: `${node.type}-${Date.now()}`,
        x: snapToGrid(node.x + 20),
        y: snapToGrid(node.y + 20),
        config: { ...node.config },
      };
      setNodes(prev => [...prev, newNode]);
      setSelectedNodeIds(new Set([newNode.id]));
      saveToHistory();
    },
    [saveToHistory]
  );

  const updateNode = useCallback((updated: AgentNodeData) => {
    setNodes(prev => prev.map(n => (n.id === updated.id ? updated : n)));
  }, []);

  // ── Context menu actions ───────────────────────────────────────────────────

  const ctxNode = contextMenu?.nodeId ? nodes.find(n => n.id === contextMenu.nodeId) : null;

  // ── Selected node for properties panel ───────────────────────────────────

  const selectedNode =
    selectedNodeIds.size === 1
      ? nodes.find(n => n.id === [...selectedNodeIds][0]) ?? null
      : null;

  // ── Connecting visual: source port coords in canvas space ─────────────────

  const connectingSourceNode = connecting
    ? nodes.find(n => n.id === connecting.sourceNodeId)
    : null;
  const connectingSourcePos = connectingSourceNode
    ? getPortCoordinates(connectingSourceNode, connecting!.sourcePort)
    : null;

  // ── Cursor style ──────────────────────────────────────────────────────────

  const cursorStyle =
    tool === 'hand' || isPanningRef.current
      ? 'grabbing'
      : tool === 'connect'
      ? 'crosshair'
      : 'default';

  // ── Code export ───────────────────────────────────────────────────────────

  const generatedCode = generateAgentCode(nodes, edges, agentName);
  const bundleConfig = buildBundleConfig(nodes, edges, agentName, auth?.host, projectSettings);
  const generatedConfig = JSON.stringify(bundleConfig, null, 2);
  const generatedCICDWorkflow = bundleConfig.cicd.enabled ? generateCICDWorkflow(bundleConfig) : undefined;

  const handleDownloadZip = async () => {
    setIsDownloadingZip(true);
    try {
      await downloadProjectZip(nodes, edges, agentName, auth?.host, projectSettings);
    } finally {
      setIsDownloadingZip(false);
    }
  };

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden">
      <Header
        agentName={agentName}
        currentTool={tool}
        setTool={setTool}
        onUndo={undo}
        onRedo={redo}
        canUndo={canUndo}
        canRedo={canRedo}
        onExportCode={() => setShowCodeExport(true)}
        onDownloadZip={handleDownloadZip}
        isDownloadingZip={isDownloadingZip}
        onAgentNameChange={setAgentName}
        auth={auth}
        onConnect={setAuth}
        onDisconnect={() => setAuth(null)}
        onOpenSettings={() => setShowSettings(true)}
      />

      <div className="flex flex-1 overflow-hidden">
        <Sidebar onDragStart={handleSidebarDragStart} />

        {/* Canvas */}
        <div className="flex-1 relative overflow-hidden bg-[#F8FAFC]">
          <svg
            ref={canvasRef}
            className="absolute inset-0 w-full h-full"
            style={{ cursor: cursorStyle }}
            onMouseDown={handleCanvasMouseDown}
            onMouseMove={handleCanvasMouseMove}
            onMouseUp={handleCanvasMouseUp}
            onMouseLeave={handleCanvasMouseUp}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
          >
            <DotGrid />
            {/* Background dot pattern — fills entire SVG regardless of transform */}
            <rect width="100%" height="100%" fill="url(#dotgrid)" />

            <g transform={`translate(${viewport.x}, ${viewport.y}) scale(${viewport.zoom})`}>
              {/* Group nodes — rendered behind everything else */}
              {nodes.filter(n => n.type === 'group').map(node => (
                <GroupView
                  key={node.id}
                  node={node}
                  isSelected={selectedNodeIds.has(node.id)}
                  isEditing={editingNodeId === node.id}
                  isDimmed={selectedNodeIds.size > 0 && !selectedNodeIds.has(node.id)}
                  onMouseDown={handleNodeMouseDown}
                  onContextMenu={handleNodeContextMenu}
                  onDoubleClick={handleNodeDoubleClick}
                  onResizeStart={handleGroupResizeStart}
                  onEditChange={handleEditChange}
                  onEditComplete={handleEditComplete}
                />
              ))}

              {/* Edges */}
              {edges.map(edge => {
                const src = nodes.find(n => n.id === edge.source);
                const tgt = nodes.find(n => n.id === edge.target);
                if (!src || !tgt) return null;
                return (
                  <EdgeView
                    key={edge.id}
                    edge={edge}
                    sourceNode={src}
                    targetNode={tgt}
                    isSelected={selectedEdgeIds.has(edge.id)}
                    onSelect={handleEdgeSelect}
                    isDimmed={selectedNodeIds.size > 0 && !selectedNodeIds.has(edge.source) && !selectedNodeIds.has(edge.target)}
                  />
                );
              })}

              {/* Connecting line preview */}
              {connecting && connectingSourcePos && (
                <path
                  d={generateBezierPath(
                    connectingSourcePos.x,
                    connectingSourcePos.y,
                    connecting.currX,
                    connecting.currY,
                    connecting.sourcePort,
                    'left'
                  )}
                  stroke="#94a3b8"
                  strokeWidth={1.5}
                  strokeDasharray="6 4"
                  fill="none"
                  pointerEvents="none"
                />
              )}

              {/* Regular nodes (groups handled above) */}
              {nodes.filter(n => n.type !== 'group').map(node => (
                <NodeView
                  key={node.id}
                  node={node}
                  isSelected={selectedNodeIds.has(node.id)}
                  isEditing={editingNodeId === node.id}
                  isDimmed={selectedNodeIds.size > 0 && !selectedNodeIds.has(node.id)}
                  onMouseDown={handleNodeMouseDown}
                  onContextMenu={handleNodeContextMenu}
                  onPortMouseDown={handlePortMouseDown}
                  onDoubleClick={handleNodeDoubleClick}
                  onEditChange={handleEditChange}
                  onEditComplete={handleEditComplete}
                />
              ))}

              {/* Marquee selection box */}
              {selectionBox && (selectionBox.width > 2 || selectionBox.height > 2) && (
                <rect
                  x={selectionBox.x}
                  y={selectionBox.y}
                  width={selectionBox.width}
                  height={selectionBox.height}
                  fill="#2272B4"
                  fillOpacity="0.07"
                  stroke="#2272B4"
                  strokeWidth={1}
                  strokeDasharray="5 3"
                  pointerEvents="none"
                />
              )}
            </g>
          </svg>

          {/* Zoom controls */}
          <div className="absolute bottom-3 right-3 flex items-center gap-1 bg-white border border-slate-200 rounded-lg shadow-sm px-1 py-1 select-none">
            <button
              onClick={zoomOut}
              disabled={viewport.zoom <= 0.2}
              className="w-7 h-7 flex items-center justify-center rounded text-slate-500 hover:bg-slate-100 disabled:opacity-30 text-base font-medium transition-colors"
              title="Zoom out"
            >−</button>
            <button
              onClick={zoomReset}
              className="px-2 h-7 text-[11px] font-mono text-slate-500 hover:bg-slate-100 rounded transition-colors min-w-[42px]"
              title="Reset zoom"
            >{Math.round(viewport.zoom * 100)}%</button>
            <button
              onClick={zoomIn}
              disabled={viewport.zoom >= 4}
              className="w-7 h-7 flex items-center justify-center rounded text-slate-500 hover:bg-slate-100 disabled:opacity-30 text-base font-medium transition-colors"
              title="Zoom in"
            >+</button>
          </div>

          {/* Empty state hint */}
          {nodes.length === 0 && (
            <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
              <div className="text-center text-slate-400">
                <p className="text-sm font-medium">Drag bricks from the sidebar</p>
                <p className="text-xs mt-1">to start building your agent</p>
              </div>
            </div>
          )}
        </div>

        {/* Right panel */}
        <RightPanel
          selectedNode={selectedNode}
          onUpdateNode={updateNode}
          onDeleteNode={deleteNode}
          onDuplicateNode={duplicateNode}
          onClose={() => setSelectedNodeIds(new Set())}
          models={auth?.models ?? DATABRICKS_MODELS}
        />
      </div>

      {/* Context menu */}
      {contextMenu && (
        <ContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          nodeId={contextMenu.nodeId}
          onDelete={() => contextMenu.nodeId && deleteNode(contextMenu.nodeId)}
          onDuplicate={() => ctxNode && duplicateNode(ctxNode)}
          onClose={() => setContextMenu(null)}
        />
      )}

      {/* Code export modal */}
      {showCodeExport && (
        <CodeExportModal
          code={generatedCode}
          configJson={generatedConfig}
          cicdWorkflow={generatedCICDWorkflow}
          cicdProvider={bundleConfig.cicd.enabled ? bundleConfig.cicd.provider : undefined}
          onClose={() => setShowCodeExport(false)}
        />
      )}

      {/* Project settings modal */}
      {showSettings && (
        <ProjectSettingsModal
          settings={projectSettings}
          onUpdate={setProjectSettings}
          onClose={() => setShowSettings(false)}
        />
      )}
    </div>
  );
}
