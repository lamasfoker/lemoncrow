// Wire shape the map engine lays out. Adapters produce it: the Review Map from
// a Snapshot (review/types.ts extends these), `/code` from the current index.

export type MapLevel = "architecture" | "file" | "symbol";
export type MapLaneHint = "used_by" | "uses" | "";
/** "subject" is a subject member with no uncommitted or reviewed change. */
export type MapStatus = "changed" | "new" | "impacted" | "context" | "subject";
export type MapActivityKind = "search" | "read" | "edit" | "verify";

export interface MapMemberSymbol {
  id: string;
  label: string;
  qualified_name: string;
  kind?: string;
  path: string;
  line: number;
  end_line: number;
  change: string;
  caller_count: number;
  usage_count: number;
  centrality_rank: number | null;
  centrality_percentile: number | null;
  target_ids?: string[];
}

export interface MapReviewProgress {
  targets: number;
  reviewed: number;
  needs_changes: number;
  changed_since_review: number;
  open_threads: number;
  attention: string;
  /** Outstanding targets per the server's canonical queue. */
  outstanding?: number;
  /** Earliest canonical queue position of those targets; -1 when none. */
  next_position?: number;
}

export interface MapNode {
  id: string;
  label: string;
  qualified_name?: string;
  kind: string;
  /** Parent directory; absent from older servers (derive from `path`). */
  module?: string;
  /** Which side of the subject a surrounding node sits on; "" for the subject. */
  lane?: MapLaneHint;
  path: string;
  line: number;
  end_line: number;
  status: MapStatus;
  change?: string;
  change_kinds?: string[];
  additions?: number;
  deletions?: number;
  category?: string;
  rank?: number;
  reasons?: string[];
  review?: MapReviewProgress | null;
  symbol_count?: number;
  symbols?: MapMemberSymbol[];
  exactness: string;
  important_public_contract?: boolean;
  boundaries?: string[];
  uncertain?: boolean;
  caller_count?: number;
  centrality_rank?: number | null;
  /** Live overlay: what an agent is doing to this node right now. */
  activity?: MapActivityKind;
  /** Live overlay: short session marks of agents touching this node. */
  sessions?: string[];
}

export interface MapEdgeEvidence {
  kind: string;
  path: string;
  line: number;
  uncertainty?: string;
  symbol?: string;
  snippet?: string;
}

export interface MapEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
  exactness: string;
  weight: number;
  uncertain?: boolean;
  evidence?: MapEdgeEvidence[];
  /** Names the relationship lands on, most frequent first. */
  symbols?: { name: string; count: number }[];
}

export interface MapStats {
  filtered_relationships?: number;
  filtered_reasons?: Record<string, number>;
  /** Symbol focus: outgoing calls could be attributed to the symbol's exact span. */
  outgoing_attributed?: boolean;
}

export interface MapGraph<N extends MapNode = MapNode, E extends MapEdge = MapEdge> {
  level: MapLevel;
  focus: string | null;
  nodes: N[];
  edges: E[];
  truncated: boolean;
  omitted_node_count?: number;
  stats?: MapStats;
  reason?: string;
}

export interface MapContext {
  source: string;
  readiness?: {
    backend?: string;
    reason?: string;
    graph?: { state?: string; reason?: string };
  };
}

export interface MapPayload<N extends MapNode = MapNode, E extends MapEdge = MapEdge> {
  level: MapLevel;
  graph: MapGraph<N, E>;
  context: MapContext;
}
