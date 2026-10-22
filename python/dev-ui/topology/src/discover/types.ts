export interface GraphNode {
  id: string;
  type: string;
  name: string;
  fqn: string;
  props: Record<string, unknown>;
}
export interface GraphEdge {
  src: string;
  dst: string;
  type: string;
}
export interface GraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
  warnings: string[];
  built_at: string;
}

// Lattice NodeTypes palette, mapped onto the Du Bois dark theme strokes.
export const NODE_COLORS: Record<string, string> = {
  Catalog: "#1a5fc4",
  Schema: "#5f6469",
  Table: "#1f2124",
  View: "#0e7490",
  Volume: "#4d7c0f",
  Function: "#137333",
  Model: "#be123c",
  GenieSpace: "#8b5000",
  VectorSearchIndex: "#0b7285",
  ServingEndpoint: "#c2410c",
  App: "#7040a0",
};

// Port of Lattice EdgeLegend, cut to the v1 edge types.
export const EDGE_LEGEND: ReadonlyArray<{ type: string; color: string; dashed?: boolean; description: string }> = [
  { type: "contains", color: "#d9dce6", description: "Catalog→Schema→Table/View/Volume/Function/Model" },
  { type: "derivedFrom", color: "#0e7490", description: "View→source Table" },
  { type: "feedsInto", color: "#1a5fc4", dashed: true, description: "Table→Table lineage" },
  { type: "indexesFrom", color: "#0b7285", description: "Vector Search index→source Table" },
  { type: "serves", color: "#be123c", description: "Serving endpoint→Model" },
  { type: "uses", color: "#7040a0", description: "Genie space/App→asset" },
];

export const NODE_TYPES = Object.keys(NODE_COLORS);
