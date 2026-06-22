import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
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
  ignoreDiscovery,
  loadCorpusContext,
  loadCorpusData,
  loadReviewCenter,
  loadSourcesConsole,
  loadToday,
  recordWorkspaceActivity,
  rejectReviewItem,
  searchWorkspace,
  snoozeDiscovery
} from "./api";
import { useWorkspaceStore } from "./store";
import type {
  BrainState,
  ConsoleSourceChannelStats,
  ConsoleSourcesResponse,
  ReviewCenterItem,
  TodayContinueItem,
  TodayDiscoveryItem,
  TodayResponse,
  TodayReviewItem,
  WorkspaceCorpusResponse,
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
    brain,
    setMode,
    toggleLeft,
    setDocumentText,
    setSelectedText,
    setServiceToken,
    setBrain
  } = useWorkspaceStore();
  const lastAnalyzedText = useRef(documentText);
  const lastEditedActivityAt = useRef(0);
  const [pinStatus, setPinStatus] = useState<"idle" | "saved" | "failed">("idle");

  const corpusQuery = useQuery({
    queryKey: ["corpus-context", serviceToken],
    queryFn: () => loadCorpusContext(serviceToken),
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
  }, [documentText, serviceToken]);

  useEffect(() => {
    void logWorkspaceActivity("opened", mode);
    void logWorkspaceActivity("viewed", mode);
  }, [mode, serviceToken]);

  async function runAnalysis(trigger: BrainState["lastTrigger"], text = documentText) {
    const query = selectedText || text;
    if (!query.trim()) {
      setBrain({ status: "idle", lastTrigger: trigger, updatedAt: Date.now(), error: null });
      return;
    }
    setBrain({ status: "analyzing", lastTrigger: trigger, error: null });
    try {
      const payload = await analyzeWorkspaceContext(query, serviceToken, trigger);
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
    if (!serviceToken) {
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
      await recordWorkspaceActivity(serviceToken, {
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
          onModeChange={setMode}
          onTokenChange={setServiceToken}
          onRefresh={refreshCurrentSurface}
        />
        {mode === "today" ? (
          <TodayWorkspace serviceToken={serviceToken} onOpenWorkspace={setMode} setBrain={setBrain} />
        ) : mode === "review" ? (
          <ReviewCenter serviceToken={serviceToken} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : mode === "graph" ? (
          <GraphWorkspace serviceToken={serviceToken} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : mode === "corpus" ? (
          <CorpusWorkspace serviceToken={serviceToken} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} setBrain={setBrain} />
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
  onModeChange,
  onTokenChange,
  onRefresh
}: {
  mode: WorkspaceMode;
  serviceToken: string;
  onModeChange: (mode: WorkspaceMode) => void;
  onTokenChange: (serviceToken: string) => void;
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
      <label className="token-field">
        <span>服务令牌</span>
        <input
          type="password"
          value={serviceToken}
          onChange={(event) => onTokenChange(event.target.value)}
          placeholder="可选本地令牌"
        />
      </label>
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
  serviceToken: string;
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
      const result = await searchWorkspace(query, serviceToken, "agentic");
      setSearchResult(result);
      setBrain(searchToBrain(result, query));
      if (result.error) {
        setSearchError(result.error);
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
          <SectionTitle icon={<Search size={18} />} title="Ask PSKA" subtitle="走 FastReAct agentic 检索" />
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
                      <h4>{source.title || source.source_item_id || "未命名来源"}</h4>
                      {source.url ? <p>{source.url}</p> : null}
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
                        <small>{chunk.title || chunk.source_item_id || "片段"}</small>
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

type ReviewActionState = "处理中" | "已批准" | "已批准并应用" | "已拒绝" | "已应用" | "操作失败";

function ReviewCenter({
  serviceToken,
  onPinCurrent,
  pinStatus
}: {
  serviceToken: string;
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
      if (action === "reject") {
        await rejectReviewItem(serviceToken, item.review_item_id);
        mark(item.review_item_id, "已拒绝");
      } else if (action === "apply") {
        await applyReviewItem(serviceToken, item.review_item_id);
        mark(item.review_item_id, "已应用");
      } else {
        await approveReviewItem(serviceToken, item.review_item_id, action === "approve_apply");
        mark(item.review_item_id, action === "approve_apply" ? "已批准并应用" : "已批准");
      }
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
                  <h2>{item.title || item.review_item_id}</h2>
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
                      <span key={`${item.review_item_id}-${index}`}>{ref.title || ref.source_item_id || ref.chunk_id || "source ref"}</span>
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

function normalizeContinueItems(data?: TodayResponse): TodayContinueItem[] {
  const items = data?.continue_working || [];
  return items
    .filter((item) => item.pinned || item.activity_type !== "viewed" || item.target_type !== "workspace_surface")
    .map((item) => ({
      ...item,
      subtitle: item.subtitle || item.type || "source",
      summary: item.summary || "最近进入 PSKA 的资料。"
    }));
}

function TodaySearchResult({ result }: { result: WorkspaceSearchResponse }) {
  const parsed = parseAgenticAnswer(result.answer);
  const answer = cleanAgenticAnswer(parsed?.answer || result.answer || "");
  const refs = [
    ...(parsed?.source_refs || []),
    ...(parsed?.citations || []),
    ...(result.source_refs || []),
    ...(result.citations || []),
    ...(result.workspace?.evidence?.citations || []),
    ...(result.retrieval?.results || [])
  ]
    .map((ref) => ({
      title: ref.title,
      snippet: ref.snippet,
      source_item_id: "source_item_id" in ref ? ref.source_item_id : undefined
    }))
    .filter((ref) => ref.title || ref.snippet || ref.source_item_id);

  if (!answer && refs.length === 0 && !result.error) {
    return <div className="review-empty compact">PSKA 没有为这个问题找到可展示的真实证据。</div>;
  }

  return (
    <article className="today-search-result">
      {answer ? <p className="answer-text">{answer}</p> : null}
      {result.fallback_reason ? <small className="search-note">Fallback: {result.fallback_reason}</small> : null}
      {refs.length > 0 ? (
        <div className="source-ref-list">
          {refs.slice(0, 5).map((ref, index) => (
            <div className="source-ref" key={`${ref.source_item_id || ref.title || "ref"}-${index}`}>
              <strong>{ref.title || ref.source_item_id || "来源"}</strong>
              {ref.snippet ? <p>{trimText(ref.snippet, 180)}</p> : null}
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}

function searchToBrain(result: WorkspaceSearchResponse, query: string): Partial<BrainState> {
  const parsed = parseAgenticAnswer(result.answer);
  const answer = cleanAgenticAnswer(parsed?.answer || result.answer || "");
  const refs = [
    ...(parsed?.source_refs || []),
    ...(parsed?.citations || []),
    ...(result.source_refs || []),
    ...(result.citations || []),
    ...(result.workspace?.evidence?.citations || []),
    ...(result.retrieval?.results || [])
  ]
    .map((ref, index) => ({
      id: `today-search-${index}`,
      title: ref.title || ("source_item_id" in ref ? ref.source_item_id : undefined) || query,
      score: "score" in ref && typeof ref.score === "number" ? Math.round(ref.score * 100) : undefined,
      snippet: ref.snippet || answer || "PSKA 返回了相关证据。",
      source: "PSKA Agentic"
    }))
    .filter((item) => item.title || item.snippet);
  return {
    status: result.error ? "error" : "synced",
    lastTrigger: "manual",
    updatedAt: Date.now(),
    error: result.error || null,
    relatedKnowledge: [
      ...(answer ? [{ id: "today-answer", title: query, snippet: answer, source: "PSKA Agentic answer" }] : []),
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
      title: chunk.title || chunk.source_item_id || "Corpus chunk",
      snippet: trimText(chunk.snippet || chunk.text || "", 180) || "真实 corpus 片段。",
      source: chunk.source_channel || "PSKA Corpus"
    })),
    entities: [...new Set([...entityLabels, ...sources.map((source) => source.title || "").flatMap(extractEntities)])].filter(Boolean).slice(0, 8),
    timeline: sources.slice(0, 5).map((source, index) => ({
      id: source.source_item_id || `corpus-source-${index}`,
      age: formatSourceAge(source.created_at),
      title: source.title || source.source_item_id || "未命名来源",
      detail: source.source_channel || "PSKA 来源材料"
    })),
    connections: sources.slice(0, 5).map((source, index) => ({
      id: `corpus-source-connection-${index}`,
      label: source.title || source.source_item_id || "来源",
      relation: source.source_channel || "source"
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
    summary: item.summary || "PSKA 发现了一个可检查的知识线索。"
  }));
}

function normalizeReviewItems(data?: TodayResponse): TodayReviewItem[] {
  const items = data?.needs_review || [];
  return items.map((item) => ({
    ...item,
    summary: item.summary || "等待人工审核。"
  }));
}

function evidenceLabel(count?: number) {
  if (!count) {
    return "待检查";
  }
  return `${count} 条证据`;
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

function trimText(value: string, maxLength: number) {
  const normalized = value.replace(/\s+/g, " ").trim();
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
  serviceToken: string;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
  setBrain: (brain: Partial<BrainState>) => void;
}) {
  const [query, setQuery] = useState("");
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
  const corpus = corpusQuery.data;
  const sourceSummary = sourcesQuery.data;
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
    connectors: sourceSummary?.knowledge_sources?.source_count ?? sourceSummary?.connector_state?.state_count ?? 0
  };

  useEffect(() => {
    if (corpus) {
      setBrain(corpusToBrain(corpus));
    }
  }, [corpus, setBrain]);

  function refetchAll() {
    void corpusQuery.refetch();
    void sourcesQuery.refetch();
  }

  return (
    <section className="main-workspace corpus-surface" aria-label="语料库">
      <div className="corpus-header">
        <div>
          <span className="eyebrow">Corpus</span>
          <h1>资料、片段和同步状态。</h1>
          <p>这里展示已经进入 PSKA 的真实语料，以及 connector 记录的授权范围和同步进度。</p>
        </div>
        <div className="corpus-summary" aria-label="语料库摘要">
          <span><strong>{counts.sources}</strong> Sources</span>
          <span><strong>{counts.documents}</strong> Docs</span>
          <span><strong>{counts.chunks}</strong> Chunks</span>
          <span><strong>{counts.connectors}</strong> Connectors</span>
        </div>
        <div className="corpus-actions">
          <button type="button" onClick={onPinCurrent}>
            <Pin size={15} />
            {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "置顶失败" : "置顶语料库"}
          </button>
          <button type="button" onClick={refetchAll}>
            <RefreshCw size={15} />
            刷新
          </button>
        </div>
      </div>

      <div className="corpus-tools">
        <label>
          <Search size={16} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选 source、chunk、URL 或 channel" />
        </label>
      </div>

      {corpusQuery.isError || sourcesQuery.isError ? (
        <div className="review-empty error-state">语料库无法完整加载。请检查 8765 后端、数据库或服务令牌。</div>
      ) : corpusQuery.isLoading && sourcesQuery.isLoading ? (
        <div className="review-empty">正在加载真实语料库...</div>
      ) : (
        <div className="corpus-workspace-grid">
          <section className="corpus-panel corpus-source-panel">
            <SectionTitle icon={<FileText size={18} />} title="Sources" subtitle={`${filteredSources.length} 条可见来源`} />
            {filteredSources.length === 0 ? (
              <div className="review-empty compact">当前没有匹配的 source。</div>
            ) : (
              <div className="corpus-source-list">
                {filteredSources.slice(0, 30).map((source, index) => (
                  <article className="corpus-source-card" key={source.source_item_id || `corpus-source-${index}`}>
                    <div className="card-row">
                      <span className="pill muted">{source.source_channel || "source"}</span>
                      <small>{formatSourceAge(source.created_at)}</small>
                    </div>
                    <h2>{source.title || source.source_item_id || "未命名来源"}</h2>
                    <dl>
                      <div><dt>Chunks</dt><dd>{source.chunk_count ?? "-"}</dd></div>
                      <div><dt>ID</dt><dd>{source.source_item_id || "-"}</dd></div>
                    </dl>
                    {source.url ? <p>{source.url}</p> : null}
                  </article>
                ))}
              </div>
            )}
          </section>

          <section className="corpus-panel corpus-chunk-panel">
            <SectionTitle icon={<TextCursorInput size={18} />} title="Chunks" subtitle={`${filteredChunks.length} 个片段`} />
            {filteredChunks.length === 0 ? (
              <div className="review-empty compact">当前没有匹配的 chunk。</div>
            ) : (
              <div className="corpus-chunk-list">
                {filteredChunks.slice(0, 36).map((chunk, index) => (
                  <article className="corpus-chunk-card" key={chunk.chunk_id || `corpus-chunk-${index}`}>
                    <div className="card-row">
                      <span className="pill">{chunk.source_channel || "chunk"}</span>
                      <small>{chunk.title || chunk.source_item_id || "片段"}</small>
                    </div>
                    <p>{trimText(chunk.snippet || chunk.text || "", 260) || "该 chunk 暂无可显示文本。"}</p>
                  </article>
                ))}
              </div>
            )}
          </section>

          <aside className="corpus-panel connector-panel">
            <SectionTitle icon={<Folder size={18} />} title="Knowledge Sources" subtitle="系统正在看的地方" />
            <ConnectorSummary payload={sourceSummary} />
          </aside>
        </div>
      )}
    </section>
  );
}

function ConnectorSummary({ payload }: { payload?: ConsoleSourcesResponse }) {
  const knowledgeSources = payload?.knowledge_sources?.sources || [];
  const states = payload?.connector_state?.states || [];
  const channels = Object.entries(payload?.source_channels || {}).sort((a, b) => channelCount(b[1]) - channelCount(a[1]));
  const commands = payload?.recommended_commands || payload?.files?.recommended_commands || [];
  return (
    <div className="connector-summary">
      <div className="connector-roots">
        <h3>知识来源</h3>
        {knowledgeSources.length === 0 ? (
          <p>还没有 Knowledge Source。</p>
        ) : knowledgeSources.map((source) => (
          <article className="connector-state-card" key={source.knowledge_source_id || source.uri}>
            <div className="card-row">
              <span className={`pill ${source.status === "failed" ? "warning" : ""}`}>{source.status || "unknown"}</span>
              <small>{source.mode || "manual"}</small>
            </div>
            <h3>{source.name || source.path || source.uri || "Knowledge Source"}</h3>
            <p>{source.path || source.uri}</p>
            {source.last_sync_run ? (
              <dl>
                <div><dt>Scanned</dt><dd>{source.last_sync_run.scanned ?? 0}</dd></div>
                <div><dt>New</dt><dd>{source.last_sync_run.new_files ?? 0}</dd></div>
                <div><dt>Changed</dt><dd>{source.last_sync_run.changed_files ?? 0}</dd></div>
                <div><dt>Failed</dt><dd>{source.last_sync_run.failed ?? 0}</dd></div>
              </dl>
            ) : null}
            {source.last_error ? <p className="connector-error">{source.last_error}</p> : null}
          </article>
        ))}
      </div>
      {knowledgeSources.length === 0 ? <div className="connector-state-list">
        {states.length === 0 ? (
          <div className="review-empty compact">当前没有 connector state。</div>
        ) : states.map((state) => (
          <article className="connector-state-card" key={state.connector_state_id || state.connector_id}>
            <div className="card-row">
              <span className={`pill ${state.sync_status === "failed" ? "warning" : ""}`}>{state.sync_status || "unknown"}</span>
              <small>{state.enabled ? "enabled" : "disabled"}</small>
            </div>
            <h3>{state.connector_state_id || state.connector_id || "connector"}</h3>
            <dl>
              <div><dt>Cursor</dt><dd>{state.scan_cursor || "-"}</dd></div>
              <div><dt>Last success</dt><dd>{formatReviewDate(state.last_success_at || undefined)}</dd></div>
              <div><dt>Roots</dt><dd>{state.roots?.length || 0}</dd></div>
            </dl>
            {state.last_error ? <p className="connector-error">{state.last_error}</p> : null}
          </article>
        ))}
      </div> : null}
      <div className="connector-channels">
        <h3>Source channels</h3>
        {channels.length === 0 ? (
          <p>暂无 channel 统计。</p>
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
      {commands.length > 0 ? (
        <div className="connector-commands">
          <h3>命令</h3>
          {commands.slice(0, 4).map((command) => (
            <code key={command}>{command}</code>
          ))}
        </div>
      ) : null}
    </div>
  );
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
  serviceToken: string;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
}) {
  const corpusQuery = useQuery({
    queryKey: ["graph-corpus", serviceToken],
    queryFn: () => loadCorpusData(serviceToken, 30),
    retry: 1
  });
  const todayQuery = useQuery({
    queryKey: ["graph-today", serviceToken],
    queryFn: () => loadToday(serviceToken),
    retry: 1
  });
  const graph = useMemo(() => buildWorkspaceGraph(corpusQuery.data, todayQuery.data), [corpusQuery.data, todayQuery.data]);
  const loading = corpusQuery.isLoading || todayQuery.isLoading;
  const error = corpusQuery.isError || todayQuery.isError;

  return (
    <section className="main-workspace canvas-surface graph-surface" aria-label="Graph 工作区">
      <div className="graph-toolbar">
        <button type="button" onClick={onPinCurrent}>
          <Pin size={15} />
          {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "置顶失败" : "置顶 Graph"}
        </button>
        <button type="button" onClick={() => {
          void corpusQuery.refetch();
          void todayQuery.refetch();
        }}>
          <RefreshCw size={15} />
          刷新
        </button>
      </div>
      <div className="graph-summary" aria-label="Graph 摘要">
        <span><strong>{graph.counts.sources}</strong> Sources</span>
        <span><strong>{graph.counts.chunks}</strong> Chunks</span>
        <span><strong>{graph.counts.discoveries}</strong> Discoveries</span>
        <span><strong>{graph.counts.reviews}</strong> Reviews</span>
        <span><strong>{graph.counts.hyperedges}</strong> Hyperedges</span>
      </div>
      {error ? (
        <div className="review-empty error-state">Graph 无法加载。请检查 8765 后端或服务令牌。</div>
      ) : loading ? (
        <div className="review-empty">正在加载真实 Graph 数据...</div>
      ) : graph.nodes.length === 0 ? (
        <div className="review-empty">当前没有可视化节点。</div>
      ) : (
        <ReactFlow
          nodes={graph.nodes}
          edges={graph.edges}
          nodeTypes={nodeTypes}
          fitView
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable
          panOnDrag
          zoomOnScroll
        >
          <Background gap={24} color="#d8d6cc" />
          <MiniMap pannable zoomable />
          <Controls />
        </ReactFlow>
      )}
    </section>
  );
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
      data: { title: trimText(title, 72), body: trimText(body, 160), icon, kind: lane }
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
      sources: sources.length,
      chunks: chunks.length,
      discoveries: discoveries.length,
      reviews: reviews.length,
      hyperedges: hyperedges.length
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
      {brain.error && <div className="review-empty error-state">{brain.error}</div>}
      <BrainPanel title="相关知识">
        <div className="knowledge-list">
          {brain.relatedKnowledge.length === 0 ? (
            <div className="review-empty">暂无真实检索结果。</div>
          ) : brain.relatedKnowledge.map((item) => (
            <button className="knowledge-item" type="button" key={item.id}>
              <span>
                <strong>{item.title}</strong>
                <small>{typeof item.score === "number" ? `匹配度：${item.score}%` : item.source || "未评分"}</small>
              </span>
              <p>{item.snippet}</p>
            </button>
          ))}
        </div>
      </BrainPanel>
      <BrainPanel title="实体">
        <div className="tag-cloud">
          {brain.entities.length === 0 ? (
            <span>暂无实体</span>
          ) : brain.entities.map((entity) => (
            <span key={entity}>
              <Tag size={13} />
              {entity}
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
              <small>{item.age}</small>
              <strong>{item.title}</strong>
              <p>{item.detail}</p>
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
              <span>{item.relation}</span>
              <strong>{item.label}</strong>
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
    return { title: "语料库", summary: "查看真实 source、chunk 和 connector 状态。" };
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
