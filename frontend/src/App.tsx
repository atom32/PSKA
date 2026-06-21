import { useEffect, useMemo, useRef, useState } from "react";
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
import { analyzeWorkspaceContext, applyReviewItem, approveReviewItem, loadCorpusContext, loadReviewCenter, loadToday, recordWorkspaceActivity, rejectReviewItem } from "./api";
import { useWorkspaceStore } from "./store";
import type { BrainState, ReviewCenterItem, TodayContinueItem, TodayDiscoveryItem, TodayResponse, TodayReviewItem, WorkspaceMode } from "./types";

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
    setBrain({ status: "analyzing", lastTrigger: trigger });
    const payload = await analyzeWorkspaceContext(selectedText || text, serviceToken, trigger);
    setBrain(payload);
  }

  function refreshCurrentSurface() {
    if (mode === "today" || mode === "review") {
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
          <TodayWorkspace serviceToken={serviceToken} onOpenWorkspace={setMode} />
        ) : mode === "review" ? (
          <ReviewCenter serviceToken={serviceToken} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : mode === "document" ? (
          <DocumentWorkspace editor={editor} selectedText={selectedText} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : (
          <CanvasWorkspace onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
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
        <NavItem collapsed={collapsed} icon={<Folder size={18} />} label="语料库" />
        <NavItem collapsed={collapsed} icon={<BookOpen size={18} />} label="项目" />
        <NavItem collapsed={collapsed} icon={<Tags size={18} />} label="标签" />
        <NavItem collapsed={collapsed} icon={<Search size={18} />} label="搜索" />
        <NavItem collapsed={collapsed} icon={<GitPullRequest size={18} />} label="Review" active={mode === "review"} onClick={() => onModeChange("review")} />
      </nav>
      {!collapsed && (
        <div className="tree">
          <p>当前项目</p>
          <button type="button">Agent 运行时</button>
          <button type="button">GraphRAG 笔记</button>
          <button type="button">工具调度</button>
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

const continueItems = [
  {
    id: "product-design",
    title: "PSKA Product Design",
    meta: "今天 09:42 · 文档",
    detail: "继续收敛 Knowledge IDE 的首页交互。",
    mode: "document" as const
  },
  {
    id: "fastreact-runtime",
    title: "FastReAct Runtime",
    meta: "昨天 · 画布",
    detail: "Agentic service layer 与 PSKA 边界。",
    mode: "canvas" as const
  }
];

const discoveryItems = [
  {
    id: "disc-1",
    kind: "发现关联",
    title: "FastReAct ↔ Tool Runtime",
    detail: "最近草稿和历史笔记都指向同一个模式：候选、审核、合并。",
    evidence: "3 条证据"
  },
  {
    id: "disc-2",
    kind: "发现冲突",
    title: "2025 观点 vs 2026 观点",
    detail: "旧笔记强调 chat-first，新设计转向 thinking-first workspace。",
    evidence: "2 条证据"
  }
];

const reviewItems = [
  {
    id: "review-1",
    title: "记忆候选：PSKA 不自动写入正文",
    detail: "建议进入长期记忆，用于约束未来 UI 和 agent 行为。",
    source: "Product discussion"
  },
  {
    id: "review-2",
    title: "关系候选：Review 类似 Git PR",
    detail: "候选 → 审核 → 合并，可能是 PSKA 的核心治理模式。",
    source: "Design notes"
  },
  {
    id: "review-3",
    title: "Digest 完成：Agent Runtime 资料簇",
    detail: "系统整理出 6 个相关来源，等待人工确认。",
    source: "Local digest"
  }
];

function TodayWorkspace({ serviceToken, onOpenWorkspace }: { serviceToken: string; onOpenWorkspace: (mode: WorkspaceMode) => void }) {
  const [actions, setActions] = useState<Record<string, TodayAction>>({});
  const todayQuery = useQuery({
    queryKey: ["today", serviceToken],
    queryFn: () => loadToday(serviceToken),
    retry: 1
  });
  const data = todayQuery.data;
  const continueWorking = normalizeContinueItems(data);
  const discoveries = normalizeDiscoveries(data);
  const needsReview = normalizeReviewItems(data);
  const usingFallback = !data || todayQuery.isError;

  function mark(id: string, value: TodayAction) {
    setActions((current) => ({ ...current, [id]: value }));
  }

  async function approveFromToday(item: { review_item_id?: string | null; id?: string; recommended_action?: string }, success: TodayAction) {
    const displayId = item.review_item_id || item.id || "local";
    if (!item.review_item_id || usingFallback) {
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
    if (!item.review_item_id || usingFallback) {
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

  return (
    <section className="main-workspace today-surface" aria-label="Today">
      <div className="today-header">
        <div>
          <span className="eyebrow">Today</span>
          <h1>继续思考，不处理收件箱。</h1>
          <p>{usingFallback ? "本地原型数据正在显示；后端可用时会切换为真实 PSKA 数据。" : "真实 PSKA 数据已接入；系统发现和待审核项放在旁边，随时可处理。"}</p>
        </div>
        <div className="today-summary" aria-label="今日摘要">
          <span>
            <strong>{continueWorking.length}</strong>
            继续工作
          </span>
          <span>
            <strong>{discoveries.length}</strong>
            新发现
          </span>
          <span>
            <strong>{needsReview.length}</strong>
            待审核
          </span>
        </div>
      </div>

      <div className="today-grid">
        <section className="today-section continue-working">
          <SectionTitle icon={<PlayCircle size={18} />} title="Continue Working" subtitle="回到上次真正的工作现场" />
          <div className="today-list">
            {continueWorking.map((item) => (
              <button className="work-item" type="button" key={item.id} onClick={() => onOpenWorkspace(item.opened_surface === "canvas" ? "canvas" : "document")}>
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
            {discoveries.map((item) => (
              <article className="today-card discovery-card" key={item.id}>
                <div className="card-row">
                  <span className="pill">{item.label}</span>
                  <small>{actions[item.id] || evidenceLabel(item.evidence_count)}</small>
                </div>
                <h2>{item.title}</h2>
                <p>{item.summary}</p>
                <div className="card-actions">
                  <button type="button" onClick={() => approveFromToday(item, "已接受")}>
                    接受
                  </button>
                  <button type="button" onClick={() => rejectFromToday(item, "已忽略")}>
                    忽略
                  </button>
                  <button type="button" onClick={() => mark(item.id, "稍后")}>
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
            {needsReview.map((item) => (
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
  if (items.length) {
    return items.map((item) => ({
      ...item,
      subtitle: item.subtitle || item.type || "source",
      summary: item.summary || "最近进入 PSKA 的资料。"
    }));
  }
  return continueItems.map((item) => ({
    id: item.id,
    title: item.title,
    subtitle: item.meta,
    summary: item.detail,
    opened_surface: item.mode
  }));
}

function normalizeDiscoveries(data?: TodayResponse): TodayDiscoveryItem[] {
  const items = data?.discoveries || [];
  if (items.length) {
    return items.map((item) => ({
      ...item,
      summary: item.summary || "PSKA 发现了一个可检查的知识线索。"
    }));
  }
  return discoveryItems.map((item) => ({
    id: item.id,
    label: item.kind,
    title: item.title,
    summary: item.detail,
    evidence_count: Number(item.evidence.match(/\d+/)?.[0] || 0)
  }));
}

function normalizeReviewItems(data?: TodayResponse): TodayReviewItem[] {
  const items = data?.needs_review || [];
  if (items.length) {
    return items.map((item) => ({
      ...item,
      summary: item.summary || "等待人工审核。"
    }));
  }
  return reviewItems.map((item) => ({
    review_item_id: item.id,
    review_type: item.source,
    title: item.title,
    summary: item.detail,
    recommended_action: "approve_or_reject"
  }));
}

function evidenceLabel(count?: number) {
  if (!count) {
    return "待检查";
  }
  return `${count} 条证据`;
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

function CanvasWorkspace({
  onPinCurrent,
  pinStatus
}: {
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
}) {
  const nodes = useMemo(
    () => [
      {
        id: "topic",
        type: "pskaCard",
        position: { x: 80, y: 90 },
        data: { title: "Agent 运行时", icon: "text", body: "当前画布主题与草稿聚类。" }
      },
      {
        id: "fastreact",
        type: "pskaCard",
        position: { x: 420, y: 40 },
        data: { title: "FastReAct", icon: "link", body: "Agentic 服务层、规划与编排。" }
      },
      {
        id: "retrieval",
        type: "pskaCard",
        position: { x: 405, y: 250 },
        data: { title: "PSKA 检索 API", icon: "doc", body: "带 ACL 和来源引用的直接证据检索。" }
      },
      {
        id: "memory",
        type: "pskaCard",
        position: { x: 760, y: 160 },
        data: { title: "记忆层", icon: "image", body: "已确认的长期上下文、图谱关系和画像卡片。" }
      }
    ],
    []
  );
  const edges = useMemo(
    () => [
      { id: "e1", source: "topic", target: "fastreact", label: "通过它规划", animated: true },
      { id: "e2", source: "topic", target: "retrieval", label: "通过它检索" },
      { id: "e3", source: "retrieval", target: "memory", label: "提供证据 grounding" },
      { id: "e4", source: "fastreact", target: "memory", label: "提出候选" }
    ],
    []
  );

  return (
    <section className="main-workspace canvas-surface" aria-label="画布工作区">
      <div className="canvas-toolbar">
        <button type="button" onClick={onPinCurrent}>
          <Pin size={15} />
          {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "置顶失败" : "置顶画布"}
        </button>
      </div>
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView>
        <Background gap={24} color="#d8d6cc" />
        <MiniMap pannable zoomable />
        <Controls />
      </ReactFlow>
    </section>
  );
}

function CanvasCardNode({ data }: NodeProps<{ title: string; body: string; icon: "text" | "doc" | "image" | "link" }>) {
  const Icon = data.icon === "image" ? Image : data.icon === "link" ? Link2 : data.icon === "doc" ? FileText : TextCursorInput;
  return (
    <div className="canvas-card">
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
        <span>{brain.status === "analyzing" ? "正在分析上下文" : brain.status === "synced" ? "已与 PSKA 同步" : brain.status === "offline" ? "本地上下文模式" : "安静观察中"}</span>
        <small>触发：{triggerLabel(brain.lastTrigger)}</small>
      </div>
      <BrainPanel title="相关知识">
        <div className="knowledge-list">
          {brain.relatedKnowledge.map((item) => (
            <button className="knowledge-item" type="button" key={item.id}>
              <span>
                <strong>{item.title}</strong>
                <small>匹配度：{item.score}%</small>
              </span>
              <p>{item.snippet}</p>
            </button>
          ))}
        </div>
      </BrainPanel>
      <BrainPanel title="实体">
        <div className="tag-cloud">
          {brain.entities.map((entity) => (
            <span key={entity}>
              <Tag size={13} />
              {entity}
            </span>
          ))}
        </div>
      </BrainPanel>
      <BrainPanel title="上下文时间线">
        <div className="timeline">
          {brain.timeline.map((item) => (
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
          {brain.connections.map((item) => (
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
