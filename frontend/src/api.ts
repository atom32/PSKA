import type { BrainState, TodayResponse, WorkspaceCorpusResponse, WorkspaceSearchResponse } from "./types";

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
    return { status: "idle", lastTrigger: trigger, updatedAt: Date.now() };
  }

  try {
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
      throw new Error(`search ${searchResponse.status}`);
    }

    const searchData = (await searchResponse.json()) as WorkspaceSearchResponse;
    return mapSearchToBrain(searchData, trigger);
  } catch {
    return localAnalyze(query, trigger);
  }
}

export async function loadCorpusContext(serviceToken: string): Promise<Partial<BrainState>> {
  try {
    const response = await fetch("/workspace/corpus/data?limit=12", { headers: headers(serviceToken) });
    if (!response.ok) {
      throw new Error(`corpus ${response.status}`);
    }
    const data = (await response.json()) as WorkspaceCorpusResponse;
    return {
      entities: unique([
        ...(data.entities || []).map((entity) => entity.label || entity.canonical_name || entity.name || entity.entity_id || ""),
        "记忆层"
      ]).slice(0, 8),
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

export async function loadToday(serviceToken: string): Promise<TodayResponse> {
  const response = await fetch("/workspace/today/data?owner_user_id=user_primary&limit=10", { headers: headers(serviceToken) });
  if (!response.ok) {
    throw new Error(`today ${response.status}`);
  }
  return (await response.json()) as TodayResponse;
}

export async function approveReviewItem(serviceToken: string, reviewItemId: string, apply = false): Promise<void> {
  const response = await fetch(`/review-items/${encodeURIComponent(reviewItemId)}/approve`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: "user_primary",
      reason: "Approved from PSKA Today",
      apply
    })
  });
  if (!response.ok) {
    throw new Error(`approve ${response.status}`);
  }
}

export async function rejectReviewItem(serviceToken: string, reviewItemId: string): Promise<void> {
  const response = await fetch(`/review-items/${encodeURIComponent(reviewItemId)}/reject`, {
    method: "POST",
    headers: headers(serviceToken),
    body: JSON.stringify({
      actor_user_id: "user_primary",
      reason: "Rejected from PSKA Today"
    })
  });
  if (!response.ok) {
    throw new Error(`reject ${response.status}`);
  }
}

function mapSearchToBrain(data: WorkspaceSearchResponse, trigger: BrainState["lastTrigger"]): Partial<BrainState> {
  const evidence = data.workspace?.evidence;
  const results = data.retrieval?.results || [];
  const citations = evidence?.citations || [];
  const memory = evidence?.memory_context || [];
  const graphPaths = evidence?.graph_paths || [];

  const items = [...results, ...citations, ...memory]
    .map((item) => ({
      title: "title" in item ? item.title : undefined,
      snippet: "snippet" in item ? item.snippet : undefined,
      text: "text" in item ? item.text : undefined,
      score: "score" in item ? item.score : undefined,
      confidence: "confidence" in item ? item.confidence : undefined
    }))
    .filter((item) => item.title || item.snippet || item.text);

  return {
    status: "synced",
    lastTrigger: trigger,
    updatedAt: Date.now(),
    relatedKnowledge: items
      .slice(0, 6)
      .map((item, index) => ({
        id: `result-${index}`,
        title: item.title || item.text?.slice(0, 52) || "相关记忆",
        score: Math.round(((item.score || item.confidence || 0.78) as number) * 100),
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

function localAnalyze(query: string, trigger: BrainState["lastTrigger"]): Partial<BrainState> {
  const terms = unique(
    query
      .replace(/[#*_`>-]/g, " ")
      .split(/\s+/)
      .filter((word) => word.length > 4)
      .filter((word) => /^[A-Z0-9]/.test(word) || word.includes("API") || word.includes("RAG"))
  ).slice(0, 8);

  const entities = terms.length ? terms : ["Agent 运行时", "记忆层", "检索 API", "GraphRAG"];
  return {
    status: "offline",
    lastTrigger: trigger,
    updatedAt: Date.now(),
    entities,
    relatedKnowledge: entities.slice(0, 4).map((entity, index) => ({
      id: `local-${entity}-${index}`,
      title: `${entity} 笔记`,
      score: 91 - index * 4,
      snippet: "本地上下文分析已启用。连接 PSKA 服务后，这里会替换为真实检索证据。",
      source: "本地分析器"
    })),
    connections: entities.slice(0, 4).map((entity, index) => ({
      id: `local-conn-${index}`,
      label: entity,
      relation: index === 0 ? "当前主题" : "建议连接"
    }))
  };
}

function unique(values: string[]) {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
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
