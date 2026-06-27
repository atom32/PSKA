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
  WorkspaceAskResponse,
  WorkspaceCorpusResponse,
  WorkspaceGraphPathResponse,
  WorkspaceGraphResponse,
  WorkspaceMode,
  WorkspaceSearchResponse
} from "./types";

export type PSKAIdentity = {
  serviceToken?: string;
  tenantId?: string;
  userId?: string;
  representedUserId?: string;
};

export type PSKAAuth = string | PSKAIdentity;

export type PSKAGatewaySession = {
  authenticated: boolean;
  tenant_id?: string;
  user_id?: string;
  represented_user_id?: string;
  subject?: string;
  display_name?: string;
  email?: string;
  roles?: string[];
  groups?: string[];
  auth_provider?: string;
  expires_at?: string;
};

const DEFAULT_TENANT_ID = "tenant_default";
const DEFAULT_USER_ID = "user_primary";

const resolveIdentity = (auth: PSKAAuth): Required<PSKAIdentity> => {
  if (typeof auth === "string") {
    return {
      serviceToken: auth,
      tenantId: DEFAULT_TENANT_ID,
      userId: DEFAULT_USER_ID,
      representedUserId: DEFAULT_USER_ID
    };
  }
  const userId = clean(auth.userId) || DEFAULT_USER_ID;
  return {
    serviceToken: clean(auth.serviceToken),
    tenantId: clean(auth.tenantId) || DEFAULT_TENANT_ID,
    userId,
    representedUserId: clean(auth.representedUserId) || userId
  };
};

const headers = (auth: PSKAAuth) => {
  const identity = resolveIdentity(auth);
  const result: Record<string, string> = { "Content-Type": "application/json" };
  if (identity.serviceToken) {
    result.Authorization = `Bearer ${identity.serviceToken}`;
  }
  result["X-PSKA-Tenant-Id"] = identity.tenantId;
  result["X-PSKA-User-Id"] = identity.userId;
  result["X-PSKA-Represented-User-Id"] = identity.representedUserId;
  result["X-PSKA-Subject"] = identity.userId.includes(":") ? identity.userId : `pska:${identity.userId}`;
  return result;
};

const requestUserPayload = (auth: PSKAAuth) => {
  const identity = resolveIdentity(auth);
  return {
    tenant_id: identity.tenantId,
    user_id: identity.userId,
    represented_user_id: identity.representedUserId
  };
};

const ownerUserId = (auth: PSKAAuth) => resolveIdentity(auth).representedUserId;
const actorUserId = (auth: PSKAAuth) => resolveIdentity(auth).userId;
const clean = (value?: string) => (value || "").trim();

export async function loadGatewaySession(): Promise<PSKAGatewaySession | null> {
  try {
    const response = await fetch("/auth/session", { headers: { Accept: "application/json" } });
    if (!response.ok) {
      return null;
    }
    const data = (await response.json()) as PSKAGatewaySession;
    return data.authenticated ? data : null;
  } catch {
    return null;
  }
}

export async function analyzeWorkspaceContext(
  query: string,
  serviceToken: PSKAAuth,
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
      ...requestUserPayload(serviceToken),
      top_k: 5
    })
  });

  if (!searchResponse.ok) {
    throw new Error(`PSKA search request failed with HTTP ${searchResponse.status}`);
  }

  const searchData = (await searchResponse.json()) as WorkspaceSearchResponse;
  return mapSearchToBrain(searchData, trigger);
}

export async function loadCorpusContext(serviceToken: PSKAAuth): Promise<Partial<BrainState>> {
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

export async function loadCorpusData(serviceToken: PSKAAuth, limit = 16): Promise<WorkspaceCorpusResponse> {
  const params = new URLSearchParams({ owner_user_id: ownerUserId(serviceToken), limit: String(limit) });
  const response = await fetch(`/workspace/corpus/data?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`corpus ${response.status}`);
  }
  return (await response.json()) as WorkspaceCorpusResponse;
}

export async function loadGraphData(serviceToken: PSKAAuth, limit = 60, nodeTypes: string[] = []): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
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
  serviceToken: PSKAAuth,
  nodeId: string,
  limit = 80,
  hops = 1,
  nodeTypes: string[] = []
): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
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
  serviceToken: PSKAAuth,
  query: string,
  limit = 80,
  hops = 1,
  topK = 5,
  nodeTypes: string[] = []
): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
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
  serviceToken: PSKAAuth,
  query: string,
  mode: "deterministic" | "agentic" = "deterministic"
): Promise<WorkspaceGraphPathResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
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

export async function loadSourcesConsole(serviceToken: PSKAAuth, limit = 20): Promise<ConsoleSourcesResponse> {
  const params = new URLSearchParams({ owner_user_id: ownerUserId(serviceToken), limit: String(limit) });
  const response = await fetch(`/console/sources/data?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`sources ${response.status}`);
  }
  return (await response.json()) as ConsoleSourcesResponse;
}

export async function runFileSync(serviceToken: PSKAAuth): Promise<FileSyncResponse> {
  const response = await fetch("/files/sync", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      owner_user_id: ownerUserId(serviceToken),
      skip_twitter_archives: false
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "同步资料失败"));
  }
  return (await response.json()) as FileSyncResponse;
}

export async function runDigestNow(serviceToken: PSKAAuth): Promise<DigestNowResponse> {
  const response = await fetch("/digest/now", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      owner_user_id: ownerUserId(serviceToken),
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

export async function loadDigestLogs(serviceToken: PSKAAuth, limit = 8): Promise<DigestLogsResponse> {
  const params = new URLSearchParams({ owner_user_id: ownerUserId(serviceToken), limit: String(limit) });
  const response = await fetch(`/digest/logs?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`digest logs ${response.status}`);
  }
  return (await response.json()) as DigestLogsResponse;
}

export async function cleanupKnowledgeSource(
  serviceToken: PSKAAuth,
  knowledgeSourceId: string,
  execute = false
): Promise<KnowledgeSourceCleanupResponse> {
  const response = await fetch(`/knowledge-sources/${encodeURIComponent(knowledgeSourceId)}/cleanup`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      owner_user_id: ownerUserId(serviceToken),
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
  serviceToken: PSKAAuth,
  mode: "agentic" | "direct" = "direct"
): Promise<WorkspaceSearchResponse> {
  const response = await fetch("/workspace/search/query", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      query,
      mode,
      capture: false,
      ...requestUserPayload(serviceToken),
      top_k: 8
    })
  });
  if (!response.ok) {
    throw new Error(`search ${response.status}`);
  }
  return (await response.json()) as WorkspaceSearchResponse;
}

export async function askWorkspace(
  query: string,
  serviceToken: PSKAAuth,
  intent: "auto" | "quick" | "deep" = "auto",
  surface = "ask"
): Promise<WorkspaceAskResponse> {
  const response = await fetch("/workspace/ask", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      query,
      intent,
      surface,
      ...requestUserPayload(serviceToken),
      top_k: 8
    })
  });
  if (!response.ok) {
    const error = await responseError(response, "Ask PSKA 失败");
    if (response.status === 404 && error.includes("/workspace/ask")) {
      const legacy = await searchWorkspace(query, serviceToken, "direct");
      return legacySearchToAskResponse(legacy, query, intent, surface);
    }
    throw new Error(error);
  }
  return (await response.json()) as WorkspaceAskResponse;
}

export type WorkspaceAskStreamUpdate = {
  event: string;
  data: Record<string, unknown>;
  result: WorkspaceAskResponse;
};

export async function askWorkspaceStream(
  query: string,
  serviceToken: PSKAAuth,
  intent: "auto" | "quick" | "deep" = "auto",
  surface = "ask",
  onUpdate?: (update: WorkspaceAskStreamUpdate) => void
): Promise<WorkspaceAskResponse> {
  const response = await fetch("/workspace/ask/stream", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      query,
      intent,
      surface,
      ...requestUserPayload(serviceToken),
      top_k: 8
    })
  });
  if (!response.ok) {
    const error = await responseError(response, "Ask PSKA 失败");
    if (response.status === 404 && error.includes("/workspace/ask")) {
      const fallback = await askWorkspace(query, serviceToken, intent, surface);
      onUpdate?.({ event: "done", data: {}, result: fallback });
      return fallback;
    }
    throw new Error(error);
  }
  if (!response.body) {
    const fallback = await askWorkspace(query, serviceToken, intent, surface);
    onUpdate?.({ event: "done", data: {}, result: fallback });
    return fallback;
  }

  const result: WorkspaceAskResponse = {
    ok: true,
    query,
    answer: "",
    citations: [],
    source_refs: [],
    agent_steps: [],
    timing: {},
    evidence: {}
  };
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (value) {
      buffer += decoder.decode(value, { stream: !done });
      buffer = consumeAskSseBuffer(buffer, result, onUpdate);
    }
    if (done) {
      buffer += decoder.decode();
      consumeAskSseBuffer(`${buffer}\n\n`, result, onUpdate);
      break;
    }
  }
  return result;
}

function consumeAskSseBuffer(
  buffer: string,
  result: WorkspaceAskResponse,
  onUpdate?: (update: WorkspaceAskStreamUpdate) => void
) {
  let remaining = buffer;
  let boundary = remaining.indexOf("\n\n");
  while (boundary !== -1) {
    const frame = remaining.slice(0, boundary);
    remaining = remaining.slice(boundary + 2);
    const parsed = parseAskSseFrame(frame);
    if (parsed) {
      applyAskSseEvent(result, parsed.event, parsed.data);
      onUpdate?.({ event: parsed.event, data: parsed.data, result: { ...result, agent_steps: [...(result.agent_steps || [])] } });
    }
    boundary = remaining.indexOf("\n\n");
  }
  return remaining;
}

function parseAskSseFrame(frame: string): { event: string; data: Record<string, unknown> } | null {
  const lines = frame.split(/\r?\n/);
  let event = "message";
  const dataLines: string[] = [];
  lines.forEach((line) => {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
  });
  if (!dataLines.length) {
    return null;
  }
  try {
    const data = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
    return { event, data };
  } catch {
    return { event: "error", data: { error: dataLines.join("\n") } };
  }
}

function applyAskSseEvent(result: WorkspaceAskResponse, event: string, data: Record<string, unknown>) {
  if (event === "route") {
    result.route = isRecord(data.route) ? data.route as WorkspaceAskResponse["route"] : result.route;
    result.timing = { ...(result.timing || {}), ...(isRecord(data.timing) ? data.timing as WorkspaceAskResponse["timing"] : {}) };
    return;
  }
  if (event === "agent_step") {
    const step = isRecord(data.step) ? data.step : null;
    if (step) {
      result.agent_steps = [...(result.agent_steps || []), step as NonNullable<WorkspaceAskResponse["agent_steps"]>[number]];
    }
    result.timing = { ...(result.timing || {}), ...(isRecord(data.timing) ? data.timing as WorkspaceAskResponse["timing"] : {}) };
    return;
  }
  if (event === "evidence") {
    result.evidence = isRecord(data.evidence) ? data.evidence as WorkspaceAskResponse["evidence"] : result.evidence;
    result.citations = Array.isArray(data.citations) ? data.citations as Array<Record<string, unknown>> : result.citations;
    result.source_refs = result.evidence?.source_refs || result.citations;
    result.quality_signals = isRecord(data.quality_signals) ? data.quality_signals : result.quality_signals;
    return;
  }
  if (event === "answer_delta") {
    result.answer = `${result.answer || ""}${typeof data.delta === "string" ? data.delta : ""}`;
    if (typeof data.time_to_first_answer_ms === "number") {
      result.timing = { ...(result.timing || {}), time_to_first_answer_ms: data.time_to_first_answer_ms };
    }
    return;
  }
  if (event === "trace") {
    result.trace = isRecord(data.trace) ? data.trace : result.trace;
    result.agentic_service = isRecord(data.agentic_service) ? data.agentic_service : result.agentic_service;
    return;
  }
  if (event === "done") {
    result.ok = data.ok !== false;
    result.timing = { ...(result.timing || {}), ...(isRecord(data.timing) ? data.timing as WorkspaceAskResponse["timing"] : {}) };
    result.quality_signals = isRecord(data.quality_signals) ? data.quality_signals : result.quality_signals;
    return;
  }
  if (event === "error") {
    result.ok = false;
    result.error = typeof data.error === "string" ? data.error : "Ask PSKA stream failed";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function legacySearchToAskResponse(
  legacy: WorkspaceSearchResponse,
  query: string,
  intent: "auto" | "quick" | "deep",
  surface: string
): WorkspaceAskResponse {
  const workspaceEvidence = legacy.workspace?.evidence || {};
  const retrieval = legacy.retrieval || {};
  const fallbackRetrieval = legacy.fallback?.retrieval || {};
  const citations = legacy.citations || legacy.source_refs || workspaceEvidence.citations || fallbackRetrieval.citations || [];
  const results = retrieval.results || fallbackRetrieval.results || [];
  return {
    ok: legacy.ok,
    query,
    answer: legacy.answer,
    error: legacy.error,
    route: {
      intent,
      selected_intent: "quick",
      retrieval_owner: "pska",
      surface,
      requires_agentic_service_online: false,
      fallback_from: "workspace_ask_unavailable",
      tool_policy: { mode: "none" }
    },
    evidence: {
      citations,
      source_refs: citations,
      results,
      graph_paths: workspaceEvidence.graph_paths || fallbackRetrieval.graph_paths || [],
      memory_context: workspaceEvidence.memory_context || [],
      profile_context: [],
      gaps: [],
      conflicts: []
    },
    citations,
    source_refs: citations,
    trace: {
      mode: "quick",
      compatibility_fallback: "workspace_search_query",
      reason: "workspace_ask_endpoint_not_found",
      legacy_trace: legacy.trace || {}
    },
    timing: {},
    agentic_service: legacy.agentic_service
  };
}

async function responseError(response: Response, fallback: string) {
  try {
    const payload = (await response.json()) as { error?: string; message?: string };
    return payload.error || payload.message || `${fallback} (${response.status})`;
  } catch {
    return `${fallback} (${response.status})`;
  }
}

export async function loadToday(serviceToken: PSKAAuth): Promise<TodayResponse> {
  const params = new URLSearchParams({ owner_user_id: ownerUserId(serviceToken), limit: "10" });
  const response = await fetch(`/workspace/today/data?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`today ${response.status}`);
  }
  return (await response.json()) as TodayResponse;
}

export async function loadReviewCenter(serviceToken: PSKAAuth, status = "pending"): Promise<ReviewCenterResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
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
  serviceToken: PSKAAuth,
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
      owner_user_id: ownerUserId(serviceToken),
      actor_user_id: actorUserId(serviceToken),
      ...payload
    })
  });
  if (!response.ok) {
    throw new Error(`activity ${response.status}`);
  }
  return (await response.json()) as WorkspaceActivityResponse;
}

export async function acceptDiscovery(serviceToken: PSKAAuth, discoveryId: string): Promise<void> {
  const response = await fetch(`/workspace/discoveries/${encodeURIComponent(discoveryId)}/accept`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: actorUserId(serviceToken),
      reason: "Accepted from PSKA Today"
    })
  });
  if (!response.ok) {
    throw new Error(`accept discovery ${response.status}`);
  }
}

export async function ignoreDiscovery(serviceToken: PSKAAuth, discoveryId: string): Promise<void> {
  const response = await fetch(`/workspace/discoveries/${encodeURIComponent(discoveryId)}/ignore`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: actorUserId(serviceToken),
      reason: "Ignored from PSKA Today"
    })
  });
  if (!response.ok) {
    throw new Error(`ignore discovery ${response.status}`);
  }
}

export async function snoozeDiscovery(serviceToken: PSKAAuth, discoveryId: string): Promise<void> {
  const response = await fetch(`/workspace/discoveries/${encodeURIComponent(discoveryId)}/snooze`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: actorUserId(serviceToken),
      reason: "Snoozed from PSKA Today"
    })
  });
  if (!response.ok) {
    throw new Error(`snooze discovery ${response.status}`);
  }
}

export async function approveReviewItem(serviceToken: PSKAAuth, reviewItemId: string, apply = false): Promise<ReviewActionResponse> {
  const response = await fetch(`/review-items/${encodeURIComponent(reviewItemId)}/approve`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: actorUserId(serviceToken),
      reason: "Approved from PSKA Review Center",
      apply
    })
  });
  if (!response.ok) {
    throw new Error(`approve ${response.status}`);
  }
  return (await response.json()) as ReviewActionResponse;
}

export async function rejectReviewItem(serviceToken: PSKAAuth, reviewItemId: string): Promise<ReviewActionResponse> {
  const response = await fetch(`/review-items/${encodeURIComponent(reviewItemId)}/reject`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: actorUserId(serviceToken),
      reason: "Rejected from PSKA Review Center"
    })
  });
  if (!response.ok) {
    throw new Error(`reject ${response.status}`);
  }
  return (await response.json()) as ReviewActionResponse;
}

export async function applyReviewItem(serviceToken: PSKAAuth, reviewItemId: string): Promise<ReviewActionResponse> {
  const response = await fetch(`/review-items/${encodeURIComponent(reviewItemId)}/apply`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: actorUserId(serviceToken),
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
