export type WorkspaceMode = "today" | "document" | "canvas";

export type KnowledgeItem = {
  id: string;
  title: string;
  score: number;
  snippet: string;
  source?: string;
};

export type TimelineItem = {
  id: string;
  age: string;
  title: string;
  detail: string;
};

export type ConnectionItem = {
  id: string;
  label: string;
  relation: string;
};

export type BrainState = {
  relatedKnowledge: KnowledgeItem[];
  entities: string[];
  timeline: TimelineItem[];
  connections: ConnectionItem[];
  status: "idle" | "analyzing" | "synced" | "offline";
  lastTrigger: "pause" | "blur" | "significant-change" | "manual";
  updatedAt: number | null;
};

export type WorkspaceCorpusResponse = {
  ok?: boolean;
  entities?: Array<{ label?: string; canonical_name?: string; name?: string; entity_id?: string }>;
  memories?: Array<{ text?: string; confidence?: number; metadata?: Record<string, unknown> }>;
  sources?: Array<{ title?: string; source_item_id?: string; source_channel?: string; created_at?: string }>;
  hyperedges?: Array<{ label?: string; summary?: string; relation?: string; confidence?: number }>;
};

export type WorkspaceSearchResponse = {
  ok?: boolean;
  workspace?: {
    evidence?: {
      citations?: Array<{ title?: string; snippet?: string; source_item_id?: string }>;
      graph_paths?: Array<{ explanation?: string; entities?: string[] }>;
      memory_context?: Array<{ text?: string; confidence?: number }>;
    };
  };
  retrieval?: {
    results?: Array<{ title?: string; snippet?: string; score?: number }>;
  };
};

export type TodayContinueItem = {
  id: string;
  type?: string;
  title: string;
  subtitle?: string;
  summary?: string;
  opened_surface?: WorkspaceMode;
};

export type TodayDiscoveryItem = {
  id: string;
  type?: string;
  label: string;
  title: string;
  summary: string;
  confidence?: number | null;
  evidence_count?: number;
  review_item_id?: string | null;
};

export type TodayReviewItem = {
  review_item_id: string;
  review_type?: string;
  title: string;
  summary: string;
  confidence?: number | null;
  recommended_action?: string;
  recommended_actions?: string[];
  source_ref_status?: string;
  evidence_count?: number;
};

export type TodayResponse = {
  ok?: boolean;
  continue_working?: TodayContinueItem[];
  discoveries?: TodayDiscoveryItem[];
  needs_review?: TodayReviewItem[];
  system?: {
    source_counts?: Record<string, number>;
    digest_backlog?: Record<string, number>;
    pending_reviews?: Record<string, number>;
    failed_jobs?: { count?: number };
  };
};
