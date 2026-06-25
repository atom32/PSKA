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

export type WorkspaceGraphNode = {
  id: string;
  type: "source" | "document" | "passage" | "claim" | "digest" | "phrase" | "entity" | "fact" | "hyperedge" | "memory" | "memory_suggestion" | "action" | string;
  label?: string;
  summary?: string;
  object_type?: string;
  object_id?: string;
  confidence?: number;
  token_estimate?: number;
  source_refs?: Array<{ source_item_id?: string; document_id?: string; chunk_id?: string; passage_window_id?: string; title?: string; url?: string }>;
};

export type WorkspaceGraphEdge = {
  id: string;
  source: string;
  target: string;
  type?: string;
  label?: string;
  confidence?: number;
  source_refs?: Array<{ source_item_id?: string; document_id?: string; chunk_id?: string; passage_window_id?: string }>;
};

export type WorkspaceGraphInsightNode = {
  id?: string;
  type?: string;
  label?: string;
  summary?: string;
  degree?: number;
};

export type WorkspaceGraphResponse = {
  ok?: boolean;
  ontology_version?: string;
  owner_user_id?: string;
  nodes?: WorkspaceGraphNode[];
  edges?: WorkspaceGraphEdge[];
  matches?: Array<WorkspaceGraphInsightNode & { score?: number }>;
  projection?: {
    nodes?: number;
    edges?: number;
    unfiltered_nodes?: number;
    unfiltered_edges?: number;
    source_nodes?: number;
    source_edges?: number;
    node_types?: string[] | null;
  };
  evidence_path?: {
    node_id?: string;
    nodes?: WorkspaceGraphNode[];
    edges?: WorkspaceGraphEdge[];
    evidence_node_count?: number;
    understanding_node_count?: number;
  };
  insights?: {
    layer_coverage?: Record<string, number>;
    evidence_health?: {
      grounded_nodes?: number;
      total_nodes?: number;
      grounded_ratio?: number;
      grounded_by_type?: Record<string, number>;
      evidence_edge_count?: number;
      semantic_edge_count?: number;
    };
    central_nodes?: WorkspaceGraphInsightNode[];
    topic_clusters?: Array<{
      cluster_id?: string;
      title?: string;
      summary?: string;
      node_count?: number;
      edge_count?: number;
      types?: Record<string, number>;
      anchor_nodes?: WorkspaceGraphInsightNode[];
    }>;
    guided_tour?: Array<{
      title?: string;
      reason?: string;
      node_ids?: string[];
    }>;
  };
  counts?: {
    sources?: number;
    documents?: number;
    passages?: number;
    claims?: number;
    digest_notes?: number;
    memories?: number;
    review_items?: number;
    phrases?: number;
    entities?: number;
    facts?: number;
    hyperedges?: number;
  };
  notes?: string[];
};

export type WorkspaceGraphPathResponse = {
  ok?: boolean;
  owner_user_id?: string;
  query?: string;
  ontology_version?: string;
  mode?: "deterministic" | "agentic" | string;
  display_mode?: string;
  requires_agentic_service_online?: boolean;
  answer?: string;
  query_seeds?: {
    terms?: string[];
    passages?: Array<Record<string, unknown>>;
    facts?: Array<Record<string, unknown>>;
    graph_path_count?: number;
  };
  top_facts?: Array<Record<string, unknown>>;
  supporting_passages?: Array<Record<string, unknown>>;
  filtered_out_facts?: Array<Record<string, unknown>>;
  citations?: Array<Record<string, unknown>>;
  graph_paths?: Array<Record<string, unknown>>;
  path_summary?: {
    summary?: string;
    result_count?: number;
    citation_count?: number;
    graph_path_count?: number;
    kept_fact_count?: number;
    filtered_fact_count?: number;
    filter_mode?: string;
    has_graph_signal?: boolean;
    fallback?: string | null;
    diagnostics?: Record<string, unknown>;
  };
  agentic_retrieval?: Record<string, unknown>;
  agentic_repair?: {
    attempted?: boolean;
    accepted?: boolean;
    repaired_answer_chars?: number;
    final_answer_mode?: string;
    error?: unknown;
  };
  agentic_trace?: {
    retrieval_plan?: unknown;
    query_understanding?: unknown;
    iterations?: unknown;
    expansion_decisions?: Array<Record<string, unknown>>;
    graph_paths_used?: unknown;
    fact_relevance_filter?: Record<string, unknown>;
    evidence_check?: unknown;
    events?: Array<Record<string, unknown>>;
  };
  agentic_source_refs?: Array<Record<string, unknown>>;
  agentic_service?: Record<string, unknown>;
  error?: string | { message?: string; detail?: string; type?: string };
};

export type FileSyncResponse = {
  ok?: boolean;
  error?: string;
  totals?: {
    roots?: number;
    scanned?: number;
    ingested?: number;
    new_files?: number;
    changed_files?: number;
    unchanged_files?: number;
    twitter_zip_count?: number;
    twitter_imported?: number;
    twitter_skipped?: number;
    failed?: number;
  };
  twitter_archives?: {
    enabled?: boolean;
    input?: string;
    archive_root?: string;
    zip_count?: number;
    imported?: number;
    skipped?: number;
    failed?: Array<{ error?: string } | string>;
    reason?: string;
  };
  failed?: Array<{ error?: string } | string>;
};

export type DigestNowResponse = {
  ok?: boolean;
  error?: string;
  sync?: FileSyncResponse | null;
  digest?: {
    scheduled_source_item_ids?: string[];
  };
  summary?: {
    synced?: FileSyncResponse["totals"] | null;
    scheduled_source_items?: number;
    worker_processed?: number;
    candidate_write?: {
      entities?: number;
      hyperedges?: number;
      knowledge_claims?: number;
      digest_notes?: number;
      review_items?: number;
      memory_candidates?: number;
      saved_candidates?: number;
      review_candidates?: number;
    };
    pending_review_count?: number;
    failed_digest_jobs?: number;
  };
  failed_digest_jobs?: unknown[];
};

export type DigestLogEntry = {
  job_id: string;
  status?: string;
  attempts?: number;
  max_attempts?: number;
  worker_id?: string | null;
  external_run_id?: string | null;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  source_item_ids?: string[];
  source_item_count?: number;
  candidate_summary?: {
    entities?: number;
    hyperedges?: number;
    knowledge_claims?: number;
    digest_notes?: number;
    review_items?: number;
    agent_memories?: number;
    profile_cards?: number;
    saved_candidates?: number;
    review_candidates?: number;
    warnings?: string[];
  };
  knowledge_claims?: Array<{ statement?: string; claim_type?: string; confidence?: number; evidence_text?: string }>;
  digest_notes?: Array<{ title?: string; synopsis?: string; actions?: unknown[]; open_questions?: unknown[]; risks?: unknown[] }>;
  latest_event?: { event_type?: string; message?: string; created_at?: string; detail?: Record<string, unknown> } | null;
  timeline?: Array<{ event_type?: string; message?: string; created_at?: string; detail?: Record<string, unknown> }>;
};

export type DigestLogsResponse = {
  ok?: boolean;
  owner_user_id?: string;
  summary?: {
    status_counts?: Record<string, number>;
    candidate_totals?: {
      knowledge_claims?: number;
      digest_notes?: number;
      hyperedges?: number;
      review_items?: number;
      saved_candidates?: number;
      review_candidates?: number;
    };
    recent_claims?: Array<{ statement?: string; claim_type?: string; confidence?: number; job_id?: string }>;
    recent_digest_notes?: Array<{ title?: string; synopsis?: string; job_id?: string }>;
    latest_failure?: { job_id?: string; error?: string | null; updated_at?: string } | null;
    has_useful_output?: boolean;
  };
  logs?: DigestLogEntry[];
  count?: number;
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
  input_sources?: Array<{
    kind?: string;
    name?: string;
    path?: string;
    status?: string;
    mode?: string;
    configured?: boolean;
    knowledge_source_id?: string;
    zip_count?: number;
  }>;
  workspace?: {
    root?: string;
    excluded_paths?: string[];
  };
  recommended_commands?: string[];
  notes?: string[];
};

export type WorkspaceSearchResponse = {
  ok?: boolean;
  answer?: string;
  error?: string | { type?: string; message?: string; detail?: string };
  mode?: string;
  display_mode?: string;
  fallback_reason?: string;
  fallback?: {
    mode?: string;
    display_mode?: string;
    retrieval?: {
      results?: Array<{ title?: string; snippet?: string; score?: number; source_item_id?: string }>;
      citations?: Array<{ title?: string; snippet?: string; source_item_id?: string; url?: string }>;
      graph_paths?: Array<{ explanation?: string; entities?: string[] }>;
    };
  };
  source_refs?: Array<{ title?: string; snippet?: string; source_item_id?: string; url?: string }>;
  citations?: Array<{ title?: string; snippet?: string; source_item_id?: string; url?: string }>;
  trace?: {
    events?: Array<Record<string, unknown>>;
    tool_calls?: Array<Record<string, unknown>>;
    event_count?: number;
    run_id?: string;
    session_id?: string;
    status?: string;
    error?: string;
    fastreact_metadata?: Record<string, unknown>;
    [key: string]: unknown;
  };
  agentic_service?: {
    provider?: string;
    adapter?: string;
    url?: string;
    run_id?: string;
    session_id?: string;
  };
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

export type KnowledgeSourceCleanupResponse = {
  ok?: boolean;
  dry_run?: boolean;
  root?: string;
  execute?: boolean;
  knowledge_source?: {
    knowledge_source_id?: string;
    name?: string;
    uri?: string;
    path?: string;
    mode?: string;
    status?: string;
  };
  source_item_ids?: string[];
  source_items?: Array<{ source_item_id?: string; title?: string; url?: string; created_at?: string }>;
  counts?: Record<string, number>;
  deleted?: Record<string, number>;
  error?: string;
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

export type ReviewActionResponse = {
  review_item?: ReviewCenterItem & { status?: string };
  application_result?: {
    applied?: boolean;
    status?: string;
    review_type?: string;
    action?: string;
    promotion_type?: string;
    target_ids?: Record<string, string>;
    source_refs?: Array<Record<string, unknown>>;
    summary?: string;
    metadata?: Record<string, unknown>;
  };
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
