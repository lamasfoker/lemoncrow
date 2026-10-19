// The Review Map is the shared map engine (map/) applied to one Snapshot.
// Review-typed views of the engine's generic layout types live here so Review
// code reads Snapshot fields (selection, provenance) without casts.
import type * as Engine from "../map/mapModel";
import type { ReviewMapEdge, ReviewMapNode } from "./types";

export * from "../map/mapModel";

export type MapCardLayout = Engine.MapCardLayout<ReviewMapNode>;
export type MapEdgeLayout = Engine.MapEdgeLayout<ReviewMapEdge>;
export type MapModel = Engine.MapModel<ReviewMapNode, ReviewMapEdge>;
export type Relationship = Engine.Relationship<ReviewMapNode, ReviewMapEdge>;
export type SearchHit = Engine.SearchHit<ReviewMapNode>;
