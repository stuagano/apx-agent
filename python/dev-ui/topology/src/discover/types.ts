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
  Catalog: "#4299E0",
  Schema: "#92A4B3",
  Table: "#E8ECF0",
  View: "#06B6D4",
  Volume: "#A3E635",
  Function: "#3BA65E",
  Model: "#E11D48",
  GenieSpace: "#FACB66",
  VectorSearchIndex: "#22D3EE",
  ServingEndpoint: "#FB923C",
  App: "#A78BFA",
};

// Port of Lattice EdgeLegend, cut to the v1 edge types.
export const EDGE_LEGEND: ReadonlyArray<{ type: string; color: string; dashed?: boolean; description: string }> = [
  { type: "contains", color: "#445461", description: "Catalog→Schema→Table/View/Volume/Function/Model" },
  { type: "derivedFrom", color: "#06B6D4", description: "View→source Table" },
  { type: "feedsInto", color: "#4299E0", dashed: true, description: "Table→Table lineage" },
  { type: "indexesFrom", color: "#22D3EE", description: "Vector Search index→source Table" },
  { type: "serves", color: "#E11D48", description: "Serving endpoint→Model" },
  { type: "uses", color: "#A78BFA", description: "Genie space/App→asset" },
];

export const NODE_TYPES = Object.keys(NODE_COLORS);
