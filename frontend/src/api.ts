import type {
  BrainState,
  ConsoleSourcesResponse,
  DigestLogsResponse,
  DigestNowResponse,
  FileSyncResponse,
  KnowledgeSourceCleanupResponse,
  ReviewCenterResponse,
  ReviewActionResponse,
  TodayResponse,
  WorkspaceActivityResponse,
  WorkspaceActivityType,
  WorkspaceCorpusResponse,
  WorkspaceGraphPathResponse,
  WorkspaceGraphResponse,
  WorkspaceMode,
  WorkspaceSearchResponse
} from "./types";

const headers = (serviceToken: string) => {
  const result: Record<string, string> = { "Content-Type": "application/json" };
  if (serviceToken) {
    result.Authorization = `Bearer ${serviceToken}`;
  }
  return result;
};

export async function analyzeWorkspaceContext(
  query: string,
  serviceToken: string,
  trigger: BrainState["lastTrigger"]
): Promise<Partial<BrainState>> {
  if (!query.trim()) {
    return { status: "idle", lastTrigger: trigger, updatedAt: Date.now(), error: null };
  }

  const searchResponse = await fetch("/workspace/search/query", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      query,
      mode: "direct",
      capture: false,
      user_id: "user_primary",
      represented_user_id: "user_primary",
      top_k: 5
    })
  });

  if (!searchResponse.ok) {
    throw new Error(`PSKA search request failed with HTTP ${searchResponse.status}`);
  }

  const searchData = (await searchResponse.json()) as WorkspaceSearchResponse;
  return mapSearchToBrain(searchData, trigger);
}

export async function loadCorpusContext(serviceToken: string): Promise<Partial<BrainState>> {
  try {
    const data = await loadCorpusData(serviceToken, 12);
    return {
      entities: unique((data.entities || []).map((entity) => entity.label || entity.canonical_name || entity.name || entity.entity_id || "")).slice(0, 8),
      timeline: (data.sources || []).slice(0, 5).map((source, index) => ({
        id: source.source_item_id || `source-${index}`,
        age: formatAge(source.created_at),
        title: source.title || source.source_channel || "未命名来源",
        detail: source.source_channel || "PSKA 来源材料"
      })),
      connections: (data.hyperedges || []).slice(0, 5).map((edge, index) => ({
        id: `edge-${index}`,
        label: edge.label || edge.summary || "知识关系",
        relation: edge.relation || "关联到"
      }))
    };
  } catch {
    return {};
  }
}

export async function loadCorpusData(serviceToken: string, limit = 16): Promise<WorkspaceCorpusResponse> {
  const response = await fetch(`/workspace/corpus/data?owner_user_id=user_primary&limit=${limit}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`corpus ${response.status}`);
  }
  return (await response.json()) as WorkspaceCorpusResponse;
}

export async function loadGraphData(serviceToken: string, limit = 60, nodeTypes: string[] = []): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: "user_primary",
    limit: String(limit)
  });
  if (nodeTypes.length > 0) {
    params.set("node_types", nodeTypes.join(","));
  }
  const response = await fetch(`/workspace/graph/data?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`graph ${response.status}`);
  }
  return (await response.json()) as WorkspaceGraphResponse;
}

export async function loadGraphSubgraph(
  serviceToken: string,
  nodeId: string,
  limit = 80,
  hops = 1,
  nodeTypes: string[] = []
): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: "user_primary",
    node_id: nodeId,
    limit: String(limit),
    hops: String(hops)
  });
  if (nodeTypes.length > 0) {
    params.set("node_types", nodeTypes.join(","));
  }
  const response = await fetch(`/workspace/graph/subgraph?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`graph subgraph ${response.status}`);
  }
  return (await response.json()) as WorkspaceGraphResponse;
}

export async function loadGraphSearchSubgraph(
  serviceToken: string,
  query: string,
  limit = 80,
  hops = 1,
  topK = 5,
  nodeTypes: string[] = []
): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: "user_primary",
    query,
    limit: String(limit),
    hops: String(hops),
    top_k: String(topK)
  });
  if (nodeTypes.length > 0) {
    params.set("node_types", nodeTypes.join(","));
  }
  const response = await fetch(`/workspace/graph/search-subgraph?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`graph search subgraph ${response.status}`);
  }
  return (await response.json()) as WorkspaceGraphResponse;
}

export async function loadGraphPath(
  serviceToken: string,
  query: string,
  mode: "deterministic" | "agentic" = "deterministic"
): Promise<WorkspaceGraphPathResponse> {
  const params = new URLSearchParams({
    owner_user_id: "user_primary",
    query,
    mode,
    top_k: "8"
  });
  const response = await fetch(`/workspace/graph/path?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`graph path ${response.status}`);
  }
  return (await response.json()) as WorkspaceGraphPathResponse;
}

export async function loadSourcesConsole(serviceToken: string, limit = 20): Promise<ConsoleSourcesResponse> {
  const response = await fetch(`/console/sources/data?owner_user_id=user_primary&limit=${limit}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`sources ${response.status}`);
  }
  return (await response.json()) as ConsoleSourcesResponse;
}

export async function runFileSync(serviceToken: string): Promise<FileSyncResponse> {
  const response = await fetch("/files/sync", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      owner_user_id: "user_primary",
      skip_twitter_archives: false
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "同步资料失败"));
  }
  return (await response.json()) as FileSyncResponse;
}

export async function runDigestNow(serviceToken: string): Promise<DigestNowResponse> {
  const response = await fetch("/digest/now", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      owner_user_id: "user_primary",
      limit: 20,
      batch_size: 20,
      force: false,
      skip_sync: false,
      max_worker_runs: 1,
      reason: "manual frontend digest-now"
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "同步并理解失败"));
  }
  return (await response.json()) as DigestNowResponse;
}

export async function loadDigestLogs(serviceToken: string, limit = 8): Promise<DigestLogsResponse> {
  const response = await fetch(`/digest/logs?owner_user_id=user_primary&limit=${limit}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`digest logs ${response.status}`);
  }
  return (await response.json()) as DigestLogsResponse;
}

export async function cleanupKnowledgeSource(
  serviceToken: string,
  knowledgeSourceId: string,
  execute = false
): Promise<KnowledgeSourceCleanupResponse> {
  const response = await fetch(`/knowledge-sources/${encodeURIComponent(knowledgeSourceId)}/cleanup`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      owner_user_id: "user_primary",
      execute,
      pause_knowledge_source: true,
      delete_knowledge_source: false
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, execute ? "清理资料来源失败" : "预览清理失败"));
  }
  return (await response.json()) as KnowledgeSourceCleanupResponse;
}

export async function searchWorkspace(
  query: string,
  serviceToken: string,
  mode: "agentic" | "direct" = "direct"
): Promise<WorkspaceSearchResponse> {
  const response = await fetch("/workspace/search/query", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      query,
      mode,
      capture: false,
      user_id: "user_primary",
      represented_user_id: "user_primary",
      top_k: 8
    })
  });
  if (!response.ok) {
    throw new Error(`search ${response.status}`);
  }
  return (await response.json()) as WorkspaceSearchResponse;
}

async function responseError(response: Response, fallback: string) {
  try {
    const payload = (await response.json()) as { error?: string; message?: string };
    return payload.error || payload.message || `${fallback} (${response.status})`;
  } catch {
    return `${fallback} (${response.status})`;
  }
}

export async function loadToday(serviceToken: string): Promise<TodayResponse> {
  const response = await fetch("/workspace/today/data?owner_user_id=user_primary&limit=10", { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`today ${response.status}`);
  }
  return (await response.json()) as TodayResponse;
}

export async function loadReviewCenter(serviceToken: string, status = "pending"): Promise<ReviewCenterResponse> {
  const params = new URLSearchParams({
    owner_user_id: "user_primary",
    status,
    limit: "50"
  });
  const response = await fetch(`/console/reviews/data?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`reviews ${response.status}`);
  }
  return (await response.json()) as ReviewCenterResponse;
}

export async function recordWorkspaceActivity(
  serviceToken: string,
  payload: {
    activity_type: WorkspaceActivityType;
    surface: WorkspaceMode;
    target_type: string;
    target_id: string;
    title: string;
    summary?: string;
    metadata?: Record<string, unknown>;
  }
): Promise<WorkspaceActivityResponse> {
  const response = await fetch("/workspace/activity", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      owner_user_id: "user_primary",
      actor_user_id: "user_primary",
      ...payload
    })
  });
  if (!response.ok) {
    throw new Error(`activity ${response.status}`);
  }
  return (await response.json()) as WorkspaceActivityResponse;
}

export async function acceptDiscovery(serviceToken: string, discoveryId: string): Promise<void> {
  const response = await fetch(`/workspace/discoveries/${encodeURIComponent(discoveryId)}/accept`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: "user_primary",
      reason: "Accepted from PSKA Today"
    })
  });
  if (!response.ok) {
    throw new Error(`accept discovery ${response.status}`);
  }
}

export async function ignoreDiscovery(serviceToken: string, discoveryId: string): Promise<void> {
  const response = await fetch(`/workspace/discoveries/${encodeURIComponent(discoveryId)}/ignore`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: "user_primary",
      reason: "Ignored from PSKA Today"
    })
  });
  if (!response.ok) {
    throw new Error(`ignore discovery ${response.status}`);
  }
}

export async function snoozeDiscovery(serviceToken: string, discoveryId: string): Promise<void> {
  const response = await fetch(`/workspace/discoveries/${encodeURIComponent(discoveryId)}/snooze`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: "user_primary",
      reason: "Snoozed from PSKA Today"
    })
  });
  if (!response.ok) {
    throw new Error(`snooze discovery ${response.status}`);
  }
}

export async function approveReviewItem(serviceToken: string, reviewItemId: string, apply = false): Promise<ReviewActionResponse> {
  const response = await fetch(`/review-items/${encodeURIComponent(reviewItemId)}/approve`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: "user_primary",
      reason: "Approved from PSKA Review Center",
      apply
    })
  });
  if (!response.ok) {
    throw new Error(`approve ${response.status}`);
  }
  return (await response.json()) as ReviewActionResponse;
}

export async function rejectReviewItem(serviceToken: string, reviewItemId: string): Promise<ReviewActionResponse> {
  const response = await fetch(`/review-items/${encodeURIComponent(reviewItemId)}/reject`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: "user_primary",
      reason: "Rejected from PSKA Review Center"
    })
  });
  if (!response.ok) {
    throw new Error(`reject ${response.status}`);
  }
  return (await response.json()) as ReviewActionResponse;
}

export async function applyReviewItem(serviceToken: string, reviewItemId: string): Promise<ReviewActionResponse> {
  const response = await fetch(`/review-items/${encodeURIComponent(reviewItemId)}/apply`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: "user_primary",
      reason: "Applied from PSKA Review Center"
    })
  });
  if (!response.ok) {
    throw new Error(`apply ${response.status}`);
  }
  return (await response.json()) as ReviewActionResponse;
}

function mapSearchToBrain(data: WorkspaceSearchResponse, trigger: BrainState["lastTrigger"]): Partial<BrainState> {
  const evidence = data.workspace?.evidence;
  const results = data.retrieval?.results || [];
  const citations = evidence?.citations || [];
  const memory = evidence?.memory_context || [];
  const graphPaths = evidence?.graph_paths || [];

  const items = dedupeSearchItems([...results, ...citations, ...memory]);

  return {
    status: "synced",
    lastTrigger: trigger,
    updatedAt: Date.now(),
    error: null,
    relatedKnowledge: items
      .slice(0, 6)
      .map((item, index) => ({
        id: `result-${index}`,
        title: item.title || item.text?.slice(0, 52) || "相关记忆",
        score: knowledgeScore(item.score || item.confidence),
        snippet: item.snippet || item.text || "PSKA 中有可用证据。",
        source: "PSKA Retrieval API"
      })),
    entities: unique(graphPaths.flatMap((path) => path.entities || [])).slice(0, 8),
    connections: graphPaths.slice(0, 5).map((path, index) => ({
      id: `graph-${index}`,
      label: (path.entities || [])[0] || "图谱路径",
      relation: path.explanation || "通过证据连接"
    }))
  };
}

type SearchBrainItem = {
  title?: string;
  snippet?: string;
  text?: string;
  source_item_id?: string;
  score?: number;
  confidence?: number;
};

function dedupeSearchItems(values: unknown[]): SearchBrainItem[] {
  const merged = new Map<string, SearchBrainItem>();
  const order: string[] = [];
  values
    .map((value) => {
      const item = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
      return {
        title: typeof item.title === "string" ? item.title : undefined,
        snippet: typeof item.snippet === "string" ? item.snippet : undefined,
        text: typeof item.text === "string" ? item.text : undefined,
        source_item_id: typeof item.source_item_id === "string" ? item.source_item_id : undefined,
        score: typeof item.score === "number" ? item.score : undefined,
        confidence: typeof item.confidence === "number" ? item.confidence : undefined
      };
    })
    .filter((item) => item.title || item.snippet || item.text || item.source_item_id)
    .forEach((item) => {
      const key = searchItemKey(item);
      if (!key) {
        return;
      }
      const current = merged.get(key);
      if (!current) {
        merged.set(key, item);
        order.push(key);
        return;
      }
      current.title ||= item.title;
      current.source_item_id ||= item.source_item_id;
      if (item.snippet && (!current.snippet || item.snippet.length > current.snippet.length)) {
        current.snippet = item.snippet;
      }
      if (item.text && (!current.text || item.text.length > current.text.length)) {
        current.text = item.text;
      }
      if (typeof item.score === "number" && (typeof current.score !== "number" || item.score > current.score)) {
        current.score = item.score;
      }
      if (typeof item.confidence === "number" && (typeof current.confidence !== "number" || item.confidence > current.confidence)) {
        current.confidence = item.confidence;
      }
    });
  return order.map((key) => merged.get(key)).filter((item): item is SearchBrainItem => Boolean(item));
}

function searchItemKey(item: SearchBrainItem) {
  const sourceId = normalizeSearchIdentity(item.source_item_id);
  if (sourceId) {
    return `source:${sourceId}`;
  }
  const title = normalizeSearchIdentity(item.title);
  if (title) {
    return `title:${title}`;
  }
  const text = normalizeSearchIdentity(item.text || item.snippet);
  return text ? `text:${text.slice(0, 160)}` : "";
}

function normalizeSearchIdentity(value?: string) {
  return (value || "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

function unique(values: string[]) {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function knowledgeScore(value: unknown) {
  return typeof value === "number" ? Math.round(value * 100) : undefined;
}

function formatAge(value?: string) {
  if (!value) {
    return "最近";
  }
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) {
    return "最近";
  }
  const days = Math.max(1, Math.round((Date.now() - timestamp) / 86_400_000));
  if (days < 31) {
    return `${days} 天前`;
  }
  const months = Math.max(1, Math.round(days / 30));
  return `${months} 个月前`;
}
