import type {
  BrainState,
  ChunkingPreviewResponse,
  ConsoleSourcesResponse,
  DigestLogsResponse,
  DigestNowResponse,
  EvidenceBriefResponse,
  FileSyncResponse,
  KnowledgeBaseSearchResponse,
  KnowledgeBaseListResponse,
  KnowledgeBaseResponse,
  KnowledgeSourceCleanupResponse,
  KnowledgeSourceCreateResponse,
  AskConversationResponse,
  PromptProfilesResponse,
  ReviewCenterResponse,
  ReviewActionResponse,
  SourcePreviewResponse,
  WorkspaceDocumentDeleteResponse,
  WorkspaceDocumentLinkResponse,
  WorkspaceDocumentMoveResponse,
  WorkspaceDocumentsResponse,
  WorkspaceReaderSourceResponse,
  WorkspaceSourceIngestResponse,
  SourceSyncResponse,
  TodayResponse,
  WorkspaceActivityResponse,
  WorkspaceActivityType,
  WorkspaceAskResponse,
  WorkspaceCorpusResponse,
  WorkspaceGraphPathResponse,
  WorkspaceGraphResponse,
  WorkspaceMode,
  WorkspaceSearchResponse,
  WritingBoardResponse,
  WritingBoardsResponse,
  WritingComposeResponse,
  WritingEdge,
  WritingNode,
  WritingQuestionSuggestion
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

export type WorkspaceUploadProgress = {
  phase: "uploading" | "processing";
  loaded: number;
  total?: number;
  percent?: number;
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

const formHeaders = (auth: PSKAAuth) => {
  const result = headers(auth);
  delete result["Content-Type"];
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

const appendKnowledgeBaseParams = (params: URLSearchParams, options: { knowledgeBaseId?: string; knowledgeBaseIds?: string[] }) => {
  const ids = unique([...(options.knowledgeBaseId ? [options.knowledgeBaseId] : []), ...(options.knowledgeBaseIds || [])].map(clean).filter(Boolean));
  ids.forEach((id) => params.append("knowledge_base_ids", id));
};

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

type KnowledgeBaseScopedOptions = {
  knowledgeBaseId?: string;
  knowledgeBaseIds?: string[];
};

export async function listKnowledgeBases(
  serviceToken: PSKAAuth,
  options: { includeArchived?: boolean } = {}
): Promise<KnowledgeBaseListResponse> {
  const params = new URLSearchParams({ owner_user_id: ownerUserId(serviceToken) });
  if (options.includeArchived) {
    params.set("include_archived", "true");
  }
  const response = await fetch(`/workspace/knowledge-bases?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "知识库加载失败"));
  }
  return (await response.json()) as KnowledgeBaseListResponse;
}

export async function loadKnowledgeBase(serviceToken: PSKAAuth, knowledgeBaseId: string): Promise<KnowledgeBaseResponse> {
  const params = new URLSearchParams({ owner_user_id: ownerUserId(serviceToken) });
  const response = await fetch(`/workspace/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "知识库详情加载失败"));
  }
  return (await response.json()) as KnowledgeBaseResponse;
}

export async function createKnowledgeBase(
  serviceToken: PSKAAuth,
  payload: { name: string; description?: string }
): Promise<KnowledgeBaseResponse> {
  const response = await fetch("/workspace/knowledge-bases", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      ...payload,
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "创建知识库失败"));
  }
  return (await response.json()) as KnowledgeBaseResponse;
}

export async function patchKnowledgeBase(
  serviceToken: PSKAAuth,
  knowledgeBaseId: string,
  payload: Partial<{ name: string; description: string; pinned: boolean }>
): Promise<KnowledgeBaseResponse> {
  const response = await fetch(`/workspace/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`, {
    method: "PATCH",
    headers: headers(serviceToken),
    body: JSON.stringify({
      ...payload,
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "保存知识库失败"));
  }
  return (await response.json()) as KnowledgeBaseResponse;
}

export async function restoreKnowledgeBase(serviceToken: PSKAAuth, knowledgeBaseId: string): Promise<KnowledgeBaseResponse> {
  const response = await fetch(`/workspace/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/restore`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "恢复知识库失败"));
  }
  return (await response.json()) as KnowledgeBaseResponse;
}

export async function pinKnowledgeBase(serviceToken: PSKAAuth, knowledgeBaseId: string): Promise<KnowledgeBaseResponse> {
  const response = await fetch(`/workspace/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/pin`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "置顶知识库失败"));
  }
  return (await response.json()) as KnowledgeBaseResponse;
}

export async function unpinKnowledgeBase(serviceToken: PSKAAuth, knowledgeBaseId: string): Promise<KnowledgeBaseResponse> {
  const response = await fetch(`/workspace/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/pin`, {
    method: "DELETE",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "取消置顶知识库失败"));
  }
  return (await response.json()) as KnowledgeBaseResponse;
}

export async function deleteKnowledgeBase(serviceToken: PSKAAuth, knowledgeBaseId: string): Promise<KnowledgeBaseResponse> {
  const response = await fetch(`/workspace/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`, {
    method: "DELETE",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "归档知识库失败"));
  }
  return (await response.json()) as KnowledgeBaseResponse;
}

export async function loadCorpusData(serviceToken: PSKAAuth, limit = 16, options: KnowledgeBaseScopedOptions = {}): Promise<WorkspaceCorpusResponse> {
  const params = new URLSearchParams({ owner_user_id: ownerUserId(serviceToken), limit: String(limit) });
  appendKnowledgeBaseParams(params, options);
  const response = await fetch(`/workspace/corpus/data?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`corpus ${response.status}`);
  }
  return (await response.json()) as WorkspaceCorpusResponse;
}

export async function loadGraphData(serviceToken: PSKAAuth, limit = 60, nodeTypes: string[] = [], options: KnowledgeBaseScopedOptions = {}): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
    limit: String(limit)
  });
  appendKnowledgeBaseParams(params, options);
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
  nodeTypes: string[] = [],
  options: KnowledgeBaseScopedOptions = {}
): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
    node_id: nodeId,
    limit: String(limit),
    hops: String(hops)
  });
  appendKnowledgeBaseParams(params, options);
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
  nodeTypes: string[] = [],
  options: KnowledgeBaseScopedOptions = {}
): Promise<WorkspaceGraphResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
    query,
    limit: String(limit),
    hops: String(hops),
    top_k: String(topK)
  });
  appendKnowledgeBaseParams(params, options);
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

export async function runDigestNow(serviceToken: PSKAAuth, options: KnowledgeBaseScopedOptions = {}): Promise<DigestNowResponse> {
  const knowledgeBaseIds = unique([...(options.knowledgeBaseId ? [options.knowledgeBaseId] : []), ...(options.knowledgeBaseIds || [])].map(clean).filter(Boolean));
  const response = await fetch("/workspace/digest/run", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      limit: 20,
      batch_size: 8,
      force: false,
      run_worker: false,
      triggered_by: actorUserId(serviceToken),
      reason: "manual workspace digest run",
      ...(knowledgeBaseIds.length ? { knowledge_base_ids: knowledgeBaseIds } : {}),
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "整理资料失败"));
  }
  return (await response.json()) as DigestNowResponse;
}

export async function retryDigestJob(serviceToken: PSKAAuth, jobId: string): Promise<{ job?: unknown }> {
  const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/retry`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "重试 Digest 任务失败"));
  }
  return (await response.json()) as { job?: unknown };
}

export async function loadDigestLogs(serviceToken: PSKAAuth, limit = 8, options: KnowledgeBaseScopedOptions = {}): Promise<DigestLogsResponse> {
  const params = new URLSearchParams({ owner_user_id: ownerUserId(serviceToken), limit: String(limit) });
  appendKnowledgeBaseParams(params, options);
  const response = await fetch(`/digest/logs?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`digest logs ${response.status}`);
  }
  return (await response.json()) as DigestLogsResponse;
}

export async function previewChunking(
  serviceToken: PSKAAuth,
  payload: { text: string; chunking?: Record<string, unknown>; processing_config?: Record<string, unknown> }
): Promise<ChunkingPreviewResponse> {
  const response = await fetch("/workspace/chunking/preview", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      ...payload,
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "Chunk preview 失败"));
  }
  return (await response.json()) as ChunkingPreviewResponse;
}

export async function previewKnowledgeSource(
  serviceToken: PSKAAuth,
  payload: { source_type: string; url?: string; path?: string; name?: string; limit?: number }
): Promise<SourcePreviewResponse> {
  const response = await fetch("/workspace/sources/preview", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      ...payload,
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "预览输入源失败"));
  }
  return (await response.json()) as SourcePreviewResponse;
}

export async function createKnowledgeSource(
  serviceToken: PSKAAuth,
  payload: { source_type: string; url?: string; path?: string; name?: string; preview?: boolean; knowledge_base_id?: string }
): Promise<KnowledgeSourceCreateResponse> {
  const response = await fetch("/workspace/sources", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      ...payload,
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "添加输入源失败"));
  }
  return (await response.json()) as KnowledgeSourceCreateResponse;
}

export async function createTextSource(
  serviceToken: PSKAAuth,
  payload: { title?: string; text: string; digest_mode?: "after_upload" | "manual" | "disabled"; knowledge_base_id?: string }
): Promise<WorkspaceSourceIngestResponse> {
  const response = await fetch("/workspace/sources/text", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      ...payload,
      digest_mode: payload.digest_mode || "after_upload",
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "添加文本资料失败"));
  }
  return (await response.json()) as WorkspaceSourceIngestResponse;
}

export async function uploadWorkspaceSource(
  serviceToken: PSKAAuth,
  file: File,
  payload: { title?: string; digest_mode?: "after_upload" | "manual" | "disabled"; knowledge_base_id?: string } = {},
  onProgress?: (progress: WorkspaceUploadProgress) => void
): Promise<WorkspaceSourceIngestResponse> {
  const form = new FormData();
  form.set("file", file);
  form.set("filename", file.name);
  form.set("title", payload.title || file.name);
  form.set("digest_mode", payload.digest_mode || "after_upload");
  if (payload.knowledge_base_id) {
    form.set("knowledge_base_id", payload.knowledge_base_id);
  }
  const identity = resolveIdentity(serviceToken);
  form.set("tenant_id", identity.tenantId);
  form.set("user_id", identity.userId);
  form.set("represented_user_id", identity.representedUserId);
  if (!onProgress) {
    const response = await fetch("/workspace/sources/upload", {
      method: "POST",
      headers: formHeaders(serviceToken),
      body: form
    });
    if (!response.ok) {
      throw new Error(await responseError(response, "上传资料失败"));
    }
    return (await response.json()) as WorkspaceSourceIngestResponse;
  }
  return await new Promise<WorkspaceSourceIngestResponse>((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/workspace/sources/upload");
    Object.entries(formHeaders(serviceToken)).forEach(([key, value]) => {
      request.setRequestHeader(key, value);
    });
    request.upload.onprogress = (event) => {
      const total = event.lengthComputable ? event.total : file.size || undefined;
      const percent = total ? Math.max(1, Math.min(99, Math.round((event.loaded / total) * 100))) : undefined;
      onProgress({ phase: "uploading", loaded: event.loaded, total, percent });
    };
    request.onload = () => {
      const text = request.responseText || "";
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(xhrErrorMessage(text, request.status, "上传资料失败")));
        return;
      }
      onProgress({ phase: "processing", loaded: file.size, total: file.size, percent: 100 });
      try {
        resolve(JSON.parse(text) as WorkspaceSourceIngestResponse);
      } catch {
        reject(new Error("上传资料失败：服务端返回了不可解析的响应。"));
      }
    };
    request.onerror = () => reject(new Error("上传资料失败：网络连接中断。"));
    request.onabort = () => reject(new Error("上传资料已取消。"));
    request.send(form);
  });
}

export async function loadWorkspaceDocuments(serviceToken: PSKAAuth, includeDeleted = true, options: KnowledgeBaseScopedOptions = {}): Promise<WorkspaceDocumentsResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
    include_deleted: includeDeleted ? "true" : "false",
    limit: "120"
  });
  appendKnowledgeBaseParams(params, options);
  const response = await fetch(`/workspace/documents/data?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "资料列表加载失败"));
  }
  return (await response.json()) as WorkspaceDocumentsResponse;
}

export async function loadReaderSource(
  serviceToken: PSKAAuth,
  sourceItemId: string,
  options: KnowledgeBaseScopedOptions & { maxDocumentChars?: number } = {}
): Promise<WorkspaceReaderSourceResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
    source_item_id: sourceItemId,
    max_document_chars: String(options.maxDocumentChars || 60000)
  });
  appendKnowledgeBaseParams(params, options);
  const response = await fetch(`/workspace/reader/source?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "原文加载失败"));
  }
  return (await response.json()) as WorkspaceReaderSourceResponse;
}

export async function deleteWorkspaceDocuments(
  serviceToken: PSKAAuth,
  sourceItemIds: string[],
  options: { execute?: boolean; restore?: boolean; hardDelete?: boolean; reason?: string; knowledgeBaseId?: string; deleteMode?: "membership" | "source" | "hard" } = {}
): Promise<WorkspaceDocumentDeleteResponse> {
  const response = await fetch("/workspace/documents/delete", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      source_item_ids: sourceItemIds,
      execute: options.execute ?? false,
      restore: options.restore ?? false,
      hard_delete: options.hardDelete ?? false,
      ...(options.knowledgeBaseId ? { knowledge_base_id: options.knowledgeBaseId } : {}),
      ...(options.deleteMode ? { delete_mode: options.deleteMode } : {}),
      reason: options.reason,
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "资料删除失败"));
  }
  return (await response.json()) as WorkspaceDocumentDeleteResponse;
}

export async function linkWorkspaceDocuments(
  serviceToken: PSKAAuth,
  sourceItemIds: string[],
  options: { execute?: boolean; targetKnowledgeBaseId: string; membershipType?: string; metadata?: Record<string, unknown> }
): Promise<WorkspaceDocumentLinkResponse> {
  const response = await fetch("/workspace/documents/link", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      source_item_ids: sourceItemIds,
      target_knowledge_base_id: options.targetKnowledgeBaseId,
      execute: options.execute ?? true,
      ...(options.membershipType ? { membership_type: options.membershipType } : {}),
      ...(options.metadata ? { metadata: options.metadata } : {}),
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "资料加入知识库失败"));
  }
  return (await response.json()) as WorkspaceDocumentLinkResponse;
}

export async function moveWorkspaceDocuments(
  serviceToken: PSKAAuth,
  sourceItemIds: string[],
  options: { execute?: boolean; sourceKnowledgeBaseId: string; targetKnowledgeBaseId: string; membershipType?: string; metadata?: Record<string, unknown> }
): Promise<WorkspaceDocumentMoveResponse> {
  const response = await fetch("/workspace/documents/move", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      source_item_ids: sourceItemIds,
      source_knowledge_base_id: options.sourceKnowledgeBaseId,
      target_knowledge_base_id: options.targetKnowledgeBaseId,
      execute: options.execute ?? true,
      ...(options.membershipType ? { membership_type: options.membershipType } : {}),
      ...(options.metadata ? { metadata: options.metadata } : {}),
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "资料移动失败"));
  }
  return (await response.json()) as WorkspaceDocumentMoveResponse;
}

export async function syncKnowledgeSources(
  serviceToken: PSKAAuth,
  knowledgeSourceId?: string,
  options: { sourceTypes?: string[]; knowledgeBaseId?: string } = {}
): Promise<SourceSyncResponse> {
  const response = await fetch("/workspace/sources/sync", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      ...(knowledgeSourceId ? { knowledge_source_ids: [knowledgeSourceId] } : {}),
      ...(options.sourceTypes?.length ? { source_types: options.sourceTypes } : {}),
      ...(options.knowledgeBaseId ? { knowledge_base_id: options.knowledgeBaseId } : {}),
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "同步资料源失败"));
  }
  return (await response.json()) as SourceSyncResponse;
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
    throw new Error(await responseError(response, execute ? "清理高级资料源失败" : "预览清理失败"));
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

export async function searchKnowledgeBases(
  serviceToken: PSKAAuth,
  payload: { query: string; knowledgeBaseIds: string[]; topK?: number; mode?: "hybrid" | "lexical" | "direct" }
): Promise<KnowledgeBaseSearchResponse> {
  const response = await fetch("/workspace/knowledge-bases/search", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      query: payload.query,
      knowledge_base_ids: payload.knowledgeBaseIds,
      top_k: payload.topK ?? 8,
      mode: payload.mode || "hybrid",
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "知识库搜索失败"));
  }
  return (await response.json()) as KnowledgeBaseSearchResponse;
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

type WorkspaceAskIntent = "auto" | "quick" | "deep";

export async function askWorkspaceStream(
  query: string,
  serviceToken: PSKAAuth,
  intent: WorkspaceAskIntent = "auto",
  surface = "ask",
  onUpdate?: (update: WorkspaceAskStreamUpdate) => void,
  options: { scope?: Record<string, unknown>; topK?: number; sessionId?: string; skipIntentClassifier?: boolean } = {}
): Promise<WorkspaceAskResponse> {
  const response = await fetch("/workspace/ask/stream", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      query,
      intent,
      surface,
      ...(options.scope ? { scope: options.scope } : {}),
      ...(options.sessionId ? { session_id: options.sessionId } : {}),
      ...(options.skipIntentClassifier ? { skip_intent_classifier: true } : {}),
      ...requestUserPayload(serviceToken),
      top_k: options.topK || 8
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
    status: "running",
    answer: "",
    citations: [],
    source_refs: [],
    agent_steps: [],
    progress: [],
    timing: {},
    evidence: {}
  };
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
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
  } catch (error) {
    result.ok = false;
    result.status = "failed";
    result.error = streamErrorMessage(error);
    onUpdate?.({ event: "error", data: { error: result.error }, result });
  }
  return result;
}

export async function loadAskConversations(serviceToken: PSKAAuth): Promise<AskConversationResponse> {
  const response = await fetch("/workspace/ask/conversations", { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "加载对话失败"));
  }
  return (await response.json()) as AskConversationResponse;
}

export async function createAskConversation(serviceToken: PSKAAuth, title?: string, options: { scope?: Record<string, unknown>; metadata?: Record<string, unknown> } = {}): Promise<AskConversationResponse> {
  const response = await fetch("/workspace/ask/conversations", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      title: title || "Ask PSKA",
      ...(options.scope ? { scope: options.scope } : {}),
      ...(options.metadata ? { metadata: options.metadata } : {}),
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "创建对话失败"));
  }
  return (await response.json()) as AskConversationResponse;
}

export async function loadAskConversation(serviceToken: PSKAAuth, conversationId: string): Promise<AskConversationResponse> {
  const response = await fetch(`/workspace/ask/conversations/${encodeURIComponent(conversationId)}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "加载对话记录失败"));
  }
  return (await response.json()) as AskConversationResponse;
}

export async function deleteAskConversation(serviceToken: PSKAAuth, conversationId: string): Promise<AskConversationResponse> {
  const response = await fetch(`/workspace/ask/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "删除对话失败"));
  }
  return (await response.json()) as AskConversationResponse;
}

export async function askConversationStream(
  conversationId: string,
  query: string,
  serviceToken: PSKAAuth,
  onUpdate?: (update: WorkspaceAskStreamUpdate) => void,
  options: { surface?: string; intent?: WorkspaceAskIntent; skipIntentClassifier?: boolean; topK?: number; temperature?: number; maxTokens?: number; sourceItemIds?: string[]; scope?: Record<string, unknown> } = {}
): Promise<WorkspaceAskResponse> {
  const scope = {
    ...(options.scope || {}),
    ...(options.sourceItemIds?.length ? { source_item_ids: options.sourceItemIds } : {})
  };
  const response = await fetch(`/workspace/ask/conversations/${encodeURIComponent(conversationId)}/messages/stream`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      query,
      intent: options.intent || "auto",
      surface: options.surface || "ask",
      ...(Object.keys(scope).length ? { scope } : {}),
      ...(options.skipIntentClassifier ? { skip_intent_classifier: true } : {}),
      ...requestUserPayload(serviceToken),
      top_k: options.topK || 8,
      temperature: options.temperature,
      max_tokens: options.maxTokens
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "Ask PSKA 对话失败"));
  }
  if (!response.body) {
    return askWorkspaceStream(query, serviceToken, options.intent || "auto", options.surface || "ask", onUpdate, {
      sessionId: conversationId,
      scope: Object.keys(scope).length ? scope : undefined,
      skipIntentClassifier: options.skipIntentClassifier
    });
  }
  const result: WorkspaceAskResponse = {
    ok: true,
    query,
    status: "running",
    answer: "",
    citations: [],
    source_refs: [],
    agent_steps: [],
    progress: [],
    timing: {},
    evidence: {}
  };
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
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
  } catch (error) {
    result.ok = false;
    result.status = "failed";
    result.error = streamErrorMessage(error);
    onUpdate?.({ event: "error", data: { error: result.error }, result });
  }
  return result;
}

export async function loadPromptProfiles(serviceToken: PSKAAuth): Promise<PromptProfilesResponse> {
  const response = await fetch("/workspace/prompt-profiles", { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "Prompt Profiles 加载失败"));
  }
  return (await response.json()) as PromptProfilesResponse;
}

export async function updatePromptProfiles(
  serviceToken: PSKAAuth,
  profiles: Array<{ profile_type: string; scope?: string; name?: string; config: Record<string, unknown> }>
): Promise<PromptProfilesResponse> {
  const response = await fetch("/workspace/prompt-profiles", {
    method: "PUT",
    headers: headers(serviceToken),
    body: JSON.stringify({
      profiles,
      ...requestUserPayload(serviceToken)
    })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "Prompt Profiles 保存失败"));
  }
  return (await response.json()) as PromptProfilesResponse;
}

export async function listWritingBoards(serviceToken: PSKAAuth): Promise<WritingBoardsResponse> {
  const identity = resolveIdentity(serviceToken);
  const params = new URLSearchParams({ owner_user_id: identity.representedUserId, limit: "50" });
  const response = await fetch(`/workspace/writing/boards?${params.toString()}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "写作工作区加载失败"));
  }
  return (await response.json()) as WritingBoardsResponse;
}

export async function createWritingBoard(serviceToken: PSKAAuth, payload: { title: string; goal?: string; metadata?: Record<string, unknown> }): Promise<WritingBoardResponse> {
  const response = await fetch("/workspace/writing/boards", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({ ...payload, ...requestUserPayload(serviceToken) })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "创建写作工作区失败"));
  }
  return (await response.json()) as WritingBoardResponse;
}

export async function loadWritingBoard(serviceToken: PSKAAuth, boardId: string): Promise<WritingBoardResponse> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}`, { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(await responseError(response, "写作工作区加载失败"));
  }
  return (await response.json()) as WritingBoardResponse;
}

export async function patchWritingBoard(serviceToken: PSKAAuth, boardId: string, payload: Partial<{ title: string; goal: string; metadata: Record<string, unknown> }>): Promise<WritingBoardResponse> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}`, {
    method: "PATCH",
    headers: headers(serviceToken),
    body: JSON.stringify({ ...payload, ...requestUserPayload(serviceToken) })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "保存写作工作区失败"));
  }
  return (await response.json()) as WritingBoardResponse;
}

export async function deleteWritingBoard(serviceToken: PSKAAuth, boardId: string): Promise<void> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}`, {
    method: "DELETE",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "删除写作项目失败"));
  }
}

export async function createWritingNode(serviceToken: PSKAAuth, boardId: string, payload: Partial<WritingNode>): Promise<{ ok?: boolean; node?: WritingNode }> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}/nodes`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({ ...payload, ...requestUserPayload(serviceToken) })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "创建写作节点失败"));
  }
  return (await response.json()) as { ok?: boolean; node?: WritingNode };
}

export async function patchWritingNode(serviceToken: PSKAAuth, boardId: string, nodeId: string, payload: Partial<WritingNode>): Promise<{ ok?: boolean; node?: WritingNode }> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}/nodes/${encodeURIComponent(nodeId)}`, {
    method: "PATCH",
    headers: headers(serviceToken),
    body: JSON.stringify({ ...payload, ...requestUserPayload(serviceToken) })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "保存写作节点失败"));
  }
  return (await response.json()) as { ok?: boolean; node?: WritingNode };
}

export async function deleteWritingNode(serviceToken: PSKAAuth, boardId: string, nodeId: string): Promise<void> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}/nodes/${encodeURIComponent(nodeId)}`, {
    method: "DELETE",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "删除写作节点失败"));
  }
}

export async function createWritingEdge(serviceToken: PSKAAuth, boardId: string, payload: Partial<WritingEdge>): Promise<{ ok?: boolean; edge?: WritingEdge }> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}/edges`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({ ...payload, ...requestUserPayload(serviceToken) })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "创建写作关系失败"));
  }
  return (await response.json()) as { ok?: boolean; edge?: WritingEdge };
}

export async function deleteWritingEdge(serviceToken: PSKAAuth, boardId: string, edgeId: string): Promise<void> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}/edges/${encodeURIComponent(edgeId)}`, {
    method: "DELETE",
    headers: headers(serviceToken),
    body: JSON.stringify(requestUserPayload(serviceToken))
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "删除写作关系失败"));
  }
}

export async function suggestWritingQuestions(
  serviceToken: PSKAAuth,
  boardId: string,
  payload: { node_id?: string; direction?: "decompose" | "followup" | "evidence_gap" | "counterpoint" }
): Promise<{ ok?: boolean; suggestions?: WritingQuestionSuggestion[]; persisted?: boolean }> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}/suggest-questions`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({ ...payload, ...requestUserPayload(serviceToken) })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "生成追问建议失败"));
  }
  return (await response.json()) as { ok?: boolean; suggestions?: WritingQuestionSuggestion[]; persisted?: boolean };
}

export async function composeWritingDraft(
  serviceToken: PSKAAuth,
  boardId: string,
  payload: { section_node_id?: string; answer_node_ids: string[] }
): Promise<WritingComposeResponse> {
  const response = await fetch(`/workspace/writing/boards/${encodeURIComponent(boardId)}/compose`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({ ...payload, ...requestUserPayload(serviceToken) })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "生成章节草稿失败"));
  }
  return (await response.json()) as WritingComposeResponse;
}

export async function createEvidenceBrief(
  serviceToken: PSKAAuth,
  payload: {
    job_id?: string;
    digest_note_ids?: string[];
    knowledge_claim_ids?: string[];
    review_item_ids?: string[];
    ask_run_ids?: string[];
    title?: string;
    limit?: number;
  }
): Promise<EvidenceBriefResponse> {
  const response = await fetch("/workspace/evidence-briefs", {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({ ...payload, ...requestUserPayload(serviceToken) })
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "生成 Brief 失败"));
  }
  return (await response.json()) as EvidenceBriefResponse;
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
      onUpdate?.({
        event: parsed.event,
        data: parsed.data,
        result: {
          ...result,
          agent_steps: [...(result.agent_steps || [])],
          progress: [...(result.progress || [])]
        }
      });
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
  if (event === "conversation") {
    const conversation = isRecord(data.conversation) ? data.conversation : null;
    const run = isRecord(data.run) ? data.run : null;
    if (conversation && typeof conversation.conversation_id === "string") {
      result.conversation_id = conversation.conversation_id;
    }
    if (run && typeof run.run_id === "string") {
      result.run_id = run.run_id;
    }
    return;
  }
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
  if (event === "progress") {
    const progress = isRecord(data.progress) ? data.progress : null;
    if (progress) {
      result.progress = [...(result.progress || []), progress as NonNullable<WorkspaceAskResponse["progress"]>[number]];
    }
    result.timing = { ...(result.timing || {}), ...(isRecord(data.timing) ? data.timing as WorkspaceAskResponse["timing"] : {}) };
    return;
  }
  if (event === "evidence") {
    result.evidence = isRecord(data.evidence) ? data.evidence as WorkspaceAskResponse["evidence"] : result.evidence;
    result.citations = Array.isArray(data.citations) ? data.citations as Array<Record<string, unknown>> : result.citations;
    result.source_refs = result.evidence?.source_refs || result.citations;
    result.citation_audit = isRecord(data.citation_audit) ? data.citation_audit as WorkspaceAskResponse["citation_audit"] : result.citation_audit;
    result.evidence_check = isRecord(data.evidence_check) ? data.evidence_check : result.evidence_check;
    result.quality_signals = isRecord(data.quality_signals) ? data.quality_signals : result.quality_signals;
    return;
  }
  if (event === "answer_delta") {
    result.answer = `${result.answer || ""}${typeof data.delta === "string" ? data.delta : ""}`;
    if (typeof data.answer_type === "string") {
      result.answer_type = data.answer_type;
    }
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
    result.status = typeof data.status === "string" ? data.status : result.ok === false ? "failed" : "succeeded";
    if (typeof data.error === "string") {
      result.error = data.error;
    }
    if (typeof data.conversation_id === "string") {
      result.conversation_id = data.conversation_id;
    }
    if (typeof data.run_id === "string") {
      result.run_id = data.run_id;
    }
    if (typeof data.answer === "string" && data.answer.trim()) {
      result.answer = data.answer;
    }
    if (Array.isArray(data.citations)) {
      result.citations = data.citations as Array<Record<string, unknown>>;
    }
    if (Array.isArray(data.source_refs)) {
      result.source_refs = data.source_refs as Array<Record<string, unknown>>;
    }
    if (isRecord(data.evidence)) {
      result.evidence = data.evidence as WorkspaceAskResponse["evidence"];
    }
    if (Array.isArray(data.source_windows)) {
      result.source_windows = data.source_windows as Array<Record<string, unknown>>;
      result.evidence = { ...(result.evidence || {}), source_windows: result.source_windows };
    }
    if (Array.isArray(data.evidence_claims)) {
      result.evidence_claims = data.evidence_claims;
      result.evidence = { ...(result.evidence || {}), evidence_claims: result.evidence_claims };
    }
    if (isRecord(data.scope_applied)) {
      result.scope_applied = data.scope_applied;
      result.route = { ...(result.route || {}), scope_applied: result.scope_applied };
    }
    if (isRecord(data.citation_audit)) {
      result.citation_audit = data.citation_audit as WorkspaceAskResponse["citation_audit"];
    }
    result.intent = typeof data.intent === "string" ? data.intent : result.intent;
    result.rewrite_query = typeof data.rewrite_query === "string" ? data.rewrite_query : result.rewrite_query;
    result.answer_type = typeof data.answer_type === "string" ? data.answer_type : result.answer_type;
    result.evidence_check = isRecord(data.evidence_check) ? data.evidence_check : result.evidence_check;
    result.no_answer_reasons = Array.isArray(data.no_answer_reasons) ? data.no_answer_reasons : result.no_answer_reasons;
    result.agent_steps = Array.isArray(data.agent_steps) && data.agent_steps.length ? data.agent_steps as WorkspaceAskResponse["agent_steps"] : result.agent_steps;
    result.progress = Array.isArray(data.progress) && data.progress.length ? data.progress as WorkspaceAskResponse["progress"] : result.progress;
    result.trace = isRecord(data.trace) ? data.trace : result.trace;
    result.timing = { ...(result.timing || {}), ...(isRecord(data.timing) ? data.timing as WorkspaceAskResponse["timing"] : {}) };
    result.quality_signals = isRecord(data.quality_signals) ? data.quality_signals : result.quality_signals;
    return;
  }
  if (event === "error") {
    result.ok = false;
    result.status = "failed";
    result.error = typeof data.error === "string" ? data.error : "Ask PSKA stream failed";
  }
}

function streamErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Ask PSKA stream interrupted";
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

function xhrErrorMessage(text: string, status: number, fallback: string) {
  try {
    const payload = JSON.parse(text) as { error?: string; message?: string };
    return payload.error || payload.message || `${fallback} (${status})`;
  } catch {
    return text.trim() || `${fallback} (${status})`;
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

export async function loadReviewCenter(serviceToken: PSKAAuth, status = "pending", options: KnowledgeBaseScopedOptions = {}): Promise<ReviewCenterResponse> {
  const params = new URLSearchParams({
    owner_user_id: ownerUserId(serviceToken),
    status,
    limit: "50"
  });
  appendKnowledgeBaseParams(params, options);
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
