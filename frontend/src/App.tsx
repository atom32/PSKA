import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import CytoscapeComponent from "react-cytoscapejs";
import {
  BookOpen,
  Brain,
  CalendarDays,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  FileText,
  Folder,
  GitPullRequest,
  Hash,
  Image,
  Link2,
  Network,
  Pin,
  PlayCircle,
  RefreshCw,
  Search,
  Sparkles,
  Tag,
  Tags,
  TextCursorInput
} from "lucide-react";
import { Background, Controls, Handle, MiniMap, Position, ReactFlow, type NodeProps } from "reactflow";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Placeholder from "@tiptap/extension-placeholder";
import Table from "@tiptap/extension-table";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import TableRow from "@tiptap/extension-table-row";
import { useQuery } from "@tanstack/react-query";
import {
  analyzeWorkspaceContext,
  acceptDiscovery,
  applyReviewItem,
  approveReviewItem,
  cleanupKnowledgeSource,
  ignoreDiscovery,
  loadCorpusContext,
  loadCorpusData,
  loadDigestLogs,
  loadGatewaySession,
  loadGraphData,
  loadGraphPath,
  loadGraphSearchSubgraph,
  loadGraphSubgraph,
  loadReviewCenter,
  loadSourcesConsole,
  loadToday,
  recordWorkspaceActivity,
  rejectReviewItem,
  runDigestNow,
  runFileSync,
  searchWorkspace,
  snoozeDiscovery
} from "./api";
import type { PSKAAuth, PSKAIdentity } from "./api";
import { useWorkspaceStore } from "./store";
import type {
  BrainState,
  ConsoleSourceChannelStats,
  ConsoleSourcesResponse,
  DigestNowResponse,
  DigestLogsResponse,
  FileSyncResponse,
  KnowledgeSourceCleanupResponse,
  ReviewCenterItem,
  TodayContinueItem,
  TodayDiscoveryItem,
  TodayResponse,
  TodayReviewItem,
  WorkspaceCorpusResponse,
  WorkspaceGraphEdge,
  WorkspaceGraphNode,
  WorkspaceGraphPathResponse,
  WorkspaceGraphResponse,
  WorkspaceSearchResponse,
  WorkspaceMode
} from "./types";

const nodeTypes = {
  pskaCard: CanvasCardNode
};

export default function App() {
  const {
    mode,
    leftCollapsed,
    documentText,
    selectedText,
    serviceToken,
    tenantId,
    userId,
    representedUserId,
    brain,
    setMode,
    toggleLeft,
    setDocumentText,
    setSelectedText,
    setServiceToken,
    setTenantId,
    setUserId,
    setRepresentedUserId,
    setBrain
  } = useWorkspaceStore();
  const pskaIdentity = useMemo<PSKAIdentity>(
    () => ({
      serviceToken,
      tenantId,
      userId,
      representedUserId: representedUserId || userId
    }),
    [serviceToken, tenantId, userId, representedUserId]
  );
  const lastAnalyzedText = useRef(documentText);
  const lastEditedActivityAt = useRef(0);
  const [pinStatus, setPinStatus] = useState<"idle" | "saved" | "failed">("idle");

  useEffect(() => {
    let cancelled = false;
    void loadGatewaySession().then((session) => {
      if (!session || cancelled) {
        return;
      }
      if (session.tenant_id) {
        setTenantId(session.tenant_id);
      }
      if (session.user_id) {
        setUserId(session.user_id);
      }
      setRepresentedUserId(session.represented_user_id || session.user_id || "");
      setServiceToken("");
    });
    return () => {
      cancelled = true;
    };
  }, [setRepresentedUserId, setServiceToken, setTenantId, setUserId]);

  const corpusQuery = useQuery({
    queryKey: ["corpus-context", pskaIdentity],
    queryFn: () => loadCorpusContext(pskaIdentity),
    enabled: mode !== "today" && mode !== "review"
  });

  useEffect(() => {
    if (corpusQuery.data) {
      setBrain(corpusQuery.data);
    }
  }, [corpusQuery.data, setBrain]);

  const editor = useEditor({
    extensions: [
      StarterKit,
      Placeholder.configure({ placeholder: "开始写作。暂停后，PSKA 会在旁边安静观察。" }),
      Table.configure({ resizable: true }),
      TableRow,
      TableHeader,
      TableCell
    ],
    content: toDocumentHtml(documentText),
    editorProps: {
      attributes: {
        class: "editor-prose"
      },
      handleDOMEvents: {
        blur: () => {
          void runAnalysis("blur");
          return false;
        }
      }
    },
    onUpdate: ({ editor: updatedEditor }) => {
      const text = updatedEditor.getText({ blockSeparator: "\n" });
      setDocumentText(text);
      const changedEnough = Math.abs(text.length - lastAnalyzedText.current.length) > 180;
      if (changedEnough) {
        lastAnalyzedText.current = text;
        void logWorkspaceActivity("edited", "document", {
          summary: "编辑了当前文档草稿。",
          metadata: { text_length: text.length }
        });
        void runAnalysis("significant-change", text);
      }
    },
    onSelectionUpdate: ({ editor: updatedEditor }) => {
      const { from, to } = updatedEditor.state.selection;
      const text = updatedEditor.state.doc.textBetween(from, to, " ");
      setSelectedText(text);
    }
  });

  useEffect(() => {
    const handle = window.setTimeout(() => {
      if (documentText !== lastAnalyzedText.current) {
        lastAnalyzedText.current = documentText;
        void logWorkspaceActivity("edited", "document", {
          summary: "暂停后保存了文档编辑活动。",
          metadata: { text_length: documentText.length }
        });
        void runAnalysis("pause", documentText);
      }
    }, 3000);
    return () => window.clearTimeout(handle);
  }, [documentText, pskaIdentity]);

  useEffect(() => {
    void logWorkspaceActivity("opened", mode);
    void logWorkspaceActivity("viewed", mode);
  }, [mode, pskaIdentity]);

  async function runAnalysis(trigger: BrainState["lastTrigger"], text = documentText) {
    const query = selectedText || text;
    if (!query.trim()) {
      setBrain({ status: "idle", lastTrigger: trigger, updatedAt: Date.now(), error: null });
      return;
    }
    setBrain({ status: "analyzing", lastTrigger: trigger, error: null });
    try {
      const payload = await analyzeWorkspaceContext(query, pskaIdentity, trigger);
      setBrain(payload);
    } catch (error) {
      setBrain({
        status: "error",
        lastTrigger: trigger,
        updatedAt: Date.now(),
        error: error instanceof Error ? error.message : "PSKA 服务请求失败。"
      });
    }
  }

  function refreshCurrentSurface() {
    if (mode === "today" || mode === "review" || mode === "graph") {
      setBrain({ status: "idle", lastTrigger: "manual", updatedAt: Date.now() });
      return;
    }
    void runAnalysis("manual");
  }

  async function logWorkspaceActivity(
    activityType: "opened" | "edited" | "viewed" | "pinned",
    surface: WorkspaceMode,
    options: { summary?: string; metadata?: Record<string, unknown>; throwOnError?: boolean } = {}
  ) {
    if (!userId.trim()) {
      return;
    }
    if (activityType === "edited") {
      const now = Date.now();
      if (now - lastEditedActivityAt.current < 2500) {
        return;
      }
      lastEditedActivityAt.current = now;
    }
    try {
      const target = workspaceActivityTarget(surface);
      await recordWorkspaceActivity(pskaIdentity, {
        activity_type: activityType,
        surface,
        target_type: "workspace_surface",
        target_id: surface,
        title: target.title,
        summary: options.summary || target.summary,
        metadata: options.metadata
      });
    } catch {
      if (options.throwOnError) {
        throw new Error("activity logging failed");
      }
    }
  }

  function pinCurrentWorkspace() {
    setPinStatus("idle");
    void logWorkspaceActivity("pinned", mode, { throwOnError: true })
      .then(() => {
        setPinStatus("saved");
        window.setTimeout(() => setPinStatus("idle"), 1800);
      })
      .catch(() => {
        setPinStatus("failed");
        window.setTimeout(() => setPinStatus("idle"), 2200);
      });
  }

  return (
    <main className={`app-shell ${leftCollapsed ? "left-collapsed" : ""}`}>
      <LeftSidebar collapsed={leftCollapsed} mode={mode} onModeChange={setMode} onToggle={toggleLeft} />
      <section className="workspace-column">
        <TopBar
          mode={mode}
          serviceToken={serviceToken}
          tenantId={tenantId}
          userId={userId}
          representedUserId={representedUserId}
          onModeChange={setMode}
          onTokenChange={setServiceToken}
          onTenantChange={setTenantId}
          onUserChange={setUserId}
          onRepresentedUserChange={setRepresentedUserId}
          onRefresh={refreshCurrentSurface}
        />
        {mode === "today" ? (
          <TodayWorkspace serviceToken={pskaIdentity} onOpenWorkspace={setMode} setBrain={setBrain} />
        ) : mode === "review" ? (
          <ReviewCenter serviceToken={pskaIdentity} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : mode === "graph" ? (
          <GraphWorkspace serviceToken={pskaIdentity} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : mode === "corpus" ? (
          <CorpusWorkspace serviceToken={pskaIdentity} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} setBrain={setBrain} />
        ) : mode === "document" ? (
          <DocumentWorkspace editor={editor} selectedText={selectedText} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : (
          <CanvasWorkspace brain={brain} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        )}
      </section>
      <BrainSidebar brain={brain} onRefresh={refreshCurrentSurface} />
    </main>
  );
}

function LeftSidebar({
  collapsed,
  mode,
  onModeChange,
  onToggle
}: {
  collapsed: boolean;
  mode: WorkspaceMode;
  onModeChange: (mode: WorkspaceMode) => void;
  onToggle: () => void;
}) {
  return (
    <aside className="left-sidebar">
      <button className="collapse-button" type="button" onClick={onToggle} title={collapsed ? "展开侧栏" : "收起侧栏"}>
        {collapsed ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}
      </button>
      <div className="brand-lockup">
        <div className="brand-mark">P</div>
        {!collapsed && (
          <div>
            <strong>PSKA</strong>
            <span>个人知识工作台</span>
          </div>
        )}
      </div>
      <nav className="nav-stack" aria-label="工作区导航">
        <NavItem collapsed={collapsed} icon={<CalendarDays size={18} />} label="Today" active={mode === "today"} onClick={() => onModeChange("today")} />
        <NavItem collapsed={collapsed} icon={<TextCursorInput size={18} />} label="文档" active={mode === "document"} onClick={() => onModeChange("document")} />
        <NavItem collapsed={collapsed} icon={<Network size={18} />} label="画布" active={mode === "canvas"} onClick={() => onModeChange("canvas")} />
        <NavItem collapsed={collapsed} icon={<Hash size={18} />} label="Graph" active={mode === "graph"} onClick={() => onModeChange("graph")} />
        <NavItem collapsed={collapsed} icon={<Folder size={18} />} label="语料库" active={mode === "corpus"} onClick={() => onModeChange("corpus")} />
        <NavItem collapsed={collapsed} icon={<BookOpen size={18} />} label="项目" />
        <NavItem collapsed={collapsed} icon={<Tags size={18} />} label="标签" />
        <NavItem collapsed={collapsed} icon={<Search size={18} />} label="搜索" />
        <NavItem collapsed={collapsed} icon={<GitPullRequest size={18} />} label="Review" active={mode === "review"} onClick={() => onModeChange("review")} />
      </nav>
      {!collapsed && (
        <div className="tree">
          <p>当前项目</p>
          <span>真实项目索引尚未连接。</span>
        </div>
      )}
    </aside>
  );
}

function NavItem({
  collapsed,
  icon,
  label,
  active = false,
  onClick
}: {
  collapsed: boolean;
  icon: JSX.Element;
  label: string;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button className={`nav-item ${active ? "active" : ""}`} type="button" title={label} onClick={onClick}>
      {icon}
      {!collapsed && <span>{label}</span>}
    </button>
  );
}

function TopBar({
  mode,
  serviceToken,
  tenantId,
  userId,
  representedUserId,
  onModeChange,
  onTokenChange,
  onTenantChange,
  onUserChange,
  onRepresentedUserChange,
  onRefresh
}: {
  mode: WorkspaceMode;
  serviceToken: string;
  tenantId: string;
  userId: string;
  representedUserId: string;
  onModeChange: (mode: WorkspaceMode) => void;
  onTokenChange: (serviceToken: string) => void;
  onTenantChange: (tenantId: string) => void;
  onUserChange: (userId: string) => void;
  onRepresentedUserChange: (representedUserId: string) => void;
  onRefresh: () => void;
}) {
  return (
    <header className="top-bar">
      <div className="mode-switch" role="tablist" aria-label="工作台模式">
        <button className={mode === "today" ? "active" : ""} type="button" onClick={() => onModeChange("today")}>
          <CalendarDays size={17} />
          Today
        </button>
        <button className={mode === "document" ? "active" : ""} type="button" onClick={() => onModeChange("document")}>
          <TextCursorInput size={17} />
          文档
        </button>
        <button className={mode === "canvas" ? "active" : ""} type="button" onClick={() => onModeChange("canvas")}>
          <Network size={17} />
          画布
        </button>
        <button className={mode === "graph" ? "active" : ""} type="button" onClick={() => onModeChange("graph")}>
          <Hash size={17} />
          Graph
        </button>
        <button className={mode === "corpus" ? "active" : ""} type="button" onClick={() => onModeChange("corpus")}>
          <Folder size={17} />
          语料库
        </button>
        <button className={mode === "review" ? "active" : ""} type="button" onClick={() => onModeChange("review")}>
          <GitPullRequest size={17} />
          Review
        </button>
      </div>
      <div className="identity-fields" aria-label="PSKA 身份上下文">
        <label className="token-field compact">
          <span>Tenant</span>
          <input value={tenantId} onChange={(event) => onTenantChange(event.target.value)} placeholder="tenant_default" />
        </label>
        <label className="token-field compact">
          <span>User</span>
          <input value={userId} onChange={(event) => onUserChange(event.target.value)} placeholder="user_primary" />
        </label>
        <label className="token-field compact">
          <span>As</span>
          <input value={representedUserId} onChange={(event) => onRepresentedUserChange(event.target.value)} placeholder={userId || "user_primary"} />
        </label>
        <label className="token-field">
          <span>令牌/JWT</span>
          <input
            type="password"
            value={serviceToken}
            onChange={(event) => onTokenChange(event.target.value)}
            placeholder="可选本地令牌"
          />
        </label>
      </div>
      <button className="icon-button" type="button" onClick={onRefresh} title="刷新上下文">
        <RefreshCw size={18} />
      </button>
    </header>
  );
}

type TodayAction = "待处理" | "处理中" | "已接受" | "已忽略" | "稍后" | "已批准" | "已批准并应用" | "已拒绝" | "操作失败";

function TodayWorkspace({
  serviceToken,
  onOpenWorkspace,
  setBrain
}: {
  serviceToken: PSKAAuth;
  onOpenWorkspace: (mode: WorkspaceMode) => void;
  setBrain: (brain: Partial<BrainState>) => void;
}) {
  const [actions, setActions] = useState<Record<string, TodayAction>>({});
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<WorkspaceSearchResponse | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const todayQuery = useQuery({
    queryKey: ["today", serviceToken],
    queryFn: () => loadToday(serviceToken),
    retry: 1
  });
  const corpusQuery = useQuery({
    queryKey: ["today-corpus", serviceToken],
    queryFn: () => loadCorpusData(serviceToken, 12),
    retry: 1
  });
  const data = todayQuery.data;
  const continueWorking = normalizeContinueItems(data);
  const discoveries = normalizeDiscoveries(data);
  const needsReview = normalizeReviewItems(data);
  const corpus = corpusQuery.data;
  const sourceCounts = data?.system?.source_counts || corpus?.counts || {};
  const visibleSources = (corpus?.sources || []).slice(0, 6);
  const visibleChunks = (corpus?.chunks || []).slice(0, 4);

  useEffect(() => {
    if (corpus && !searchResult) {
      setBrain(corpusToBrain(corpus));
    }
  }, [corpus, searchResult, setBrain]);

  function mark(id: string, value: TodayAction) {
    setActions((current) => ({ ...current, [id]: value }));
  }

  async function runTodaySearch(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    const query = searchQuery.trim();
    if (!query) {
      setSearchResult(null);
      setSearchError("请输入要问 PSKA 的问题。");
      return;
    }
    setSearching(true);
    setSearchError(null);
    try {
      const result = await searchWorkspace(query, serviceToken, "direct");
      setSearchResult(result);
      setBrain(searchToBrain(result, query));
      if (result.error) {
        setSearchError(displaySearchError(result.error));
      }
    } catch (error) {
      setSearchResult(null);
      setSearchError(error instanceof Error ? error.message : "PSKA 查询失败。");
    } finally {
      setSearching(false);
    }
  }

  async function approveFromToday(item: { review_item_id?: string | null; id?: string; recommended_action?: string }, success: TodayAction) {
    const displayId = item.review_item_id || item.id || "local";
    if (!item.review_item_id) {
      mark(displayId, success);
      return;
    }
    mark(item.review_item_id, "处理中");
    try {
      await approveReviewItem(serviceToken, item.review_item_id, item.recommended_action === "approve_apply");
      mark(item.review_item_id, item.recommended_action === "approve_apply" ? "已批准并应用" : success);
      await todayQuery.refetch();
    } catch {
      mark(item.review_item_id, "操作失败");
    }
  }

  async function rejectFromToday(item: { review_item_id?: string | null; id?: string }, success: TodayAction) {
    const displayId = item.review_item_id || item.id || "local";
    if (!item.review_item_id) {
      mark(displayId, success);
      return;
    }
    mark(item.review_item_id, "处理中");
    try {
      await rejectReviewItem(serviceToken, item.review_item_id);
      mark(item.review_item_id, success);
      await todayQuery.refetch();
    } catch {
      mark(item.review_item_id, "操作失败");
    }
  }

  async function acceptDiscoveryFromToday(item: TodayDiscoveryItem) {
    mark(item.id, "处理中");
    try {
      await acceptDiscovery(serviceToken, item.id);
      mark(item.id, "已接受");
      await todayQuery.refetch();
    } catch {
      if (item.review_item_id) {
        await approveFromToday(item, "已接受");
        return;
      }
      mark(item.id, "操作失败");
    }
  }

  async function ignoreDiscoveryFromToday(item: TodayDiscoveryItem) {
    mark(item.id, "处理中");
    try {
      await ignoreDiscovery(serviceToken, item.id);
      mark(item.id, "已忽略");
      await todayQuery.refetch();
    } catch {
      if (item.review_item_id) {
        await rejectFromToday(item, "已忽略");
        return;
      }
      mark(item.id, "操作失败");
    }
  }

  async function snoozeDiscoveryFromToday(item: TodayDiscoveryItem) {
    mark(item.id, "处理中");
    try {
      await snoozeDiscovery(serviceToken, item.id);
      mark(item.id, "稍后");
      await todayQuery.refetch();
    } catch {
      mark(item.id, "操作失败");
    }
  }

  return (
    <section className="main-workspace today-surface" aria-label="Today">
      <div className="today-header">
        <div>
          <span className="eyebrow">Today</span>
          <h1>继续思考，不处理收件箱。</h1>
          <p>{todayQuery.isError ? "PSKA 后端暂时不可用；这里不会显示原型数据。" : "真实 PSKA 数据已接入；Today 任务区只显示已产生的活动、发现和审核候选。"}</p>
        </div>
        <div className="today-summary" aria-label="今日摘要">
          <span>
            <strong>{sourceCounts.source_items ?? sourceCounts.sources_total ?? corpus?.sources?.length ?? 0}</strong>
            来源
          </span>
          <span>
            <strong>{sourceCounts.chunks ?? sourceCounts.chunks_matching ?? corpus?.chunks?.length ?? 0}</strong>
            Chunks
          </span>
          <span>
            <strong>{needsReview.length}</strong>
            待审核
          </span>
        </div>
      </div>

      {todayQuery.isError ? (
        <div className="review-empty error-state">Today 无法加载。请检查 8765 后端、Vite 代理或服务令牌。</div>
      ) : todayQuery.isLoading ? (
        <div className="review-empty">正在加载真实 Today 数据...</div>
      ) : (
      <div className="today-grid">
        <section className="today-section today-search">
          <SectionTitle icon={<Search size={18} />} title="Ask PSKA" subtitle="快速 direct 检索" />
          <form className="today-search-form" onSubmit={runTodaySearch}>
            <textarea value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="问 PSKA" />
            <button className="primary" type="submit" disabled={searching}>
              {searching ? "查询中" : "查询"}
            </button>
          </form>
          {searchError ? <div className="review-empty error-state compact">{searchError}</div> : null}
          {searchResult ? <TodaySearchResult result={searchResult} /> : (
            <div className="review-empty compact">当前没有查询结果。</div>
          )}
        </section>

        <section className="today-section corpus-overview">
          <SectionTitle icon={<BookOpen size={18} />} title="Corpus" subtitle="当前工作区真实资料" />
          {corpusQuery.isError ? (
            <div className="review-empty error-state compact">Corpus 无法加载。请检查 8765 后端、数据库或服务令牌。</div>
          ) : corpusQuery.isLoading ? (
            <div className="review-empty compact">正在加载真实 Corpus...</div>
          ) : visibleSources.length === 0 && visibleChunks.length === 0 ? (
            <div className="review-empty compact">当前工作区还没有可展示的真实资料。</div>
          ) : (
            <div className="corpus-columns">
              <div className="corpus-column">
                <h3>Sources</h3>
                <div className="corpus-list">
                  {visibleSources.map((source, index) => (
                    <article className="corpus-item" key={source.source_item_id || `source-${index}`}>
                      <div className="card-row">
                        <span className="pill muted">{source.source_channel || "source"}</span>
                        <small>{formatSourceAge(source.created_at)}</small>
                      </div>
                      <h4>{displayText(source.title || source.source_item_id, "未命名来源")}</h4>
                      {source.url ? <p>{displayText(source.url)}</p> : null}
                    </article>
                  ))}
                </div>
              </div>
              <div className="corpus-column">
                <h3>Chunks</h3>
                <div className="corpus-list">
                  {visibleChunks.map((chunk, index) => (
                    <article className="corpus-item" key={chunk.chunk_id || `chunk-${index}`}>
                      <div className="card-row">
                        <span className="pill muted">{chunk.source_channel || "chunk"}</span>
                        <small>{displayText(chunk.title || chunk.source_item_id, "片段")}</small>
                      </div>
                      <p>{trimText(chunk.snippet || chunk.text || "", 220) || "该 chunk 暂无可显示文本。"}</p>
                    </article>
                  ))}
                </div>
              </div>
            </div>
          )}
        </section>

        <section className="today-section continue-working">
          <SectionTitle icon={<PlayCircle size={18} />} title="Continue Working" subtitle="回到上次真正的工作现场" />
          <div className="today-list">
            {continueWorking.length === 0 ? (
              <div className="review-empty">当前没有可继续的工作记录。置顶工作区、编辑文档或打开具体资料后，这里会出现真实活动。</div>
            ) : continueWorking.map((item) => (
              <button className="work-item" type="button" key={item.id} onClick={() => onOpenWorkspace(item.opened_surface || "document")}>
                <span>
                  <strong>{item.title}</strong>
                  <small>{item.subtitle}</small>
                </span>
                <p>{item.summary}</p>
              </button>
            ))}
          </div>
        </section>

        <section className="today-section discoveries">
          <SectionTitle icon={<Sparkles size={18} />} title="Discoveries" subtitle="系统今天递回来的新线索" />
          <div className="today-list">
            {discoveries.length === 0 ? (
              <div className="review-empty">当前没有达到质量阈值的新发现。导入资料后需要 digest/discovery worker 产出候选，这里才会出现内容。</div>
            ) : discoveries.map((item) => (
              <article className="today-card discovery-card" key={item.id}>
                <div className="card-row">
                  <span className="pill">{item.label}</span>
                  <small>{actions[item.id] || discoveryQualityLabel(item)}</small>
                </div>
                <h2>{item.title}</h2>
                <p>{item.summary}</p>
                <div className="card-actions">
                  <button type="button" onClick={() => acceptDiscoveryFromToday(item)}>
                    接受
                  </button>
                  <button type="button" onClick={() => ignoreDiscoveryFromToday(item)}>
                    忽略
                  </button>
                  <button type="button" onClick={() => snoozeDiscoveryFromToday(item)}>
                    稍后
                  </button>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="today-section needs-review">
          <SectionTitle icon={<CheckCircle2 size={18} />} title="Needs Review" subtitle="会影响长期记忆的候选" />
          <div className="today-list">
            {needsReview.length === 0 ? (
              <div className="review-empty">当前没有待审核候选。只有 discovery/review 流程生成了待确认记忆或关系，这里才会出现内容。</div>
            ) : needsReview.map((item) => (
              <article className="today-card review-card" key={item.review_item_id}>
                <div className="card-row">
                  <span className="pill muted">{item.review_type || "review"}</span>
                  <small>{actions[item.review_item_id] || recommendedActionLabel(item.recommended_action)}</small>
                </div>
                <h2>{item.title}</h2>
                <p>{item.summary}</p>
                <div className="card-actions">
                  <button type="button" onClick={() => approveFromToday(item, "已批准")}>
                    {item.recommended_action === "approve_apply" ? "批准并应用" : "批准"}
                  </button>
                  <button type="button" onClick={() => rejectFromToday(item, "已拒绝")}>
                    拒绝
                  </button>
                  <button type="button" onClick={() => mark(item.review_item_id, "稍后")}>
                    稍后
                  </button>
                </div>
              </article>
            ))}
          </div>
        </section>
      </div>
      )}
    </section>
  );
}

type ReviewActionState = string;

function ReviewCenter({
  serviceToken,
  onPinCurrent,
  pinStatus
}: {
  serviceToken: PSKAAuth;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
}) {
  const [status, setStatus] = useState("pending");
  const [actions, setActions] = useState<Record<string, ReviewActionState>>({});
  const reviewQuery = useQuery({
    queryKey: ["review-center", serviceToken, status],
    queryFn: () => loadReviewCenter(serviceToken, status),
    retry: 1
  });
  const items = reviewQuery.data?.review_items || [];
  const total = reviewQuery.data?.total_matching ?? reviewQuery.data?.count ?? items.length;

  function mark(reviewItemId: string, value: ReviewActionState) {
    setActions((current) => ({ ...current, [reviewItemId]: value }));
  }

  async function runReviewAction(item: ReviewCenterItem, action: "approve" | "approve_apply" | "reject" | "apply") {
    mark(item.review_item_id, "处理中");
    try {
      let result;
      if (action === "reject") {
        result = await rejectReviewItem(serviceToken, item.review_item_id);
      } else if (action === "apply") {
        result = await applyReviewItem(serviceToken, item.review_item_id);
      } else {
        result = await approveReviewItem(serviceToken, item.review_item_id, action === "approve_apply");
      }
      mark(item.review_item_id, reviewActionStatusLabel(action, result?.application_result?.summary));
      await reviewQuery.refetch();
    } catch {
      mark(item.review_item_id, "操作失败");
    }
  }

  return (
    <section className="main-workspace review-surface" aria-label="Review Center">
      <div className="review-center-header">
        <div>
          <span className="eyebrow">Review Center</span>
          <h1>审核候选，再写入长期知识。</h1>
          <p>数据来自真实 Review API：/console/reviews/data。</p>
        </div>
        <div className="review-summary" aria-label="Review 摘要">
          <span>
            <strong>{total}</strong>
            {statusLabel(status)}
          </span>
          <span>
            <strong>{items.filter((item) => item.source_ref_status === "present").length}</strong>
            有证据
          </span>
          <button className="icon-button" type="button" onClick={() => reviewQuery.refetch()} title="刷新 Review">
            <RefreshCw size={18} />
          </button>
        </div>
        <button className="review-pin-action" type="button" onClick={onPinCurrent}>
          <Pin size={16} />
          {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "置顶失败" : "置顶 Review"}
        </button>
      </div>

      <div className="review-filter" role="tablist" aria-label="Review 状态">
        {["pending", "approved", "rejected", "applied"].map((value) => (
          <button className={status === value ? "active" : ""} type="button" key={value} onClick={() => setStatus(value)}>
            {statusLabel(value)}
          </button>
        ))}
      </div>

      {reviewQuery.isError ? (
        <div className="review-empty error-state">Review Center 暂时无法加载。请检查服务令牌或后端服务。</div>
      ) : reviewQuery.isLoading ? (
        <div className="review-empty">正在加载 Review Center...</div>
      ) : items.length === 0 ? (
        <div className="review-empty">当前没有 {statusLabel(status)}。</div>
      ) : (
        <div className="review-list">
          {items.map((item) => (
            <article className="review-center-item" key={item.review_item_id}>
              <div className="review-item-main">
                <div className="review-item-title">
                  <GitPullRequest size={17} />
                  <h2>{displayText(item.title, item.review_item_id)}</h2>
                </div>
                <div className="review-item-tags">
                  <span className="pill">{item.review_type || "review"}</span>
                  <span className={`pill ${item.source_ref_status === "present" ? "" : "warning"}`}>
                    {item.source_ref_status === "present" ? "证据已连接" : "缺少证据"}
                  </span>
                  {!item.apply_supported && <span className="pill muted">不可应用</span>}
                  {item.apply_supported && !item.apply_ready && <span className="pill warning">需检查后应用</span>}
                </div>
                <dl className="review-meta-grid">
                  <div>
                    <dt>置信度</dt>
                    <dd>{confidenceLabel(item.confidence)}</dd>
                  </div>
                  <div>
                    <dt>建议</dt>
                    <dd>{recommendedActionLabel(item.recommended_action)}</dd>
                  </div>
                  <div>
                    <dt>创建时间</dt>
                    <dd>{formatReviewDate(item.created_at)}</dd>
                  </div>
                  <div>
                    <dt>ID</dt>
                    <dd>{item.review_item_id}</dd>
                  </div>
                </dl>
                {item.source_refs?.length ? (
                  <div className="source-ref-row">
                    {item.source_refs.slice(0, 3).map((ref, index) => (
                      <span key={`${item.review_item_id}-${index}`}>
                        {displayText(ref.title || ref.source_item_id || ref.chunk_id, "source ref")}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="review-center-actions">
                <small>{actions[item.review_item_id] || item.status || "pending"}</small>
                {item.status === "pending" ? (
                  <>
                    <button type="button" onClick={() => runReviewAction(item, "approve")}>
                      批准
                    </button>
                    {item.recommended_actions?.includes("approve_apply") && (
                      <button className="primary" type="button" onClick={() => runReviewAction(item, "approve_apply")}>
                        批准并应用
                      </button>
                    )}
                    <button className="danger" type="button" onClick={() => runReviewAction(item, "reject")}>
                      拒绝
                    </button>
                  </>
                ) : item.can_apply_now ? (
                  <button className="primary" type="button" onClick={() => runReviewAction(item, "apply")}>
                    应用
                  </button>
                ) : null}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function reviewActionStatusLabel(action: "approve" | "approve_apply" | "reject" | "apply", summary?: string) {
  if (summary) {
    return summary;
  }
  if (action === "reject") {
    return "已拒绝";
  }
  if (action === "apply") {
    return "已应用";
  }
  return action === "approve_apply" ? "已批准并应用" : "已批准";
}

function normalizeContinueItems(data?: TodayResponse): TodayContinueItem[] {
  const items = data?.continue_working || [];
  return items
    .filter((item) => item.pinned || item.activity_type !== "viewed" || item.target_type !== "workspace_surface")
    .map((item) => ({
      ...item,
      title: displayText(item.title, "未命名活动"),
      subtitle: displayText(item.subtitle || item.type, "source"),
      summary: displayText(item.summary, "最近进入 PSKA 的资料。")
    }));
}

function TodaySearchResult({ result }: { result: WorkspaceSearchResponse }) {
  const parsed = parseAgenticAnswer(result.answer);
  const eventAnswer = finalAnswerFromTraceEvents(result);
  const answer = cleanAgenticAnswer(parsed?.answer || result.answer || eventAnswer || "");
  const events = agenticTraceEvents(result);
  const streamItems = summarizeAgenticEvents(result).slice(0, 8);
  const refs = normalizeSearchRefs([
    ...(parsed?.source_refs || []),
    ...(parsed?.citations || []),
    ...(result.source_refs || []),
    ...(result.citations || []),
    ...(result.workspace?.evidence?.citations || []),
    ...(result.retrieval?.results || []),
    ...(result.fallback?.retrieval?.citations || []),
    ...(result.fallback?.retrieval?.results || [])
  ]);

  if (!answer && refs.length === 0 && !result.error) {
    return <div className="review-empty compact">PSKA 没有为这个问题找到可展示的真实证据。</div>;
  }

  return (
    <article className="today-search-result">
      {answer ? <p className="answer-text">{displayText(answer)}</p> : null}
      {result.fallback_reason ? <small className="search-note">Fallback: {displayText(result.fallback_reason)}</small> : null}
      {result.agentic_service?.run_id || result.trace?.event_count || events.length ? (
        <div className="event-stream-summary">
          <div className="event-stream-header">
            <strong>FastReAct event stream</strong>
            <span>
              {displayText(result.agentic_service?.run_id || result.trace?.run_id, "run")} · {result.trace?.event_count ?? events.length} events
            </span>
          </div>
          {streamItems.length > 0 ? (
            <ol className="event-stream-list">
              {streamItems.map((item, index) => (
                <li key={`${item.type}-${index}`}>
                  <span>{item.type}</span>
                  <p>{displayText(item.message, "已记录事件")}</p>
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}
      {refs.length > 0 ? (
        <div className="source-ref-list">
          {refs.slice(0, 5).map((ref, index) => (
            <div className="source-ref" key={`${ref.source_item_id || ref.title || "ref"}-${index}`}>
              <strong>{displayText(ref.title || ref.source_item_id, "来源")}</strong>
              {ref.snippet ? <p>{trimText(displayText(ref.snippet), 180)}</p> : null}
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}

type AgenticEventSummary = {
  type: string;
  message: string;
};

type SearchEvidenceRef = {
  title: string;
  snippet: string;
  source_item_id?: string;
  score?: number;
};

function normalizeSearchRefs(values: unknown[]): SearchEvidenceRef[] {
  const merged = new Map<string, SearchEvidenceRef>();
  const order: string[] = [];
  values
    .map((value) => {
      const ref = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
      return {
        title: displayText(ref.title, ""),
        snippet: displayText(ref.snippet, ""),
        source_item_id: displayText(ref.source_item_id, "") || undefined,
        score: typeof ref.score === "number" ? ref.score : undefined
      };
    })
    .filter((ref) => ref.title || ref.snippet || ref.source_item_id)
    .forEach((ref) => {
      const key = searchRefKey(ref);
      if (!key) {
        return;
      }
      const current = merged.get(key);
      if (!current) {
        merged.set(key, ref);
        order.push(key);
        return;
      }
      if (!current.title && ref.title) {
        current.title = ref.title;
      }
      if (!current.source_item_id && ref.source_item_id) {
        current.source_item_id = ref.source_item_id;
      }
      if (ref.snippet && (!current.snippet || ref.snippet.length > current.snippet.length)) {
        current.snippet = ref.snippet;
      }
      if (typeof ref.score === "number" && (typeof current.score !== "number" || ref.score > current.score)) {
        current.score = ref.score;
      }
    });
  return order.map((key) => merged.get(key)).filter((ref): ref is SearchEvidenceRef => Boolean(ref));
}

function searchRefKey(ref: SearchEvidenceRef) {
  const sourceId = normalizeSearchRefIdentity(ref.source_item_id);
  if (sourceId) {
    return `source:${sourceId}`;
  }
  const title = normalizeSearchRefIdentity(ref.title);
  if (title) {
    return `title:${title}`;
  }
  const snippet = normalizeSearchRefIdentity(ref.snippet);
  return snippet ? `snippet:${snippet.slice(0, 160)}` : "";
}

function normalizeSearchRefIdentity(value?: string) {
  return displayText(value, "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

function agenticTraceEvents(result: WorkspaceSearchResponse): Array<Record<string, unknown>> {
  return Array.isArray(result.trace?.events) ? result.trace.events : [];
}

function finalAnswerFromTraceEvents(result: WorkspaceSearchResponse) {
  const events = agenticTraceEvents(result);
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.type !== "session_end") {
      continue;
    }
    const metadata = isRecord(event.metadata) ? event.metadata : {};
    const value = firstString(event.content, event.final_content, event.answer, metadata.final_content, metadata.final, metadata.answer);
    if (value) {
      return value;
    }
  }
  return "";
}

function summarizeAgenticEvents(result: WorkspaceSearchResponse): AgenticEventSummary[] {
  const events = agenticTraceEvents(result);
  const summaries = events
    .map((event) => summarizeAgenticEvent(event))
    .filter((event): event is AgenticEventSummary => Boolean(event));
  if (summaries.length > 0) {
    return summaries;
  }
  return (result.trace?.tool_calls || []).map((call) => ({
    type: displayText(asString(call.tool_name), "tool_call"),
    message: compactJson(call.tool_args)
  }));
}

function summarizeAgenticEvent(event: Record<string, unknown>): AgenticEventSummary | null {
  const type = displayText(asString(event.type || event.event_type), "event");
  if (type === "tool_call") {
    return {
      type: displayText(asString(event.tool_name), "tool_call"),
      message: compactJson(event.tool_args || event.args || event.arguments)
    };
  }
  if (type === "tool_result") {
    return {
      type: `${displayText(asString(event.tool_name), "tool_result")} result`,
      message: compactJson(event.content || event.result || event.output)
    };
  }
  if (type === "session_end") {
    const metadata = isRecord(event.metadata) ? event.metadata : {};
    return {
      type: "session_end",
      message: trimText(firstString(event.content, event.final_content, event.answer, metadata.final_content, metadata.final, metadata.answer), 260) || "Agentic run completed."
    };
  }
  const metadata = isRecord(event.metadata) ? event.metadata : {};
  const message = firstString(event.message, event.content, metadata.message, metadata.status, metadata.detail);
  return message ? { type, message: trimText(message, 260) } : null;
}

function firstString(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return "";
}

function asString(value: unknown) {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function compactJson(value: unknown) {
  if (typeof value === "string") {
    return trimText(value, 260);
  }
  if (value === null || value === undefined) {
    return "";
  }
  try {
    return trimText(JSON.stringify(value), 260);
  } catch {
    return String(value);
  }
}

function displaySearchError(error: WorkspaceSearchResponse["error"]) {
  if (!error) {
    return "PSKA 查询失败。";
  }
  if (typeof error === "string") {
    return error;
  }
  return displayText(error.message || error.detail || error.type, "PSKA 查询失败。");
}

function searchToBrain(result: WorkspaceSearchResponse, query: string): Partial<BrainState> {
  const parsed = parseAgenticAnswer(result.answer);
  const eventAnswer = finalAnswerFromTraceEvents(result);
  const answer = cleanAgenticAnswer(parsed?.answer || result.answer || eventAnswer || "");
  const refs = normalizeSearchRefs([
    ...(parsed?.source_refs || []),
    ...(parsed?.citations || []),
    ...(result.source_refs || []),
    ...(result.citations || []),
    ...(result.workspace?.evidence?.citations || []),
    ...(result.retrieval?.results || []),
    ...(result.fallback?.retrieval?.citations || []),
    ...(result.fallback?.retrieval?.results || [])
  ])
    .map((ref, index) => ({
      id: `today-search-${index}`,
      title: displayText(ref.title || ref.source_item_id, query),
      score: typeof ref.score === "number" ? Math.round(ref.score * 100) : undefined,
      snippet: displayText(ref.snippet || answer, "PSKA 返回了相关证据。"),
      source: "PSKA Agentic"
    }))
    .filter((item) => item.title || item.snippet);
  return {
    status: result.error ? "error" : "synced",
    lastTrigger: "manual",
    updatedAt: Date.now(),
    error: result.error ? displaySearchError(result.error) : null,
    relatedKnowledge: [
      ...(answer ? [{ id: "today-answer", title: query, snippet: displayText(answer), source: "PSKA Agentic answer" }] : []),
      ...refs
    ].slice(0, 6),
    entities: extractEntities(`${query} ${answer}`).slice(0, 8),
    connections: refs.slice(0, 5).map((item, index) => ({
      id: `today-connection-${index}`,
      label: item.title,
      relation: item.source || "相关证据"
    }))
  };
}

function corpusToBrain(corpus: WorkspaceCorpusResponse): Partial<BrainState> {
  const sources = corpus.sources || [];
  const chunks = corpus.chunks || [];
  const entityLabels = (corpus.entities || []).map((entity) => entity.label || entity.canonical_name || entity.name || entity.entity_id || "");
  return {
    status: "synced",
    lastTrigger: "manual",
    updatedAt: Date.now(),
    error: null,
    relatedKnowledge: chunks.slice(0, 6).map((chunk, index) => ({
      id: chunk.chunk_id || `corpus-chunk-${index}`,
      title: displayText(chunk.title || chunk.source_item_id, "资料片段"),
      snippet: trimText(chunk.snippet || chunk.text || "", 180) || "PSKA 中的可检索片段。",
      source: displayText(chunk.source_channel, "PSKA 资料库")
    })),
    entities: [...new Set([...entityLabels, ...sources.map((source) => displayText(source.title)).flatMap(extractEntities)])].filter(Boolean).slice(0, 8),
    timeline: sources.slice(0, 5).map((source, index) => ({
      id: source.source_item_id || `corpus-source-${index}`,
      age: formatSourceAge(source.created_at),
      title: displayText(source.title || source.source_item_id, "未命名来源"),
      detail: displayText(source.source_channel, "PSKA 来源材料")
    })),
    connections: sources.slice(0, 5).map((source, index) => ({
      id: `corpus-source-connection-${index}`,
      label: displayText(source.title || source.source_item_id, "来源"),
      relation: displayText(source.source_channel, "source")
    }))
  };
}

function parseAgenticAnswer(value?: string): { answer?: string; source_refs?: Array<{ title?: string; snippet?: string; source_item_id?: string }>; citations?: Array<{ title?: string; snippet?: string; source_item_id?: string }> } | null {
  if (!value) {
    return null;
  }
  const trimmed = value.trim();
  const unfenced = stripCodeFence(trimmed);
  const jsonText = extractJsonObject(unfenced) || unfenced;
  if (!jsonText.startsWith("{")) {
    return extractAnswerField(unfenced);
  }
  try {
    const parsed = JSON.parse(jsonText) as {
      answer?: string;
      source_refs?: Array<{ title?: string; snippet?: string; source_item_id?: string }>;
      citations?: Array<{ title?: string; snippet?: string; source_item_id?: string }>;
    };
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return extractAnswerField(unfenced);
  }
}

function stripCodeFence(value: string) {
  return value.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/i, "").trim();
}

function extractJsonObject(value: string) {
  const start = value.indexOf("{");
  const end = value.lastIndexOf("}");
  if (start === -1 || end <= start) {
    return null;
  }
  return value.slice(start, end + 1);
}

function extractAnswerField(value: string) {
  const match = value.match(/"answer"\s*:\s*"((?:\\.|[^"\\])*)"/s);
  if (!match) {
    return null;
  }
  try {
    return { answer: JSON.parse(`"${match[1]}"`) as string };
  } catch {
    return { answer: match[1].replace(/\\"/g, "\"") };
  }
}

function cleanAgenticAnswer(value: string) {
  const trimmed = value.trim();
  const parsed = parseAgenticAnswer(trimmed);
  if (parsed?.answer && parsed.answer !== value) {
    return parsed.answer.trim();
  }
  const unfenced = stripCodeFence(trimmed);
  return unfenced.replace(/\s*\[\.\.\. truncated \.\.\.\]\s*$/i, "").trim();
}

function extractEntities(value: string) {
  const chineseNames = value.match(/[\u4e00-\u9fa5]{2,8}/g) || [];
  const latinTerms = value.match(/\b[A-Z][A-Za-z0-9+-]{2,}\b/g) || [];
  return [...new Set([...chineseNames, ...latinTerms])]
    .filter((term) => !["当前没有", "真实资料", "相关证据"].includes(term))
    .slice(0, 12);
}

function normalizeDiscoveries(data?: TodayResponse): TodayDiscoveryItem[] {
  const items = data?.discoveries || [];
  return items.map((item) => ({
    ...item,
    title: displayText(item.title, "未命名发现"),
    label: displayText(item.label || discoveryTypeLabel(item.type), "发现"),
    summary: displayText(item.summary, "PSKA 发现了一个可检查的知识线索。")
  }));
}

function normalizeReviewItems(data?: TodayResponse): TodayReviewItem[] {
  const items = data?.needs_review || [];
  return items.map((item) => ({
    ...item,
    title: displayText(item.title, "待审核候选"),
    summary: displayText(item.summary, "等待人工审核。")
  }));
}

function evidenceLabel(count?: number) {
  if (!count) {
    return "待检查";
  }
  return `${count} 条证据`;
}

function discoveryTypeLabel(type?: string) {
  if (type === "topic") {
    return "主题发现";
  }
  if (type === "hyperedge") {
    return "已有关系";
  }
  if (type === "memory_candidate") {
    return "记忆候选";
  }
  if (type === "profile_update") {
    return "画像候选";
  }
  if (type === "action_candidate") {
    return "行动候选";
  }
  if (type === "low_confidence") {
    return "低置信候选";
  }
  if (type === "conflict") {
    return "发现冲突";
  }
  return "发现";
}

function discoveryQualityLabel(item: TodayDiscoveryItem) {
  const score = typeof item.discovery_score === "number" ? item.discovery_score : null;
  const evidence = evidenceLabel(item.evidence_count);
  if (score === null) {
    return evidence;
  }
  return `score ${Math.round(score * 100)} · ${evidence}`;
}

function recommendedActionLabel(action?: string) {
  if (action === "approve_apply") {
    return "可批准并应用";
  }
  if (action === "inspect_then_approve_or_reject") {
    return "需检查";
  }
  return "待处理";
}

function displayText(value: unknown, fallback = ""): string {
  if (value === null || value === undefined) {
    return fallback;
  }
  if (typeof value === "string") {
    return value || fallback;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    const text = value.map((item) => displayText(item)).filter(Boolean).join(" · ");
    return text || fallback;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    for (const key of ["message", "detail", "summary", "statement", "title", "text", "type"]) {
      const text = displayText(record[key]);
      if (text) {
        return text;
      }
    }
    try {
      return JSON.stringify(value);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

function trimText(value: unknown, maxLength: number) {
  const normalized = displayText(value).replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(0, maxLength - 1))}…`;
}

function formatSourceAge(value?: string) {
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
  return `${Math.max(1, Math.round(days / 30))} 个月前`;
}

function statusLabel(status: string) {
  if (status === "pending") {
    return "待审核";
  }
  if (status === "approved") {
    return "已批准";
  }
  if (status === "rejected") {
    return "已拒绝";
  }
  if (status === "applied") {
    return "已应用";
  }
  return status;
}

function confidenceLabel(value?: number | null) {
  if (value === null || value === undefined) {
    return "未提供";
  }
  return `${Math.round(value * 100)}%`;
}

function formatReviewDate(value?: string) {
  if (!value) {
    return "未知";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function SectionTitle({ icon, title, subtitle }: { icon: JSX.Element; title: string; subtitle: string }) {
  return (
    <div className="section-title">
      <div>
        {icon}
        <h2>{title}</h2>
      </div>
      <p>{subtitle}</p>
    </div>
  );
}

function CorpusWorkspace({
  serviceToken,
  onPinCurrent,
  pinStatus,
  setBrain
}: {
  serviceToken: PSKAAuth;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
  setBrain: (brain: Partial<BrainState>) => void;
}) {
  const [query, setQuery] = useState("");
  const [operationStatus, setOperationStatus] = useState<"idle" | "syncing" | "digesting" | "cleaning" | "success" | "error">("idle");
  const [operationMessage, setOperationMessage] = useState("");
  const [cleanupPreview, setCleanupPreview] = useState<KnowledgeSourceCleanupResponse | null>(null);
  const [cleanupTargetId, setCleanupTargetId] = useState<string | null>(null);
  const [cleanupConfirmText, setCleanupConfirmText] = useState("");
  const [operationSummary, setOperationSummary] = useState<{
    scanned?: number;
    ingested?: number;
    changed?: number;
    failed?: number;
    scheduled?: number;
    reviews?: number;
    claims?: number;
    digestNotes?: number;
    saved?: number;
    inputSources?: number;
    unchanged?: number;
    twitterZips?: number;
    twitterImported?: number;
    twitterSkipped?: number;
  }>();
  const corpusQuery = useQuery({
    queryKey: ["corpus-workspace", serviceToken],
    queryFn: () => loadCorpusData(serviceToken, 60),
    retry: 1
  });
  const sourcesQuery = useQuery({
    queryKey: ["corpus-sources-console", serviceToken],
    queryFn: () => loadSourcesConsole(serviceToken, 40),
    retry: 1
  });
  const digestLogsQuery = useQuery({
    queryKey: ["corpus-digest-logs", serviceToken],
    queryFn: () => loadDigestLogs(serviceToken, 8),
    retry: 1
  });
  const corpus = corpusQuery.data;
  const sourceSummary = sourcesQuery.data;
  const digestLogs = digestLogsQuery.data;
  const normalizedQuery = query.trim().toLowerCase();
  const filteredSources = (corpus?.sources || []).filter((source) =>
    corpusText([source.title, source.source_channel, source.url, source.source_item_id]).includes(normalizedQuery)
  );
  const filteredChunks = (corpus?.chunks || []).filter((chunk) =>
    corpusText([chunk.title, chunk.source_channel, chunk.source_item_id, chunk.text, chunk.snippet]).includes(normalizedQuery)
  );
  const counts = {
    sources: sourceSummary?.source_counts?.source_items ?? corpus?.counts?.sources_total ?? corpus?.sources?.length ?? 0,
    documents: sourceSummary?.source_counts?.documents ?? corpus?.counts?.documents ?? 0,
    chunks: sourceSummary?.source_counts?.chunks ?? corpus?.counts?.chunks_matching ?? corpus?.chunks?.length ?? 0,
    inputSources: sourceSummary?.input_sources?.length ?? sourceSummary?.knowledge_sources?.source_count ?? sourceSummary?.connector_state?.state_count ?? 0
  };
  const actionRunning = operationStatus === "syncing" || operationStatus === "digesting" || operationStatus === "cleaning";
  const statusMessage = operationMessage || latestSyncMessage(sourceSummary);

  useEffect(() => {
    if (corpus) {
      setBrain(corpusToBrain(corpus));
    }
  }, [corpus, setBrain]);

  async function refetchAll() {
    await Promise.all([corpusQuery.refetch(), sourcesQuery.refetch(), digestLogsQuery.refetch()]);
  }

  async function handleFileSync() {
    setOperationStatus("syncing");
    setOperationMessage("正在同步本地文件和 Twitter/X 输入源...");
    setOperationSummary(undefined);
    try {
      const payload = await runFileSync(serviceToken);
      const summary = fileSyncSummary(payload);
      setOperationStatus(payload.ok === false ? "error" : "success");
      setOperationSummary(summary);
      setOperationMessage(payload.ok === false ? operationFailureMessage(payload.error, summary.failed) : summaryMessage(summary));
      await refetchAll();
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "同步资料失败。");
    }
  }

  async function handleDigestNow() {
    setOperationStatus("digesting");
    setOperationMessage("正在同步并整理新发现...");
    setOperationSummary(undefined);
    try {
      const payload = await runDigestNow(serviceToken);
      const summary = digestNowSummary(payload);
      setOperationStatus(payload.ok === false ? "error" : "success");
      setOperationSummary(summary);
      setOperationMessage(payload.ok === false ? operationFailureMessage(payload.error, summary.failed) : summaryMessage(summary));
      await refetchAll();
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "同步并理解失败。");
    }
  }

  async function handleCleanupKnowledgeSource(knowledgeSourceId: string, execute: boolean) {
    setOperationStatus("cleaning");
    setCleanupTargetId(knowledgeSourceId);
    setOperationMessage(execute ? "正在清理资料来源和派生知识..." : "正在预览清理影响...");
    try {
      const payload = await cleanupKnowledgeSource(serviceToken, knowledgeSourceId, execute);
      setCleanupPreview(payload);
      if (!execute) {
        setCleanupConfirmText("");
      }
      setOperationStatus("success");
      const counts = payload.deleted || payload.counts || {};
      setOperationMessage(execute ? cleanupDoneMessage(counts) : cleanupPreviewMessage(counts));
      if (execute) {
        await refetchAll();
      }
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "清理资料来源失败。");
    } finally {
      setCleanupTargetId(null);
    }
  }

  return (
    <section className="main-workspace corpus-surface" aria-label="语料库">
      <div className="corpus-header">
        <div>
          <span className="eyebrow">资料更新台</span>
          <h1>资料库</h1>
          <p>把本地资料同步进 PSKA，并让它整理出可回顾的发现。</p>
        </div>
        <div className="corpus-summary" aria-label="语料库摘要">
          <span><strong>{counts.sources}</strong> 资料</span>
          <span><strong>{counts.documents}</strong> 文档</span>
          <span><strong>{counts.chunks}</strong> 片段</span>
          <span><strong>{counts.inputSources}</strong> 输入源</span>
        </div>
        <div className="corpus-actions">
          <button type="button" onClick={onPinCurrent}>
            <Pin size={15} />
            {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "置顶失败" : "置顶语料库"}
          </button>
          <button type="button" onClick={() => void refetchAll()} disabled={actionRunning}>
            <RefreshCw size={15} />
            刷新视图
          </button>
          <button type="button" onClick={() => void handleFileSync()} disabled={actionRunning}>
            <Folder size={15} />
            {operationStatus === "syncing" ? "同步中" : "同步输入源"}
          </button>
          <button type="button" onClick={() => void handleDigestNow()} disabled={actionRunning}>
            <Sparkles size={15} />
            {operationStatus === "digesting" ? "理解中" : "同步并理解"}
          </button>
        </div>
      </div>

      <div className={`corpus-operation ${operationStatus}`} role="status">
        <div>
          <strong>{operationTitle(operationStatus)}</strong>
          <p>{statusMessage}</p>
        </div>
        {operationSummary ? (
          <div className="operation-stats" aria-label="运行摘要">
            <span>扫描 {operationSummary.scanned ?? 0}</span>
            <span>入库 {operationSummary.ingested ?? 0}</span>
            <span>变更 {operationSummary.changed ?? 0}</span>
            <span>失败 {operationSummary.failed ?? 0}</span>
            {operationSummary.scheduled !== undefined ? <span>调度 {operationSummary.scheduled}</span> : null}
            {operationSummary.digestNotes !== undefined ? <span>Digest {operationSummary.digestNotes}</span> : null}
            {operationSummary.claims !== undefined ? <span>Claims {operationSummary.claims}</span> : null}
            {operationSummary.saved !== undefined ? <span>已保存 {operationSummary.saved}</span> : null}
            {operationSummary.reviews !== undefined ? <span>待回顾 {operationSummary.reviews}</span> : null}
            {operationSummary.inputSources !== undefined ? <span>输入源 {operationSummary.inputSources}</span> : null}
            {operationSummary.unchanged !== undefined ? <span>未变 {operationSummary.unchanged}</span> : null}
            {operationSummary.twitterZips !== undefined ? <span>Twitter Zip {operationSummary.twitterZips}</span> : null}
            {operationSummary.twitterImported !== undefined ? <span>Twitter 导入 {operationSummary.twitterImported}</span> : null}
            {operationSummary.twitterSkipped !== undefined ? <span>Twitter 已有 {operationSummary.twitterSkipped}</span> : null}
          </div>
        ) : null}
      </div>

      <UnderstandingSummary payload={digestLogs} />

      <div className="corpus-tools">
        <label>
          <Search size={16} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索资料标题、片段内容或来源路径" />
        </label>
      </div>

      {corpusQuery.isError || sourcesQuery.isError ? (
        <div className="review-empty error-state">语料库无法完整加载。请检查 8765 后端、数据库或服务令牌。</div>
      ) : corpusQuery.isLoading && sourcesQuery.isLoading ? (
        <div className="review-empty">正在加载真实语料库...</div>
      ) : (
        <div className="corpus-workspace-grid">
          <section className="corpus-panel corpus-source-panel">
            <SectionTitle icon={<FileText size={18} />} title="已收进来的资料" subtitle={`${filteredSources.length} 条可见资料`} />
            {filteredSources.length === 0 ? (
              <div className="review-empty compact">{normalizedQuery ? "当前没有匹配的资料。" : "还没有同步进来的资料。点击“同步资料”开始扫描。"}</div>
            ) : (
              <div className="corpus-source-list">
                {filteredSources.slice(0, 30).map((source, index) => (
                  <article className="corpus-source-card" key={source.source_item_id || `corpus-source-${index}`}>
                    <div className="card-row">
                      <span className="pill muted">{source.source_channel || "source"}</span>
                      <small>{formatSourceAge(source.created_at)}</small>
                    </div>
                    <h2>{displayText(source.title || source.source_item_id, "未命名来源")}</h2>
                    <dl>
                      <div><dt>片段</dt><dd>{source.chunk_count ?? "-"}</dd></div>
                      <div><dt>ID</dt><dd>{source.source_item_id || "-"}</dd></div>
                    </dl>
                    {source.url ? <p>{displayText(source.url)}</p> : null}
                  </article>
                ))}
              </div>
            )}
          </section>

          <section className="corpus-panel corpus-chunk-panel">
            <SectionTitle icon={<TextCursorInput size={18} />} title="可检索片段" subtitle={`${filteredChunks.length} 个片段`} />
            {filteredChunks.length === 0 ? (
              <div className="review-empty compact">{normalizedQuery ? "当前没有匹配的片段。" : "资料还没有可检索片段。同步后会出现在这里。"}</div>
            ) : (
              <div className="corpus-chunk-list">
                {filteredChunks.slice(0, 36).map((chunk, index) => (
                  <article className="corpus-chunk-card" key={chunk.chunk_id || `corpus-chunk-${index}`}>
                    <div className="card-row">
                      <span className="pill">{chunk.source_channel || "chunk"}</span>
                      <small>{displayText(chunk.title || chunk.source_item_id, "片段")}</small>
                    </div>
                    <p>{trimText(chunk.snippet || chunk.text || "", 260) || "该 chunk 暂无可显示文本。"}</p>
                  </article>
                ))}
              </div>
            )}
          </section>

          <aside className="corpus-panel connector-panel">
            <SectionTitle icon={<Folder size={18} />} title="PSKA 输入源" subtitle="本地文件 roots 与连接器 inbox" />
            <ConnectorSummary
              payload={sourceSummary}
              cleanupPreview={cleanupPreview}
              cleanupConfirmText={cleanupConfirmText}
              cleanupTargetId={cleanupTargetId}
              actionRunning={actionRunning}
              onCleanupConfirmTextChange={setCleanupConfirmText}
              onPreviewCleanup={(knowledgeSourceId) => void handleCleanupKnowledgeSource(knowledgeSourceId, false)}
              onConfirmCleanup={(knowledgeSourceId) => void handleCleanupKnowledgeSource(knowledgeSourceId, true)}
            />
          </aside>

          <section className="corpus-panel digest-log-panel">
            <SectionTitle icon={<Sparkles size={18} />} title="Digest 任务日志" subtitle={`${digestLogs?.count ?? 0} 次最近理解任务`} />
            <DigestLogPanel payload={digestLogs} isLoading={digestLogsQuery.isLoading} isError={digestLogsQuery.isError} />
          </section>
        </div>
      )}
    </section>
  );
}

function UnderstandingSummary({ payload }: { payload?: DigestLogsResponse }) {
  const summary = payload?.summary;
  const totals = summary?.candidate_totals || {};
  const statusCounts = summary?.status_counts || {};
  const recentNote = summary?.recent_digest_notes?.[0];
  const recentClaim = summary?.recent_claims?.[0];
  const latestFailure = summary?.latest_failure;
  return (
    <section className="understanding-summary" aria-label="理解结果摘要">
      <div>
        <span className="eyebrow">理解结果</span>
        <h2>{displayText(recentNote?.title || recentClaim?.statement || "等待新的 Digest 输出", "等待新的 Digest 输出")}</h2>
        <p>
          {displayText(
            recentNote?.synopsis || latestFailure?.error || (summary?.has_useful_output ? "最近任务已产生可回顾的候选知识。" : "同步后运行“同步并理解”，这里会显示 claims、digest、回顾项和失败原因。")
          )}
        </p>
      </div>
      <div className="understanding-metrics">
        <span><strong>{totals.knowledge_claims ?? 0}</strong> Claims</span>
        <span><strong>{totals.digest_notes ?? 0}</strong> Digest</span>
        <span><strong>{totals.hyperedges ?? 0}</strong> 连接</span>
        <span><strong>{totals.review_candidates ?? totals.review_items ?? 0}</strong> 待确认</span>
        <span><strong>{statusCounts.failed ?? 0}</strong> 失败</span>
      </div>
    </section>
  );
}

function DigestLogPanel({ payload, isLoading, isError }: { payload?: DigestLogsResponse; isLoading: boolean; isError: boolean }) {
  const logs = payload?.logs || [];
  if (isError) {
    return <div className="review-empty error-state compact">Digest 日志无法加载。</div>;
  }
  if (isLoading) {
    return <div className="review-empty compact">正在加载 Digest 日志...</div>;
  }
  if (logs.length === 0) {
    return <div className="review-empty compact">还没有 digest 任务日志。运行“同步并理解”后会显示过程。</div>;
  }
  return (
    <div className="digest-log-list">
      {logs.map((log) => {
        const summary = log.candidate_summary || {};
        const note = (log.digest_notes || [])[0];
        const claim = (log.knowledge_claims || [])[0];
        return (
          <article className="digest-log-card" key={log.job_id}>
            <div className="card-row">
              <span className={`pill ${log.status === "failed" ? "warning" : log.status === "succeeded" ? "" : "muted"}`}>{log.status || "unknown"}</span>
              <small>{formatReviewDate(log.updated_at)}</small>
            </div>
            <h3>{displayText(note?.title || log.latest_event?.message || log.job_id, "Digest 任务")}</h3>
            <p>{displayText(note?.synopsis || claim?.statement || log.error, "任务已记录，等待候选写回。")}</p>
            <div className="digest-log-metrics">
              <span>Digest {summary.digest_notes ?? 0}</span>
              <span>Claims {summary.knowledge_claims ?? 0}</span>
              <span>关系 {summary.hyperedges ?? 0}</span>
              <span>记忆 {summary.agent_memories ?? 0}</span>
              <span>回顾 {summary.review_items ?? 0}</span>
            </div>
            <ol className="digest-timeline">
              {(log.timeline || []).slice(-4).map((event, index) => (
                <li key={`${log.job_id}-${event.event_type || "event"}-${index}`}>
                  <span>{event.event_type || "event"}</span>
                  <p>{displayText(event.message, "已记录事件")}</p>
                </li>
              ))}
            </ol>
          </article>
        );
      })}
    </div>
  );
}

function ConnectorSummary({
  payload,
  cleanupPreview,
  cleanupConfirmText,
  cleanupTargetId,
  actionRunning,
  onCleanupConfirmTextChange,
  onPreviewCleanup,
  onConfirmCleanup
}: {
  payload?: ConsoleSourcesResponse;
  cleanupPreview: KnowledgeSourceCleanupResponse | null;
  cleanupConfirmText: string;
  cleanupTargetId: string | null;
  actionRunning: boolean;
  onCleanupConfirmTextChange: (value: string) => void;
  onPreviewCleanup: (knowledgeSourceId: string) => void;
  onConfirmCleanup: (knowledgeSourceId: string) => void;
}) {
  const knowledgeSources = payload?.knowledge_sources?.sources || [];
  const inputSources = payload?.input_sources || [];
  const states = payload?.connector_state?.states || [];
  const channels = Object.entries(payload?.source_channels || {}).sort((a, b) => channelCount(b[1]) - channelCount(a[1]));
  return (
    <div className="connector-summary">
      <div className="connector-roots">
        <h3>输入源总览</h3>
        {inputSources.length === 0 ? (
          <p>还没有输入源。可以添加本地文件夹，或放入 Twitter/X zip archive。</p>
        ) : inputSources.map((source, index) => (
          <article className="connector-state-card" key={`${source.kind || "input"}-${source.path || index}`}>
            <div className="card-row">
              <span className={`pill ${source.status === "missing" || source.status === "paused" ? "warning" : ""}`}>{inputKindLabel(source.kind)}</span>
              <small>{displayText(source.status || source.mode, "unknown")}</small>
            </div>
            <h3>{displayText(source.name, "输入源")}</h3>
            <p>{displayText(source.path, "未配置路径")}</p>
            {source.kind === "twitter_archive" ? (
              <dl>
                <div><dt>Zip</dt><dd>{source.zip_count ?? 0}</dd></div>
                <div><dt>模式</dt><dd>导入后归档</dd></div>
              </dl>
            ) : null}
          </article>
        ))}
        {payload?.workspace?.excluded_paths?.length ? (
          <div className="connector-exclusions">
            <h3>系统排除路径</h3>
            {payload.workspace.excluded_paths.map((path) => <code key={path}>{path}</code>)}
          </div>
        ) : null}
      </div>
      <div className="connector-roots">
        <h3>可清理的文件资料源</h3>
        {knowledgeSources.length === 0 ? (
          <p>还没有资料位置。请先在配置中添加一个资料文件夹。</p>
        ) : knowledgeSources.map((source) => {
          const knowledgeSourceId = source.knowledge_source_id || "";
          const previewMatches = cleanupPreview?.knowledge_source?.knowledge_source_id === knowledgeSourceId;
          const counts = cleanupPreview?.counts || {};
          const confirmToken = cleanupConfirmToken(source);
          const confirmReady = previewMatches && cleanupConfirmText.trim() === confirmToken;
          return (
          <article className="connector-state-card" key={source.knowledge_source_id || source.uri}>
            <div className="card-row">
              <span className={`pill ${source.status === "failed" ? "warning" : ""}`}>{source.status || "unknown"}</span>
              <small>{source.mode || "manual"}</small>
            </div>
            <h3>{displayText(source.name || source.path || source.uri, "资料位置")}</h3>
            <p>{displayText(source.path || source.uri, "未配置路径")}</p>
            {source.last_sync_run ? (
              <dl>
                <div><dt>扫描</dt><dd>{source.last_sync_run.scanned ?? 0}</dd></div>
                <div><dt>新增</dt><dd>{source.last_sync_run.new_files ?? 0}</dd></div>
                <div><dt>变更</dt><dd>{source.last_sync_run.changed_files ?? 0}</dd></div>
                <div><dt>失败</dt><dd>{source.last_sync_run.failed ?? 0}</dd></div>
              </dl>
            ) : null}
            {source.last_error ? <p className="connector-error">{displayText(source.last_error)}</p> : null}
            {knowledgeSourceId ? (
              <div className="source-cleanup-actions">
                <button type="button" onClick={() => onPreviewCleanup(knowledgeSourceId)} disabled={actionRunning}>
                  {cleanupTargetId === knowledgeSourceId ? "预览中" : "预览清理"}
                </button>
                <button
                  className="danger"
                  type="button"
                  onClick={() => onConfirmCleanup(knowledgeSourceId)}
                  disabled={actionRunning || !confirmReady}
                >
                  确认清理
                </button>
              </div>
            ) : null}
            {previewMatches ? (
              <div className="source-cleanup-preview">
                <strong>{cleanupPreview?.dry_run ? "清理预览" : "清理结果"}</strong>
                <label>
                  输入 <code>{confirmToken}</code> 后允许确认清理
                  <input
                    value={cleanupConfirmText}
                    onChange={(event) => onCleanupConfirmTextChange(event.target.value)}
                    placeholder={confirmToken}
                  />
                </label>
                <div className="operation-stats compact">
                  <span>资料 {counts.source_items ?? 0}</span>
                  <span>文档 {counts.documents ?? 0}</span>
                  <span>片段 {counts.chunks ?? 0}</span>
                  <span>Claims {counts.knowledge_claims ?? 0}</span>
                  <span>Digest {counts.digest_notes ?? 0}</span>
                  <span>关系 {counts.hyperedges ?? 0}</span>
                  <span>任务 {counts.jobs ?? 0}</span>
                  <span>孤立实体 {counts.orphan_entities ?? 0}</span>
                </div>
                <p>{cleanupPreview?.root || source.path || source.uri}</p>
              </div>
            ) : null}
          </article>
        );
        })}
      </div>
      {knowledgeSources.length === 0 ? <div className="connector-state-list">
        {states.length === 0 ? (
          <div className="review-empty compact">还没有可显示的同步状态。</div>
        ) : states.map((state) => (
          <article className="connector-state-card" key={state.connector_state_id || state.connector_id}>
            <div className="card-row">
              <span className={`pill ${state.sync_status === "failed" ? "warning" : ""}`}>{state.sync_status || "unknown"}</span>
              <small>{state.enabled ? "enabled" : "disabled"}</small>
            </div>
            <h3>{displayText(state.connector_state_id || state.connector_id, "资料来源")}</h3>
            <dl>
              <div><dt>Cursor</dt><dd>{state.scan_cursor || "-"}</dd></div>
              <div><dt>最近成功</dt><dd>{formatReviewDate(state.last_success_at || undefined)}</dd></div>
              <div><dt>位置</dt><dd>{state.roots?.length || 0}</dd></div>
            </dl>
            {state.last_error ? <p className="connector-error">{displayText(state.last_error)}</p> : null}
          </article>
        ))}
      </div> : null}
      <div className="connector-channels">
        <h3>资料类型</h3>
        {channels.length === 0 ? (
          <p>暂无资料类型统计。</p>
        ) : channels.map(([channel, value]) => (
          <div key={channel}>
            <span>
              {channel}
              {channelLatest(value) ? <small>{channelLatest(value)}</small> : null}
            </span>
            <strong>{channelCount(value)}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}

function fileSyncSummary(payload: FileSyncResponse) {
  const totals = payload.totals || {};
  const twitterEnabled = payload.twitter_archives?.enabled === true;
  return {
    inputSources: (totals.roots || 0) + (twitterEnabled ? 1 : 0),
    scanned: totals.scanned || 0,
    ingested: totals.ingested || 0,
    changed: (totals.new_files || 0) + (totals.changed_files || 0),
    unchanged: totals.unchanged_files || 0,
    twitterZips: totals.twitter_zip_count ?? payload.twitter_archives?.zip_count ?? 0,
    twitterImported: totals.twitter_imported ?? payload.twitter_archives?.imported ?? 0,
    twitterSkipped: totals.twitter_skipped ?? payload.twitter_archives?.skipped ?? 0,
    failed: totals.failed ?? payload.failed?.length ?? 0
  };
}

function digestNowSummary(payload: DigestNowResponse) {
  const synced = payload.summary?.synced || payload.sync?.totals || {};
  const candidateWrite = payload.summary?.candidate_write || {};
  const twitterEnabled = payload.sync?.twitter_archives?.enabled === true;
  return {
    inputSources: (synced.roots || 0) + (twitterEnabled ? 1 : 0),
    scanned: synced.scanned || 0,
    ingested: synced.ingested || 0,
    changed: (synced.new_files || 0) + (synced.changed_files || 0),
    unchanged: synced.unchanged_files || 0,
    twitterZips: synced.twitter_zip_count ?? payload.sync?.twitter_archives?.zip_count ?? 0,
    twitterImported: synced.twitter_imported ?? payload.sync?.twitter_archives?.imported ?? 0,
    twitterSkipped: synced.twitter_skipped ?? payload.sync?.twitter_archives?.skipped ?? 0,
    failed: payload.summary?.failed_digest_jobs ?? payload.failed_digest_jobs?.length ?? payload.sync?.totals?.failed ?? 0,
    scheduled: payload.summary?.scheduled_source_items ?? payload.digest?.scheduled_source_item_ids?.length ?? 0,
    digestNotes: candidateWrite.digest_notes ?? 0,
    claims: candidateWrite.knowledge_claims ?? 0,
    saved: candidateWrite.saved_candidates ?? 0,
    reviews: payload.summary?.pending_review_count ?? 0
  };
}

function summaryMessage(summary: ReturnType<typeof fileSyncSummary> & { scheduled?: number; reviews?: number; claims?: number; digestNotes?: number; saved?: number }) {
  const parts = [
    `输入源 ${summary.inputSources ?? 0} 个`,
    `本地扫描 ${summary.scanned ?? 0} 个`,
    `本地入库 ${summary.ingested ?? 0} 个`,
    `变更 ${summary.changed ?? 0} 个`,
    `未变 ${summary.unchanged ?? 0} 个`,
    `Twitter Zip ${summary.twitterZips ?? 0} 个`,
    `Twitter 导入 ${summary.twitterImported ?? 0} 个`,
    `Twitter 已有 ${summary.twitterSkipped ?? 0} 个`,
    `失败 ${summary.failed ?? 0} 个`
  ];
  if (summary.scheduled !== undefined) {
    parts.push(`调度 ${summary.scheduled} 个`);
  }
  if (summary.digestNotes !== undefined) {
    parts.push(`Digest ${summary.digestNotes} 条`);
  }
  if (summary.claims !== undefined) {
    parts.push(`Claims ${summary.claims} 条`);
  }
  if (summary.saved !== undefined) {
    parts.push(`已保存 ${summary.saved} 项`);
  }
  if (summary.reviews !== undefined) {
    parts.push(`待回顾 ${summary.reviews} 条`);
  }
  return parts.join("，");
}

function cleanupPreviewMessage(counts: Record<string, number>) {
  return `预览完成：将影响资料 ${counts.source_items ?? 0} 个、片段 ${counts.chunks ?? 0} 个、关系 ${counts.hyperedges ?? 0} 条、Digest ${counts.digest_notes ?? 0} 条。`;
}

function cleanupDoneMessage(counts: Record<string, number>) {
  return `清理完成：已处理资料 ${counts.source_items ?? 0} 个、片段 ${counts.chunks ?? 0} 个、关系 ${counts.hyperedges ?? 0} 条。`;
}

function cleanupConfirmToken(source: { name?: string; path?: string; uri?: string }) {
  const name = displayText(source.name, "").trim();
  if (name) {
    return name;
  }
  const rawPath = displayText(source.path || source.uri, "").trim();
  const normalized = rawPath.replace(/\/+$/, "");
  const last = normalized.split("/").filter(Boolean).pop();
  return last || "cleanup";
}

function inputKindLabel(kind?: string) {
  if (kind === "twitter_archive") {
    return "Twitter/X";
  }
  if (kind === "files_root") {
    return "本地文件";
  }
  return displayText(kind, "输入源");
}

function operationFailureMessage(error: string | undefined, failed: number | undefined) {
  if (error) {
    return error;
  }
  return failed ? `有 ${failed} 项没有完成。` : "操作没有完成，请稍后再试。";
}

function operationTitle(status: "idle" | "syncing" | "digesting" | "cleaning" | "success" | "error") {
  if (status === "syncing") {
    return "同步资料";
  }
  if (status === "digesting") {
    return "同步并理解";
  }
  if (status === "cleaning") {
    return "清理资料来源";
  }
  if (status === "success") {
    return "已完成";
  }
  if (status === "error") {
    return "需要处理";
  }
  return "同步状态";
}

function latestSyncMessage(payload?: ConsoleSourcesResponse) {
  const sources = payload?.knowledge_sources?.sources || [];
  const latest = sources.find((source) => source.last_sync_run);
  if (!latest?.last_sync_run) {
    return "还没有同步记录。";
  }
  const run = latest.last_sync_run;
  return `最近同步：扫描 ${run.scanned ?? 0} 个，新增 ${run.new_files ?? 0} 个，变更 ${run.changed_files ?? 0} 个，失败 ${run.failed ?? 0} 个。`;
}

function corpusText(values: Array<string | undefined>) {
  return values.filter(Boolean).join(" ").toLowerCase();
}

function channelCount(value: ConsoleSourceChannelStats) {
  if (typeof value === "number") {
    return value;
  }
  if (value && typeof value === "object" && typeof value.source_items === "number") {
    return value.source_items;
  }
  return 0;
}

function channelLatest(value: ConsoleSourceChannelStats) {
  if (!value || typeof value !== "object") {
    return "";
  }
  const at = value.latest_source_item_at ? formatReviewDate(value.latest_source_item_at) : "";
  const id = value.latest_source_item_id ? trimText(value.latest_source_item_id, 18) : "";
  return [at, id].filter(Boolean).join(" / ");
}

function DocumentWorkspace({
  editor,
  selectedText,
  onPinCurrent,
  pinStatus
}: {
  editor: ReturnType<typeof useEditor>;
  selectedText: string;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
}) {
  return (
    <section className="main-workspace document-surface" aria-label="文档工作区">
      <div className="document-toolbar">
        <button type="button" onClick={() => editor?.chain().focus().toggleHeading({ level: 1 }).run()} title="一级标题">
          H1
        </button>
        <button type="button" onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()} title="二级标题">
          H2
        </button>
        <button type="button" onClick={() => editor?.chain().focus().toggleBulletList().run()} title="项目列表">
          <Hash size={16} />
        </button>
        <button type="button" onClick={() => editor?.chain().focus().toggleCodeBlock().run()} title="代码块">
          {"{}"}
        </button>
        <button type="button" onClick={() => editor?.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()} title="插入表格">
          表格
        </button>
        <button type="button" onClick={onPinCurrent} title="置顶文档工作区">
          <Pin size={15} />
          {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "失败" : "置顶"}
        </button>
        <span>{selectedText ? `已选中 ${selectedText.length} 个字符` : "环境上下文检测"}</span>
      </div>
      <EditorContent editor={editor} />
    </section>
  );
}

function GraphWorkspace({
  serviceToken,
  onPinCurrent,
  pinStatus
}: {
  serviceToken: PSKAAuth;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
}) {
  const [graphLimit, setGraphLimit] = useState(20);
  const [activeTypes, setActiveTypes] = useState(() => new Set(["source", "document", "passage", "claim", "digest", "fact", "hyperedge", "memory", "memory_suggestion", "action"]));
  const activeTypeList = useMemo(() => Array.from(activeTypes).sort(), [activeTypes]);
  const graphQuery = useQuery({
    queryKey: ["workspace-graph-v2", serviceToken, graphLimit, activeTypeList.join(",")],
    queryFn: () => loadGraphData(serviceToken, graphLimit, activeTypeList),
    retry: 1
  });
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [graphSearch, setGraphSearch] = useState("");
  const [neighborhoodOnly, setNeighborhoodOnly] = useState(false);
  const [pathQuery, setPathQuery] = useState("GraphRAG digest claims");
  const [pathMode, setPathMode] = useState<"deterministic" | "agentic">("deterministic");
  const [pathResult, setPathResult] = useState<WorkspaceGraphPathResponse | null>(null);
  const [pathStatus, setPathStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [pathError, setPathError] = useState("");
  const [controlsOpen, setControlsOpen] = useState(false);
  const [insightsOpen, setInsightsOpen] = useState(false);
  const [expandedGraph, setExpandedGraph] = useState<WorkspaceGraphResponse | null>(null);
  const [expandStatus, setExpandStatus] = useState<"idle" | "loading" | "error">("idle");
  const [expandError, setExpandError] = useState("");
  const graph = useMemo(() => mergeGraphResponses(graphQuery.data, expandedGraph) || undefined, [graphQuery.data, expandedGraph]);
  const selectedEvidencePath = useMemo(() => graphEvidencePath(graph, selectedNodeId), [graph, selectedNodeId]);
  const graphElements = useMemo(
    () => graphToCytoscapeElements(graph, activeTypes, graphSearch, neighborhoodOnly ? selectedNodeId : null, selectedEvidencePath),
    [graph, activeTypes, graphSearch, neighborhoodOnly, selectedNodeId, selectedEvidencePath]
  );
  const cytoscapeLayout = useMemo(() => graphLayoutForElementCount(graphElements.length), [graphElements.length]);
  const selectedNode = (graph?.nodes || []).find((node) => node.id === selectedNodeId);
  const graphSearchMatches = useMemo(() => graphSearchResultNodes(graph, activeTypes, graphSearch), [graph, activeTypes, graphSearch]);
  const selectedNeighborhood = useMemo(() => graphNodeNeighborhood(graph, selectedNodeId), [graph, selectedNodeId]);
  const loading = graphQuery.isLoading;
  const error = graphQuery.isError;
  const typeOptions = ["source", "document", "passage", "claim", "digest", "phrase", "entity", "fact", "hyperedge", "memory", "memory_suggestion", "action"];

  function toggleType(type: string) {
    setActiveTypes((current) => {
      const next = new Set(current);
      if (next.has(type)) {
        next.delete(type);
      } else {
        next.add(type);
      }
      return next;
    });
  }

  async function handleGraphPath(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = pathQuery.trim();
    if (!query) {
      return;
    }
    setPathStatus("loading");
    setPathError("");
    try {
      const payload = await loadGraphPath(serviceToken, query, pathMode);
      setPathResult(payload);
      setPathStatus(payload.ok === false ? "error" : "success");
      setPathError(displayText(payload.error));
    } catch (err) {
      setPathStatus("error");
      setPathError(err instanceof Error ? err.message : "Graph path 查询失败。");
    }
  }

  async function handleExpandSelectedNode() {
    if (!selectedNodeId) {
      return;
    }
    setExpandStatus("loading");
    setExpandError("");
    try {
      const payload = await loadGraphSubgraph(serviceToken, selectedNodeId, Math.max(graphLimit, 80), 1, activeTypeList);
      setExpandedGraph((current) => mergeGraphResponses(current, payload));
      setExpandStatus("idle");
    } catch (err) {
      setExpandStatus("error");
      setExpandError(err instanceof Error ? err.message : "Graph subgraph 展开失败。");
    }
  }

  async function handleSearchSubgraph() {
    const query = graphSearch.trim();
    if (!query) {
      return;
    }
    setExpandStatus("loading");
    setExpandError("");
    try {
      const payload = await loadGraphSearchSubgraph(serviceToken, query, Math.max(graphLimit, 80), 1, 5, activeTypeList);
      setExpandedGraph((current) => mergeGraphResponses(current, payload));
      const firstNodeId = payload.nodes?.[0]?.id;
      if (firstNodeId) {
        setSelectedNodeId(firstNodeId);
      }
      setExpandStatus("idle");
    } catch (err) {
      setExpandStatus("error");
      setExpandError(err instanceof Error ? err.message : "Graph search subgraph 失败。");
    }
  }

  return (
    <section className="main-workspace canvas-surface graph-surface" aria-label="Graph 工作区">
      <div className="graph-toolbar">
        <button type="button" onClick={onPinCurrent}>
          <Pin size={15} />
          {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "置顶失败" : "置顶 Graph"}
        </button>
        <button type="button" onClick={() => {
          void graphQuery.refetch();
        }}>
          <RefreshCw size={15} />
          刷新
        </button>
      </div>
      <div className={`graph-control-dock ${controlsOpen ? "open" : ""}`} aria-label="Graph 控制抽屉">
        <div className="graph-control-head">
          <div className="graph-summary" aria-label="Graph 摘要">
            <span><strong>{graph?.counts?.sources ?? 0}</strong> Sources</span>
            <span><strong>{graph?.counts?.claims ?? 0}</strong> Claims</span>
            <span><strong>{graph?.counts?.digest_notes ?? 0}</strong> Digest</span>
            <span><strong>{graph?.counts?.facts ?? 0}</strong> Facts</span>
          </div>
          <div className="graph-dock-actions">
            <button type="button" className={insightsOpen ? "active" : ""} onClick={() => setInsightsOpen((value) => !value)}>
              <Sparkles size={14} />
              Insights
            </button>
            <button type="button" className={controlsOpen ? "active" : ""} onClick={() => setControlsOpen((value) => !value)}>
              {controlsOpen ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
              Controls
            </button>
          </div>
        </div>
        {insightsOpen ? <GraphInsightsPanel graph={graph} onSelectNode={setSelectedNodeId} /> : null}
        {controlsOpen ? (
          <div className="graph-control-body">
            <div className="graph-filterbar" aria-label="Graph 节点类型过滤">
              {typeOptions.map((type) => (
                <button key={type} type="button" className={activeTypes.has(type) ? "active" : ""} onClick={() => toggleType(type)}>
                  {graphTypeLabel(type)}
                </button>
              ))}
            </div>
            <div className="graph-densitybar" aria-label="Graph 数据密度">
              <button
                type="button"
                className={graphLimit === 20 ? "active" : ""}
                onClick={() => {
                  setGraphLimit(20);
                  setSelectedNodeId(null);
                }}
              >
                Overview
              </button>
              <button
                type="button"
                className={graphLimit === 80 ? "active" : ""}
                onClick={() => setGraphLimit(80)}
              >
                Detail
              </button>
              <span>{graphElements.length} visible</span>
            </div>
            <div className="graph-local-search" aria-label="Graph 本地搜索">
              <Search size={15} />
              <input
                value={graphSearch}
                onChange={(event) => setGraphSearch(event.target.value)}
                placeholder="搜索节点、摘要、证据"
              />
              {graphSearch ? <span>{graphSearchMatches.length} 命中</span> : <span>本地图谱</span>}
              <button
                type="button"
                className={neighborhoodOnly ? "active" : ""}
                disabled={!selectedNodeId}
                onClick={() => setNeighborhoodOnly((value) => !value)}
              >
                邻域
              </button>
              <button
                type="button"
                disabled={!graphSearch.trim() || expandStatus === "loading"}
                onClick={() => void handleSearchSubgraph()}
              >
                拉取子图
              </button>
            </div>
            <form className="graph-path-search" onSubmit={(event) => void handleGraphPath(event)} aria-label="GraphRAG 路径查询">
              <Search size={15} />
              <input
                value={pathQuery}
                onChange={(event) => setPathQuery(event.target.value)}
                placeholder="查询 GraphRAG 路径、facts 与证据"
              />
              <div className="graph-path-mode" aria-label="GraphRAG 查询模式">
                <button type="button" className={pathMode === "deterministic" ? "active" : ""} onClick={() => setPathMode("deterministic")}>
                  Direct
                </button>
                <button type="button" className={pathMode === "agentic" ? "active" : ""} onClick={() => setPathMode("agentic")}>
                  Agentic
                </button>
              </div>
              <button type="submit" disabled={pathStatus === "loading"}>
                {pathStatus === "loading" ? "查询中" : pathMode === "agentic" ? "Agentic 问答" : "解释路径"}
              </button>
            </form>
          </div>
        ) : null}
      </div>
      {error ? (
        <div className="review-empty error-state">Graph 无法加载。请检查 8765 后端或服务令牌。</div>
      ) : loading ? (
        <div className="review-empty">正在加载真实 Graph 数据...</div>
      ) : graphElements.length === 0 ? (
        <div className="review-empty">当前没有可视化节点。</div>
      ) : (
        <div className="graph-v2-layout">
          <CytoscapeComponent
            elements={graphElements}
            stylesheet={graphStylesheet}
            layout={cytoscapeLayout}
            className="cytoscape-graph"
            cy={(cy: any) => {
              cy.removeAllListeners("tap");
              cy.on("tap", "node", (event: any) => setSelectedNodeId(event.target.id()));
              cy.on("tap", (event: any) => {
                if (event.target === cy) {
                  setSelectedNodeId(null);
                }
              });
            }}
          />
          <aside className="graph-inspector" aria-label="Graph 节点详情">
            {selectedNode ? (
              <>
                <span className="eyebrow">{graphTypeLabel(selectedNode.type)}</span>
                <h2>{displayText(selectedNode.label || selectedNode.id, "未命名节点")}</h2>
                <p>{displayText(selectedNode.summary, "暂无摘要。")}</p>
                <dl>
                  <div><dt>ID</dt><dd>{selectedNode.object_id || selectedNode.id}</dd></div>
                  <div><dt>类型</dt><dd>{selectedNode.object_type || selectedNode.type}</dd></div>
                  {selectedNode.confidence !== undefined ? <div><dt>置信度</dt><dd>{selectedNode.confidence}</dd></div> : null}
                  {selectedNode.token_estimate !== undefined ? <div><dt>Tokens</dt><dd>{selectedNode.token_estimate}</dd></div> : null}
                </dl>
                <div className="graph-inspector-actions">
                  <button type="button" onClick={() => void handleExpandSelectedNode()} disabled={expandStatus === "loading"}>
                    {expandStatus === "loading" ? "展开中" : "Expand"}
                  </button>
                  <button type="button" onClick={() => setNeighborhoodOnly((value) => !value)}>
                    {neighborhoodOnly ? "显示全图" : "只看邻域"}
                  </button>
                </div>
                {expandError ? <p className="graph-path-warning">{expandError}</p> : null}
                {selectedNode.source_refs?.length ? (
                  <div className="graph-source-refs">
                    <strong>Evidence refs</strong>
                    {selectedNode.source_refs.slice(0, 6).map((ref, index) => (
                      <code key={`${selectedNode.id}-${index}`}>{ref.passage_window_id || ref.chunk_id || ref.document_id || ref.source_item_id}</code>
                    ))}
                  </div>
                ) : null}
                <GraphNeighborhoodPanel graph={graph} selectedNodeId={selectedNodeId} neighborhood={selectedNeighborhood} />
                <GraphEvidencePathPanel evidencePath={selectedEvidencePath} />
              </>
            ) : (
              <>
                <span className="eyebrow">GraphRAG v2</span>
                <h2>选择一个节点</h2>
                <p>点击 digest、claim、hyperedge 或 passage，查看它如何追溯到原文证据。</p>
              </>
            )}
            <GraphPathPanel result={pathResult} status={pathStatus} error={pathError} />
          </aside>
        </div>
      )}
    </section>
  );
}

function GraphInsightsPanel({
  graph,
  onSelectNode
}: {
  graph?: WorkspaceGraphResponse;
  onSelectNode: (nodeId: string) => void;
}) {
  const insights = graph?.insights;
  if (!insights) {
    return null;
  }
  const coverage = insights.layer_coverage || {};
  const health = insights.evidence_health || {};
  const clusters = insights.topic_clusters || [];
  const tour = insights.guided_tour || [];
  const centralNodes = insights.central_nodes || [];
  return (
    <section className="graph-insights" aria-label="Graph Insights">
      <div className="graph-insight-header">
        <div>
          <span className="eyebrow">Graph Insights</span>
          <h2>知识解释器</h2>
        </div>
        <span>{Math.round((health.grounded_ratio ?? 0) * 100)}% grounded</span>
      </div>
      <div className="graph-insight-metrics">
        <span><strong>{coverage.evidence ?? 0}</strong> Evidence</span>
        <span><strong>{coverage.understanding ?? 0}</strong> Understanding</span>
        <span><strong>{coverage.semantic ?? 0}</strong> Semantic</span>
        <span><strong>{coverage.review ?? 0}</strong> Review</span>
      </div>
      {tour.length ? (
        <div className="graph-insight-section">
          <strong>Guided Tour</strong>
          <div className="graph-tour-list">
            {tour.slice(0, 4).map((step, index) => (
              <article key={`tour-${index}`}>
                <b>{trimText(step.title || `Step ${index + 1}`, 86)}</b>
                <small>{trimText(step.reason || "", 145)}</small>
                <div>
                  {(step.node_ids || []).slice(0, 4).map((nodeId, nodeIndex) => (
                    <button key={`${nodeId}-${nodeIndex}`} type="button" onClick={() => onSelectNode(nodeId)}>
                      {trimText(graphNodeLabel(graph, nodeId), 34)}
                    </button>
                  ))}
                </div>
              </article>
            ))}
          </div>
        </div>
      ) : null}
      {clusters.length ? (
        <div className="graph-insight-section">
          <strong>Topic Clusters</strong>
          <div className="graph-cluster-list">
            {clusters.slice(0, 4).map((cluster) => (
              <article key={cluster.cluster_id || cluster.title}>
                <b>{trimText(cluster.title || "Topic cluster", 90)}</b>
                <small>{trimText(cluster.summary || "", 150)}</small>
                <span>{cluster.node_count ?? 0} nodes · {cluster.edge_count ?? 0} edges</span>
                <div>
                  {(cluster.anchor_nodes || []).slice(0, 3).map((node, nodeIndex) => (
                    <button key={`${node.id || "anchor"}-${nodeIndex}`} type="button" onClick={() => node.id && onSelectNode(node.id)}>
                      {graphTypeLabel(node.type || "node")} · {trimText(node.label || node.id || "", 28)}
                    </button>
                  ))}
                </div>
              </article>
            ))}
          </div>
        </div>
      ) : null}
      {centralNodes.length ? (
        <div className="graph-insight-section">
          <strong>Central Nodes</strong>
          <div className="graph-central-list">
            {centralNodes.slice(0, 6).map((node, nodeIndex) => (
              <button key={`${node.id || "central"}-${nodeIndex}`} type="button" onClick={() => node.id && onSelectNode(node.id)}>
                <span>{graphTypeLabel(node.type || "node")}</span>
                <b>{trimText(node.label || node.id || "", 42)}</b>
                <small>{node.degree ?? 0} links</small>
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function GraphPathPanel({
  result,
  status,
  error
}: {
  result: WorkspaceGraphPathResponse | null;
  status: "idle" | "loading" | "success" | "error";
  error: string;
}) {
  if (status === "idle" && !result) {
    return (
      <div className="graph-path-panel">
        <span className="eyebrow">Path Explain</span>
        <p>输入问题后，这里会显示 query seeds、facts、passages 和 citations。</p>
      </div>
    );
  }
  if (status === "loading") {
    return (
      <div className="graph-path-panel">
        <span className="eyebrow">Path Explain</span>
        <p>正在计算 GraphRAG 检索路径...</p>
      </div>
    );
  }
  if (status === "error" && !result) {
    return (
      <div className="graph-path-panel error-state">
        <span className="eyebrow">Path Explain</span>
        <p>{error || "Graph path 查询失败。"}</p>
      </div>
    );
  }

  const seeds = result?.query_seeds;
  const facts = result?.top_facts || [];
  const filteredFacts = result?.filtered_out_facts || [];
  const passages = result?.supporting_passages || [];
  const citations = result?.citations || [];
  const graphPaths = result?.graph_paths || [];
  const expansionDecisions = result?.agentic_trace?.expansion_decisions || [];
  return (
    <div className="graph-path-panel">
      <span className="eyebrow">Path Explain</span>
      <h3>{displayText(result?.query, "GraphRAG 查询")}</h3>
      <p>{result?.answer || result?.path_summary?.summary || "暂无路径摘要。"}</p>
      {error ? <p className="graph-path-warning">{error}</p> : null}
      {result?.mode || result?.agentic_service ? (
        <div className="graph-path-run">
          <span>{displayText(result.mode, "deterministic")}</span>
          {result.requires_agentic_service_online ? <span>FastReAct required</span> : <span>direct retrieval</span>}
          {result.display_mode ? <span>{displayText(result.display_mode)}</span> : null}
          {result.agentic_service ? <span>{displayText(result.agentic_service.provider || result.agentic_service.adapter || result.agentic_service.run_id, "agentic service")}</span> : null}
        </div>
      ) : null}
      <div className="graph-path-metrics">
        <span><strong>{seeds?.terms?.length ?? 0}</strong> Seeds</span>
        <span><strong>{facts.length}</strong> Facts</span>
        <span><strong>{passages.length}</strong> Passages</span>
        <span><strong>{citations.length}</strong> Citations</span>
        <span><strong>{filteredFacts.length}</strong> Filtered</span>
        <span><strong>{graphPaths.length}</strong> Paths</span>
      </div>
      {result?.path_summary?.filter_mode ? (
        <p className="graph-path-filter-note">
          {displayText(result.path_summary.filter_mode)} · kept {result.path_summary.kept_fact_count ?? facts.length} · filtered {result.path_summary.filtered_fact_count ?? filteredFacts.length}
        </p>
      ) : null}
      {result?.agentic_repair?.attempted ? (
        <p className={result.agentic_repair.accepted ? "graph-path-repair-note" : "graph-path-warning"}>
          Repair {result.agentic_repair.accepted ? "accepted" : "not accepted"} · {displayText(result.agentic_repair.final_answer_mode || result.display_mode || result.mode)}
          {result.agentic_repair.repaired_answer_chars ? ` · ${result.agentic_repair.repaired_answer_chars} chars` : ""}
        </p>
      ) : null}
      {expansionDecisions.length ? (
        <div className="graph-path-section">
          <strong>Agentic Expansion</strong>
          {expansionDecisions.slice(0, 4).map((decision, index) => (
            <article key={`expansion-${index}`}>
              <b>{trimText(decision.target || decision.action || decision.type || `decision ${index + 1}`, 92)}</b>
              <small>{trimText(decision.decision || decision.reason || decision.summary || decision, 140)}</small>
            </article>
          ))}
        </div>
      ) : null}
      {result?.agentic_trace?.evidence_check ? (
        <div className="graph-path-section">
          <strong>Evidence Check</strong>
          <article>
            <small>{trimText(result.agentic_trace.evidence_check, 180)}</small>
          </article>
        </div>
      ) : null}
      {seeds?.terms?.length ? (
        <div className="graph-path-section">
          <strong>Query Seeds</strong>
          <p>{seeds.terms.join(" · ")}</p>
        </div>
      ) : null}
      {facts.length ? (
        <div className="graph-path-section">
          <strong>Top Facts</strong>
          {facts.slice(0, 4).map((fact, index) => (
            <article key={`fact-${index}`}>
              <b>{trimText(fact.statement || fact.summary || fact.explanation || fact.fact_id, 92)}</b>
              <small>{trimText(fact.filter_reason || fact.why_it_matters || fact.relation_type || fact.fact_id, 120)}</small>
            </article>
          ))}
        </div>
      ) : null}
      {filteredFacts.length ? (
        <div className="graph-path-section subdued">
          <strong>Filtered Facts</strong>
          {filteredFacts.slice(0, 3).map((fact, index) => (
            <article key={`filtered-fact-${index}`}>
              <b>{trimText(fact.statement || fact.summary || fact.explanation || fact.fact_id, 92)}</b>
              <small>{trimText(fact.filter_reason || "Filtered by relevance check", 120)}</small>
            </article>
          ))}
        </div>
      ) : null}
      {passages.length ? (
        <div className="graph-path-section">
          <strong>Supporting Passages</strong>
          {passages.slice(0, 4).map((passage, index) => (
            <article key={`passage-${index}`}>
              <b>{trimText(passage.title || passage.source_item_id || passage.result_id, 82)}</b>
              <small>{trimText(passage.snippet, 140)}</small>
            </article>
          ))}
        </div>
      ) : null}
      {graphPaths.length ? (
        <div className="graph-path-section">
          <strong>Graph Paths</strong>
          {graphPaths.slice(0, 3).map((path, index) => (
            <article key={`graph-path-${index}`}>
              <b>{trimText(path.explanation || path.path_id || "graph path", 110)}</b>
              <small>{trimText(graphPathMeta(path), 140)}</small>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function GraphNeighborhoodPanel({
  graph,
  selectedNodeId,
  neighborhood
}: {
  graph?: WorkspaceGraphResponse;
  selectedNodeId: string | null;
  neighborhood: GraphNeighborhood;
}) {
  if (!selectedNodeId) {
    return null;
  }
  if (neighborhood.edges.length === 0) {
    return (
      <div className="graph-neighborhood-panel">
        <strong>Local Connections</strong>
        <p>这个节点当前没有可见的一跳连接。</p>
      </div>
    );
  }
  const nodeById = new Map((graph?.nodes || []).map((node) => [node.id, node]));
  return (
    <div className="graph-neighborhood-panel">
      <strong>Local Connections</strong>
      <div className="graph-neighborhood-metrics">
        <span><b>{neighborhood.neighborIds.size}</b> neighbors</span>
        <span><b>{neighborhood.edges.length}</b> edges</span>
      </div>
      <div className="graph-neighborhood-list">
        {neighborhood.edges.slice(0, 8).map((edge) => {
          const neighborId = edge.source === selectedNodeId ? edge.target : edge.source;
          const neighbor = nodeById.get(neighborId);
          return (
            <article key={edge.id}>
              <span>{edge.source === selectedNodeId ? "out" : "in"} · {edge.label || edge.type}</span>
              <b>{trimText(neighbor?.label || neighborId, 80)}</b>
              <small>{graphTypeLabel(neighbor?.type || "node")} · {trimText(neighbor?.summary || edge.type || "", 110)}</small>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function GraphEvidencePathPanel({ evidencePath }: { evidencePath: GraphEvidencePath }) {
  if (!evidencePath.selectedNodeId) {
    return null;
  }
  if (evidencePath.nodes.length <= 1 && evidencePath.edges.length === 0) {
    return (
      <div className="graph-evidence-panel">
        <strong>Evidence Path</strong>
        <p>这个节点当前还没有可追溯的证据链。</p>
      </div>
    );
  }
  const evidenceNodes = evidencePath.nodes.filter((node) => ["source", "document", "passage"].includes(node.type));
  const understandingNodes = evidencePath.nodes.filter((node) => ["claim", "digest", "phrase", "fact", "hyperedge", "memory", "memory_suggestion", "action"].includes(node.type));
  return (
    <div className="graph-evidence-panel">
      <strong>Evidence Path</strong>
      <div className="graph-evidence-metrics">
        <span><b>{evidenceNodes.length}</b> evidence</span>
        <span><b>{understandingNodes.length}</b> understanding</span>
        <span><b>{evidencePath.edges.length}</b> links</span>
      </div>
      {evidenceNodes.length ? (
        <div className="graph-evidence-list">
          <span>来自哪里</span>
          {evidenceNodes.slice(0, 6).map((node) => (
            <article key={`evidence-${node.id}`}>
              <b>{trimText(node.label || node.id, 86)}</b>
              <small>{graphTypeLabel(node.type)} · {trimText(node.summary || node.object_id || "", 118)}</small>
            </article>
          ))}
        </div>
      ) : null}
      {understandingNodes.length ? (
        <div className="graph-evidence-list">
          <span>产生了什么理解</span>
          {understandingNodes.slice(0, 6).map((node) => (
            <article key={`understanding-${node.id}`}>
              <b>{trimText(node.label || node.id, 86)}</b>
              <small>{graphTypeLabel(node.type)} · {trimText(node.summary || node.object_id || "", 118)}</small>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function graphPathMeta(path: Record<string, unknown>) {
  const depth = displayText(path.depth, "");
  const score = typeof path.score === "number" ? `score ${path.score.toFixed(2)}` : "";
  const edgeCount = Array.isArray(path.edges) ? `${path.edges.length} edges` : "";
  return [depth ? `depth ${depth}` : "", score, edgeCount].filter(Boolean).join(" · ") || "Graph traversal path";
}

function mergeGraphResponses(
  base: WorkspaceGraphResponse | null | undefined,
  extra: WorkspaceGraphResponse | null | undefined
): WorkspaceGraphResponse | null {
  if (!base && !extra) {
    return null;
  }
  if (!base) {
    return extra || null;
  }
  if (!extra) {
    return base;
  }
  const nodesById = new Map<string, WorkspaceGraphNode>();
  for (const node of [...(base.nodes || []), ...(extra.nodes || [])]) {
    nodesById.set(node.id, node);
  }
  const edgesById = new Map<string, WorkspaceGraphEdge>();
  for (const edge of [...(base.edges || []), ...(extra.edges || [])]) {
    edgesById.set(edge.id, edge);
  }
  return {
    ...base,
    nodes: Array.from(nodesById.values()),
    edges: Array.from(edgesById.values()),
    projection: {
      ...(base.projection || {}),
      nodes: nodesById.size,
      edges: edgesById.size
    }
  };
}

const graphStylesheet = [
  {
    selector: "node",
    style: {
      "background-color": "data(color)",
      label: "data(label)",
      color: "#1c2520",
      "font-size": 10,
      "text-wrap": "wrap",
      "text-max-width": 110,
      "border-width": 1,
      "border-color": "#f8f3e8",
      width: "data(size)",
      height: "data(size)"
    }
  },
  {
    selector: "edge",
    style: {
      width: 1.5,
      "line-color": "#9d988d",
      "target-arrow-color": "#9d988d",
      "target-arrow-shape": "triangle",
      "curve-style": "bezier",
      label: "data(label)",
      "font-size": 8,
      color: "#58625b",
      "text-background-color": "#f8f3e8",
      "text-background-opacity": 0.8,
      "text-background-padding": 2
    }
  },
  {
    selector: "node:selected",
    style: {
      "border-width": 4,
      "border-color": "#1f8f6a"
    }
  },
  {
    selector: "node.search-match",
    style: {
      "border-width": 4,
      "border-color": "#d7a63e",
      "background-blacken": -0.08
    }
  },
  {
    selector: "node.focus-node",
    style: {
      "border-width": 5,
      "border-color": "#1f8f6a",
      "font-size": 12
    }
  },
  {
    selector: "node.evidence-path-node",
    style: {
      "border-width": 4,
      "border-color": "#315f7c",
      "background-blacken": -0.05
    }
  },
  {
    selector: "edge.neighborhood-edge",
    style: {
      width: 2.6,
      "line-color": "#1f8f6a",
      "target-arrow-color": "#1f8f6a"
    }
  },
  {
    selector: "edge.evidence-path-edge",
    style: {
      width: 3.1,
      "line-color": "#315f7c",
      "target-arrow-color": "#315f7c",
      "font-size": 9
    }
  }
];

const graphLayout = {
  name: "cose",
  animate: false,
  fit: true,
  padding: 72,
  nodeRepulsion: 18000,
  idealEdgeLength: 230,
  componentSpacing: 140,
  nodeOverlap: 20
};

function graphLayoutForElementCount(elementCount: number) {
  if (elementCount > 700) {
    return {
      name: "grid",
      animate: false,
      fit: true,
      padding: 60,
      avoidOverlap: true
    };
  }
  if (elementCount > 380) {
    return {
      ...graphLayout,
      nodeRepulsion: 10000,
      idealEdgeLength: 170,
      componentSpacing: 90,
      padding: 54
    };
  }
  return graphLayout;
}

type GraphNeighborhood = {
  nodeIds: Set<string>;
  neighborIds: Set<string>;
  edges: WorkspaceGraphEdge[];
};

type GraphEvidencePath = {
  selectedNodeId: string | null;
  nodeIds: Set<string>;
  edgeIds: Set<string>;
  nodes: WorkspaceGraphNode[];
  edges: WorkspaceGraphEdge[];
};

function graphToCytoscapeElements(
  graph: WorkspaceGraphResponse | undefined,
  activeTypes: Set<string>,
  searchText = "",
  focusNodeId: string | null = null,
  evidencePath: GraphEvidencePath | null = null
) {
  const nodes = (graph?.nodes || []).filter((node) => activeTypes.has(node.type));
  const searchMatches = new Set(graphSearchResultNodes(graph, activeTypes, searchText).map((node) => node.id));
  const neighborhood = graphNodeNeighborhood(graph, focusNodeId);
  const visibleNodeIds = focusNodeId ? neighborhood.nodeIds : new Set(nodes.map((node) => node.id));
  const visibleEdgeIds = focusNodeId ? new Set(neighborhood.edges.map((edge) => edge.id)) : null;
  const filteredNodes = nodes.filter((node) => visibleNodeIds.has(node.id));
  const nodeIds = new Set(filteredNodes.map((node) => node.id));
  const elements: Array<{ data: Record<string, unknown>; classes?: string }> = filteredNodes.map((node) => ({
    data: {
      id: node.id,
      label: trimText(displayText(node.label || node.id, "node"), 36),
      color: graphNodeColor(node.type),
      size: graphNodeSize(node.type)
    },
    classes: [
      node.type,
      searchMatches.has(node.id) ? "search-match" : "",
      focusNodeId === node.id ? "focus-node" : "",
      evidencePath?.nodeIds.has(node.id) ? "evidence-path-node" : ""
    ].filter(Boolean).join(" ")
  }));
  for (const edge of graph?.edges || []) {
    if (visibleEdgeIds && !visibleEdgeIds.has(edge.id)) {
      continue;
    }
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
      continue;
    }
    elements.push({
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label: trimText(edge.label || edge.type || "", 24),
        color: "#9d988d",
        size: 1
      },
      classes: [
        edge.type || "edge",
        focusNodeId ? "neighborhood-edge" : "",
        evidencePath?.edgeIds.has(edge.id) ? "evidence-path-edge" : ""
      ].filter(Boolean).join(" ")
    });
  }
  return elements;
}

function graphSearchResultNodes(graph: WorkspaceGraphResponse | undefined, activeTypes: Set<string>, searchText: string) {
  const query = searchText.trim().toLowerCase();
  if (!query) {
    return [];
  }
  return (graph?.nodes || [])
    .filter((node) => activeTypes.has(node.type))
    .filter((node) => graphSearchHaystack(node).includes(query))
    .slice(0, 50);
}

function graphSearchHaystack(node: WorkspaceGraphNode) {
  return corpusText([node.id, node.type, node.label, node.summary, node.object_type, node.object_id, JSON.stringify(node.source_refs || [])]);
}

function graphNodeLabel(graph: WorkspaceGraphResponse | undefined, nodeId: string) {
  const node = (graph?.nodes || []).find((item) => item.id === nodeId);
  return node?.label || node?.object_id || nodeId;
}

function graphNodeNeighborhood(graph: WorkspaceGraphResponse | undefined, selectedNodeId: string | null): GraphNeighborhood {
  const nodeIds = new Set<string>();
  const neighborIds = new Set<string>();
  const edges: WorkspaceGraphEdge[] = [];
  if (!graph || !selectedNodeId) {
    return { nodeIds, neighborIds, edges };
  }
  nodeIds.add(selectedNodeId);
  for (const edge of graph.edges || []) {
    if (edge.source !== selectedNodeId && edge.target !== selectedNodeId) {
      continue;
    }
    edges.push(edge);
    const neighborId = edge.source === selectedNodeId ? edge.target : edge.source;
    neighborIds.add(neighborId);
    nodeIds.add(neighborId);
  }
  return { nodeIds, neighborIds, edges };
}

function graphEvidencePath(graph: WorkspaceGraphResponse | undefined, selectedNodeId: string | null): GraphEvidencePath {
  const empty = {
    selectedNodeId,
    nodeIds: new Set<string>(),
    edgeIds: new Set<string>(),
    nodes: [] as WorkspaceGraphNode[],
    edges: [] as WorkspaceGraphEdge[]
  };
  if (!graph || !selectedNodeId) {
    return empty;
  }
  const nodeById = new Map((graph.nodes || []).map((node) => [node.id, node]));
  if (!nodeById.has(selectedNodeId)) {
    return empty;
  }
  const evidenceLabels = new Set([
    "contains",
    "grounds",
    "summarizes",
    "formalizes",
    "member",
    "evidence",
    "remembered_from",
    "needs_review_from",
    "suggests",
    "suggests_relationship",
    "represented_by",
    "participates_in",
    "mentions",
    "links_to"
  ]);
  const adjacency = new Map<string, WorkspaceGraphEdge[]>();
  for (const edge of graph.edges || []) {
    const key = (edge.label || edge.type || "").toLowerCase();
    if (!evidenceLabels.has(key)) {
      continue;
    }
    adjacency.set(edge.source, [...(adjacency.get(edge.source) || []), edge]);
    adjacency.set(edge.target, [...(adjacency.get(edge.target) || []), edge]);
  }
  const nodeIds = new Set<string>([selectedNodeId]);
  const edgeIds = new Set<string>();
  const selectedEdges: WorkspaceGraphEdge[] = [];
  const queue: Array<{ nodeId: string; depth: number }> = [{ nodeId: selectedNodeId, depth: 0 }];
  const maxDepth = 5;
  const maxNodes = 44;
  const maxEdges = 70;
  while (queue.length && nodeIds.size < maxNodes && edgeIds.size < maxEdges) {
    const current = queue.shift();
    if (!current || current.depth >= maxDepth) {
      continue;
    }
    for (const edge of adjacency.get(current.nodeId) || []) {
      if (edgeIds.size >= maxEdges) {
        break;
      }
      const nextNodeId = edge.source === current.nodeId ? edge.target : edge.source;
      if (!nodeById.has(nextNodeId)) {
        continue;
      }
      if (!edgeIds.has(edge.id)) {
        edgeIds.add(edge.id);
        selectedEdges.push(edge);
      }
      if (!nodeIds.has(nextNodeId) && nodeIds.size < maxNodes) {
        nodeIds.add(nextNodeId);
        queue.push({ nodeId: nextNodeId, depth: current.depth + 1 });
      }
    }
  }
  const typeRank: Record<string, number> = {
    source: 0,
    document: 1,
    passage: 2,
    claim: 3,
    phrase: 4,
    fact: 5,
    hyperedge: 6,
    digest: 7,
    memory: 8,
    memory_suggestion: 7,
    action: 8,
    entity: 9
  };
  const pathNodes = Array.from(nodeIds)
    .map((nodeId) => nodeById.get(nodeId))
    .filter((node): node is WorkspaceGraphNode => Boolean(node))
    .sort((left, right) => (typeRank[left.type] ?? 99) - (typeRank[right.type] ?? 99));
  return { selectedNodeId, nodeIds, edgeIds, nodes: pathNodes, edges: selectedEdges };
}

function graphNodeColor(type: string) {
  const colors: Record<string, string> = {
    source: "#5a7d9a",
    document: "#7b8c53",
    passage: "#d5a03a",
    claim: "#ba6b57",
    digest: "#8f6fc8",
    phrase: "#66835d",
    entity: "#4f9d7a",
    fact: "#315f7c",
    hyperedge: "#c15472",
    memory: "#3f7d90",
    memory_suggestion: "#b78246",
    action: "#9a7050"
  };
  return colors[type] || "#6c756f";
}

function graphNodeSize(type: string) {
  return type === "fact" || type === "hyperedge" || type === "digest" ? 42 : type === "source" || type === "document" ? 38 : 32;
}

function graphTypeLabel(type: string) {
  const labels: Record<string, string> = {
    source: "Source",
    document: "Document",
    passage: "Passage",
    claim: "Claim",
    digest: "Digest",
    phrase: "Phrase",
    entity: "Entity",
    fact: "Fact",
    hyperedge: "Hyperedge",
    memory: "Memory",
    memory_suggestion: "Memory Suggestion",
    action: "Action"
  };
  return labels[type] || type;
}

function buildWorkspaceGraph(corpus?: WorkspaceCorpusResponse, today?: TodayResponse) {
  const lanes = {
    sources: 80,
    chunks: 430,
    discoveries: 790,
    reviews: 1150,
    hyperedges: 1510,
    entities: 1870
  };
  const laneRows: Record<keyof typeof lanes, number> = {
    sources: 0,
    chunks: 0,
    discoveries: 0,
    reviews: 0,
    hyperedges: 0,
    entities: 0
  };
  const top = 130;
  const rowGap = 185;
  const nodes: Array<{
    id: string;
    type: string;
    position: { x: number; y: number };
    data: { title: string; body: string; icon: "text" | "doc" | "image" | "link"; kind: string };
    draggable: boolean;
  }> = [];
  const edges: Array<{ id: string; source: string; target: string; label?: string; animated?: boolean }> = [];
  const seenNodes = new Set<string>();
  const seenEdges = new Set<string>();
  const sources = (corpus?.sources || []).slice(0, 10);
  const chunks = (corpus?.chunks || []).slice(0, 18);
  const discoveries = (today?.discoveries || []).slice(0, 10);
  const reviews = (today?.needs_review || []).slice(0, 10);
  const hyperedges = (corpus?.hyperedges || []).slice(0, 10);

  function addNode(id: string, title: string, body: string, lane: keyof typeof lanes, icon: "text" | "doc" | "image" | "link" = "text") {
    if (seenNodes.has(id)) {
      return;
    }
    seenNodes.add(id);
    const position = { x: lanes[lane], y: top + laneRows[lane] * rowGap };
    laneRows[lane] += 1;
    nodes.push({
      id,
      type: "pskaCard",
      position,
      draggable: true,
      data: { title: trimText(displayText(title, "未命名节点"), 72), body: trimText(displayText(body, "暂无摘要"), 160), icon, kind: lane }
    });
  }

  function addEdge(source: string, target: string, label: string, animated = false) {
    if (!seenNodes.has(source) || !seenNodes.has(target)) {
      return;
    }
    const id = `${source}->${target}:${label}`;
    if (seenEdges.has(id)) {
      return;
    }
    seenEdges.add(id);
    edges.push({ id, source, target, label, animated });
  }

  sources.forEach((source, index) => {
    addNode(
      graphSourceNodeId(source.source_item_id || `source-${index}`),
      source.title || source.source_item_id || "Source",
      `${source.source_channel || "source"} / ${source.chunk_count || 0} chunks`,
      "sources",
      "doc"
    );
  });

  chunks.forEach((chunk, index) => {
    const sourceKey = chunk.source_item_id || "";
    const chunkId = graphChunkNodeId(chunk.chunk_id || `chunk-${index}`);
    addNode(chunkId, chunk.title || chunk.source_item_id || "Chunk", chunk.snippet || chunk.text || "Corpus chunk", "chunks", "text");
    if (sourceKey) {
      addEdge(graphSourceNodeId(sourceKey), chunkId, "contains");
    }
  });

  discoveries.forEach((discovery, index) => {
    const discoveryId = graphDiscoveryNodeId(discovery.id || `discovery-${index}`);
    addNode(discoveryId, discovery.title, `${discovery.type || "discovery"} / ${discoveryQualityLabel(discovery)}`, "discoveries", "image");
    for (const ref of discoverySourceRefs(discovery)) {
      if (ref.source_item_id) {
        addEdge(graphSourceNodeId(ref.source_item_id), discoveryId, "evidence", true);
      }
    }
    if (discovery.review_item_id) {
      const reviewId = graphReviewNodeId(discovery.review_item_id);
      addNode(reviewId, discovery.review_item_id, "Linked review candidate", "reviews", "link");
      addEdge(discoveryId, reviewId, "requires review", true);
    }
  });

  reviews.forEach((review, index) => {
    const reviewId = graphReviewNodeId(review.review_item_id);
    addNode(reviewId, review.title, `${review.review_type || "review"} / ${review.source_ref_status || "source refs"}`, "reviews", "link");
    for (const ref of review.source_refs || []) {
      if (ref.source_item_id) {
        addEdge(graphSourceNodeId(ref.source_item_id), reviewId, "grounds");
      }
    }
  });

  hyperedges.forEach((edge, index) => {
    const hyperedgeId = graphHyperedgeNodeId(edge.hyperedge_id || `hyperedge-${index}`);
    addNode(hyperedgeId, edge.relation_type || edge.label || "Hyperedge", edge.evidence_text || edge.summary || `${edge.members?.length || 0} members`, "hyperedges", "link");
    for (const member of edge.members || []) {
      const entityId = graphEntityNodeId(member.entity_id || member.label || `entity-${index}`);
      addNode(entityId, member.label || member.entity_id || "Entity", member.entity_type || member.role || "entity", "entities", "text");
      addEdge(entityId, hyperedgeId, member.role || "member");
    }
    for (const ref of edge.source_refs || []) {
      if (ref.source_item_id) {
        addEdge(graphSourceNodeId(ref.source_item_id), hyperedgeId, "evidence");
      }
    }
  });

  return {
    nodes,
    edges,
    counts: {
      sources: corpus?.counts?.sources_total ?? sources.length,
      chunks: today?.system?.source_counts?.chunks ?? corpus?.counts?.chunks_matching ?? chunks.length,
      discoveries: today?.discoveries?.length ?? discoveries.length,
      reviews: today?.system?.pending_reviews?.total_matching ?? today?.needs_review?.length ?? reviews.length,
      hyperedges: corpus?.counts?.hyperedges ?? hyperedges.length
    }
  };
}

function discoverySourceRefs(discovery: TodayDiscoveryItem) {
  const refs: Array<{ source_item_id?: string; chunk_id?: string; title?: string; url?: string }> = [];
  for (const evidence of [...(discovery.evidence || []), ...(discovery.evidence_snapshot || [])]) {
    if (evidence.source_item_id) {
      refs.push({ source_item_id: String(evidence.source_item_id) });
    }
    const sourceRefs = Array.isArray(evidence.source_refs) ? evidence.source_refs : [];
    for (const ref of sourceRefs) {
      if (ref && typeof ref === "object" && "source_item_id" in ref) {
        refs.push({ source_item_id: String(ref.source_item_id || "") });
      }
    }
  }
  return refs.filter((ref, index, all) => ref.source_item_id && all.findIndex((item) => item.source_item_id === ref.source_item_id) === index);
}

function graphSourceNodeId(id: string) {
  return `source:${id}`;
}

function graphChunkNodeId(id: string) {
  return `chunk:${id}`;
}

function graphDiscoveryNodeId(id: string) {
  return `discovery:${id}`;
}

function graphReviewNodeId(id: string) {
  return `review:${id}`;
}

function graphEntityNodeId(id: string) {
  return `entity:${id}`;
}

function graphHyperedgeNodeId(id: string) {
  return `hyperedge:${id}`;
}

function CanvasWorkspace({
  brain,
  onPinCurrent,
  pinStatus
}: {
  brain: BrainState;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
}) {
  const nodes = useMemo(
    () =>
      brain.entities.slice(0, 8).map((entity, index) => ({
        id: `entity-${index}`,
        type: "pskaCard",
        position: { x: 80 + (index % 3) * 280, y: 80 + Math.floor(index / 3) * 190 },
        data: { title: entity, icon: "text", body: "来自真实 PSKA 语料或检索上下文。" }
      })),
    [brain.entities]
  );
  const edges = useMemo(
    () =>
      brain.connections
        .slice(0, Math.max(0, nodes.length - 1))
        .map((connection, index) => ({
          id: `edge-${index}`,
          source: nodes[index]?.id,
          target: nodes[index + 1]?.id,
          label: connection.relation
        }))
        .filter((edge) => edge.source && edge.target),
    [brain.connections, nodes]
  );

  return (
    <section className="main-workspace canvas-surface" aria-label="画布工作区">
      <div className="canvas-toolbar">
        <button type="button" onClick={onPinCurrent}>
          <Pin size={15} />
          {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "置顶失败" : "置顶画布"}
        </button>
      </div>
      {nodes.length === 0 ? (
        <div className="review-empty">当前没有可绘制的真实实体。刷新上下文或导入更多资料后会显示画布。</div>
      ) : (
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView>
          <Background gap={24} color="#d8d6cc" />
          <MiniMap pannable zoomable />
          <Controls />
        </ReactFlow>
      )}
    </section>
  );
}

function CanvasCardNode({ data }: NodeProps<{ title: string; body: string; icon: "text" | "doc" | "image" | "link"; kind?: string }>) {
  const Icon = data.icon === "image" ? Image : data.icon === "link" ? Link2 : data.icon === "doc" ? FileText : TextCursorInput;
  return (
    <div className={`canvas-card ${data.kind ? `canvas-card-${data.kind}` : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div className="canvas-card-title">
        <Icon size={17} />
        {data.title}
      </div>
      <p>{data.body}</p>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function BrainSidebar({ brain, onRefresh }: { brain: BrainState; onRefresh: () => void }) {
  return (
    <aside className="brain-sidebar">
      <div className="brain-header">
        <div>
          <Brain size={20} />
          <strong>PSKA 大脑</strong>
        </div>
        <button className="icon-button" type="button" onClick={onRefresh} title="刷新上下文">
          <RefreshCw size={17} />
        </button>
      </div>
      <div className={`brain-status ${brain.status}`}>
        <span>{brain.status === "analyzing" ? "正在分析上下文" : brain.status === "synced" ? "已与 PSKA 同步" : brain.status === "error" ? "PSKA 请求失败" : "等待真实上下文"}</span>
        <small>触发：{triggerLabel(brain.lastTrigger)}</small>
      </div>
      {brain.error && <div className="review-empty error-state">{displayText(brain.error)}</div>}
      <BrainPanel title="相关知识">
        <div className="knowledge-list">
          {brain.relatedKnowledge.length === 0 ? (
            <div className="review-empty">暂无真实检索结果。</div>
          ) : brain.relatedKnowledge.map((item) => (
            <button className="knowledge-item" type="button" key={item.id}>
              <span>
                <strong>{displayText(item.title, "相关知识")}</strong>
                <small>{typeof item.score === "number" ? `匹配度：${item.score}%` : displayText(item.source, "未评分")}</small>
              </span>
              <p>{displayText(item.snippet, "暂无摘要")}</p>
            </button>
          ))}
        </div>
      </BrainPanel>
      <BrainPanel title="实体">
        <div className="tag-cloud">
          {brain.entities.length === 0 ? (
            <span>暂无实体</span>
          ) : brain.entities.map((entity) => (
            <span key={displayText(entity)}>
              <Tag size={13} />
              {displayText(entity)}
            </span>
          ))}
        </div>
      </BrainPanel>
      <BrainPanel title="上下文时间线">
        <div className="timeline">
          {brain.timeline.length === 0 ? (
            <div className="review-empty">暂无真实来源时间线。</div>
          ) : brain.timeline.map((item) => (
            <div className="timeline-item" key={item.id}>
              <small>{displayText(item.age)}</small>
              <strong>{displayText(item.title, "时间线事件")}</strong>
              <p>{displayText(item.detail, "暂无详情")}</p>
            </div>
          ))}
        </div>
      </BrainPanel>
      <BrainPanel title="建议连接">
        <div className="connections">
          {brain.connections.length === 0 ? (
            <div className="review-empty">暂无真实关系建议。</div>
          ) : brain.connections.map((item) => (
            <div key={item.id}>
              <span>{displayText(item.relation, "相关")}</span>
              <strong>{displayText(item.label, "建议连接")}</strong>
            </div>
          ))}
        </div>
      </BrainPanel>
    </aside>
  );
}

function triggerLabel(trigger: BrainState["lastTrigger"]) {
  if (trigger === "pause") {
    return "暂停";
  }
  if (trigger === "blur") {
    return "失焦";
  }
  if (trigger === "significant-change") {
    return "明显变更";
  }
  return "手动刷新";
}

function workspaceActivityTarget(surface: WorkspaceMode) {
  if (surface === "review") {
    return { title: "Review Center", summary: "查看真实 Review 队列。" };
  }
  if (surface === "canvas") {
    return { title: "画布工作区", summary: "查看画布工作区。" };
  }
  if (surface === "graph") {
    return { title: "Graph 工作区", summary: "查看真实 PSKA 图谱与候选关系。" };
  }
  if (surface === "corpus") {
    return { title: "语料库", summary: "查看已同步资料、可检索片段和资料位置。" };
  }
  if (surface === "document") {
    return { title: "文档工作区", summary: "查看文档工作区。" };
  }
  return { title: "Today", summary: "查看 Today 工作区。" };
}

function BrainPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="brain-panel">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function toDocumentHtml(text: string) {
  if (!text.trim()) {
    return "";
  }
  return text
    .split(/\n{2,}/)
    .map((block, index) => {
      if (index === 0) {
        return `<h1>${escapeHtml(block)}</h1>`;
      }
      return `<p>${escapeHtml(block).replace(/\n/g, "<br>")}</p>`;
    })
    .join("");
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
