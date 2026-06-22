export type WorkspaceMode = "today" | "document" | "canvas" | "graph" | "corpus" | "review";

export type KnowledgeItem = {
  id: string;
  title: string;
  score?: number;
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
  status: "idle" | "analyzing" | "synced" | "error";
  lastTrigger: "pause" | "blur" | "significant-change" | "manual";
  updatedAt: number | null;
  error?: string | null;
};

export type WorkspaceCorpusSource = {
  source_item_id?: string;
  title?: string;
  source_channel?: string;
  url?: string;
  created_at?: string;
  chunk_count?: number;
  metadata?: Record<string, unknown>;
};

export type WorkspaceCorpusChunk = {
  chunk_id?: string;
  source_item_id?: string;
  title?: string;
  text?: string;
  snippet?: string;
  source_channel?: string;
  created_at?: string;
};

export type WorkspaceCorpusResponse = {
  ok?: boolean;
  counts?: Record<string, number>;
  entities?: Array<{ label?: string; canonical_name?: string; name?: string; entity_id?: string; entity_type?: string }>;
  memories?: Array<{ text?: string; confidence?: number; metadata?: Record<string, unknown> }>;
  sources?: WorkspaceCorpusSource[];
  chunks?: WorkspaceCorpusChunk[];
  hyperedges?: Array<{
    hyperedge_id?: string;
    label?: string;
    summary?: string;
    relation?: string;
    relation_type?: string;
    evidence_text?: string;
    confidence?: number;
    source_refs?: Array<{ source_item_id?: string; chunk_id?: string; title?: string; url?: string }>;
    members?: Array<{ entity_id?: string; label?: string; entity_type?: string; role?: string }>;
  }>;
};

export type ConsoleConnectorState = {
  connector_state_id?: string;
  connector_id?: string;
  owner_user_id?: string;
  enabled?: boolean;
  scan_cursor?: string | null;
  sync_status?: string | null;
  last_success_at?: string | null;
  last_error_at?: string | null;
  last_error?: string | null;
  roots?: string[];
};

export type ConsoleSourceSummary = {
  source_item_id?: string;
  source_channel?: string;
  record_type?: string;
  title?: string;
  created_at?: string;
};

export type ConsoleSourceChannelStats = number | { source_items?: number; latest_source_item_id?: string; latest_source_item_at?: string };

export type ConsoleSourcesResponse = {
  ok?: boolean;
  read_only?: boolean;
  source_counts?: Record<string, number>;
  source_channels?: Record<string, ConsoleSourceChannelStats>;
  knowledge_sources?: {
    source_count?: number;
    sources?: Array<{
      knowledge_source_id?: string;
      name?: string;
      source_type?: string;
      uri?: string;
      path?: string;
      mode?: string;
      status?: string;
      connector_id?: string;
      last_sync_at?: string | null;
      last_error?: string | null;
      last_sync_run?: {
        scanned?: number;
        ingested?: number;
        new_files?: number;
        changed_files?: number;
        unchanged_files?: number;
        moved_files?: number;
        missing_files?: number;
        skipped?: number;
        failed?: number;
        status?: string;
        error?: string | null;
      } | null;
    }>;
  };
  recent_sources?: ConsoleSourceSummary[];
  connector_state?: {
    state_count?: number;
    enabled_state_count?: number;
    state_sync_status?: Record<string, number>;
    states?: ConsoleConnectorState[];
  };
  files?: {
    roots?: string[];
    configured?: boolean;
    recommended_commands?: string[];
  };
  recommended_commands?: string[];
  notes?: string[];
};

export type WorkspaceSearchResponse = {
  ok?: boolean;
  answer?: string;
  error?: string;
  fallback_reason?: string;
  source_refs?: Array<{ title?: string; snippet?: string; source_item_id?: string; url?: string }>;
  citations?: Array<{ title?: string; snippet?: string; source_item_id?: string; url?: string }>;
  trace?: Array<Record<string, unknown>>;
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
  activity_type?: string;
  target_type?: string;
  target_id?: string;
  pinned?: boolean;
};

export type TodayDiscoveryItem = {
  id: string;
  type?: string;
  label: string;
  title: string;
  summary: string;
  evidence?: Array<Record<string, unknown>>;
  confidence?: number | null;
  discovery_score?: number | null;
  quality_signals?: Record<string, unknown>;
  fingerprint?: string;
  evidence_snapshot?: Array<Record<string, unknown>>;
  evidence_count?: number;
  producer?: string;
  created_at?: string;
  status?: string;
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
  source_refs?: Array<{ source_item_id?: string; chunk_id?: string; url?: string; title?: string }>;
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

export type ReviewCenterItem = {
  review_item_id: string;
  owner_user_id?: string;
  review_type?: string;
  status?: string;
  title: string;
  confidence?: number | null;
  source_refs?: Array<{ source_item_id?: string; chunk_id?: string; url?: string; title?: string }>;
  source_ref_status?: string;
  created_at?: string;
  recommended_action?: string;
  recommended_actions?: string[];
  apply_supported?: boolean;
  apply_ready?: boolean;
  can_apply_now?: boolean;
};

export type ReviewCenterResponse = {
  ok?: boolean;
  owner_user_id?: string;
  status?: string;
  review_items?: ReviewCenterItem[];
  count?: number;
  total_matching?: number;
  supports_single_item_actions?: boolean;
};

export type WorkspaceActivityType = "opened" | "edited" | "viewed" | "pinned";

export type WorkspaceActivityResponse = {
  ok?: boolean;
  activity?: {
    activity_type?: WorkspaceActivityType;
    surface?: WorkspaceMode;
    target_type?: string;
    target_id?: string;
    title?: string;
    summary?: string;
    created_at?: string;
  };
};
