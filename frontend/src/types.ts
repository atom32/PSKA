export type WorkspaceMode = "today" | "writing" | "document" | "canvas" | "graph" | "corpus" | "review";

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

export type WritingNodeType = "goal" | "question" | "answer" | "evidence" | "gap" | "section" | "draft";

export type WritingBoard = {
  board_id: string;
  tenant_id?: string;
  owner_user_id?: string;
  title: string;
  goal?: string;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type WritingNode = {
  node_id: string;
  board_id: string;
  tenant_id?: string;
  owner_user_id?: string;
  node_type: WritingNodeType;
  title: string;
  body_markdown?: string;
  position?: { x?: number; y?: number };
  size?: { width?: number; height?: number };
  status?: string;
  source_refs?: Array<Record<string, unknown>>;
  citations?: Array<Record<string, unknown>>;
  quality_signals?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type WritingEdge = {
  edge_id: string;
  board_id: string;
  tenant_id?: string;
  owner_user_id?: string;
  source_node_id: string;
  target_node_id: string;
  edge_type: "decomposes_to" | "answered_by" | "supported_by" | "raises" | "conflicts_with" | "included_in" | "follows" | string;
  label?: string;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type WritingBoardResponse = {
  ok?: boolean;
  board?: WritingBoard;
  nodes?: WritingNode[];
  edges?: WritingEdge[];
};

export type WritingBoardsResponse = {
  ok?: boolean;
  boards?: WritingBoard[];
};

export type WritingQuestionSuggestion = {
  suggestion_id?: string;
  title?: string;
  question: string;
  direction?: string;
  rationale?: string;
};

export type WritingComposeResponse = {
  ok?: boolean;
  board_id?: string;
  section_node_id?: string | null;
  answer_node_ids?: string[];
  draft_markdown?: string;
  source_refs?: Array<Record<string, unknown>>;
  citations?: Array<Record<string, unknown>>;
  retrieval_used?: boolean;
};

export type EvidenceBriefResponse = {
  ok?: boolean;
  reason?: string;
  warnings?: string[];
  source_refs?: Array<Record<string, unknown>>;
  brief?: {
    board_id?: string;
    title?: string;
    status?: string;
    review_status?: string;
    knowledge_base_id?: string;
    knowledge_base_name?: string;
    knowledge_base_ids?: string[];
    knowledge_base_names?: string[];
    source_refs?: Array<Record<string, unknown>>;
    lineage?: Record<string, unknown>;
    warnings?: Array<Record<string, unknown>>;
  };
  board?: WritingBoard;
  nodes?: WritingNode[];
  edges?: WritingEdge[];
  error?: string;
};

export type EvidenceWikiSearchResult = {
  board?: WritingBoard;
  snippet?: string;
  match_fields?: string[];
  source_refs?: Array<Record<string, unknown>>;
  lineage?: Record<string, unknown>;
  published_at?: string | null;
  access?: Record<string, unknown>;
  taxonomy?: EvidenceWikiTaxonomy;
  content_review?: EvidenceWikiContentReview;
};

export type EvidenceWikiTaxonomy = {
  tags?: string[];
  categories?: string[];
  topics?: string[];
  collections?: string[];
};

export type EvidenceWikiTaxonomyFacet = {
  value?: string;
  count?: number;
};

export type EvidenceWikiContentRevision = {
  revision_id?: string;
  revision?: number;
  title?: string;
  summary?: string;
  body_markdown?: string;
  content_node_id?: string;
  edited_at?: string | null;
  editor_user_id?: string;
  restored_from_revision_id?: string;
};

export type EvidenceWikiContentReview = {
  status?: "draft" | "needs_review" | "published" | string;
  needs_review?: boolean;
  current_revision?: number;
  published_revision?: number;
  reason?: string;
  updated_at?: string | null;
  published_at?: string | null;
};

export type EvidenceWikiSearchResponse = {
  ok?: boolean;
  query?: string;
  count?: number;
  total_count?: number;
  scope_applied?: Record<string, unknown>;
  taxonomy_filters?: EvidenceWikiTaxonomy;
  taxonomy_facets?: Record<string, EvidenceWikiTaxonomyFacet[]>;
  results?: EvidenceWikiSearchResult[];
};

export type EvidenceWikiPage = {
  board_id?: string;
  title?: string;
  summary?: string;
  body_markdown?: string;
  content_node_id?: string;
  wiki_content_updated_at?: string | null;
  wiki_content_revision?: number;
  content_revision_count?: number;
  content_revisions?: EvidenceWikiContentRevision[];
  content_review?: EvidenceWikiContentReview;
  published_at?: string | null;
  publish_updated_at?: string | null;
  status?: string;
  publish_status?: string;
  lifecycle_status?: string;
  knowledge_base_ids?: string[];
  knowledge_base_names?: string[];
  source_refs?: Array<Record<string, unknown>>;
  lineage?: Record<string, unknown>;
  review_gate?: Record<string, unknown>;
  access?: Record<string, unknown>;
  taxonomy?: EvidenceWikiTaxonomy;
  related_pages?: EvidenceWikiRelatedPage[];
  node_ids?: string[];
};

export type EvidenceWikiRelatedPage = {
  board?: WritingBoard;
  reason?: string;
  shared_source_item_ids?: string[];
  shared_knowledge_base_ids?: string[];
  shared_taxonomy?: EvidenceWikiTaxonomy;
  taxonomy?: EvidenceWikiTaxonomy;
  source_refs?: Array<Record<string, unknown>>;
  published_at?: string | null;
  access?: Record<string, unknown>;
};

export type EvidenceWikiPageResponse = {
  ok?: boolean;
  reason?: string;
  error?: string;
  page?: EvidenceWikiPage;
  board?: WritingBoard;
  nodes?: WritingNode[];
};

export type EvidenceWikiTaxonomyUpdateResponse = {
  ok?: boolean;
  reason?: string;
  error?: string;
  taxonomy?: EvidenceWikiTaxonomy;
  board?: WritingBoard;
  page?: EvidenceWikiPage;
};

export type EvidenceWikiContentUpdateResponse = {
  ok?: boolean;
  reason?: string;
  error?: string;
  board?: WritingBoard;
  content_node?: WritingNode;
  page?: EvidenceWikiPage;
  content_revisions?: EvidenceWikiContentRevision[];
};

export type EvidenceWikiPublishResponse = {
  ok?: boolean;
  reason?: string;
  error?: string;
  publish_status?: string;
  review_gate?: Record<string, unknown>;
  board?: WritingBoard;
};

export type WorkspaceCorpusSource = {
  source_item_id?: string;
  title?: string;
  source_channel?: string;
  knowledge_base_id?: string;
  knowledge_base_name?: string;
  knowledge_base_ids?: string[];
  knowledge_base_names?: string[];
  url?: string;
  created_at?: string;
  snippet?: string;
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
  filters?: {
    knowledge_base_ids?: string[];
    source_channel?: string | null;
    query?: string;
    limit?: number;
  };
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

export type KnowledgeBase = {
  knowledge_base_id: string;
  owner_user_id?: string;
  tenant_id?: string;
  name: string;
  slug?: string;
  description?: string;
  kb_type?: string;
  status?: string;
  visibility?: string;
  visible_team_ids?: string[];
  default_space_id?: string | null;
  is_default?: boolean;
  pinned_at?: string | null;
  counts?: {
    source_items?: number;
    documents?: number;
    chunks?: number;
    active_chunks?: number;
    embedded_chunks?: number;
    processing_spans?: number;
    failed_processing_spans?: number;
    offline_index_states?: number;
    offline_index_dirty?: number;
  };
  readiness?: Record<string, unknown> & {
    retrieval_ready?: boolean;
    has_source_items?: boolean;
    has_documents?: boolean;
    has_chunks?: boolean;
    processing_status?: string;
    source_item_count?: number;
    document_count?: number;
    chunk_count?: number;
    active_chunk_count?: number;
    embedded_chunk_count?: number;
    embedding_coverage?: number;
    embedding_models?: string[];
    embedding_status?: string;
    processing_count?: number;
    failed_processing_count?: number;
    offline_index_state_count?: number;
    offline_index_dirty_count?: number;
    offline_index_fresh?: boolean;
    last_sync_at?: string | null;
    last_processing_at?: string | null;
    last_digest_at?: string | null;
    last_error?: string | null;
  };
  capabilities?: Record<string, unknown>;
  source_item_ids?: string[];
  created_at?: string;
  updated_at?: string;
  deleted_at?: string | null;
};

export type KnowledgeBaseListResponse = {
  ok?: boolean;
  tenant_id?: string;
  owner_user_id?: string;
  default_knowledge_base_id?: string;
  knowledge_bases?: KnowledgeBase[];
  error?: string;
};

export type KnowledgeBaseResponse = {
  ok?: boolean;
  tenant_id?: string;
  owner_user_id?: string;
  knowledge_base?: KnowledgeBase;
  error?: string;
};

export type KnowledgeBaseScope = {
  mode: "current" | "all" | "selected" | "attachments";
  currentKnowledgeBaseId?: string;
  selectedKnowledgeBaseIds?: string[];
};

export type WorkspaceSourceIngestResponse = {
  ok?: boolean;
  action?: "text" | "upload" | string;
  knowledge_source?: {
    knowledge_source_id?: string;
    name?: string;
    source_type?: string;
    uri?: string;
    status?: string;
  };
  knowledge_base_ids?: string[];
  source_item_ids?: string[];
  documents?: Array<Record<string, unknown>>;
  chunk_stats?: {
    count?: number;
    min_chars?: number;
    max_chars?: number;
    total_chars?: number;
  };
  sync_run?: Record<string, unknown>;
  sync_report?: Record<string, unknown>;
  digest?: {
    scheduled_source_item_ids?: string[];
    job?: Record<string, unknown> | null;
  } | null;
  error?: string;
};

export type WorkspaceDocumentEntry = WorkspaceCorpusSource & {
  lifecycle_status?: string;
  deleted_at?: string | null;
  deleted_by?: string | null;
  delete_reason?: string | null;
  document_count?: number;
  chunk_count?: number;
  impact?: Record<string, number>;
};

export type WorkspaceDocumentsResponse = {
  ok?: boolean;
  tenant_id?: string;
  owner_user_id?: string;
  knowledge_base_ids?: string[];
  include_deleted?: boolean;
  documents?: WorkspaceDocumentEntry[];
  counts?: Record<string, number>;
  error?: string;
};

export type WorkspaceReaderSourceResponse = {
  ok?: boolean;
  tenant_id?: string;
  owner_user_id?: string;
  source_item?: WorkspaceDocumentEntry;
  documents?: Array<{
    document_id?: string;
    source_item_id?: string;
    title?: string;
    body?: string;
    body_truncated?: boolean;
    body_chars?: number;
    metadata?: Record<string, unknown>;
    lifecycle_status?: string;
  }>;
  chunks?: Array<{
    chunk_id?: string;
    document_id?: string;
    source_item_id?: string;
    ordinal?: number;
    text?: string;
    text_chars?: number;
    metadata?: Record<string, unknown>;
    lifecycle_status?: string;
  }>;
  passage_windows?: Array<{
    passage_window_id?: string;
    source_item_id?: string;
    document_id?: string;
    ordinal?: number;
    title?: string;
    text?: string;
    start_char?: number;
    end_char?: number;
    token_estimate?: number;
    metadata?: Record<string, unknown>;
  }>;
  scope_applied?: Record<string, unknown>;
  counts?: Record<string, number>;
  error?: string;
};

export type WorkspaceDocumentDeleteResponse = {
  ok?: boolean;
  dry_run?: boolean;
  execute?: boolean;
  restore?: boolean;
  hard_delete?: boolean;
  delete_mode?: string;
  knowledge_base_ids?: string[];
  source_item_ids?: string[];
  counts?: Record<string, number>;
  deleted?: Record<string, number>;
  notes?: string[];
  error?: string;
};

export type WorkspaceDocumentLinkResponse = {
  ok?: boolean;
  dry_run?: boolean;
  execute?: boolean;
  knowledge_base_ids?: string[];
  target_knowledge_base_ids?: string[];
  source_item_ids?: string[];
  counts?: Record<string, number>;
  linked?: Record<string, number>;
  notes?: string[];
  error?: string;
};

export type WorkspaceDocumentMoveResponse = {
  ok?: boolean;
  dry_run?: boolean;
  execute?: boolean;
  source_knowledge_base_id?: string;
  target_knowledge_base_id?: string;
  knowledge_base_ids?: string[];
  source_item_ids?: string[];
  counts?: Record<string, number>;
  moved?: Record<string, number>;
  notes?: string[];
  error?: string;
};

export type AskConversation = {
  conversation_id?: string;
  title?: string;
  status?: string;
  summary?: string;
  metadata?: Record<string, unknown>;
  scope_applied?: Record<string, unknown>;
  knowledge_base_ids?: string[];
  created_at?: string;
  updated_at?: string;
};

export type AskMessage = {
  message_id?: string;
  conversation_id?: string;
  role?: string;
  content?: string;
  run_id?: string | null;
  citations?: Array<Record<string, unknown>>;
  source_refs?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  scope_applied?: Record<string, unknown>;
  knowledge_base_ids?: string[];
  created_at?: string;
};

export type AskRun = {
  run_id?: string;
  conversation_id?: string;
  status?: string;
  query?: string;
  result?: WorkspaceAskResponse;
  route?: Record<string, unknown>;
  scope_applied?: Record<string, unknown>;
  knowledge_base_ids?: string[];
  evidence_check?: Record<string, unknown>;
  started_at?: string;
  finished_at?: string;
};

export type AskConversationResponse = {
  ok?: boolean;
  conversation?: AskConversation;
  messages?: AskMessage[];
  runs?: AskRun[];
  conversations?: AskConversation[];
  error?: string;
};

export type PromptProfile = {
  prompt_profile_id?: string;
  profile_type?: "ask" | "digest" | "review" | "writing" | string;
  scope?: string;
  name?: string;
  owner_user_id?: string | null;
  current_version?: number;
  config?: Record<string, unknown>;
};

export type PromptProfilesResponse = {
  ok?: boolean;
  profiles?: PromptProfile[];
  effective?: Record<string, PromptProfile>;
  defaults?: Record<string, PromptProfile>;
  error?: string;
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
  quality_tier?: string;
  support_kinds?: string[];
  promotion_reason?: string;
  review_eligible?: boolean;
  diagnostics?: Record<string, unknown>;
  source_refs?: Array<{ source_item_id?: string; document_id?: string; chunk_id?: string; passage_window_id?: string; title?: string; url?: string }>;
};

export type WorkspaceGraphEdge = {
  id: string;
  source: string;
  target: string;
  type?: string;
  label?: string;
  confidence?: number;
  quality_tier?: string;
  support_kinds?: string[];
  promotion_reason?: string;
  review_eligible?: boolean;
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
  scope_applied?: Record<string, unknown>;
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

export type WorkspaceAskResponse = {
  ok?: boolean;
  query?: string;
  conversation_id?: string;
  run_id?: string;
  status?: string;
  answer?: string;
  route?: {
    intent?: "auto" | "quick" | "deep" | string;
    selected_intent?: "quick" | "deep" | string;
    retrieval_owner?: "pska" | "fastreact_pska_mcp" | string;
    surface?: string;
    requires_agentic_service_online?: boolean;
    fallback_from?: string;
    requested_intent?: string;
    rewrite_query?: string;
    scope_applied?: Record<string, unknown>;
    understand?: Record<string, unknown>;
    intent_contract?: {
      schema?: string;
      interaction_intent?: string;
      task_intent?: string;
      ask_intent?: string;
      requires_evidence?: boolean;
      execution_depth?: "none" | "quick" | "deep" | string;
      requested_depth?: "auto" | "quick" | "deep" | string;
      scope_policy?: string;
      answer_contract?: string;
      quick_deep_applicable?: boolean;
      depth_override?: Record<string, unknown>;
    };
    tool_policy?: Record<string, unknown>;
  };
  evidence?: {
    citations?: Array<Record<string, unknown>>;
    source_refs?: Array<Record<string, unknown>>;
    results?: Array<Record<string, unknown>>;
    source_windows?: Array<Record<string, unknown>>;
    graph_paths?: Array<Record<string, unknown>>;
    memory_context?: Array<Record<string, unknown>>;
    profile_context?: Array<Record<string, unknown>>;
    gaps?: unknown[];
    conflicts?: unknown[];
    dropped_citations?: Array<Record<string, unknown>>;
    evidence_claims?: unknown[];
    no_answer_reasons?: unknown[];
  };
  citations?: Array<Record<string, unknown>>;
  source_refs?: Array<Record<string, unknown>>;
  citation_markers?: Array<Record<string, unknown>>;
  source_windows?: Array<Record<string, unknown>>;
  intent?: string;
  rewrite_query?: string;
  scope_applied?: Record<string, unknown>;
  evidence_claims?: unknown[];
  answer_type?: string | null;
  citation_audit?: {
    used?: Array<Record<string, unknown>>;
    dropped?: Array<Record<string, unknown>>;
  };
  evidence_check?: Record<string, unknown>;
  no_answer_reasons?: unknown[];
  agent_steps?: Array<WorkspaceAskAgentStep>;
  progress?: Array<WorkspaceAskProgress>;
  trace?: Record<string, unknown>;
  timing?: {
    total_ms?: number;
    time_to_first_answer_ms?: number;
    time_to_first_agent_event_ms?: number;
  };
  quality_signals?: Record<string, unknown>;
  agentic_service?: Record<string, unknown>;
  tenant_id?: string;
  owner_user_id?: string;
  error?: string | { message?: string; detail?: string; type?: string };
};

export type WorkspaceAskProgress = {
  stage?: "query_understand" | "understand" | "search" | "rerank" | "graph" | "read" | "generate" | "evidence_check" | string;
  phase?: string;
  status?: string;
  title?: string;
  detail?: string;
  step_id?: string;
  tool_name?: string | null;
  elapsed_ms?: number | null;
  evidence_count?: number | null;
  source_ref_count?: number | null;
};

export type WorkspaceAskAgentStep = {
  step_id?: string;
  phase?: string;
  status?: string;
  title?: string;
  detail?: string;
  tool_name?: string | null;
  tool_call_id?: string | null;
  evidence_count?: number | null;
  source_ref_count?: number | null;
  elapsed_ms?: number | null;
  raw_event_id?: string | null;
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

export type SourceAdapterDefinition = {
  source_type?: string;
  connector_id?: string;
  label?: string;
};

export type SourcePreviewResource = {
  resource_id?: string;
  title?: string;
  uri?: string;
  record_type?: string;
  updated_at?: string | null;
  content_hash?: string | null;
  summary?: string;
  metadata?: Record<string, unknown>;
};

export type SourcePreviewResponse = {
  ok?: boolean;
  source?: Record<string, unknown>;
  preview?: {
    ok?: boolean;
    count?: number;
    validation?: Record<string, unknown>;
    resources?: SourcePreviewResource[];
  };
  adapters?: SourceAdapterDefinition[];
  error?: string;
};

export type KnowledgeSourceCreateResponse = {
  ok?: boolean;
  knowledge_source?: {
    knowledge_source_id?: string;
    name?: string;
    source_type?: string;
    uri?: string;
    path?: string;
    status?: string;
  };
  knowledge_base_ids?: string[];
  preview?: SourcePreviewResponse["preview"] | null;
  adapters?: SourceAdapterDefinition[];
  error?: string;
};

export type SourceSyncResponse = {
  ok?: boolean;
  totals?: {
    sources?: number;
    scanned?: number;
    ingested?: number;
    new_files?: number;
    changed_files?: number;
    unchanged_files?: number;
    skipped?: number;
    failed?: number;
  };
  failed?: Array<{ error?: string } | string>;
  knowledge_base_ids?: string[];
  knowledge_sources?: Array<Record<string, unknown>>;
  sync_runs?: Array<Record<string, unknown>>;
  reports?: Array<Record<string, unknown>>;
  error?: string;
};

export type DigestNowResponse = {
  ok?: boolean;
  error?: string;
  scope_applied?: Record<string, unknown>;
  mode?: "queued" | "sync_worker" | string;
  queued?: boolean;
  job?: {
    job_id?: string;
    job_type?: string;
    status?: string;
    error?: string | null;
    created_at?: string;
    updated_at?: string;
  } | null;
  scheduled?: {
    job?: {
      job_id?: string;
      job_type?: string;
      status?: string;
      error?: string | null;
      created_at?: string;
      updated_at?: string;
    } | null;
    scheduled_source_item_ids?: string[];
    skipped_source_item_ids?: string[];
    selected_source_items?: unknown[];
    skipped_source_items?: unknown[];
  };
  sync?: FileSyncResponse | null;
  digest?: {
    scheduled_source_item_ids?: string[];
  };
  worker_runs?: unknown[];
  worker_status?: {
    requested?: boolean;
    ok?: boolean;
    processed?: number;
    failed_runs?: number;
    diagnostics?: string[];
  };
  summary?: {
    synced?: FileSyncResponse["totals"] | null;
    scheduled_source_items?: number;
    worker_processed?: number;
    worker_diagnostics?: string[];
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
    queued_jobs?: number;
    skipped_source_items?: number;
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
  source_refs?: Array<Record<string, unknown>>;
  knowledge_base_id?: string;
  knowledge_base_name?: string;
  knowledge_base_ids?: string[];
  knowledge_base_names?: string[];
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
  knowledge_claims?: Array<{
    statement?: string;
    claim_type?: string;
    confidence?: number;
    evidence_text?: string;
    source_refs?: Array<Record<string, unknown>>;
    knowledge_base_id?: string;
    knowledge_base_name?: string;
    knowledge_base_ids?: string[];
    knowledge_base_names?: string[];
  }>;
  digest_notes?: Array<{
    title?: string;
    synopsis?: string;
    actions?: unknown[];
    open_questions?: unknown[];
    risks?: unknown[];
    source_refs?: Array<Record<string, unknown>>;
    knowledge_base_id?: string;
    knowledge_base_name?: string;
    knowledge_base_ids?: string[];
    knowledge_base_names?: string[];
  }>;
  latest_event?: { event_type?: string; message?: string; created_at?: string; detail?: Record<string, unknown> } | null;
  timeline?: Array<{ event_type?: string; message?: string; created_at?: string; detail?: Record<string, unknown> }>;
};

export type DigestLogsResponse = {
  ok?: boolean;
  owner_user_id?: string;
  scope_applied?: Record<string, unknown>;
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

export type ProcessingSpan = {
  processing_span_id?: string;
  knowledge_source_id?: string;
  sync_run_id?: string | null;
  source_item_id?: string | null;
  stage?: string;
  status?: string;
  started_at?: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  error?: string | null;
};

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
      processing_config?: Record<string, unknown> | null;
      latest_processing_spans?: ProcessingSpan[];
      processing_status?: Record<string, unknown>;
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
      sync_runs?: unknown[];
    }>;
  };
  source_adapters?: SourceAdapterDefinition[];
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
  processing_spans?: {
    span_count?: number;
    spans?: ProcessingSpan[];
  };
  workspace?: {
    root?: string;
    excluded_paths?: string[];
  };
  recommended_commands?: string[];
  notes?: string[];
};

export type ChunkingPreviewChunk = {
  ordinal?: number;
  text?: string;
  start?: number;
  end?: number;
  chars?: number;
  strategy?: string;
  context_header?: string | null;
};

export type ChunkingPreviewResponse = {
  ok?: boolean;
  processing_config?: Record<string, unknown>;
  preview?: {
    ok?: boolean;
    strategy?: string;
    requested_strategy?: string;
    strategy_diagnostics?: Record<string, unknown>;
    config?: Record<string, unknown>;
    profile?: Record<string, unknown>;
    stats?: {
      count?: number;
      min_chars?: number;
      max_chars?: number;
      avg_chars?: number;
      total_chars?: number;
    };
    chunks?: ChunkingPreviewChunk[];
    parent_windows?: Array<{
      ordinal?: number;
      start?: number;
      end?: number;
      chars?: number;
      text?: string;
      child_ordinals?: number[];
      window_policy?: string;
    }>;
  };
  error?: string;
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
  scope_applied?: Record<string, unknown>;
  citation_markers?: Array<Record<string, unknown>>;
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

export type KnowledgeBaseSearchResponse = WorkspaceSearchResponse & {
  mode?: "knowledge_base_search" | string;
  search_mode?: string;
  tenant_id?: string;
  owner_user_id?: string;
  knowledge_base_ids?: string[];
  knowledge_bases?: KnowledgeBase[];
  results?: Array<Record<string, unknown>>;
  diagnostics?: Record<string, unknown>;
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
  created_at?: string;
  can_apply_now?: boolean;
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
  knowledge_base_id?: string;
  knowledge_base_name?: string;
  knowledge_base_ids?: string[];
  knowledge_base_names?: string[];
  source_refs?: Array<{
    source_item_id?: string;
    document_id?: string;
    chunk_id?: string;
    passage_window_id?: string;
    url?: string;
    title?: string;
    knowledge_base_id?: string;
    knowledge_base_name?: string;
    knowledge_base_ids?: string[];
    knowledge_base_names?: string[];
  }>;
  source_ref_status?: string;
  support_ids?: string[];
  support_kinds?: string[];
  quality_tier?: string;
  promotion_reason?: string;
  review_eligible?: boolean;
  proposal?: Record<string, unknown>;
  created_at?: string;
  recommended_action?: string;
  recommended_actions?: string[];
  apply_supported?: boolean;
  apply_ready?: boolean;
  can_apply_now?: boolean;
  remediation?: ReviewRemediation;
  application_result?: ReviewApplicationResult;
};

export type ReviewRemediation = {
  status?: "ready" | "blocked" | "review" | "resolved" | string;
  summary?: string;
  blockers?: Array<{
    blocker_id?: string;
    label?: string;
    detail?: string;
  }>;
  actions?: Array<{
    action_id?: string;
    label?: string;
    kind?: string;
    enabled?: boolean;
    reason?: string;
  }>;
};

export type ReviewCenterResponse = {
  ok?: boolean;
  owner_user_id?: string;
  status?: string;
  scope_applied?: Record<string, unknown>;
  analytics?: ReviewCenterAnalytics;
  review_items?: ReviewCenterItem[];
  count?: number;
  total_matching?: number;
  supports_single_item_actions?: boolean;
};

export type ReviewCenterAnalytics = {
  total?: number;
  status_counts?: Record<string, number>;
  review_type_counts?: Record<string, number>;
  source_ref_status_counts?: Record<string, number>;
  quality_tier_counts?: Record<string, number>;
  recommended_action_counts?: Record<string, number>;
  apply_ready_count?: number;
  apply_supported_count?: number;
  review_eligible_count?: number;
  pending_oldest_age_days?: number;
  pending_average_age_days?: number;
  by_review_type?: Record<
    string,
    {
      total?: number;
      status_counts?: Record<string, number>;
      source_ref_status_counts?: Record<string, number>;
      apply_ready?: number;
      apply_supported?: number;
    }
  >;
};

export type ReviewApplicationResult = {
  applied?: boolean;
  status?: string;
  review_type?: string;
  action?: string;
  promotion_type?: string;
  target_ids?: Record<string, string>;
  target_preview?: ReviewAppliedTargetPreview | null;
  source_refs?: Array<Record<string, unknown>>;
  summary?: string;
  history?: ReviewDecisionHistoryEvent[];
  metadata?: Record<string, unknown>;
};

export type ReviewAppliedTargetPreview = {
  target_type?: string;
  target_id?: string;
  title?: string;
  body?: string;
  confidence?: number | null;
  source_ref_count?: number;
  updated_at?: string | null;
  attributes?: Array<{ label?: string; value?: string }>;
};

export type ReviewDecisionHistoryEvent = {
  audit_event_id?: string;
  action?: string;
  decision?: string;
  actor_user_id?: string;
  created_at?: string | null;
  reason?: string | null;
  promotion_type?: string | null;
  target_ids?: Record<string, string>;
  source_ref_count?: number;
};

export type ReviewActionResponse = {
  review_item?: ReviewCenterItem & { status?: string };
  application_result?: ReviewApplicationResult;
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
