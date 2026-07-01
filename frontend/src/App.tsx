import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
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
  LogOut,
  Maximize2,
  MessageCircle,
  Minimize2,
  Paperclip,
  Pin,
  PlayCircle,
  RefreshCw,
  RotateCcw,
  Search,
  Send,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Tag,
  TextCursorInput,
  Trash2,
  UploadCloud,
  X
} from "lucide-react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type OnNodeDrag,
  type NodeProps
} from "@xyflow/react";
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
  askConversationStream,
  askWorkspaceStream,
  applyReviewItem,
  approveReviewItem,
  cleanupKnowledgeSource,
  composeWritingDraft,
  createAskConversation,
  createEvidenceBrief,
  createKnowledgeSource,
  createTextSource,
  createWritingBoard,
  createWritingEdge,
  createWritingNode,
  deleteWritingBoard,
  deleteWritingNode,
  ignoreDiscovery,
  deleteWorkspaceDocuments,
  loadCorpusContext,
  loadCorpusData,
  loadDigestLogs,
  loadGatewaySession,
  loadGraphData,
  loadGraphSearchSubgraph,
  loadGraphSubgraph,
  loadAskConversation,
  loadReviewCenter,
  loadAskConversations,
  loadPromptProfiles,
  loadWorkspaceDocuments,
  loadSourcesConsole,
  loadToday,
  loadWritingBoard,
  listWritingBoards,
  patchWritingBoard,
  patchWritingNode,
  previewChunking,
  previewKnowledgeSource,
  recordWorkspaceActivity,
  rejectReviewItem,
  retryDigestJob,
  runDigestNow,
  snoozeDiscovery,
  syncKnowledgeSources,
  suggestWritingQuestions,
  updatePromptProfiles,
  uploadWorkspaceSource
} from "./api";
import type { PSKAAuth, PSKAIdentity } from "./api";
import { useWorkspaceStore } from "./store";
import type {
  BrainState,
  ChunkingPreviewResponse,
  ConsoleSourceChannelStats,
  ConsoleSourcesResponse,
  DigestNowResponse,
  DigestLogsResponse,
  KnowledgeSourceCleanupResponse,
  AskConversation,
  AskMessage,
  AskRun,
  ReviewCenterItem,
  SourcePreviewResponse,
  SourceSyncResponse,
  TodayContinueItem,
  TodayDiscoveryItem,
  TodayResponse,
  TodayReviewItem,
  WorkspaceAskResponse,
  WorkspaceCorpusResponse,
  WorkspaceDocumentDeleteResponse,
  WorkspaceDocumentsResponse,
  WorkspaceSourceIngestResponse,
  PromptProfilesResponse,
  WorkspaceGraphEdge,
  WorkspaceGraphNode,
  WorkspaceGraphPathResponse,
  WorkspaceGraphResponse,
  WorkspaceSearchResponse,
  WorkspaceMode,
  WritingBoard,
  WritingEdge,
  WritingNode,
  WritingNodeType,
  WritingQuestionSuggestion
} from "./types";

const nodeTypes = {
  writingNode: WritingCanvasNode,
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
    setBrain,
    clearIdentity
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
  const [gatewayAuthenticated, setGatewayAuthenticated] = useState<boolean | null>(null);
  const [activeAskConversationId, setActiveAskConversationId] = useState("");
  const activeMode: WorkspaceMode = mode === "document" || mode === "canvas" ? "writing" : mode;

  useEffect(() => {
    let cancelled = false;
    void loadGatewaySession().then((session) => {
      if (!session || cancelled) {
        if (!cancelled) {
          setGatewayAuthenticated(false);
        }
        return;
      }
      setGatewayAuthenticated(true);
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
    enabled: activeMode !== "today" && activeMode !== "review" && activeMode !== "writing"
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
    void logWorkspaceActivity("opened", activeMode);
    void logWorkspaceActivity("viewed", activeMode);
  }, [activeMode, pskaIdentity]);

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
    if (activeMode === "today" || activeMode === "review" || activeMode === "graph" || activeMode === "writing") {
      setBrain({ status: "idle", lastTrigger: "manual", updatedAt: Date.now() });
      return;
    }
    void runAnalysis("manual");
  }

  function logoutGatewaySession() {
    clearIdentity();
    window.location.assign("/logout");
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
    void logWorkspaceActivity("pinned", activeMode, { throwOnError: true })
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
    <main className={`app-shell ${leftCollapsed ? "left-collapsed" : ""} ${activeMode === "writing" ? "writing-mode" : ""} ${activeMode === "today" ? "today-mode" : ""}`}>
      <LeftSidebar
        collapsed={leftCollapsed}
        mode={activeMode}
        serviceToken={pskaIdentity}
        activeAskConversationId={activeAskConversationId}
        onAskConversationChange={setActiveAskConversationId}
        onModeChange={setMode}
        onToggle={toggleLeft}
      />
      <section className="workspace-column">
        <TopBar
          mode={activeMode}
          serviceToken={serviceToken}
          tenantId={tenantId}
          userId={userId}
          representedUserId={representedUserId}
          gatewayAuthenticated={gatewayAuthenticated}
          onModeChange={setMode}
          onTokenChange={setServiceToken}
          onTenantChange={setTenantId}
          onUserChange={setUserId}
          onRepresentedUserChange={setRepresentedUserId}
          onLogout={logoutGatewaySession}
          onRefresh={refreshCurrentSurface}
        />
        {activeMode === "today" ? (
          <TodayWorkspace
            serviceToken={pskaIdentity}
            activeConversationId={activeAskConversationId}
            onActiveConversationChange={setActiveAskConversationId}
            onOpenWorkspace={setMode}
            setBrain={setBrain}
          />
        ) : activeMode === "review" ? (
          <ReviewCenter serviceToken={pskaIdentity} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : activeMode === "graph" ? (
          <GraphWorkspace serviceToken={pskaIdentity} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : activeMode === "corpus" ? (
          <CorpusWorkspace serviceToken={pskaIdentity} onOpenWorkspace={setMode} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} setBrain={setBrain} />
        ) : activeMode === "writing" ? (
          <WritingWorkspace serviceToken={pskaIdentity} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        ) : (
          <CanvasWorkspace brain={brain} onPinCurrent={pinCurrentWorkspace} pinStatus={pinStatus} />
        )}
      </section>
      {activeMode === "writing" || activeMode === "today" ? null : <BrainSidebar brain={brain} onRefresh={refreshCurrentSurface} />}
    </main>
  );
}

function LeftSidebar({
  collapsed,
  mode,
  serviceToken,
  activeAskConversationId,
  onAskConversationChange,
  onModeChange,
  onToggle
}: {
  collapsed: boolean;
  mode: WorkspaceMode;
  serviceToken: PSKAAuth;
  activeAskConversationId: string;
  onAskConversationChange: (conversationId: string) => void;
  onModeChange: (mode: WorkspaceMode) => void;
  onToggle: () => void;
}) {
  const [contextMessage, setContextMessage] = useState("");
  const askConversationsQuery = useQuery({
    queryKey: ["left-ask-conversations", serviceToken],
    queryFn: () => loadAskConversations(serviceToken),
    enabled: mode === "today" && !collapsed,
    retry: 1
  });
  const conversations = askConversationsQuery.data?.conversations || [];

  async function createSidebarConversation() {
    setContextMessage("");
    try {
      const payload = await createAskConversation(serviceToken, "Ask PSKA");
      const conversationId = payload.conversation?.conversation_id || "";
      if (conversationId) {
        onAskConversationChange(conversationId);
        onModeChange("today");
      }
      await askConversationsQuery.refetch();
    } catch (error) {
      setContextMessage(error instanceof Error ? error.message : "创建对话失败。");
    }
  }

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
        <NavItem collapsed={collapsed} icon={<TextCursorInput size={18} />} label="写作" active={mode === "writing"} onClick={() => onModeChange("writing")} />
        <NavItem collapsed={collapsed} icon={<Hash size={18} />} label="Graph" active={mode === "graph"} onClick={() => onModeChange("graph")} />
        <NavItem collapsed={collapsed} icon={<Folder size={18} />} label="资料库" active={mode === "corpus"} onClick={() => onModeChange("corpus")} />
        <NavItem collapsed={collapsed} icon={<GitPullRequest size={18} />} label="Review" active={mode === "review"} onClick={() => onModeChange("review")} />
      </nav>
      {!collapsed && (
        <SidebarContextTree
          mode={mode}
          conversations={conversations}
          activeAskConversationId={activeAskConversationId}
          loading={askConversationsQuery.isLoading}
          message={contextMessage}
          onNewConversation={() => void createSidebarConversation()}
          onSelectConversation={(conversationId) => {
            onAskConversationChange(conversationId);
            onModeChange("today");
          }}
          onModeChange={onModeChange}
        />
      )}
    </aside>
  );
}

function SidebarContextTree({
  mode,
  conversations,
  activeAskConversationId,
  loading,
  message,
  onNewConversation,
  onSelectConversation,
  onModeChange
}: {
  mode: WorkspaceMode;
  conversations: AskConversation[];
  activeAskConversationId: string;
  loading: boolean;
  message: string;
  onNewConversation: () => void;
  onSelectConversation: (conversationId: string) => void;
  onModeChange: (mode: WorkspaceMode) => void;
}) {
  if (mode === "today") {
    return (
      <div className="tree context-tree">
        <div className="context-tree-head">
          <p>对话</p>
          <button className="context-action" type="button" onClick={onNewConversation} title="新对话">
            <MessageCircle size={14} />
          </button>
        </div>
        {loading ? <span>正在加载对话...</span> : null}
        {!loading && conversations.length === 0 ? <span>还没有 Ask PSKA 对话。</span> : null}
        <div className="context-thread-list">
          {conversations.slice(0, 10).map((conversation) => (
            <button
              className={`context-item ${conversation.conversation_id === activeAskConversationId ? "active" : ""}`}
              type="button"
              key={conversation.conversation_id}
              onClick={() => conversation.conversation_id && onSelectConversation(conversation.conversation_id)}
            >
              <MessageCircle size={14} />
              <span>{trimText(conversation.title || conversation.conversation_id || "Ask PSKA", 30)}</span>
            </button>
          ))}
        </div>
        {message ? <small>{message}</small> : null}
      </div>
    );
  }
  if (mode === "writing") {
    return (
      <div className="tree context-tree">
        <p>写作</p>
        <button className="context-item" type="button" onClick={() => onModeChange("writing")}>
          <TextCursorInput size={14} />
          <span>项目卡片</span>
        </button>
        <span>写作项目在主区以卡片选择，进入项目后是 Inquiry Graph 画布。</span>
      </div>
    );
  }
  if (mode === "corpus") {
    return (
      <div className="tree context-tree">
        <p>资料库</p>
        <span>上传、粘贴、URL/RSS 和资料删除影响都在主区管理。</span>
      </div>
    );
  }
  if (mode === "review") {
    return (
      <div className="tree context-tree">
        <p>Review</p>
        <span>审核候选、Digest 风险和待应用知识在主区处理。</span>
      </div>
    );
  }
  return (
    <div className="tree context-tree">
      <p>Graph</p>
      <span>关系浏览、路径检索和图谱问答在主区展开。</span>
    </div>
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
  gatewayAuthenticated,
  onModeChange,
  onTokenChange,
  onTenantChange,
  onUserChange,
  onRepresentedUserChange,
  onLogout,
  onRefresh
}: {
  mode: WorkspaceMode;
  serviceToken: string;
  tenantId: string;
  userId: string;
  representedUserId: string;
  gatewayAuthenticated: boolean | null;
  onModeChange: (mode: WorkspaceMode) => void;
  onTokenChange: (serviceToken: string) => void;
  onTenantChange: (tenantId: string) => void;
  onUserChange: (userId: string) => void;
  onRepresentedUserChange: (representedUserId: string) => void;
  onLogout: () => void;
  onRefresh: () => void;
}) {
  return (
    <header className="top-bar">
      <div className="mode-switch" role="tablist" aria-label="工作台模式">
        <button className={mode === "today" ? "active" : ""} type="button" onClick={() => onModeChange("today")}>
          <CalendarDays size={17} />
          Today
        </button>
        <button className={mode === "writing" ? "active" : ""} type="button" onClick={() => onModeChange("writing")}>
          <TextCursorInput size={17} />
          写作
        </button>
        <button className={mode === "graph" ? "active" : ""} type="button" onClick={() => onModeChange("graph")}>
          <Hash size={17} />
          Graph
        </button>
        <button className={mode === "corpus" ? "active" : ""} type="button" onClick={() => onModeChange("corpus")}>
          <Folder size={17} />
          资料库
        </button>
        <button className={mode === "review" ? "active" : ""} type="button" onClick={() => onModeChange("review")}>
          <GitPullRequest size={17} />
          Review
        </button>
      </div>
      {gatewayAuthenticated === false ? (
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
      ) : gatewayAuthenticated === true ? (
        <div className="gateway-session" aria-label="当前登录身份" data-testid="gateway-session">
          <span>{representedUserId || userId || "user_primary"}</span>
          <small>{tenantId || "tenant_default"}</small>
          <button className="icon-button logout-button" type="button" onClick={onLogout} title="退出登录" data-testid="logout-button">
            <LogOut size={17} />
          </button>
        </div>
      ) : null}
      <button className="icon-button top-refresh" type="button" onClick={onRefresh} title="刷新上下文">
        <RefreshCw size={18} />
      </button>
    </header>
  );
}

type TodayAction = "待处理" | "处理中" | "已接受" | "已忽略" | "稍后" | "已批准" | "已批准并应用" | "已拒绝" | "操作失败";

function TodayWorkspace({
  serviceToken,
  activeConversationId,
  onActiveConversationChange,
  onOpenWorkspace,
  setBrain
}: {
  serviceToken: PSKAAuth;
  activeConversationId: string;
  onActiveConversationChange: (conversationId: string) => void;
  onOpenWorkspace: (mode: WorkspaceMode) => void;
  setBrain: (brain: Partial<BrainState>) => void;
}) {
  const [actions, setActions] = useState<Record<string, TodayAction>>({});
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<WorkspaceAskResponse | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [attachmentFile, setAttachmentFile] = useState<File | null>(null);
  const [attachmentStatus, setAttachmentStatus] = useState("");
  const [askTemperature, setAskTemperature] = useState(0.3);
  const [askMaxTokens, setAskMaxTokens] = useState(4096);
  const [askTopK, setAskTopK] = useState(8);
  const [rightRailCollapsed, setRightRailCollapsed] = useState(false);
  const todayQuery = useQuery({
    queryKey: ["today", serviceToken],
    queryFn: () => loadToday(serviceToken),
    retry: 1
  });
  const askConversationsQuery = useQuery({
    queryKey: ["today-ask-conversations", serviceToken],
    queryFn: () => loadAskConversations(serviceToken),
    retry: 1
  });
  const askConversationQuery = useQuery({
    queryKey: ["today-ask-conversation", serviceToken, activeConversationId],
    queryFn: () => loadAskConversation(serviceToken, activeConversationId),
    enabled: Boolean(activeConversationId),
    retry: 1
  });
  const data = todayQuery.data;
  const continueWorking = normalizeContinueItems(data);
  const discoveries = normalizeDiscoveries(data);
  const needsReview = normalizeReviewItems(data);
  const conversations = askConversationsQuery.data?.conversations || [];
  const conversationMessages = askConversationQuery.data?.messages || [];
  const conversationRuns = askConversationQuery.data?.runs || [];
  const actionCount = continueWorking.length + discoveries.length + needsReview.length;

  useEffect(() => {
    if (!activeConversationId && conversations[0]?.conversation_id) {
      onActiveConversationChange(conversations[0].conversation_id);
    }
  }, [activeConversationId, conversations, onActiveConversationChange]);

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
    setAttachmentStatus("");
    setSearchResult(pendingAskResult(query));
    try {
      let conversationId = activeConversationId;
      if (!conversationId) {
        const created = await createAskConversation(serviceToken, query.slice(0, 60));
        conversationId = created.conversation?.conversation_id || "";
        if (conversationId) {
          onActiveConversationChange(conversationId);
        }
      }
      if (attachmentFile) {
        setAttachmentStatus(`正在把 ${attachmentFile.name} 加入资料库`);
        const upload = await uploadWorkspaceSource(serviceToken, attachmentFile, { digest_mode: "after_upload" });
        const sourceItemIds = upload.source_item_ids || [];
        setAttachmentStatus(`${attachmentFile.name} 已加入资料库，并用于本次提问`);
        setAttachmentFile(null);
        const result = conversationId ? await askConversationStream(conversationId, query, serviceToken, ({ result: partial }) => {
          setSearchResult({ ...partial });
        }, { surface: "today", topK: askTopK, temperature: askTemperature, maxTokens: askMaxTokens, sourceItemIds }) : await askWorkspaceStream(query, serviceToken, "auto", "today", ({ result: partial }) => {
          setSearchResult({ ...partial });
        }, { topK: askTopK, scope: sourceItemIds.length ? { source_item_ids: sourceItemIds } : undefined });
        setSearchResult(result);
        setBrain(searchToBrain(result, query));
        if (result.error) {
          setSearchError(displaySearchError(result.error));
        }
        await Promise.all([askConversationsQuery.refetch(), conversationId ? askConversationQuery.refetch() : Promise.resolve()]);
        return;
      }
      const result = conversationId ? await askConversationStream(conversationId, query, serviceToken, ({ result: partial }) => {
        setSearchResult({ ...partial });
      }, { surface: "today", topK: askTopK, temperature: askTemperature, maxTokens: askMaxTokens }) : await askWorkspaceStream(query, serviceToken, "auto", "today", ({ result: partial }) => {
        setSearchResult({ ...partial });
      });
      setSearchResult(result);
      setBrain(searchToBrain(result, query));
      if (result.error) {
        setSearchError(displaySearchError(result.error));
      }
      await Promise.all([askConversationsQuery.refetch(), conversationId ? askConversationQuery.refetch() : Promise.resolve()]);
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
      {todayQuery.isError ? (
        <div className="review-empty error-state">Today 无法加载。请检查 8765 后端、Vite 代理或服务令牌。</div>
      ) : todayQuery.isLoading ? (
        <div className="review-empty">正在加载真实 Today 数据...</div>
      ) : (
      <div className={`today-grid ${rightRailCollapsed ? "right-rail-collapsed" : ""}`}>
        <section className="today-section today-search">
          <AskConversationPanel
            messages={conversationMessages}
            runs={conversationRuns}
            isLoading={askConversationsQuery.isLoading || askConversationQuery.isLoading}
            liveQuery={searchQuery}
            liveResult={searching || conversationMessages.length === 0 ? searchResult : null}
            livePending={searching}
            composer={(
              <form className="today-search-form today-chat-composer" onSubmit={runTodaySearch} data-testid="today-ask-form">
                <textarea
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                  placeholder="询问资料库，或拖入附件后继续追问"
                  data-testid="today-ask-input"
                />
                <div className="today-chat-tools">
                  <div className="today-chat-tool-left">
                    <label className="attachment-picker" title="上传到资料库，并用于本次提问">
                      <Paperclip size={16} />
                      <input type="file" onChange={(event) => setAttachmentFile(event.target.files?.[0] || null)} data-testid="today-attachment-input" />
                      <span>{attachmentFile ? trimText(attachmentFile.name, 24) : "附件"}</span>
                    </label>
                    <details className="ask-settings">
                      <summary title="Ask 参数">
                        <SlidersHorizontal size={16} />
                        参数
                      </summary>
                      <label>
                        <span>Top K</span>
                        <input type="number" min={1} max={20} value={askTopK} onChange={(event) => setAskTopK(Number(event.target.value) || 8)} />
                      </label>
                      <label>
                        <span>Temperature</span>
                        <input type="number" min={0} max={2} step={0.1} value={askTemperature} onChange={(event) => setAskTemperature(Number(event.target.value) || 0)} />
                      </label>
                      <label>
                        <span>Max Tokens</span>
                        <input type="number" min={256} max={32000} step={256} value={askMaxTokens} onChange={(event) => setAskMaxTokens(Number(event.target.value) || 4096)} />
                      </label>
                    </details>
                  </div>
                  <button className="today-send-button" type="submit" disabled={searching} title={searching ? "查询中" : "发送"} data-testid="today-ask-submit">
                    <Send size={18} />
                    <span>{searching ? "查询中" : "发送"}</span>
                  </button>
                </div>
                {attachmentStatus ? <small className="search-note" data-testid="today-attachment-status">{attachmentStatus}</small> : null}
                {searchError ? <div className="review-empty error-state compact">{searchError}</div> : null}
              </form>
            )}
          />
        </section>

        <aside className={`today-actions-sidebar ${rightRailCollapsed ? "collapsed" : ""}`} aria-label="Today 侧栏">
          <button className="today-rail-toggle" type="button" onClick={() => setRightRailCollapsed((value) => !value)} title={rightRailCollapsed ? "展开右栏" : "折叠右栏"}>
            {rightRailCollapsed ? <ChevronLeft size={16} /> : <ChevronRight size={16} />}
            {actionCount > 0 ? <span className="rail-alert-dot" /> : null}
          </button>
          {rightRailCollapsed ? (
            <div className="today-rail-icons" aria-label="Today 待处理入口">
              <button className={continueWorking.length ? "has-alert" : ""} type="button" onClick={() => setRightRailCollapsed(false)} title="Continue">
                <PlayCircle size={18} />
              </button>
              <button className={discoveries.length ? "has-alert" : ""} type="button" onClick={() => setRightRailCollapsed(false)} title="Discoveries">
                <Sparkles size={18} />
              </button>
              <button className={needsReview.length ? "has-alert" : ""} type="button" onClick={() => setRightRailCollapsed(false)} title="Review">
                <CheckCircle2 size={18} />
              </button>
            </div>
          ) : null}
          {!rightRailCollapsed ? (
          <>
          <CollapsibleTodayPanel className="continue-working" icon={<PlayCircle size={18} />} title="Continue" count={continueWorking.length} hasAlert={continueWorking.length > 0}>
            <div className="today-rail-stack">
              {continueWorking.length === 0 ? (
                <div className="review-empty compact">当前没有可继续的工作记录。</div>
              ) : continueWorking.slice(0, 4).map((item) => (
                <button className="work-item compact" type="button" key={item.id} onClick={() => onOpenWorkspace(item.opened_surface || "document")}>
                  <span>
                    <strong>{item.title}</strong>
                    <small>{item.subtitle}</small>
                  </span>
                  <p>{trimText(item.summary, 120)}</p>
                </button>
              ))}
            </div>
          </CollapsibleTodayPanel>

          <CollapsibleTodayPanel className="discoveries" icon={<Sparkles size={18} />} title="Discoveries" count={discoveries.length} hasAlert={discoveries.length > 0}>
            <div className="today-rail-stack">
              {discoveries.length === 0 ? (
                <div className="review-empty compact">当前没有达到质量阈值的新发现。</div>
              ) : discoveries.slice(0, 3).map((item) => (
                <article className="today-card discovery-card compact" key={item.id}>
                  <div className="card-row">
                    <span className="pill">{item.label}</span>
                    <small>{actions[item.id] || discoveryQualityLabel(item)}</small>
                  </div>
                  <h2>{item.title}</h2>
                  <p>{trimText(item.summary, 130)}</p>
                  <div className="card-actions">
                    <button type="button" onClick={() => acceptDiscoveryFromToday(item)}>接受</button>
                    <button type="button" onClick={() => ignoreDiscoveryFromToday(item)}>忽略</button>
                  </div>
                </article>
              ))}
            </div>
          </CollapsibleTodayPanel>

          <CollapsibleTodayPanel className="needs-review" icon={<CheckCircle2 size={18} />} title="Review" count={needsReview.length} hasAlert={needsReview.length > 0}>
            <div className="today-rail-stack">
              {needsReview.length === 0 ? (
                <div className="review-empty compact">当前没有待审核候选。</div>
              ) : needsReview.slice(0, 3).map((item) => (
                <article className="today-card review-card compact" key={item.review_item_id}>
                  <div className="card-row">
                    <span className="pill muted">{item.review_type || "review"}</span>
                    <small>{actions[item.review_item_id] || recommendedActionLabel(item.recommended_action)}</small>
                  </div>
                  <h2>{item.title}</h2>
                  <p>{trimText(item.summary, 130)}</p>
                  <div className="card-actions">
                    <button type="button" onClick={() => approveFromToday(item, "已批准")}>批准</button>
                    <button type="button" onClick={() => rejectFromToday(item, "已拒绝")}>拒绝</button>
                  </div>
                </article>
              ))}
            </div>
          </CollapsibleTodayPanel>
          </>
          ) : null}
        </aside>
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
          {items.map((item) => {
            const supportBasis = reviewSupportBasis(item);
            const proposalSummary = reviewProposalSummary(item);
            return (
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
                  {item.quality_tier ? (
                    <span className={`pill ${item.quality_tier === "strong" ? "" : "muted"}`}>{reviewQualityTierLabel(item.quality_tier)}</span>
                  ) : null}
                  {item.promotion_reason ? <span className="pill muted">{reviewPromotionReasonLabel(item.promotion_reason)}</span> : null}
                  {item.review_eligible === false ? <span className="pill warning">仅诊断，不入库</span> : null}
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
                {supportBasis.length || proposalSummary ? (
                  <div className="review-basis">
                    {proposalSummary ? <p>{proposalSummary}</p> : null}
                    {supportBasis.length ? (
                      <div>
                        <span>依据</span>
                        {supportBasis.map((basis) => <code key={`${item.review_item_id}-${basis}`}>{basis}</code>)}
                      </div>
                    ) : null}
                  </div>
                ) : null}
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
            );
          })}
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

function AskResult({ result, pending = false }: { result: WorkspaceAskResponse | WorkspaceSearchResponse; pending?: boolean }) {
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
  const [selectedRefIndex, setSelectedRefIndex] = useState(0);
  const askEvidence = (result as WorkspaceAskResponse).evidence;
  const route = (result as WorkspaceAskResponse).route;
  const evidenceCheck = (result as WorkspaceAskResponse).evidence_check;
  const citationAudit = (result as WorkspaceAskResponse).citation_audit;
  const timing = (result as WorkspaceAskResponse).timing;
  const qualitySignals = (result as WorkspaceAskResponse).quality_signals;
  const workspaceEvidence = (result as WorkspaceSearchResponse).workspace?.evidence;
  const retrieval = (result as WorkspaceSearchResponse).retrieval;
  const fallback = (result as WorkspaceSearchResponse).fallback;
  const fallbackReason = (result as WorkspaceSearchResponse).fallback_reason;
  const parsed = parseAgenticAnswer(result.answer);
  const eventAnswer = finalAnswerFromTraceEvents(result);
  const answer = cleanAgenticAnswer(parsed?.answer || result.answer || eventAnswer || "");
  const refs = normalizeSearchRefs([
    ...(parsed?.source_refs || []),
    ...(parsed?.citations || []),
    ...(result.source_refs || []),
    ...(result.citations || []),
    ...((result as WorkspaceAskResponse).source_windows || []),
    ...(askEvidence?.citations || []),
    ...(askEvidence?.results || []),
    ...(askEvidence?.source_windows || []),
    ...(workspaceEvidence?.citations || []),
    ...(retrieval?.results || []),
    ...(fallback?.retrieval?.citations || []),
    ...(fallback?.retrieval?.results || [])
  ]);
  const gaps = normalizeAskNotes(askEvidence?.gaps);
  const conflicts = normalizeAskNotes(askEvidence?.conflicts);
  const noAnswerReasons = normalizeAskNotes((result as WorkspaceAskResponse).no_answer_reasons || evidenceCheck?.no_answer_reasons || askEvidence?.no_answer_reasons);
  const droppedCitations = normalizeDroppedCitations(citationAudit?.dropped || evidenceCheck?.dropped_citations || askEvidence?.dropped_citations);
  const diagnostics = normalizeAskNoAnswerDiagnostics(qualitySignals?.no_answer_diagnostics);
  const selectedRef = refs[Math.min(selectedRefIndex, Math.max(refs.length - 1, 0))];
  const agentSteps = normalizeAskAgentSteps((result as WorkspaceAskResponse).agent_steps);
  const progressEvents = normalizeAskProgress((result as WorkspaceAskResponse).progress);
  const displaySteps = agentSteps.length ? agentSteps : progressEvents.length ? progressToAgentSteps(progressEvents) : pending ? pendingAskSteps() : [];
  const rawEvents = agenticTraceEvents(result);
  const markdown = buildAskMarkdown((result as WorkspaceAskResponse).query || "", answer, refs, gaps, conflicts);
  const canCopy = Boolean(answer || refs.length || gaps.length || conflicts.length);

  async function handleCopyMarkdown() {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
  }

  if (!answer && refs.length === 0 && gaps.length === 0 && conflicts.length === 0 && !result.error && !pending && !route && !displaySteps.length && !rawEvents.length) {
    return <div className="review-empty compact">PSKA 没有为这个问题找到可展示的真实证据。</div>;
  }

  return (
    <article className="today-search-result">
      <div className="ask-result-header">
        <small className="search-note">
          {route ? askRouteLabel(route) : pending ? "Ask PSKA · 查询中" : "Ask PSKA"}
          {(result as WorkspaceAskResponse).answer_type ? ` · ${askAnswerTypeLabel((result as WorkspaceAskResponse).answer_type || "")}` : ""}
          {route?.intent ? ` · ${askIntentLabel(route.intent)}` : ""}
          {timing?.time_to_first_agent_event_ms !== undefined ? ` · 首过程 ${Math.round(timing.time_to_first_agent_event_ms)} ms` : ""}
          {timing?.time_to_first_answer_ms !== undefined ? ` · 首字 ${Math.round(timing.time_to_first_answer_ms)} ms` : ""}
          {timing?.total_ms !== undefined ? ` · 总耗时 ${Math.round(timing.total_ms)} ms` : ""}
        </small>
        <button type="button" onClick={() => void handleCopyMarkdown()} disabled={!canCopy}>
          {copyStatus === "copied" ? "已复制" : copyStatus === "failed" ? "复制失败" : "复制 Markdown"}
        </button>
      </div>
      {progressEvents.length ? <AskProgressStrip progress={progressEvents} /> : null}
      {displaySteps.length || rawEvents.length ? <AskProcessTimeline steps={displaySteps} rawEvents={rawEvents} defaultOpen={pending} /> : null}
      {!answer && pending ? <div className="ask-pending-state">正在等待 Ask PSKA 的第一个可见回答字符，检索过程会实时更新。</div> : null}
      {answer ? <MarkdownAnswer content={answer} /> : null}
      {fallbackReason ? <small className="search-note">{askFallbackLabel(fallbackReason)}</small> : null}
      {refs.length > 0 ? (
        <div className="ask-evidence-layout">
          <div className="source-ref-list" aria-label="引用列表">
            {refs.slice(0, 6).map((ref, index) => {
            const snippet = trimText(cleanEvidenceSnippet(ref.snippet), 180);
            return (
              <button
                type="button"
                className={`source-ref ${index === Math.min(selectedRefIndex, refs.length - 1) ? "active" : ""}`}
                key={`${ref.source_item_id || ref.chunk_id || ref.title || "ref"}-${index}`}
                onMouseEnter={() => setSelectedRefIndex(index)}
                onFocus={() => setSelectedRefIndex(index)}
                onClick={() => setSelectedRefIndex(index)}
              >
                <strong>{displayText(ref.title || ref.source_item_id, "来源")}</strong>
                <span>{[ref.source_item_id, ref.chunk_id].filter(Boolean).join(" / ")}</span>
                {snippet ? <p>{snippet}</p> : null}
              </button>
            );
          })}
          </div>
          <EvidenceWindow refItem={selectedRef} />
        </div>
      ) : null}
      {qualitySignals ? <AskQualitySignals signals={qualitySignals} /> : null}
      {diagnostics ? <AskNoAnswerDiagnostics diagnostics={diagnostics} /> : null}
      {noAnswerReasons.length || droppedCitations.length || gaps.length || conflicts.length ? (
        <div className="ask-gap-list">
          {noAnswerReasons.length ? <EvidenceNoteList title="为什么没找到" values={noAnswerReasons} /> : null}
          {droppedCitations.length ? <EvidenceNoteList title="丢弃的引用" values={droppedCitations} /> : null}
          {gaps.length ? <EvidenceNoteList title="缺口" values={gaps} /> : null}
          {conflicts.length ? <EvidenceNoteList title="冲突" values={conflicts} /> : null}
        </div>
      ) : null}
    </article>
  );
}

type MarkdownBlock =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; text: string }
  | { type: "unordered-list"; items: string[] }
  | { type: "ordered-list"; items: string[] }
  | { type: "code"; language: string; text: string }
  | { type: "table"; headers: string[]; rows: string[][] };

function MarkdownAnswer({ content, className = "" }: { content: string; className?: string }) {
  const blocks = parseMarkdownBlocks(displayText(content, ""));
  if (!blocks.length) {
    return null;
  }
  return (
    <div className={["answer-text", className].filter(Boolean).join(" ")}>
      {blocks.map((block, index) => renderMarkdownBlock(block, `answer-block-${index}`))}
    </div>
  );
}

function parseMarkdownBlocks(content: string): MarkdownBlock[] {
  const lines = content.replace(/\r\n?/g, "\n").trim().split("\n");
  const blocks: MarkdownBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^\s*```([A-Za-z0-9_-]+)?\s*$/);
    if (fence) {
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) {
        index += 1;
      }
      blocks.push({ type: "code", language: fence[1] || "", text: codeLines.join("\n") });
      continue;
    }

    const heading = line.match(/^\s*(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2].trim() });
      index += 1;
      continue;
    }

    if (isMarkdownTableStart(lines, index)) {
      const headers = parseMarkdownTableCells(lines[index]);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && isMarkdownTableRow(lines[index])) {
        rows.push(parseMarkdownTableCells(lines[index]));
        index += 1;
      }
      blocks.push({ type: "table", headers, rows });
      continue;
    }

    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    if (unordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*[-*]\s+(.+)$/);
        if (!item) {
          break;
        }
        items.push(item[1].trim());
        index += 1;
      }
      blocks.push({ type: "unordered-list", items });
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*\d+[.)]\s+(.+)$/);
        if (!item) {
          break;
        }
        items.push(item[1].trim());
        index += 1;
      }
      blocks.push({ type: "ordered-list", items });
      continue;
    }

    const paragraphLines: string[] = [];
    while (index < lines.length && lines[index].trim() && !isMarkdownBlockBoundary(lines, index)) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    if (paragraphLines.length) {
      blocks.push({ type: "paragraph", text: paragraphLines.join(" ") });
    } else {
      index += 1;
    }
  }

  return blocks;
}

function isMarkdownBlockBoundary(lines: string[], index: number) {
  const line = lines[index] || "";
  return (
    /^\s*```/.test(line) ||
    /^\s*#{1,6}\s+/.test(line) ||
    /^\s*[-*]\s+/.test(line) ||
    /^\s*\d+[.)]\s+/.test(line) ||
    isMarkdownTableStart(lines, index)
  );
}

function isMarkdownTableStart(lines: string[], index: number) {
  return isMarkdownTableRow(lines[index] || "") && isMarkdownTableSeparator(lines[index + 1] || "");
}

function isMarkdownTableRow(line: string) {
  const trimmed = line.trim();
  return trimmed.includes("|") && /^\|?.+\|.+\|?$/.test(trimmed);
}

function isMarkdownTableSeparator(line: string) {
  if (!isMarkdownTableRow(line)) {
    return false;
  }
  const cells = parseMarkdownTableCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s+/g, "")));
}

function parseMarkdownTableCells(line: string) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderMarkdownBlock(block: MarkdownBlock, key: string) {
  if (block.type === "heading") {
    return renderMarkdownHeading(block.level, block.text, key);
  }
  if (block.type === "paragraph") {
    return <p key={key}>{renderInlineMarkdown(block.text, key)}</p>;
  }
  if (block.type === "unordered-list") {
    return (
      <ul key={key}>
        {block.items.map((item, index) => (
          <li key={`${key}-item-${index}`}>{renderInlineMarkdown(item, `${key}-item-${index}`)}</li>
        ))}
      </ul>
    );
  }
  if (block.type === "ordered-list") {
    return (
      <ol key={key}>
        {block.items.map((item, index) => (
          <li key={`${key}-item-${index}`}>{renderInlineMarkdown(item, `${key}-item-${index}`)}</li>
        ))}
      </ol>
    );
  }
  if (block.type === "code") {
    return (
      <pre key={key} className="answer-code-block">
        <code>{block.text}</code>
      </pre>
    );
  }
  return (
    <div className="answer-table-wrap" key={key}>
      <table>
        <thead>
          <tr>
            {block.headers.map((header, index) => (
              <th key={`${key}-head-${index}`}>{renderInlineMarkdown(header, `${key}-head-${index}`)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, rowIndex) => (
            <tr key={`${key}-row-${rowIndex}`}>
              {block.headers.map((_, cellIndex) => (
                <td key={`${key}-cell-${rowIndex}-${cellIndex}`}>
                  {renderInlineMarkdown(row[cellIndex] || "", `${key}-cell-${rowIndex}-${cellIndex}`)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function renderMarkdownHeading(level: number, text: string, key: string) {
  if (level <= 2) {
    return (
      <h3 className={`answer-heading answer-heading-${level}`} key={key}>
        {renderInlineMarkdown(text, key)}
      </h3>
    );
  }
  if (level === 3) {
    return (
      <h4 className={`answer-heading answer-heading-${level}`} key={key}>
        {renderInlineMarkdown(text, key)}
      </h4>
    );
  }
  return (
    <h5 className={`answer-heading answer-heading-${level}`} key={key}>
      {renderInlineMarkdown(text, key)}
    </h5>
  );
}

function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let position = 0;
  let part = 0;

  function pushText(value: string) {
    if (value) {
      nodes.push(value);
    }
  }

  while (position < text.length) {
    const codeStart = text.indexOf("`", position);
    const boldStart = text.indexOf("**", position);
    const linkStart = text.indexOf("[", position);
    const candidates = [codeStart, boldStart, linkStart].filter((value) => value >= 0);
    const next = candidates.length ? Math.min(...candidates) : -1;

    if (next === -1) {
      pushText(text.slice(position));
      break;
    }

    pushText(text.slice(position, next));

    if (next === codeStart) {
      const end = text.indexOf("`", next + 1);
      if (end === -1) {
        pushText(text.slice(next));
        break;
      }
      nodes.push(<code key={`${keyPrefix}-code-${part}`}>{text.slice(next + 1, end)}</code>);
      part += 1;
      position = end + 1;
      continue;
    }

    if (next === boldStart) {
      const end = text.indexOf("**", next + 2);
      if (end === -1) {
        pushText(text.slice(next));
        break;
      }
      nodes.push(
        <strong key={`${keyPrefix}-strong-${part}`}>
          {renderInlineMarkdown(text.slice(next + 2, end), `${keyPrefix}-strong-${part}`)}
        </strong>
      );
      part += 1;
      position = end + 2;
      continue;
    }

    const link = parseInlineMarkdownLink(text, next);
    if (!link) {
      pushText(text[next]);
      position = next + 1;
      continue;
    }
    const safeHref = safeMarkdownHref(link.href);
    if (safeHref) {
      nodes.push(
        <a href={safeHref} key={`${keyPrefix}-link-${part}`} rel="noreferrer" target="_blank">
          {renderInlineMarkdown(link.label, `${keyPrefix}-link-${part}`)}
        </a>
      );
    } else {
      nodes.push(
        <span key={`${keyPrefix}-link-${part}`}>
          {renderInlineMarkdown(link.label, `${keyPrefix}-link-${part}`)}
        </span>
      );
    }
    part += 1;
    position = link.end;
  }

  return nodes;
}

function parseInlineMarkdownLink(text: string, start: number) {
  const labelEnd = text.indexOf("]", start + 1);
  if (labelEnd === -1 || text[labelEnd + 1] !== "(") {
    return null;
  }
  const hrefEnd = text.indexOf(")", labelEnd + 2);
  if (hrefEnd === -1) {
    return null;
  }
  return {
    label: text.slice(start + 1, labelEnd),
    href: text.slice(labelEnd + 2, hrefEnd).trim(),
    end: hrefEnd + 1
  };
}

function safeMarkdownHref(href: string) {
  if (/^(https?:|mailto:)/i.test(href) || href.startsWith("#")) {
    return href;
  }
  return "";
}

type AskAgentStepView = {
  id: string;
  phase: string;
  status: string;
  title: string;
  detail: string;
  meta: string;
};

type AskProgressView = {
  id: string;
  stage: string;
  status: string;
  title: string;
  meta: string;
};

function pendingAskResult(query: string): WorkspaceAskResponse {
  return {
    ok: true,
    query,
    answer: "",
    citations: [],
    source_refs: [],
    evidence: {},
    timing: {},
    agent_steps: [],
    progress: []
  };
}

function AskProgressStrip({ progress }: { progress: AskProgressView[] }) {
  const compact = progress.slice(-8);
  return (
    <div className="ask-progress-strip" aria-label="Ask progress">
      {compact.map((item) => (
        <span key={item.id} className={`ask-progress-chip ${item.status}`} data-stage={item.stage}>
          <strong>{askProgressStageLabel(item.stage)}</strong>
          <small>{item.meta || item.title}</small>
        </span>
      ))}
    </div>
  );
}

function pendingAskSteps(): AskAgentStepView[] {
  return [
    {
      id: "pending-stream",
      phase: "route",
      status: "running",
      title: "连接 Ask PSKA",
      detail: "正在建立流式查询，马上展示理解、检索和读取过程。",
      meta: ""
    }
  ];
}

function progressToAgentSteps(progress: AskProgressView[]): AskAgentStepView[] {
  return progress.map((item, index) => ({
    id: item.id || `progress-step-${index}`,
    phase: item.stage || "progress",
    status: item.status || "complete",
    title: item.title || askProgressStageLabel(item.stage),
    detail: askProgressStageDetail(item.stage, item.status),
    meta: item.meta
  }));
}

function AskProcessTimeline({
  steps,
  rawEvents,
  defaultOpen = false
}: {
  steps: AskAgentStepView[];
  rawEvents: Array<Record<string, unknown>>;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  useEffect(() => {
    setOpen(defaultOpen);
  }, [defaultOpen]);
  const visibleHead = steps.slice(0, 6);
  const visibleTail = steps.length > 12 ? steps.slice(-6) : steps.slice(6, 12);
  const omittedCount = Math.max(0, steps.length - visibleHead.length - visibleTail.length);
  const eventSummaries = rawEvents
    .slice(0, 40)
    .map((event) => summarizeAgenticEvent(event))
    .filter((event): event is AgenticEventSummary => Boolean(event));
  return (
    <div className="ask-process">
      {steps.length ? (
        <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
          <summary>
            <span>检索过程</span>
            <small>{steps.length} 步</small>
          </summary>
          <ol className="ask-process-list">
            {visibleHead.map((step) => (
              <li key={step.id} data-phase={step.phase} data-status={step.status}>
                <strong>{step.title}</strong>
                {step.detail ? <p>{step.detail}</p> : null}
                {step.meta ? <small>{step.meta}</small> : null}
              </li>
            ))}
            {omittedCount ? (
              <li data-phase="omitted" data-status="complete">
                <strong>中间过程已折叠</strong>
                <p>已省略 {omittedCount} 步检索与读取事件，保留开头规划和最终收口。</p>
              </li>
            ) : null}
            {visibleTail.map((step) => (
              <li key={step.id} data-phase={step.phase} data-status={step.status}>
                <strong>{step.title}</strong>
                {step.detail ? <p>{step.detail}</p> : null}
                {step.meta ? <small>{step.meta}</small> : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
      {eventSummaries.length ? (
        <details className="ask-raw-events">
          <summary>
            <span>事件摘要</span>
            <small>{rawEvents.length}</small>
          </summary>
          <ol className="ask-process-list">
            {eventSummaries.map((event, index) => (
              <li key={`${event.type}-${index}`} data-phase={event.type} data-status="complete">
                <strong>{event.type}</strong>
                {event.message ? <p>{event.message}</p> : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </div>
  );
}

function EvidenceNoteList({ title, values }: { title: string; values: string[] }) {
  return (
    <div className="ask-gap-section">
      <strong>{title}</strong>
      <ul>
        {values.slice(0, 4).map((value, index) => (
          <li key={`${title}-${index}`}>{value}</li>
        ))}
      </ul>
    </div>
  );
}

type AskNoAnswerDiagnostic = {
  primaryReason?: string;
  reasons: string[];
  dimensions: Array<{ dimension: string; status: string; detail: string }>;
};

function EvidenceWindow({ refItem }: { refItem?: SearchEvidenceRef }) {
  if (!refItem) {
    return null;
  }
  const coordinates = [
    refItem.source_item_id ? `source ${refItem.source_item_id}` : "",
    refItem.document_id ? `doc ${refItem.document_id}` : "",
    refItem.chunk_id ? `chunk ${refItem.chunk_id}` : "",
    refItem.passage_window_id ? `passage ${refItem.passage_window_id}` : ""
  ].filter(Boolean);
  return (
    <aside className="evidence-window" aria-label="证据窗口">
      <div className="card-row">
        <span className="pill">Evidence</span>
        {typeof refItem.score === "number" ? <small>score {Math.round(refItem.score * 100)}</small> : null}
      </div>
      <strong>{displayText(refItem.title || refItem.source_item_id, "来源")}</strong>
      {coordinates.length ? <code>{coordinates.join(" / ")}</code> : null}
      {refItem.url ? <a href={refItem.url} target="_blank" rel="noreferrer">{refItem.url}</a> : null}
      <p>{displayText(refItem.source_window?.text || refItem.snippet, "该引用没有返回可预览文本。")}</p>
    </aside>
  );
}

function AskNoAnswerDiagnostics({ diagnostics }: { diagnostics: AskNoAnswerDiagnostic }) {
  const visible = diagnostics.dimensions.filter((item) => item.status !== "ok" && item.status !== "not_applicable");
  if (!visible.length) {
    return null;
  }
  return (
    <div className="ask-diagnostics-panel">
      <div className="card-row">
        <strong>为什么还不能直接采信</strong>
        {diagnostics.primaryReason ? <span className="pill warning">{askDiagnosticLabel(diagnostics.primaryReason)}</span> : null}
      </div>
      <ul>
        {visible.slice(0, 6).map((item) => (
          <li key={`${item.dimension}-${item.status}`}>
            <span>{askDiagnosticDimensionLabel(item.dimension)}</span>
            <p>{askDiagnosticLabel(item.status)}：{item.detail}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

function AskQualitySignals({ signals }: { signals: Record<string, unknown> }) {
  const qualityBand = displayText(signals.quality_band, "");
  const evidenceStatus = displayText(signals.evidence_status, "");
  const reportReadiness = displayText(signals.report_readiness, "");
  const citations = numberSignal(signals.citation_count);
  const results = numberSignal(signals.evidence_result_count);
  const gaps = numberSignal(signals.gap_count);
  const conflicts = numberSignal(signals.conflict_count);
  const chips = [
    qualityBand ? askQualityBandLabel(qualityBand) : "",
    evidenceStatus ? askEvidenceStatusLabel(evidenceStatus) : "",
    reportReadiness ? askReportReadinessLabel(reportReadiness) : "",
    `引用 ${citations}`,
    `证据 ${results}`,
    gaps ? `缺口 ${gaps}` : "",
    conflicts ? `冲突 ${conflicts}` : ""
  ].filter(Boolean);
  if (!chips.length) {
    return null;
  }
  return (
    <div className="ask-quality-strip" aria-label="Ask quality signals">
      {chips.map((chip) => (
        <span key={chip}>{chip}</span>
      ))}
    </div>
  );
}

function normalizeAskNoAnswerDiagnostics(value: unknown): AskNoAnswerDiagnostic | null {
  if (!isRecord(value)) {
    return null;
  }
  const dimensions = Array.isArray(value.dimensions)
    ? value.dimensions
        .map((item) => {
          if (!isRecord(item)) {
            return null;
          }
          return {
            dimension: displayText(item.dimension, "unknown"),
            status: displayText(item.status, "unknown"),
            detail: displayText(item.detail, "")
          };
        })
        .filter((item): item is AskNoAnswerDiagnostic["dimensions"][number] => Boolean(item))
    : [];
  if (!dimensions.length) {
    return null;
  }
  const reasons = Array.isArray(value.reasons) ? value.reasons.map((item) => displayText(item, "")).filter(Boolean) : [];
  return {
    primaryReason: displayText(value.primary_reason, ""),
    reasons,
    dimensions
  };
}

function askDiagnosticDimensionLabel(value: string) {
  const labels: Record<string, string> = {
    evidence: "证据",
    retrieval: "检索",
    evidence_check: "证据校验",
    fastreact: "FastReAct",
    mcp: "MCP",
    permissions: "权限/可见性",
    answer: "回答"
  };
  return labels[value] || value;
}

function askDiagnosticLabel(value: string) {
  const labels: Record<string, string> = {
    no_visible_evidence: "没有可见证据",
    no_relevant_chunks: "没有相关片段",
    insufficient_evidence: "证据不足",
    conflicts_detected: "存在冲突",
    not_enough_signal: "信号不足",
    missing_citations: "缺少引用",
    source_refs_not_visible: "引用不可见",
    possibly_filtered_or_unindexed: "可能未索引或不可见",
    empty_answer: "空回答",
    uncited_answer: "回答未引用",
    agentic_service_unavailable: "FastReAct 不可用",
    fallback: "已降级"
  };
  return labels[value] || value;
}

function numberSignal(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function askQualityBandLabel(value: string) {
  const labels: Record<string, string> = {
    direct_answer: "直接回答",
    grounded: "有引用",
    no_answerable_evidence: "无可答证据",
    needs_review: "需复核",
    needs_citation_review: "需补引用",
    failed: "失败"
  };
  return labels[value] || value;
}

function askEvidenceStatusLabel(value: string) {
  const labels: Record<string, string> = {
    not_applicable: "无需证据",
    grounded: "证据已引用",
    retrieved_without_citations: "检索未引用",
    no_evidence: "未检索到证据",
    insufficient_evidence: "证据不足"
  };
  return labels[value] || value;
}

function askReportReadinessLabel(value: string) {
  const labels: Record<string, string> = {
    ready_with_citations: "可入报告",
    needs_human_review: "人工复核",
    needs_citation_review: "引用复核",
    not_ready: "不可入报告",
    failed: "不可用"
  };
  return labels[value] || value;
}

type AgenticEventSummary = {
  type: string;
  message: string;
};

type SearchEvidenceRef = {
  title: string;
  snippet: string;
  source_window?: { text?: string; window_policy?: string; start_char?: number; end_char?: number };
  source_item_id?: string;
  document_id?: string;
  chunk_id?: string;
  passage_window_id?: string;
  url?: string;
  score?: number;
};

function normalizeSearchRefs(values: unknown[]): SearchEvidenceRef[] {
  const merged = new Map<string, SearchEvidenceRef>();
  const order: string[] = [];
  values
    .map((value) => {
      const ref = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
      const sourceWindow = isRecord(ref.source_window) ? ref.source_window : undefined;
      return {
        title: displayText(ref.title, ""),
        snippet: cleanEvidenceSnippet(ref.snippet || sourceWindow?.text),
        source_window: sourceWindow
          ? {
              text: displayText(sourceWindow.text, ""),
              window_policy: displayText(sourceWindow.window_policy, "") || undefined,
              start_char: typeof sourceWindow.start_char === "number" ? sourceWindow.start_char : undefined,
              end_char: typeof sourceWindow.end_char === "number" ? sourceWindow.end_char : undefined
            }
          : undefined,
        source_item_id: displayText(ref.source_item_id, "") || undefined,
        document_id: displayText(ref.document_id, "") || undefined,
        chunk_id: displayText(ref.chunk_id || ref.result_id, "") || undefined,
        passage_window_id: displayText(ref.passage_window_id, "") || undefined,
        url: displayText(ref.url, "") || undefined,
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
      if (!current.document_id && ref.document_id) {
        current.document_id = ref.document_id;
      }
      if (!current.chunk_id && ref.chunk_id) {
        current.chunk_id = ref.chunk_id;
      }
      if (!current.passage_window_id && ref.passage_window_id) {
        current.passage_window_id = ref.passage_window_id;
      }
      if (!current.url && ref.url) {
        current.url = ref.url;
      }
      if (ref.snippet && (!current.snippet || ref.snippet.length > current.snippet.length)) {
        current.snippet = ref.snippet;
      }
      if (!current.source_window && ref.source_window) {
        current.source_window = ref.source_window;
      }
      if (typeof ref.score === "number" && (typeof current.score !== "number" || ref.score > current.score)) {
        current.score = ref.score;
      }
    });
  return order.map((key) => merged.get(key)).filter((ref): ref is SearchEvidenceRef => Boolean(ref));
}

function searchRefKey(ref: SearchEvidenceRef) {
  const sourceId = normalizeSearchRefIdentity(ref.source_item_id);
  const chunkId = normalizeSearchRefIdentity(ref.chunk_id);
  if (sourceId) {
    return `source:${sourceId}`;
  }
  if (chunkId) {
    return `source:${sourceId}:chunk:${chunkId}`;
  }
  const passageId = normalizeSearchRefIdentity(ref.passage_window_id);
  if (passageId) {
    return `passage:${passageId}`;
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

function normalizeAskNotes(values: unknown): string[] {
  if (!Array.isArray(values)) {
    return [];
  }
  return values
    .map((value) => {
      if (typeof value === "string") {
        return trimText(value, 220);
      }
      if (isRecord(value)) {
        return trimText(firstString(value.message, value.detail, value.summary, value.reason) || compactJson(value), 220);
      }
      return trimText(String(value), 220);
    })
    .filter(Boolean);
}

function normalizeDroppedCitations(values: unknown): string[] {
  if (!Array.isArray(values)) {
    return [];
  }
  return values
    .map((value) => {
      if (!isRecord(value)) {
        return "";
      }
      const reason = askDiagnosticLabel(displayText(value.drop_reason, "dropped"));
      const source = displayText(value.title || value.source_item_id || value.chunk_id, "引用");
      return trimText(`${source}：${reason}`, 220);
    })
    .filter(Boolean);
}

function normalizeAskAgentSteps(values: unknown): AskAgentStepView[] {
  if (!Array.isArray(values)) {
    return [];
  }
  return values
    .map((value, index) => {
      if (!isRecord(value)) {
        return null;
      }
      const elapsed = typeof value.elapsed_ms === "number" && Number.isFinite(value.elapsed_ms)
        ? `${Math.round(value.elapsed_ms)} ms`
        : "";
      const evidence = typeof value.evidence_count === "number" && value.evidence_count > 0
        ? `证据 ${value.evidence_count}`
        : "";
      const refs = typeof value.source_ref_count === "number" && value.source_ref_count > 0
        ? `引用 ${value.source_ref_count}`
        : "";
      const tool = displayText(value.tool_name, "");
      return {
        id: displayText(value.step_id, `step-${index}`),
        phase: displayText(value.phase, "step"),
        status: displayText(value.status, "complete"),
        title: displayText(value.title, "处理中"),
        detail: trimText(displayText(value.detail, ""), 180),
        meta: [tool, evidence, refs, elapsed].filter(Boolean).join(" · ")
      };
    })
    .filter((step): step is AskAgentStepView => Boolean(step));
}

function normalizeAskProgress(values: unknown): AskProgressView[] {
  if (!Array.isArray(values)) {
    return [];
  }
  return values
    .map((value, index) => {
      if (!isRecord(value)) {
        return null;
      }
      const elapsed = typeof value.elapsed_ms === "number" && Number.isFinite(value.elapsed_ms)
        ? `${Math.round(value.elapsed_ms)} ms`
        : "";
      const evidence = typeof value.evidence_count === "number" && value.evidence_count > 0
        ? `证据 ${value.evidence_count}`
        : "";
      const refs = typeof value.source_ref_count === "number" && value.source_ref_count > 0
        ? `引用 ${value.source_ref_count}`
        : "";
      return {
        id: displayText(value.step_id, `progress-${index}`),
        stage: displayText(value.stage, "generate"),
        status: displayText(value.status, "complete"),
        title: displayText(value.title, "处理中"),
        meta: [evidence, refs, elapsed].filter(Boolean).join(" · ")
      };
    })
    .filter((item): item is AskProgressView => Boolean(item));
}

function askProgressStageLabel(stage: string) {
  switch (stage) {
    case "query_understand":
    case "understand":
      return "理解";
    case "search":
      return "检索";
    case "rerank":
      return "重排";
    case "graph":
      return "图谱";
    case "read":
      return "读取";
    case "evidence_check":
      return "校验";
    case "generate":
      return "生成";
    default:
      return displayText(stage, "处理");
  }
}

function askProgressStageDetail(stage: string, status: string) {
  const suffix = status === "running" ? "进行中" : status === "error" ? "失败" : "完成";
  switch (stage) {
    case "query_understand":
    case "understand":
      return `识别意图、范围和是否需要检索，${suffix}。`;
    case "search":
      return `按当前范围检索资料库或附件，${suffix}。`;
    case "rerank":
      return `重排候选证据，${suffix}。`;
    case "graph":
      return `展开可用的强支撑图谱路径，${suffix}。`;
    case "read":
      return `回读 source window 或 parent window，${suffix}。`;
    case "evidence_check":
      return `校验引用是否有窗口、范围和答案支撑，${suffix}。`;
    case "generate":
      return `生成最终回答，${suffix}。`;
    default:
      return `Ask PSKA 过程事件，${suffix}。`;
  }
}

function askIntentLabel(intent?: string) {
  switch (intent) {
    case "greeting":
      return "问候";
    case "chitchat":
      return "闲聊";
    case "product_help":
      return "产品帮助";
    case "doc_only":
      return "只看附件";
    case "follow_up":
      return "追问";
    case "clarification":
      return "澄清";
    case "graph_research":
      return "图谱研究";
    case "writing":
      return "写作";
    case "kb_search":
      return "资料库检索";
    default:
      return displayText(intent, "Ask");
  }
}

function askAnswerTypeLabel(answerType?: string) {
  switch (answerType) {
    case "direct_greeting":
      return "直接回应";
    case "product_help":
      return "产品说明";
    case "kb_answer":
      return "证据回答";
    case "deep_answer":
      return "深入回答";
    case "no_answer":
      return "证据不足";
    case "clarification":
      return "需要澄清";
    default:
      return displayText(answerType, "回答");
  }
}

function askRouteLabel(route?: WorkspaceAskResponse["route"]) {
  const selected = route?.selected_intent || route?.intent;
  const label = selected === "deep" ? "深入分析" : selected === "quick" ? "快速回答" : "自动路由";
  return route?.fallback_from ? `${label} · 已降级` : label;
}

function askFallbackLabel(reason?: string) {
  if (!reason) {
    return "";
  }
  if (reason === "agentic_service_unavailable") {
    return "深入分析暂不可用，已使用快速回答。";
  }
  return `已使用备用回答：${displayText(reason)}`;
}

function buildAskMarkdown(query: string, answer: string, refs: SearchEvidenceRef[], gaps: string[], conflicts: string[]) {
  const lines = [
    query ? `## ${query}` : "## Ask PSKA",
    "",
    answer || "PSKA 没有生成可复制的回答。",
    ""
  ];
  if (refs.length > 0) {
    lines.push("### 引用");
    refs.slice(0, 8).forEach((ref, index) => {
      const title = ref.title || ref.source_item_id || `来源 ${index + 1}`;
      const source = ref.source_item_id ? ` (${ref.source_item_id})` : "";
      lines.push(`${index + 1}. ${title}${source}`);
      if (ref.snippet) {
        lines.push(`   - ${trimText(cleanEvidenceSnippet(ref.snippet), 220)}`);
      }
    });
    lines.push("");
  }
  if (gaps.length > 0) {
    lines.push("### 缺口");
    gaps.slice(0, 6).forEach((gap) => lines.push(`- ${gap}`));
    lines.push("");
  }
  if (conflicts.length > 0) {
    lines.push("### 冲突");
    conflicts.slice(0, 6).forEach((conflict) => lines.push(`- ${conflict}`));
    lines.push("");
  }
  return lines.join("\n").trim();
}

function agenticTraceEvents(result: WorkspaceAskResponse | WorkspaceSearchResponse): Array<Record<string, unknown>> {
  const trace = (result as { trace?: Record<string, unknown> }).trace;
  return Array.isArray(trace?.events) ? trace.events as Array<Record<string, unknown>> : [];
}

function finalAnswerFromTraceEvents(result: WorkspaceAskResponse | WorkspaceSearchResponse) {
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

function summarizeAgenticEvents(result: WorkspaceAskResponse | WorkspaceSearchResponse): AgenticEventSummary[] {
  const events = agenticTraceEvents(result);
  const summaries = events
    .map((event) => summarizeAgenticEvent(event))
    .filter((event): event is AgenticEventSummary => Boolean(event));
  if (summaries.length > 0) {
    return summaries;
  }
  const trace = (result as { trace?: Record<string, unknown> }).trace;
  const toolCalls = Array.isArray(trace?.tool_calls) ? trace.tool_calls as Array<Record<string, unknown>> : [];
  return toolCalls.map((call) => ({
    type: displayText(asString(call.tool_name), "tool_call"),
    message: compactJson(call.tool_args)
  }));
}

function summarizeAgenticEvent(event: Record<string, unknown>): AgenticEventSummary | null {
  const type = displayText(asString(event.type || event.event_type), "event");
  if (type === "session_start") {
    return { type, message: "已创建当前用户与租户范围内的分析会话。" };
  }
  if (type === "tool_call") {
    return {
      type: displayText(asString(event.tool_name), "tool_call"),
      message: compactJson(event.tool_args || event.args || event.arguments)
    };
  }
  if (type === "tool_result") {
    return {
      type: `${displayText(asString(event.tool_name), "tool_result")} result`,
      message: toolResultSummary(event)
    };
  }
  if (type === "session_end") {
    return {
      type: "session_end",
      message: "已形成最终回答。"
    };
  }
  const metadata = isRecord(event.metadata) ? event.metadata : {};
  const message = firstString(event.message, event.content, metadata.message, metadata.status, metadata.detail);
  return message ? { type, message: trimText(message, 260) } : null;
}

function toolResultSummary(event: Record<string, unknown>) {
  const summary = isRecord(event.result_summary) ? event.result_summary : {};
  const evidence = typeof summary.evidence_count === "number" ? `证据 ${summary.evidence_count}` : "";
  const refs = typeof summary.source_ref_count === "number" ? `引用 ${summary.source_ref_count}` : "";
  const pieces = [evidence, refs].filter(Boolean);
  return pieces.length ? pieces.join(" · ") : "已返回工具结果。";
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

function displaySearchError(error: WorkspaceAskResponse["error"] | WorkspaceSearchResponse["error"]) {
  if (!error) {
    return "PSKA 查询失败。";
  }
  if (typeof error === "string") {
    return error;
  }
  return displayText(error.message || error.detail || error.type, "PSKA 查询失败。");
}

function searchToBrain(result: WorkspaceAskResponse | WorkspaceSearchResponse, query: string): Partial<BrainState> {
  const askEvidence = (result as WorkspaceAskResponse).evidence;
  const workspaceEvidence = (result as WorkspaceSearchResponse).workspace?.evidence;
  const retrieval = (result as WorkspaceSearchResponse).retrieval;
  const fallback = (result as WorkspaceSearchResponse).fallback;
  const parsed = parseAgenticAnswer(result.answer);
  const eventAnswer = finalAnswerFromTraceEvents(result);
  const answer = cleanAgenticAnswer(parsed?.answer || result.answer || eventAnswer || "");
  const refs = normalizeSearchRefs([
    ...(parsed?.source_refs || []),
    ...(parsed?.citations || []),
    ...(result.source_refs || []),
    ...(result.citations || []),
    ...(askEvidence?.citations || []),
    ...(askEvidence?.results || []),
    ...(workspaceEvidence?.citations || []),
    ...(retrieval?.results || []),
    ...(fallback?.retrieval?.citations || []),
    ...(fallback?.retrieval?.results || [])
  ])
    .map((ref, index) => ({
      id: `today-search-${index}`,
      title: displayText(ref.title || ref.source_item_id, query),
      score: typeof ref.score === "number" ? Math.round(ref.score * 100) : undefined,
      snippet: displayText(ref.snippet || answer, "PSKA 返回了相关证据。"),
      source: "PSKA evidence"
    }))
    .filter((item) => item.title || item.snippet);
  return {
    status: result.error ? "error" : "synced",
    lastTrigger: "manual",
    updatedAt: Date.now(),
    error: result.error ? displaySearchError(result.error) : null,
    relatedKnowledge: [
      ...(answer ? [{ id: "today-answer", title: query, snippet: displayText(answer), source: "Ask PSKA" }] : []),
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

function cleanEvidenceSnippet(value: unknown) {
  let text = displayText(value, "").trim();
  if (!text) {
    return "";
  }
  text = text.replace(/^\s*---[\s\S]*?---\s*/m, "");
  text = text.replace(/\s*\|\s*-{2,}\s*(?:\|\s*-{2,}\s*)+\|?/g, " ");
  const lines = text
    .split(/\n+/)
    .map((line) => line.trim())
    .filter((line) => {
      if (!line) {
        return false;
      }
      if (/^(title|type|slug|aliases|date|attendees|tags):/i.test(line)) {
        return false;
      }
      if (/^\|.*\|$/.test(line)) {
        return false;
      }
      if (/^[-:| ]{5,}$/.test(line)) {
        return false;
      }
      return true;
    })
    .map((line) => line.replace(/^#{1,6}\s*/, ""));
  text = lines.join(" ");
  text = text.replace(/\b#{1,6}\s*/g, "");
  text = text.replace(/\s*\|\s*/g, " / ");
  text = text.replace(/\s*\/\s*\/\s*/g, " / ");
  return text.replace(/\s+/g, " ").trim();
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

function reviewQualityTierLabel(value?: string) {
  if (value === "strong") {
    return "强支撑";
  }
  if (value === "diagnostic") {
    return "诊断信号";
  }
  if (value === "weak") {
    return "弱信号";
  }
  return displayText(value, "质量未标注");
}

function reviewPromotionReasonLabel(value?: string) {
  const labels: Record<string, string> = {
    source_title: "来自标题",
    document_title: "来自文档标题",
    document_heading: "来自标题层级",
    source_ref_claim: "来自 Claim 证据",
    digest_note: "来自 Digest 证据",
    entity: "来自实体",
    hyperedge: "来自关系",
    lexical_overlap: "文本重合"
  };
  return labels[value || ""] || displayText(value, "依据已记录");
}

function reviewSupportKindLabel(value?: string) {
  const labels: Record<string, string> = {
    source_title: "资料标题",
    document_title: "文档标题",
    document_heading: "文档 heading",
    source_ref_claim: "Claim + source_refs",
    digest_note: "Digest note + source_refs",
    entity: "实体候选",
    hyperedge: "关系候选",
    lexical_chunk: "片段词面信号",
    document_body: "正文词面信号"
  };
  return labels[value || ""] || displayText(value, "");
}

function reviewSupportBasis(item: ReviewCenterItem) {
  const proposal = isRecord(item.proposal) ? item.proposal : {};
  const rawKinds = Array.isArray(item.support_kinds) && item.support_kinds.length
    ? item.support_kinds
    : Array.isArray(proposal.support_kinds)
      ? proposal.support_kinds
      : [];
  const values = rawKinds.map((kind) => reviewSupportKindLabel(displayText(kind, ""))).filter(Boolean);
  return Array.from(new Set(values));
}

function reviewProposalSummary(item: ReviewCenterItem) {
  const proposal = isRecord(item.proposal) ? item.proposal : {};
  const lifecycle = isRecord(proposal.lifecycle) ? proposal.lifecycle : {};
  if (displayText(lifecycle.status, "") === "stale") {
    return trimText(`证据状态已变化：${displayText(lifecycle.reason, "evidence_removed")}。请确认是否保留、移除或替换这条长期知识。`, 260);
  }
  return trimText(firstString(
    proposal.summary,
    proposal.statement,
    proposal.description,
    proposal.reason,
    proposal.promotion_reason
  ), 260);
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
  onOpenWorkspace,
  onPinCurrent,
  pinStatus,
  setBrain
}: {
  serviceToken: PSKAAuth;
  onOpenWorkspace: (mode: WorkspaceMode) => void;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
  setBrain: (brain: Partial<BrainState>) => void;
}) {
  const [query, setQuery] = useState("");
  const [textSourceTitle, setTextSourceTitle] = useState("");
  const [textSourceBody, setTextSourceBody] = useState("");
  const [uploadDigestAfter, setUploadDigestAfter] = useState(true);
  const [documentDeletePreview, setDocumentDeletePreview] = useState<WorkspaceDocumentDeleteResponse | null>(null);
  const [documentDeleteTarget, setDocumentDeleteTarget] = useState("");
  const [promptAsk, setPromptAsk] = useState("");
  const [promptDigest, setPromptDigest] = useState("");
  const [promptReview, setPromptReview] = useState("");
  const [promptWriting, setPromptWriting] = useState("");
  const [promptStatus, setPromptStatus] = useState<"idle" | "saving" | "success" | "error">("idle");
  const [promptError, setPromptError] = useState("");
  const [chunkPreviewText, setChunkPreviewText] = useState("");
  const [chunkPreviewStrategy, setChunkPreviewStrategy] = useState("auto");
  const [chunkPreviewSize, setChunkPreviewSize] = useState(1200);
  const [chunkPreviewResult, setChunkPreviewResult] = useState<ChunkingPreviewResponse | null>(null);
  const [chunkPreviewStatus, setChunkPreviewStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [chunkPreviewError, setChunkPreviewError] = useState("");
  const [sourceFormKind, setSourceFormKind] = useState<"url" | "rss" | "folder">("url");
  const [sourceFormValue, setSourceFormValue] = useState("");
  const [sourceFormName, setSourceFormName] = useState("");
  const [sourcePreview, setSourcePreview] = useState<SourcePreviewResponse | null>(null);
  const [sourceFormStatus, setSourceFormStatus] = useState<"idle" | "previewing" | "adding" | "syncing" | "success" | "error">("idle");
  const [sourceFormError, setSourceFormError] = useState("");
  const [operationStatus, setOperationStatus] = useState<"idle" | "syncing" | "digesting" | "queued" | "cleaning" | "briefing" | "success" | "error">("idle");
  const [operationMessage, setOperationMessage] = useState("");
  const [cleanupPreview, setCleanupPreview] = useState<KnowledgeSourceCleanupResponse | null>(null);
  const [cleanupTargetId, setCleanupTargetId] = useState<string | null>(null);
  const [cleanupConfirmText, setCleanupConfirmText] = useState("");
  const [briefingJobId, setBriefingJobId] = useState<string | null>(null);
  const [trackedDigestJobIds, setTrackedDigestJobIds] = useState<string[]>([]);
  const [operationSummary, setOperationSummary] = useState<{
    scanned?: number;
    ingested?: number;
    changed?: number;
    failed?: number;
    scheduled?: number;
    queuedJobs?: number;
    skipped?: number;
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
    refetchInterval: operationStatus === "queued" || operationStatus === "digesting" ? 3000 : false,
    retry: 1
  });
  const documentsQuery = useQuery({
    queryKey: ["workspace-documents", serviceToken],
    queryFn: () => loadWorkspaceDocuments(serviceToken, true),
    retry: 1
  });
  const promptProfilesQuery = useQuery({
    queryKey: ["prompt-profiles", serviceToken],
    queryFn: () => loadPromptProfiles(serviceToken),
    retry: 1
  });
  const corpus = corpusQuery.data;
  const sourceSummary = sourcesQuery.data;
  const digestLogs = digestLogsQuery.data;
  const documentsData = documentsQuery.data;
  const promptProfiles = promptProfilesQuery.data;
  const normalizedQuery = query.trim().toLowerCase();
  const filteredChunks = (corpus?.chunks || []).filter((chunk) =>
    corpusText([chunk.title, chunk.source_channel, chunk.source_item_id, chunk.text, chunk.snippet]).includes(normalizedQuery)
  );
  const counts = {
    sources: documentsData?.counts?.active ?? sourceSummary?.source_counts?.source_items ?? corpus?.counts?.sources_total ?? corpus?.sources?.length ?? 0,
    documents: documentsData?.counts?.documents ?? sourceSummary?.source_counts?.documents ?? corpus?.counts?.documents ?? 0,
    chunks: sourceSummary?.source_counts?.chunks ?? corpus?.counts?.chunks_matching ?? corpus?.chunks?.length ?? 0,
    inputSources: sourceSummary?.input_sources?.length ?? sourceSummary?.knowledge_sources?.source_count ?? sourceSummary?.connector_state?.state_count ?? 0
  };
  const actionRunning = operationStatus === "syncing" || operationStatus === "digesting" || operationStatus === "cleaning" || operationStatus === "briefing";
  const statusMessage = operationMessage || latestSyncMessage(sourceSummary);

  useEffect(() => {
    if (corpus) {
      setBrain(corpusToBrain(corpus));
    }
  }, [corpus, setBrain]);

  useEffect(() => {
    const effective = promptProfiles?.effective || {};
    setPromptAsk(String((effective.ask?.config?.personal_instruction || effective.ask?.config?.custom_instruction || "") ?? ""));
    setPromptDigest(String((effective.digest?.config?.focus || effective.digest?.config?.custom_instruction || "") ?? ""));
    setPromptReview(String((effective.review?.config?.review_policy || effective.review?.config?.custom_instruction || "") ?? ""));
    setPromptWriting(String((effective.writing?.config?.tone || effective.writing?.config?.custom_instruction || "") ?? ""));
  }, [promptProfilesQuery.dataUpdatedAt]);

  useEffect(() => {
    if (operationStatus !== "queued" || trackedDigestJobIds.length === 0) {
      return;
    }
    const trackedLogs = (digestLogs?.logs || []).filter((log) => trackedDigestJobIds.includes(log.job_id));
    if (trackedLogs.length === 0) {
      return;
    }
    const active = trackedLogs.some((log) => log.status === "queued" || log.status === "running");
    const failed = trackedLogs.find((log) => log.status === "failed" || log.status === "canceled");
    if (failed) {
      setOperationStatus("error");
      setOperationMessage(`Digest 后台任务没有完成：${displayText(failed.error || failed.latest_event?.message, failed.status || "failed")}。`);
      return;
    }
    if (!active && trackedLogs.every((log) => log.status === "succeeded")) {
      setOperationStatus("success");
      setOperationMessage("Digest 后台任务已完成，Review、Discoveries 和 Brief 入口已刷新。");
    }
  }, [operationStatus, trackedDigestJobIds, digestLogsQuery.dataUpdatedAt]);

  async function refetchAll() {
    await Promise.all([corpusQuery.refetch(), sourcesQuery.refetch(), digestLogsQuery.refetch(), documentsQuery.refetch()]);
  }

  async function handleCreateTextSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = textSourceBody.trim();
    if (!text) {
      return;
    }
    setOperationStatus("syncing");
    setOperationMessage("正在把粘贴文本加入资料库...");
    setOperationSummary(undefined);
    try {
      const payload = await createTextSource(serviceToken, {
        title: textSourceTitle.trim() || undefined,
        text,
        digest_mode: uploadDigestAfter ? "after_upload" : "manual"
      });
      const summary = sourceIngestSummary(payload);
      setOperationStatus(payload.ok === false ? "error" : "success");
      setOperationSummary(summary);
      setOperationMessage(payload.ok === false ? payload.error || "添加文本资料失败。" : summaryMessage(summary));
      setTextSourceTitle("");
      setTextSourceBody("");
      await refetchAll();
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "添加文本资料失败。");
    }
  }

  async function handleUploadSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem("source-file") as HTMLInputElement | null;
    const file = input?.files?.[0];
    if (!file) {
      return;
    }
    setOperationStatus("syncing");
    setOperationMessage("正在上传并处理资料...");
    setOperationSummary(undefined);
    try {
      const payload = await uploadWorkspaceSource(serviceToken, file, {
        digest_mode: uploadDigestAfter ? "after_upload" : "manual"
      });
      const summary = sourceIngestSummary(payload);
      setOperationStatus(payload.ok === false ? "error" : "success");
      setOperationSummary(summary);
      setOperationMessage(payload.ok === false ? payload.error || "上传资料失败。" : summaryMessage(summary));
      input.value = "";
      await refetchAll();
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "上传资料失败。");
    }
  }

  async function handleDocumentLifecycle(sourceItemId: string, execute: boolean, restore = false, hardDelete = false) {
    setOperationStatus("cleaning");
    setDocumentDeleteTarget(sourceItemId);
    setOperationMessage(execute ? "正在更新资料删除状态..." : "正在预览资料删除影响...");
    try {
      const payload = await deleteWorkspaceDocuments(serviceToken, [sourceItemId], {
        execute,
        restore,
        hardDelete,
        reason: restore ? "frontend restore document" : "frontend document delete"
      });
      setDocumentDeletePreview(payload);
      setOperationStatus(payload.ok === false ? "error" : "success");
      const counts = payload.deleted || payload.counts || {};
      setOperationMessage(execute ? documentLifecycleDoneMessage(counts, restore) : documentLifecyclePreviewMessage(counts));
      if (execute) {
        await refetchAll();
      }
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "资料删除操作失败。");
    } finally {
      setDocumentDeleteTarget("");
    }
  }

  async function handleSavePromptProfiles(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPromptStatus("saving");
    setPromptError("");
    try {
      const payload = await updatePromptProfiles(serviceToken, [
        { profile_type: "ask", scope: "user", name: "个人 Ask Profile", config: { personal_instruction: promptAsk.trim() } },
        { profile_type: "digest", scope: "user", name: "个人 Digest Profile", config: { focus: promptDigest.trim() } },
        { profile_type: "review", scope: "user", name: "个人 Review Profile", config: { review_policy: promptReview.trim() } },
        { profile_type: "writing", scope: "user", name: "个人 Writing Profile", config: { tone: promptWriting.trim() } }
      ]);
      setPromptStatus(payload.ok === false ? "error" : "success");
      setPromptError(payload.error || "");
      await promptProfilesQuery.refetch();
    } catch (error) {
      setPromptStatus("error");
      setPromptError(error instanceof Error ? error.message : "Prompt Profiles 保存失败。");
    }
  }

  async function handleFileSync() {
    setOperationStatus("syncing");
    setOperationMessage("正在同步当前账号的高级资料源...");
    setOperationSummary(undefined);
    try {
      const payload = await syncKnowledgeSources(serviceToken, undefined, { sourceTypes: ["upload", "text", "url", "rss", "atom", "feed", "web"] });
      const summary = sourceSyncSummary(payload);
      setOperationStatus(payload.ok === false ? "error" : "success");
      setOperationSummary(summary);
      setOperationMessage(payload.ok === false ? operationFailureMessage(payload.error, summary.failed) : sourceSyncMessage(summary));
      await refetchAll();
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "同步资料失败。");
    }
  }

  async function handleDigestNow() {
    setOperationStatus("digesting");
    setOperationMessage("正在把当前账号的资料整理任务加入队列...");
    setOperationSummary(undefined);
    try {
      const payload = await runDigestNow(serviceToken);
      const summary = digestNowSummary(payload);
      const jobId = payload.job?.job_id || payload.scheduled?.job?.job_id;
      setTrackedDigestJobIds(jobId ? [jobId] : []);
      const hasQueuedJob = (summary.queuedJobs ?? 0) > 0;
      setOperationStatus(payload.ok === false ? "error" : hasQueuedJob ? "queued" : "success");
      setOperationSummary(summary);
      setOperationMessage(payload.ok === false ? operationFailureMessage(payload.error, summary.failed) : digestRunMessage(payload, summary));
      await refetchAll();
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "整理资料失败。");
    }
  }

  async function handleRetryDigestJob(jobId: string) {
    setOperationStatus("digesting");
    setOperationMessage("正在重新排队这个 Digest 任务...");
    setOperationSummary(undefined);
    try {
      await retryDigestJob(serviceToken, jobId);
      setTrackedDigestJobIds([jobId]);
      setOperationStatus("queued");
      setOperationMessage("Digest 任务已重新排队，后台 worker 会继续处理。");
      await refetchAll();
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "重试 Digest 任务失败。");
    }
  }

  async function handleCreateEvidenceBrief(jobId?: string) {
    setOperationStatus("briefing");
    setBriefingJobId(jobId || null);
    setOperationMessage(jobId ? "正在从这次 Digest 生成 Brief 草稿..." : "正在从最近的 Digest 生成 Brief 草稿...");
    setOperationSummary(undefined);
    try {
      const payload = await createEvidenceBrief(serviceToken, jobId ? { job_id: jobId } : {});
      setOperationStatus(payload.ok === false ? "error" : "success");
      setOperationMessage(payload.ok === false ? evidenceBriefUnavailableMessage(payload) : `Brief 已生成：${payload.board?.title || payload.brief?.title || "Brief 草稿"}。已打开写作页面。`);
      await refetchAll();
      if (payload.ok !== false && payload.board?.board_id) {
        onOpenWorkspace("writing");
      }
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "生成 Brief 失败。");
    } finally {
      setBriefingJobId(null);
    }
  }

  async function handlePreviewSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = sourceFormValue.trim();
    if (!value) {
      return;
    }
    setSourceFormStatus("previewing");
    setSourceFormError("");
    try {
      const payload = await previewKnowledgeSource(serviceToken, sourceFormPayload(sourceFormKind, value, sourceFormName));
      setSourcePreview(payload);
      setSourceFormStatus(payload.ok === false || payload.preview?.ok === false ? "error" : "success");
      setSourceFormError(payload.error || (payload.preview?.ok === false ? "输入源预览失败。" : ""));
    } catch (error) {
      setSourceFormStatus("error");
      setSourceFormError(error instanceof Error ? error.message : "输入源预览失败。");
    }
  }

  async function handleAddSource() {
    const value = sourceFormValue.trim();
    if (!value) {
      return;
    }
    setSourceFormStatus("adding");
    setSourceFormError("");
    try {
      const payload = await createKnowledgeSource(serviceToken, {
        ...sourceFormPayload(sourceFormKind, value, sourceFormName),
        preview: true
      });
      setSourcePreview(payload.preview ? { ok: payload.ok, preview: payload.preview, adapters: payload.adapters } : null);
      setSourceFormStatus(payload.ok === false ? "error" : "success");
      setSourceFormError(payload.error || "");
      setOperationStatus("success");
      setOperationMessage(`${sourceKindLabel(sourceFormKind)} 输入源已添加。`);
      await refetchAll();
    } catch (error) {
      setSourceFormStatus("error");
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "添加输入源失败。");
      setSourceFormError(error instanceof Error ? error.message : "添加输入源失败。");
    }
  }

  async function handleSyncKnowledgeSources(knowledgeSourceId?: string) {
    if (!knowledgeSourceId) {
      setSourceFormStatus("syncing");
      setSourceFormError("");
    }
    setOperationStatus("syncing");
    setOperationMessage(knowledgeSourceId ? "正在同步选中的高级资料源..." : "正在同步所有高级资料源...");
    setOperationSummary(undefined);
    try {
      const payload = await syncKnowledgeSources(serviceToken, knowledgeSourceId);
      const summary = sourceSyncSummary(payload);
      setOperationStatus(payload.ok === false ? "error" : "success");
      setOperationSummary(summary);
      setOperationMessage(payload.ok === false ? operationFailureMessage(payload.error, summary.failed) : summaryMessage(summary));
      if (!knowledgeSourceId) {
        setSourceFormStatus(payload.ok === false ? "error" : "success");
      }
      await refetchAll();
    } catch (error) {
      setOperationStatus("error");
      setOperationMessage(error instanceof Error ? error.message : "同步高级资料源失败。");
      if (!knowledgeSourceId) {
        setSourceFormStatus("error");
        setSourceFormError(error instanceof Error ? error.message : "同步高级资料源失败。");
      }
    }
  }

  async function handleCleanupKnowledgeSource(knowledgeSourceId: string, execute: boolean) {
    setOperationStatus("cleaning");
    setCleanupTargetId(knowledgeSourceId);
    setOperationMessage(execute ? "正在清理高级资料源和派生知识..." : "正在预览清理影响...");
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
      setOperationMessage(error instanceof Error ? error.message : "清理高级资料源失败。");
    } finally {
      setCleanupTargetId(null);
    }
  }

  async function handleChunkPreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = chunkPreviewText.trim();
    if (!text) {
      return;
    }
    setChunkPreviewStatus("loading");
    setChunkPreviewError("");
    try {
      const payload = await previewChunking(serviceToken, {
        text,
        chunking: {
          strategy: chunkPreviewStrategy,
          chunk_size: chunkPreviewSize
        }
      });
      setChunkPreviewResult(payload);
      setChunkPreviewStatus(payload.ok === false ? "error" : "success");
      setChunkPreviewError(payload.error || "");
    } catch (error) {
      setChunkPreviewStatus("error");
      setChunkPreviewError(error instanceof Error ? error.message : "Chunk preview 失败。");
    }
  }

  return (
    <section className="main-workspace corpus-surface" aria-label="资料库">
      <div className="corpus-header">
        <div>
          <span className="eyebrow">资料库</span>
          <h1>资料库</h1>
          <p>上传、粘贴或同步资料，管理删除影响，并把 Digest 结果沉淀成可审阅 Brief。</p>
        </div>
        <div className="corpus-summary" aria-label="资料库摘要">
          <span><strong>{counts.sources}</strong> 条目</span>
          <span><strong>{counts.documents}</strong> 原文</span>
          <span><strong>{counts.chunks}</strong> 检索片段</span>
          <span><strong>{counts.inputSources}</strong> 高级源</span>
        </div>
        <div className="corpus-actions">
          <button type="button" onClick={onPinCurrent}>
            <Pin size={15} />
            {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "置顶失败" : "置顶资料库"}
          </button>
          <button type="button" onClick={() => void refetchAll()} disabled={actionRunning}>
            <RefreshCw size={15} />
            刷新视图
          </button>
          <button type="button" onClick={() => void handleFileSync()} disabled={actionRunning}>
            <Folder size={15} />
            {operationStatus === "syncing" ? "同步中" : "同步高级源"}
          </button>
          <button type="button" onClick={() => void handleDigestNow()} disabled={actionRunning}>
            <Sparkles size={15} />
            {operationStatus === "digesting" ? "入队中" : operationStatus === "queued" ? "再整理" : "整理资料"}
          </button>
        </div>
      </div>

      <div className={`corpus-operation ${operationStatus}`} role="status" data-testid="corpus-operation">
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
            {operationSummary.queuedJobs !== undefined ? <span>排队 {operationSummary.queuedJobs}</span> : null}
            {operationSummary.skipped !== undefined ? <span>跳过 {operationSummary.skipped}</span> : null}
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

      <div className="product-flow-grid">
        <SourceIngestPanel
          textTitle={textSourceTitle}
          textBody={textSourceBody}
          digestAfter={uploadDigestAfter}
          actionRunning={actionRunning}
          onTextTitleChange={setTextSourceTitle}
          onTextBodyChange={setTextSourceBody}
          onDigestAfterChange={setUploadDigestAfter}
          onTextSubmit={handleCreateTextSource}
          onUploadSubmit={handleUploadSource}
        />
        <PromptProfilePanel
          promptAsk={promptAsk}
          promptDigest={promptDigest}
          promptReview={promptReview}
          promptWriting={promptWriting}
          status={promptStatus}
          error={promptError}
          payload={promptProfiles}
          onPromptAskChange={setPromptAsk}
          onPromptDigestChange={setPromptDigest}
          onPromptReviewChange={setPromptReview}
          onPromptWritingChange={setPromptWriting}
          onSave={handleSavePromptProfiles}
        />
      </div>

      <button className="floating-ask-bubble" type="button" onClick={() => onOpenWorkspace("today")} title="回到 Today 提问">
        <MessageCircle size={18} />
        Ask
      </button>

      <section className="today-section chunk-preview-surface">
        <SectionTitle icon={<TextCursorInput size={18} />} title="Chunk Preview" subtitle="处理配置调试" />
        <form className="chunk-preview-form" onSubmit={handleChunkPreview}>
          <textarea
            value={chunkPreviewText}
            onChange={(event) => setChunkPreviewText(event.target.value)}
            placeholder="粘贴一段 Markdown、表格、代码块或长中文文本"
          />
          <div className="chunk-preview-controls">
            <label>
              <span>策略</span>
              <select value={chunkPreviewStrategy} onChange={(event) => setChunkPreviewStrategy(event.target.value)}>
                <option value="auto">auto</option>
                <option value="heading">heading</option>
                <option value="recursive">recursive</option>
                <option value="fixed">fixed</option>
              </select>
            </label>
            <label>
              <span>大小</span>
              <input
                type="number"
                min={120}
                max={6000}
                step={120}
                value={chunkPreviewSize}
                onChange={(event) => setChunkPreviewSize(Math.max(120, Number(event.target.value) || 1200))}
              />
            </label>
            <button className="primary" type="submit" disabled={chunkPreviewStatus === "loading" || !chunkPreviewText.trim()}>
              {chunkPreviewStatus === "loading" ? "预览中" : "预览"}
            </button>
          </div>
        </form>
        {chunkPreviewError ? <div className="review-empty error-state compact">{chunkPreviewError}</div> : null}
        <ChunkPreviewPanel payload={chunkPreviewResult} />
      </section>

      <div className="corpus-tools">
        <label>
          <Search size={16} />
          <input data-testid="corpus-search-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索资料标题、片段内容或来源路径" />
        </label>
      </div>

      <DocumentLifecyclePanel
        payload={documentsData}
        isLoading={documentsQuery.isLoading}
        isError={documentsQuery.isError}
        deletePreview={documentDeletePreview}
        deleteTarget={documentDeleteTarget}
        actionRunning={actionRunning}
        query={query}
        onPreview={(sourceItemId) => void handleDocumentLifecycle(sourceItemId, false)}
        onDelete={(sourceItemId) => void handleDocumentLifecycle(sourceItemId, true)}
        onRestore={(sourceItemId) => void handleDocumentLifecycle(sourceItemId, true, true)}
        onPurge={(sourceItemId) => void handleDocumentLifecycle(sourceItemId, true, false, true)}
      />

      {corpusQuery.isError || sourcesQuery.isError ? (
        <div className="review-empty error-state">资料库无法完整加载。请检查 8765 后端、数据库或服务令牌。</div>
      ) : corpusQuery.isLoading && sourcesQuery.isLoading ? (
        <div className="review-empty">正在加载真实资料库...</div>
      ) : (
        <div className="corpus-workspace-grid">
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

          <details className="corpus-panel connector-panel corpus-advanced-details">
            <summary>
              <span>高级同步</span>
              <small>URL/RSS 与管理员同步状态</small>
            </summary>
            <SourceAdapterPanel
              kind={sourceFormKind}
              value={sourceFormValue}
              name={sourceFormName}
              preview={sourcePreview}
              status={sourceFormStatus}
              error={sourceFormError}
              adapters={sourceSummary?.source_adapters || sourcePreview?.adapters || []}
              actionRunning={actionRunning}
              onKindChange={(kind) => {
                setSourceFormKind(kind);
                setSourcePreview(null);
                setSourceFormError("");
              }}
              onValueChange={setSourceFormValue}
              onNameChange={setSourceFormName}
              onPreview={handlePreviewSource}
              onAdd={() => void handleAddSource()}
              onSyncAll={() => void handleSyncKnowledgeSources()}
            />
            <ConnectorSummary
              payload={sourceSummary}
              cleanupPreview={cleanupPreview}
              cleanupConfirmText={cleanupConfirmText}
              cleanupTargetId={cleanupTargetId}
              actionRunning={actionRunning}
              onCleanupConfirmTextChange={setCleanupConfirmText}
              onSyncSource={(knowledgeSourceId) => void handleSyncKnowledgeSources(knowledgeSourceId)}
              onPreviewCleanup={(knowledgeSourceId) => void handleCleanupKnowledgeSource(knowledgeSourceId, false)}
              onConfirmCleanup={(knowledgeSourceId) => void handleCleanupKnowledgeSource(knowledgeSourceId, true)}
            />
          </details>

          <section className="corpus-panel digest-log-panel">
            <SectionTitle icon={<Sparkles size={18} />} title="Digest 任务日志" subtitle={`${digestLogs?.count ?? 0} 次最近理解任务`} />
            <DigestLogPanel
              payload={digestLogs}
              isLoading={digestLogsQuery.isLoading}
              isError={digestLogsQuery.isError}
              actionRunning={actionRunning}
              briefingJobId={briefingJobId}
              onRetryJob={(jobId) => void handleRetryDigestJob(jobId)}
              onCreateBrief={(jobId) => void handleCreateEvidenceBrief(jobId)}
            />
          </section>
        </div>
      )}
    </section>
  );
}

function SourceIngestPanel({
  textTitle,
  textBody,
  digestAfter,
  actionRunning,
  onTextTitleChange,
  onTextBodyChange,
  onDigestAfterChange,
  onTextSubmit,
  onUploadSubmit
}: {
  textTitle: string;
  textBody: string;
  digestAfter: boolean;
  actionRunning: boolean;
  onTextTitleChange: (value: string) => void;
  onTextBodyChange: (value: string) => void;
  onDigestAfterChange: (value: boolean) => void;
  onTextSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onUploadSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <section className="today-section product-ingest-panel">
      <SectionTitle icon={<UploadCloud size={18} />} title="加入资料库" subtitle="上传文件或粘贴文本" />
      <div className="ingest-forms">
        <form className="ingest-form" onSubmit={onUploadSubmit} data-testid="corpus-upload-form">
          <label>
            <span>上传文件</span>
            <input name="source-file" type="file" data-testid="corpus-upload-input" />
          </label>
          <label className="inline-toggle">
            <input type="checkbox" checked={digestAfter} onChange={(event) => onDigestAfterChange(event.target.checked)} data-testid="corpus-upload-digest-toggle" />
            <span>入库后自动 Digest</span>
          </label>
          <button className="primary" type="submit" disabled={actionRunning} data-testid="corpus-upload-submit">
            <UploadCloud size={14} />
            上传入库
          </button>
        </form>
        <form className="ingest-form" onSubmit={onTextSubmit}>
          <label>
            <span>文本标题</span>
            <input value={textTitle} onChange={(event) => onTextTitleChange(event.target.value)} placeholder="可选" />
          </label>
          <textarea value={textBody} onChange={(event) => onTextBodyChange(event.target.value)} placeholder="粘贴一段文本、Markdown、会议纪要或网页摘录" />
          <button className="primary" type="submit" disabled={actionRunning || !textBody.trim()}>
            <FileText size={14} />
            保存文本
          </button>
        </form>
      </div>
    </section>
  );
}

function PromptProfilePanel({
  promptAsk,
  promptDigest,
  promptReview,
  promptWriting,
  status,
  error,
  payload,
  onPromptAskChange,
  onPromptDigestChange,
  onPromptReviewChange,
  onPromptWritingChange,
  onSave
}: {
  promptAsk: string;
  promptDigest: string;
  promptReview: string;
  promptWriting: string;
  status: "idle" | "saving" | "success" | "error";
  error: string;
  payload?: PromptProfilesResponse;
  onPromptAskChange: (value: string) => void;
  onPromptDigestChange: (value: string) => void;
  onPromptReviewChange: (value: string) => void;
  onPromptWritingChange: (value: string) => void;
  onSave: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const effective = payload?.effective || {};
  return (
    <section className="today-section prompt-profile-panel">
      <SectionTitle icon={<Settings2 size={18} />} title="个人 Prompt" subtitle="租户默认之上的个人覆盖" />
      <form className="prompt-profile-form" onSubmit={onSave}>
        <label>
          <span>Ask</span>
          <input value={promptAsk} onChange={(event) => onPromptAskChange(event.target.value)} placeholder="回答风格、语言、结构偏好" />
        </label>
        <label>
          <span>Digest</span>
          <input value={promptDigest} onChange={(event) => onPromptDigestChange(event.target.value)} placeholder="希望重点发现的知识类型" />
        </label>
        <label>
          <span>Review</span>
          <input value={promptReview} onChange={(event) => onPromptReviewChange(event.target.value)} placeholder="高影响变更和低置信候选的处理偏好" />
        </label>
        <label>
          <span>Writing</span>
          <input value={promptWriting} onChange={(event) => onPromptWritingChange(event.target.value)} placeholder="写作语气和证据呈现偏好" />
        </label>
        <div className="prompt-profile-footer">
          <span>{Object.keys(effective).length} 个有效 profile</span>
          <button type="submit" disabled={status === "saving"}>
            <Settings2 size={14} />
            {status === "saving" ? "保存中" : "保存"}
          </button>
        </div>
      </form>
      {status === "success" ? <div className="review-empty compact">Prompt Profiles 已保存。</div> : null}
      {error ? <div className="review-empty error-state compact">{error}</div> : null}
    </section>
  );
}

function AskConversationPanel({
  messages,
  runs,
  isLoading,
  liveQuery,
  liveResult,
  livePending,
  composer
}: {
  messages: AskMessage[];
  runs: AskRun[];
  isLoading: boolean;
  liveQuery?: string;
  liveResult?: WorkspaceAskResponse | null;
  livePending?: boolean;
  composer: ReactNode;
}) {
  const showLiveResult = Boolean(liveResult);
  const runById = useMemo(() => {
    const mapped = new Map<string, WorkspaceAskResponse>();
    runs.forEach((run) => {
      const runId = displayText(run.run_id, "");
      const result = askResultFromRun(run);
      if (runId && result) {
        mapped.set(runId, result);
      }
    });
    return mapped;
  }, [runs]);
  return (
    <div className="ask-conversation-panel">
      <div className="ask-chat-main">
        <div className="ask-message-list">
          {isLoading ? (
            <div className="today-chat-empty">正在加载对话...</div>
          ) : messages.length === 0 && !showLiveResult ? (
            <div className="today-chat-empty">
              <strong>Today</strong>
              <span>问 PSKA 一个问题，或带着资料继续追问。</span>
            </div>
          ) : null}
          {messages.slice(-12).map((message) => (
            <article className={`ask-message ${message.role || "message"}`} key={message.message_id || `${message.role}-${message.created_at}`}>
              <span>{message.role === "assistant" ? "PSKA" : "你"}</span>
              {message.role === "assistant" ? (
                runById.get(displayText(message.run_id, "")) ? (
                  <AskResult result={runById.get(displayText(message.run_id, "")) as WorkspaceAskResponse} />
                ) : (
                  <p>{trimText(message.content || "", 800)}</p>
                )
              ) : (
                <p>{trimText(message.content || "", 800)}</p>
              )}
            </article>
          ))}
          {showLiveResult ? (
            <>
              {liveQuery?.trim() ? (
                <article className="ask-message user live">
                  <span>你</span>
                  <p>{trimText(liveQuery.trim(), 800)}</p>
                </article>
              ) : null}
              <article className="ask-message assistant live">
                <span>PSKA</span>
                <AskResult result={liveResult as WorkspaceAskResponse} pending={livePending} />
              </article>
            </>
          ) : null}
        </div>
        {composer}
      </div>
    </div>
  );
}

function askResultFromRun(run: AskRun): WorkspaceAskResponse | null {
  const result = isRecord(run.result) ? { ...(run.result as WorkspaceAskResponse) } : null;
  if (!result) {
    return null;
  }
  const answer = displayText(result.answer, "");
  const hasProcess = Array.isArray(result.agent_steps) && result.agent_steps.length > 0;
  const hasProgress = Array.isArray(result.progress) && result.progress.length > 0;
  const hasTrace = isRecord(result.trace) && Array.isArray(result.trace.events) && result.trace.events.length > 0;
  const hasEvidence = Array.isArray(result.citations) && result.citations.length > 0;
  if (!answer && !hasProcess && !hasProgress && !hasTrace && !hasEvidence) {
    return null;
  }
  if (!result.query && run.query) {
    result.query = run.query;
  }
  if (!result.route && isRecord(run.route)) {
    result.route = run.route as WorkspaceAskResponse["route"];
  }
  if (!result.evidence_check && isRecord(run.evidence_check)) {
    result.evidence_check = run.evidence_check;
  }
  if (!result.timing && (run.started_at || run.finished_at)) {
    result.timing = {};
  }
  return result;
}

function CollapsibleTodayPanel({
  className,
  icon,
  title,
  count,
  hasAlert,
  children
}: {
  className: string;
  icon: ReactNode;
  title: string;
  count: number;
  hasAlert: boolean;
  children: ReactNode;
}) {
  return (
    <details className={`today-section today-collapsible ${className} ${hasAlert ? "has-alert" : ""}`} open>
      <summary>
        <span>
          {icon}
          <strong>{title}</strong>
        </span>
        <span className="panel-count">{count}</span>
      </summary>
      {children}
    </details>
  );
}

function DocumentLifecyclePanel({
  payload,
  isLoading,
  isError,
  deletePreview,
  deleteTarget,
  actionRunning,
  query,
  onPreview,
  onDelete,
  onRestore,
  onPurge
}: {
  payload?: WorkspaceDocumentsResponse;
  isLoading: boolean;
  isError: boolean;
  deletePreview: WorkspaceDocumentDeleteResponse | null;
  deleteTarget: string;
  actionRunning: boolean;
  query: string;
  onPreview: (sourceItemId: string) => void;
  onDelete: (sourceItemId: string) => void;
  onRestore: (sourceItemId: string) => void;
  onPurge: (sourceItemId: string) => void;
}) {
  const normalizedQuery = query.trim().toLowerCase();
  const documents = (payload?.documents || []).filter((document) => {
    if (!normalizedQuery) return true;
    return corpusText([document.title, document.source_item_id, document.url, document.source_channel, document.snippet]).includes(normalizedQuery);
  });
  return (
    <section className="today-section document-lifecycle-panel">
      <SectionTitle icon={<FileText size={18} />} title="资料条目" subtitle={`${documents.length} 条，可预览删除影响`} />
      {isError ? <div className="review-empty error-state compact">资料条目无法加载。</div> : null}
      {isLoading ? <div className="review-empty compact">正在加载资料条目...</div> : null}
      {!isLoading && documents.length === 0 ? <div className="review-empty compact">还没有可管理的资料。</div> : null}
      <div className="document-lifecycle-list" data-testid="document-lifecycle-list">
        {documents.slice(0, 12).map((document) => {
          const sourceItemId = document.source_item_id || "";
          const isDeleted = document.lifecycle_status === "deleted";
          const previewMatches = deletePreview?.source_item_ids?.includes(sourceItemId);
          const counts = previewMatches ? deletePreview?.counts || deletePreview?.deleted || {} : document.impact || {};
          return (
            <article
              className="document-lifecycle-card"
              key={sourceItemId || document.title}
              data-testid="document-lifecycle-card"
              data-source-item-id={sourceItemId}
            >
              <div className="card-row">
                <span className={`pill ${isDeleted ? "warning" : "muted"}`}>{document.lifecycle_status || "active"}</span>
                <small>{formatSourceAge(document.created_at)}</small>
              </div>
              <h3>{displayText(document.title || sourceItemId, "未命名资料")}</h3>
              <p>{trimText(document.snippet || document.url || "", 180)}</p>
              <div className="operation-stats compact">
                <span>文档 {document.document_count ?? 0}</span>
                <span>片段 {document.chunk_count ?? 0}</span>
                <span>Claims {counts.knowledge_claims ?? 0}</span>
                <span>Digest {counts.digest_notes ?? 0}</span>
                <span>Review {counts.review_items ?? 0}</span>
              </div>
              <div className="source-cleanup-actions">
                <button type="button" onClick={() => onPreview(sourceItemId)} disabled={actionRunning || !sourceItemId} data-testid="document-preview-delete">
                  {deleteTarget === sourceItemId ? "预览中" : "预览影响"}
                </button>
                {isDeleted ? (
                  <button type="button" onClick={() => onRestore(sourceItemId)} disabled={actionRunning || !sourceItemId} data-testid="document-restore">
                    <RotateCcw size={14} />
                    恢复
                  </button>
                ) : (
                  <button className="danger" type="button" onClick={() => onDelete(sourceItemId)} disabled={actionRunning || !sourceItemId} data-testid="document-soft-delete">
                    <Trash2 size={14} />
                    软删
                  </button>
                )}
                <button className="danger ghost" type="button" onClick={() => onPurge(sourceItemId)} disabled={actionRunning || !sourceItemId} data-testid="document-hard-purge">
                  彻底清除
                </button>
              </div>
              {previewMatches ? <p className="connector-error">{(deletePreview?.notes || []).join(" ")}</p> : null}
            </article>
          );
        })}
      </div>
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

function DigestLogPanel({
  payload,
  isLoading,
  isError,
  actionRunning = false,
  briefingJobId = null,
  onRetryJob,
  onCreateBrief
}: {
  payload?: DigestLogsResponse;
  isLoading: boolean;
  isError: boolean;
  actionRunning?: boolean;
  briefingJobId?: string | null;
  onRetryJob?: (jobId: string) => void;
  onCreateBrief?: (jobId: string) => void;
}) {
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
        const retryable = log.status === "failed" || log.status === "canceled";
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
            {onCreateBrief || (onRetryJob && retryable) ? (
              <div className="digest-log-actions">
                {onCreateBrief ? (
                  <button type="button" onClick={() => onCreateBrief(log.job_id)} disabled={actionRunning || briefingJobId === log.job_id || log.status === "queued" || log.status === "running"}>
                    <BookOpen size={14} />
                    {briefingJobId === log.job_id ? "生成中" : log.status === "queued" || log.status === "running" ? "等待完成" : "生成 Brief"}
                  </button>
                ) : null}
                {onRetryJob && retryable ? (
                  <button className="secondary" type="button" onClick={() => onRetryJob(log.job_id)} disabled={actionRunning}>
                    <RotateCcw size={14} />
                    重试
                  </button>
                ) : null}
              </div>
            ) : null}
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

function ChunkPreviewPanel({ payload }: { payload: ChunkingPreviewResponse | null }) {
  const preview = payload?.preview;
  const chunks = preview?.chunks || [];
  if (!preview) {
    return null;
  }
  return (
    <div className="chunk-preview-result">
      <div className="operation-stats compact">
        <span>策略 {preview.strategy || "-"}</span>
        <span>Chunks {preview.stats?.count ?? 0}</span>
        <span>均值 {preview.stats?.avg_chars ?? 0}</span>
        <span>最大 {preview.stats?.max_chars ?? 0}</span>
      </div>
      <div className="chunk-preview-list">
        {chunks.slice(0, 5).map((chunk) => (
          <article className="chunk-preview-card" key={`${chunk.ordinal}-${chunk.start}-${chunk.end}`}>
            <div className="card-row">
              <span className="pill">{chunk.strategy || preview.strategy || "chunk"}</span>
              <small>{chunk.start ?? 0}-{chunk.end ?? 0} · {chunk.chars ?? 0} chars</small>
            </div>
            {chunk.context_header ? <strong>{chunk.context_header}</strong> : null}
            <p>{trimText(chunk.text || "", 260)}</p>
          </article>
        ))}
      </div>
    </div>
  );
}

function SourceAdapterPanel({
  kind,
  value,
  name,
  preview,
  status,
  error,
  adapters,
  actionRunning,
  onKindChange,
  onValueChange,
  onNameChange,
  onPreview,
  onAdd,
  onSyncAll
}: {
  kind: "url" | "rss" | "folder";
  value: string;
  name: string;
  preview: SourcePreviewResponse | null;
  status: "idle" | "previewing" | "adding" | "syncing" | "success" | "error";
  error: string;
  adapters: NonNullable<ConsoleSourcesResponse["source_adapters"]>;
  actionRunning: boolean;
  onKindChange: (kind: "url" | "rss" | "folder") => void;
  onValueChange: (value: string) => void;
  onNameChange: (value: string) => void;
  onPreview: (event: FormEvent<HTMLFormElement>) => void;
  onAdd: () => void;
  onSyncAll: () => void;
}) {
  const resources = preview?.preview?.resources || [];
  const visibleAdapters = adapters.filter((adapter) => !["folder", "files"].includes(displayText(adapter.source_type, "").toLocaleLowerCase()));
  const busy = status === "previewing" || status === "adding" || status === "syncing" || actionRunning;
  const hasValue = value.trim().length > 0;
  const count = preview?.preview?.count ?? resources.length;
  return (
    <div className="source-adapter-panel">
      <form className="source-adapter-form" onSubmit={onPreview}>
        <div className="source-adapter-row">
          <label>
            <span>类型</span>
            <select value={kind} onChange={(event) => onKindChange(event.target.value as "url" | "rss" | "folder")}>
              <option value="url">URL</option>
              <option value="rss">RSS/Atom</option>
            </select>
          </label>
          <label>
            <span>名称</span>
            <input value={name} onChange={(event) => onNameChange(event.target.value)} placeholder="可选" />
          </label>
        </div>
        <label>
          <span>{kind === "folder" ? "路径" : "地址"}</span>
          <input value={value} onChange={(event) => onValueChange(event.target.value)} placeholder={sourceInputPlaceholder(kind)} />
        </label>
        <div className="source-adapter-actions">
          <button type="submit" disabled={busy || !hasValue}>
            <Search size={14} />
            {status === "previewing" ? "预览中" : "Preview"}
          </button>
          <button className="primary" type="button" onClick={onAdd} disabled={busy || !hasValue}>
            {kind === "folder" ? <Folder size={14} /> : <Link2 size={14} />}
            {status === "adding" ? "添加中" : "添加"}
          </button>
          <button type="button" onClick={onSyncAll} disabled={busy}>
            <RefreshCw size={14} />
            {status === "syncing" ? "同步中" : "同步全部"}
          </button>
        </div>
      </form>
      {visibleAdapters.length ? (
        <div className="source-adapter-kinds" aria-label="支持的输入源">
          {visibleAdapters.map((adapter) => (
            <span className="pill muted" key={adapter.connector_id || adapter.source_type || adapter.label}>{adapter.label || sourceKindLabel(adapter.source_type)}</span>
          ))}
        </div>
      ) : null}
      {error ? <div className="review-empty error-state compact">{error}</div> : null}
      {preview?.preview ? (
        <div className="source-preview">
          <div className="operation-stats compact">
            <span>{sourceKindLabel(kind)}</span>
            <span>可同步 {count}</span>
            <span>{preview.preview.ok === false ? "异常" : "可用"}</span>
          </div>
          {resources.length ? (
            <div className="source-preview-list">
              {resources.slice(0, 5).map((resource) => (
                <article className="source-preview-card" key={resource.resource_id || resource.uri || resource.title}>
                  <div className="card-row">
                    <span className="pill">{resource.record_type || kind}</span>
                    <small>{formatReviewDate(resource.updated_at || undefined)}</small>
                  </div>
                  <h3>{displayText(resource.title || resource.uri, "未命名资源")}</h3>
                  <p>{displayText(resource.summary || resource.uri, "暂无摘要")}</p>
                </article>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
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
  onSyncSource,
  onPreviewCleanup,
  onConfirmCleanup
}: {
  payload?: ConsoleSourcesResponse;
  cleanupPreview: KnowledgeSourceCleanupResponse | null;
  cleanupConfirmText: string;
  cleanupTargetId: string | null;
  actionRunning: boolean;
  onCleanupConfirmTextChange: (value: string) => void;
  onSyncSource: (knowledgeSourceId: string) => void;
  onPreviewCleanup: (knowledgeSourceId: string) => void;
  onConfirmCleanup: (knowledgeSourceId: string) => void;
}) {
  const knowledgeSources = payload?.knowledge_sources?.sources || [];
  const inputSources = (payload?.input_sources || []).filter((source) => source.kind !== "twitter_archive");
  const states = payload?.connector_state?.states || [];
  const channels = Object.entries(payload?.source_channels || {})
    .filter(([channel]) => !channel.toLocaleLowerCase().includes("twitter"))
    .sort((a, b) => channelCount(b[1]) - channelCount(a[1]));
  return (
    <div className="connector-summary">
      <div className="connector-roots">
        <h3>高级同步总览</h3>
        {inputSources.length === 0 ? (
          <p>普通使用请直接上传文件或粘贴文本；这里仅显示管理员配置的 URL/RSS/Folder 同步源。</p>
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
      </div>
      <div className="connector-roots">
        <h3>高级同步源</h3>
        {knowledgeSources.length === 0 ? (
          <p>还没有高级同步源。普通用户可以直接上传文件或粘贴文本。</p>
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
            {source.latest_processing_spans?.length ? (
              <ol className="processing-span-list">
                {source.latest_processing_spans.map((span) => (
                  <li key={span.processing_span_id || `${span.stage}-${span.status}`} data-status={span.status || "unknown"}>
                    <span>{span.stage || "stage"}</span>
                    <small>{span.status || "unknown"}</small>
                  </li>
                ))}
              </ol>
            ) : null}
            {knowledgeSourceId ? (
              <div className="source-cleanup-actions">
                <button type="button" onClick={() => onSyncSource(knowledgeSourceId)} disabled={actionRunning}>
                  <RefreshCw size={14} />
                  同步
                </button>
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
            <h3>{displayText(state.connector_state_id || state.connector_id, "高级资料源")}</h3>
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

type OperationSummary = {
  inputSources?: number;
  scanned?: number;
  ingested?: number;
  changed?: number;
  unchanged?: number;
  failed?: number;
  twitterZips?: number;
  twitterImported?: number;
  twitterSkipped?: number;
  scheduled?: number;
  queuedJobs?: number;
  skipped?: number;
  reviews?: number;
  claims?: number;
  digestNotes?: number;
  saved?: number;
};

function digestNowSummary(payload: DigestNowResponse) {
  const synced = payload.summary?.synced || payload.sync?.totals || {};
  const candidateWrite = payload.summary?.candidate_write || {};
  const twitterEnabled = payload.sync?.twitter_archives?.enabled === true;
  const scheduledSourceItems = payload.summary?.scheduled_source_items
    ?? payload.scheduled?.scheduled_source_item_ids?.length
    ?? payload.digest?.scheduled_source_item_ids?.length
    ?? 0;
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
    scheduled: scheduledSourceItems,
    queuedJobs: payload.summary?.queued_jobs ?? (payload.job || payload.scheduled?.job ? 1 : 0),
    skipped: payload.summary?.skipped_source_items ?? payload.scheduled?.skipped_source_item_ids?.length ?? 0,
    digestNotes: candidateWrite.digest_notes ?? 0,
    claims: candidateWrite.knowledge_claims ?? 0,
    saved: candidateWrite.saved_candidates ?? 0,
    reviews: payload.summary?.pending_review_count ?? 0
  };
}

function digestRunMessage(payload: DigestNowResponse, summary: OperationSummary) {
  if ((summary.scheduled ?? 0) === 0 && (summary.queuedJobs ?? 0) === 0) {
    return (summary.skipped ?? 0) > 0
      ? `没有新的资料需要整理，已跳过 ${summary.skipped ?? 0} 个已覆盖条目。`
      : "没有新的资料需要整理。上传、粘贴或修改资料后会进入后台队列。";
  }
  if (payload.queued || payload.mode === "queued") {
    return `Digest 任务已排队：${summary.scheduled ?? 0} 个资料条目等待后台整理。完成后会出现在 Digest 日志、Review 和 Discoveries。`;
  }
  return summaryMessage(summary);
}

function sourceSyncSummary(payload: SourceSyncResponse) {
  const totals = payload.totals || {};
  return {
    inputSources: totals.sources || 0,
    scanned: totals.scanned || 0,
    ingested: totals.ingested || 0,
    changed: (totals.new_files || 0) + (totals.changed_files || 0),
    unchanged: totals.unchanged_files || 0,
    twitterZips: 0,
    twitterImported: 0,
    twitterSkipped: 0,
    failed: totals.failed ?? payload.failed?.length ?? 0
  };
}

function sourceSyncMessage(summary: OperationSummary) {
  if ((summary.inputSources ?? 0) === 0) {
    return "当前账号还没有可同步的资料源。请先上传文件、粘贴文本，或添加 URL/RSS。";
  }
  return summaryMessage(summary);
}

function sourceIngestSummary(payload: WorkspaceSourceIngestResponse) {
  const report = payload.sync_report || {};
  const chunkStats = payload.chunk_stats || {};
  const digest = payload.digest || {};
  return {
    inputSources: 1,
    scanned: Number(report.scanned || 1),
    ingested: Number(report.ingested || payload.source_item_ids?.length || 0),
    changed: Number(report.new_files || report.changed_files || payload.source_item_ids?.length || 0),
    unchanged: Number(report.unchanged_files || 0),
    failed: Array.isArray(report.failed) ? report.failed.length : Number(report.failed || 0),
    twitterZips: 0,
    twitterImported: 0,
    twitterSkipped: 0,
    scheduled: digest.scheduled_source_item_ids?.length || 0,
    digestNotes: 0,
    claims: 0,
    saved: 0,
    reviews: 0,
    chunks: chunkStats.count || 0
  };
}

function evidenceBriefUnavailableMessage(payload: { error?: string; reason?: string; warnings?: string[] }) {
  if (payload.error) {
    return payload.error;
  }
  if (payload.reason === "missing_source_refs") {
    return "Brief 没有生成：当前候选没有可追溯的 source refs。";
  }
  if (payload.reason === "unsupported_answer") {
    return "Brief 没有生成：当前 Ask 回答证据不足，不能直接沉淀为知识库草稿。";
  }
  if (payload.reason === "stale_evidence") {
    return "Brief 没有生成：相关证据已经删除或失效，需要先处理 Review。";
  }
  if (payload.reason === "missing_artifacts") {
    return "Brief 没有生成：没有可用的 Digest、Claim、Review 或 Ask 证据。";
  }
  return payload.warnings?.[0] || "Brief 没有生成：没有可用的证据。";
}

function documentLifecyclePreviewMessage(counts: Record<string, number>) {
  return `预览完成：会影响资料 ${counts.source_items ?? 0} 条、文档 ${counts.documents ?? 0} 个、片段 ${counts.chunks ?? 0} 个、Digest ${counts.digest_notes ?? 0} 条、Review ${counts.review_items ?? 0} 条。`;
}

function documentLifecycleDoneMessage(counts: Record<string, number>, restore: boolean) {
  return restore
    ? `恢复完成：资料 ${counts.source_items ?? 0} 条、片段 ${counts.chunks ?? 0} 个重新可用。`
    : `删除状态已更新：资料 ${counts.source_items ?? 0} 条、片段 ${counts.chunks ?? 0} 个已从检索中移除或彻底清除。`;
}

function sourceFormPayload(kind: "url" | "rss" | "folder", rawValue: string, rawName: string) {
  const value = rawValue.trim();
  const name = rawName.trim();
  return {
    source_type: kind,
    ...(kind === "folder" ? { path: value } : { url: value }),
    ...(name ? { name } : {})
  };
}

function sourceInputPlaceholder(kind?: string) {
  if (kind === "folder") {
    return "/Users/you/Documents/notes";
  }
  if (kind === "rss") {
    return "https://example.com/feed.xml";
  }
  return "https://example.com/page-or-sitemap.xml";
}

function sourceKindLabel(kind?: string) {
  if (kind === "folder" || kind === "files") {
    return "Folder";
  }
  if (kind === "rss" || kind === "atom") {
    return "RSS/Atom";
  }
  if (kind === "url" || kind === "web") {
    return "URL";
  }
  return displayText(kind, "输入源");
}

function summaryMessage(summary: OperationSummary) {
  const parts = [
    `输入源 ${summary.inputSources ?? 0} 个`,
    `扫描 ${summary.scanned ?? 0} 个`,
    `入库 ${summary.ingested ?? 0} 个`,
    `变更 ${summary.changed ?? 0} 个`,
    `未变 ${summary.unchanged ?? 0} 个`,
    `失败 ${summary.failed ?? 0} 个`
  ];
  if ((summary.twitterZips ?? 0) > 0 || (summary.twitterImported ?? 0) > 0 || (summary.twitterSkipped ?? 0) > 0) {
    parts.push(`Twitter Zip ${summary.twitterZips ?? 0} 个`);
    parts.push(`Twitter 导入 ${summary.twitterImported ?? 0} 个`);
    parts.push(`Twitter 已有 ${summary.twitterSkipped ?? 0} 个`);
  }
  if (summary.scheduled !== undefined) {
    parts.push(`调度 ${summary.scheduled} 个`);
  }
  if (summary.queuedJobs !== undefined) {
    parts.push(`排队 ${summary.queuedJobs} 个`);
  }
  if (summary.skipped !== undefined) {
    parts.push(`跳过 ${summary.skipped} 个`);
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

function operationTitle(status: "idle" | "syncing" | "digesting" | "queued" | "cleaning" | "briefing" | "success" | "error") {
  if (status === "syncing") {
    return "同步资料";
  }
  if (status === "digesting") {
    return "整理资料";
  }
  if (status === "queued") {
    return "Digest 已排队";
  }
  if (status === "cleaning") {
    return "清理资料";
  }
  if (status === "briefing") {
    return "生成 Brief 草稿";
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

const WRITING_NODE_COLLAPSED_SIZE = { width: 300, height: 180 };
const WRITING_NODE_EXPANDED_SIZE = { width: 460, height: 340 };
const WRITING_NODE_GAP = 34;

function writingNodeApproxSize(nodeType: WritingNodeType, expanded: boolean) {
  if (expanded || nodeType === "draft") {
    return WRITING_NODE_EXPANDED_SIZE;
  }
  if (nodeType === "section") {
    return { width: 320, height: 190 };
  }
  return WRITING_NODE_COLLAPSED_SIZE;
}

function writingNodeRect(node: Pick<WritingNode, "node_type" | "position" | "metadata">) {
  const size = writingNodeApproxSize(node.node_type, node.metadata?.expanded === true);
  return {
    x: Number(node.position?.x || 80),
    y: Number(node.position?.y || 80),
    width: size.width,
    height: size.height
  };
}

function writingRectsOverlap(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number }
) {
  return !(
    a.x + a.width + WRITING_NODE_GAP < b.x ||
    b.x + b.width + WRITING_NODE_GAP < a.x ||
    a.y + a.height + WRITING_NODE_GAP < b.y ||
    b.y + b.height + WRITING_NODE_GAP < a.y
  );
}

function findOpenWritingPosition(
  nodes: WritingNode[],
  preferred: { x: number; y: number },
  nodeType: WritingNodeType,
  options: { expanded?: boolean; ignoreNodeIds?: string[] } = {}
) {
  const ignore = new Set(options.ignoreNodeIds || []);
  const size = writingNodeApproxSize(nodeType, options.expanded === true);
  const existing = nodes.filter((node) => !ignore.has(node.node_id)).map(writingNodeRect);
  const start = { x: Math.max(40, Math.round(preferred.x)), y: Math.max(60, Math.round(preferred.y)) };
  const stepX = Math.max(360, size.width + WRITING_NODE_GAP + 60);
  const stepY = Math.max(230, size.height + WRITING_NODE_GAP + 40);
  const candidateAt = (dx: number, dy: number) => ({
    x: Math.max(40, start.x + dx * stepX),
    y: Math.max(60, start.y + dy * stepY)
  });
  for (let radius = 0; radius <= 9; radius += 1) {
    for (let dy = -radius; dy <= radius; dy += 1) {
      for (let dx = -radius; dx <= radius; dx += 1) {
        if (radius > 0 && Math.abs(dx) !== radius && Math.abs(dy) !== radius) {
          continue;
        }
        const candidate = candidateAt(dx, dy);
        const rect = { ...candidate, ...size };
        if (!existing.some((other) => writingRectsOverlap(rect, other))) {
          return candidate;
        }
      }
    }
  }
  return candidateAt(nodes.length % 4, Math.floor(nodes.length / 4) + 1);
}

type WritingNodeData = Record<string, unknown> & {
  node: WritingNode;
  selected: boolean;
  running: boolean;
  askPreview?: WorkspaceAskResponse;
  suggestions: WritingQuestionSuggestion[];
  canAddToSection: boolean;
  onPatch: (nodeId: string, patch: Partial<WritingNode>) => void;
  onRunAsk: (node: WritingNode) => void;
  onSuggest: (node: WritingNode, direction: "decompose" | "followup" | "evidence_gap" | "counterpoint") => void;
  onAcceptSuggestion: (node: WritingNode, suggestion: WritingQuestionSuggestion) => void;
  onAddToSection: (node: WritingNode) => void;
  onDelete: (node: WritingNode) => void;
  onOpenEditor: (node: WritingNode) => void;
};

type WritingFlowNode = Node<WritingNodeData, "writingNode">;

function WritingWorkspace({
  serviceToken,
  onPinCurrent,
  pinStatus
}: {
  serviceToken: PSKAAuth;
  onPinCurrent: () => void;
  pinStatus: "idle" | "saved" | "failed";
}) {
  const [activeBoardId, setActiveBoardId] = useState("");
  const [projectManagerOpen, setProjectManagerOpen] = useState(false);
  const [newGoal, setNewGoal] = useState("");
  const [selectedSectionId, setSelectedSectionId] = useState("");
  const [runningNodeIds, setRunningNodeIds] = useState<string[]>([]);
  const [askPreviews, setAskPreviews] = useState<Record<string, WorkspaceAskResponse>>({});
  const [suggestions, setSuggestions] = useState<Record<string, WritingQuestionSuggestion[]>>({});
  const [workspaceMessage, setWorkspaceMessage] = useState("");
  const [editingNodeId, setEditingNodeId] = useState("");
  const [editorMaximized, setEditorMaximized] = useState(false);
  const didAutoSelectBoard = useRef(false);

  const boardsQuery = useQuery({
    queryKey: ["writing-boards", serviceToken],
    queryFn: () => listWritingBoards(serviceToken),
    retry: 1
  });
  const boards = boardsQuery.data?.boards || [];

  useEffect(() => {
    if (!activeBoardId && boards.length && !projectManagerOpen && !didAutoSelectBoard.current) {
      didAutoSelectBoard.current = true;
      setActiveBoardId(boards[0].board_id);
    }
  }, [activeBoardId, boards, projectManagerOpen]);

  const boardQuery = useQuery({
    queryKey: ["writing-board", serviceToken, activeBoardId],
    queryFn: () => loadWritingBoard(serviceToken, activeBoardId),
    enabled: Boolean(activeBoardId),
    retry: 1
  });
  const board = boardQuery.data?.board;
  const writingNodes = boardQuery.data?.nodes || [];
  const writingEdges = boardQuery.data?.edges || [];
  const sections = writingNodes.filter((node) => node.node_type === "section");
  const answerNodes = writingNodes.filter((node) => node.node_type === "answer");
  const editingNode = writingNodes.find((node) => node.node_id === editingNodeId);

  useEffect(() => {
    if (sections.length && !sections.some((section) => section.node_id === selectedSectionId)) {
      setSelectedSectionId(sections[0].node_id);
    } else if (!sections.length && selectedSectionId) {
      setSelectedSectionId("");
    }
  }, [sections, selectedSectionId]);

  async function refreshBoard() {
    await boardsQuery.refetch();
    await boardQuery.refetch();
  }

  async function createBoard() {
    const goal = newGoal.trim() || "从一个问题开始，构造可引用的写作网络。";
    setWorkspaceMessage("正在创建写作画布...");
    try {
      const created = await createWritingBoard(serviceToken, {
        title: trimText(goal, 64) || "新写作网络",
        goal,
        metadata: { canvas: "xyflow", product: "inquiry_graph" }
      });
      const boardId = created.board?.board_id;
      if (!boardId) {
        throw new Error("missing board id");
      }
      setActiveBoardId(boardId);
      setProjectManagerOpen(false);
      didAutoSelectBoard.current = true;
      await createWritingNode(serviceToken, boardId, {
        node_type: "goal",
        title: "写作目标",
        body_markdown: goal,
        position: { x: 80, y: 120 },
        metadata: { expanded: true }
      });
      const section = await createWritingNode(serviceToken, boardId, {
        node_type: "section",
        title: "第一部分",
        body_markdown: "把已验证的答案节点加入这里，再生成章节草稿。",
        position: { x: 80, y: 390 },
        metadata: { expanded: false }
      });
      if (section.node?.node_id) {
        setSelectedSectionId(section.node.node_id);
      }
      setNewGoal("");
      setWorkspaceMessage("写作网络已创建。");
      await refreshBoard();
    } catch (error) {
      setWorkspaceMessage(error instanceof Error ? error.message : "创建写作网络失败。");
    }
  }

  function openBoard(boardId: string) {
    setActiveBoardId(boardId);
    setProjectManagerOpen(false);
    didAutoSelectBoard.current = true;
  }

  function closeBoard() {
    setProjectManagerOpen(true);
  }

  async function removeBoard(boardId: string) {
    const boardTitle = boards.find((item) => item.board_id === boardId)?.title || "这个写作项目";
    if (!window.confirm(`删除「${boardTitle}」？项目里的节点和边会一起删除。`)) {
      return;
    }
    await deleteWritingBoard(serviceToken, boardId);
    if (activeBoardId === boardId) {
      setActiveBoardId("");
      setProjectManagerOpen(true);
      setSelectedSectionId("");
    }
    setWorkspaceMessage("写作项目已删除。");
    await refreshBoard();
  }

  async function addNode(nodeType: WritingNodeType) {
    if (!activeBoardId) {
      return;
    }
    const preferred = {
      x: nodeType === "section" ? 100 : 420,
      y: 120 + writingNodes.length * 34
    };
    const position = findOpenWritingPosition(writingNodes, preferred, nodeType, { expanded: nodeType === "question" });
    const created = await createWritingNode(serviceToken, activeBoardId, {
      node_type: nodeType,
      title: writingNodeDefaultTitle(nodeType),
      body_markdown: nodeType === "question" ? "把要追问的问题写在这里，然后运行 Ask PSKA。" : "",
      position,
      metadata: { expanded: nodeType === "question" }
    });
    if (nodeType === "section" && created.node?.node_id) {
      setSelectedSectionId(created.node.node_id);
    }
    await boardQuery.refetch();
  }

  async function patchNode(nodeId: string, patch: Partial<WritingNode>) {
    if (!activeBoardId) {
      return;
    }
    await patchWritingNode(serviceToken, activeBoardId, nodeId, patch);
    await boardQuery.refetch();
  }

  async function saveAndCloseEditor(nodeId: string, patch: Partial<WritingNode>) {
    setEditingNodeId("");
    setEditorMaximized(false);
    await patchNode(nodeId, patch);
  }

  async function patchBoard(patch: Partial<Pick<WritingBoard, "title" | "goal" | "metadata">>) {
    if (!activeBoardId) {
      return;
    }
    await patchWritingBoard(serviceToken, activeBoardId, patch);
    await boardsQuery.refetch();
    await boardQuery.refetch();
  }

  async function removeNode(node: WritingNode) {
    if (!activeBoardId) {
      return;
    }
    await deleteWritingNode(serviceToken, activeBoardId, node.node_id);
    await boardQuery.refetch();
  }

  const onNodeDragStop: OnNodeDrag<Node<WritingNodeData, "writingNode">> = useCallback(
    (_event, node) => {
      if (!activeBoardId) {
        return;
      }
      void patchWritingNode(serviceToken, activeBoardId, node.id, { position: node.position }).then(() => boardQuery.refetch());
    },
    [activeBoardId, boardQuery, serviceToken]
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!activeBoardId || !connection.source || !connection.target) {
        return;
      }
      void createWritingEdge(serviceToken, activeBoardId, {
        source_node_id: connection.source,
        target_node_id: connection.target,
        edge_type: "raises",
        label: "引出"
      }).then(() => boardQuery.refetch());
    },
    [activeBoardId, boardQuery, serviceToken]
  );

  async function runAsk(node: WritingNode) {
    if (!activeBoardId || runningNodeIds.includes(node.node_id)) {
      return;
    }
    const query = [node.title, node.body_markdown].filter(Boolean).join("\n").trim();
    if (!query) {
      setWorkspaceMessage("问题节点需要标题或正文。");
      return;
    }
    const sessionId = writingNodeSessionId(node);
    const scope = buildWritingAskScope(activeBoardId, node, writingNodes, writingEdges, sessionId);
    setRunningNodeIds((current) => current.includes(node.node_id) ? current : [...current, node.node_id]);
    setWorkspaceMessage("Ask PSKA 正在回答这个问题节点...");
    setAskPreviews((current) => ({ ...current, [node.node_id]: pendingAskResult(query) }));
    try {
      const result = await askWorkspaceStream(
        query,
        serviceToken,
        "auto",
        "writing",
        ({ result: partial }) => {
          setAskPreviews((current) => ({ ...current, [node.node_id]: { ...partial } }));
        },
        { scope, sessionId }
      );
      const answer = cleanAgenticAnswer(result.answer || finalAnswerFromTraceEvents(result) || "");
      const lastAsk = writingNodeLastAsk(result, query, sessionId, scope);
      const placementNodes = [...writingNodes];
      const answerPosition = findOpenWritingPosition(
        placementNodes,
        {
          x: Number(node.position?.x || 0) + 390,
          y: Number(node.position?.y || 0)
        },
        "answer",
        { expanded: true }
      );
      const answerNode = await createWritingNode(serviceToken, activeBoardId, {
        node_type: "answer",
        title: `回答：${trimText(node.title || query, 42)}`,
        body_markdown: answer || "PSKA 没有生成可见回答。",
        position: answerPosition,
        status: result.error ? "error" : "complete",
        citations: result.citations || [],
        source_refs: result.source_refs || [],
        quality_signals: result.quality_signals || {},
        metadata: { route: result.route || {}, timing: result.timing || {}, session_id: sessionId, source_question_id: node.node_id, expanded: true }
      });
      if (answerNode.node?.node_id) {
        await createWritingEdge(serviceToken, activeBoardId, {
          source_node_id: node.node_id,
          target_node_id: answerNode.node.node_id,
          edge_type: "answered_by",
          label: "回答"
        });
        placementNodes.push(answerNode.node);
      }
      const refs = normalizeSearchRefs([...(result.citations || []), ...(result.source_refs || [])]);
      if (refs.length && answerNode.node?.node_id) {
        const evidencePosition = findOpenWritingPosition(
          placementNodes,
          {
            x: Number(answerNode.node.position?.x || 0) + 400,
            y: Number(answerNode.node.position?.y || 0) - 90
          },
          "evidence"
        );
        const evidenceNode = await createWritingNode(serviceToken, activeBoardId, {
          node_type: "evidence",
          title: `证据 ${refs.length}`,
          body_markdown: refs.slice(0, 5).map((ref, index) => `${index + 1}. ${ref.title || ref.source_item_id}`).join("\n"),
          position: evidencePosition,
          source_refs: result.source_refs || [],
          citations: result.citations || [],
          metadata: { expanded: false }
        });
        if (evidenceNode.node?.node_id) {
          await createWritingEdge(serviceToken, activeBoardId, {
            source_node_id: answerNode.node.node_id,
            target_node_id: evidenceNode.node.node_id,
            edge_type: "supported_by",
            label: "证据"
          });
          placementNodes.push(evidenceNode.node);
        }
      }
      const gaps = normalizeAskNotes(result.evidence?.gaps);
      if (gaps.length && answerNode.node?.node_id) {
        const gapPosition = findOpenWritingPosition(
          placementNodes,
          {
            x: Number(answerNode.node.position?.x || 0) + 400,
            y: Number(answerNode.node.position?.y || 0) + 170
          },
          "gap"
        );
        const gapNode = await createWritingNode(serviceToken, activeBoardId, {
          node_type: "gap",
          title: "证据缺口",
          body_markdown: gaps.map((gap) => `- ${gap}`).join("\n"),
          position: gapPosition,
          metadata: { expanded: false }
        });
        if (gapNode.node?.node_id) {
          await createWritingEdge(serviceToken, activeBoardId, {
            source_node_id: answerNode.node.node_id,
            target_node_id: gapNode.node.node_id,
            edge_type: "raises",
            label: "缺口"
          });
          placementNodes.push(gapNode.node);
        }
      }
      await patchWritingNode(serviceToken, activeBoardId, node.node_id, {
        status: result.error ? "error" : "complete",
        metadata: { ...(node.metadata || {}), session_id: sessionId, last_ask: lastAsk }
      });
      setWorkspaceMessage("回答节点已加入写作网络。");
      await boardQuery.refetch();
    } catch (error) {
      setWorkspaceMessage(error instanceof Error ? error.message : "Ask PSKA 节点运行失败。");
    } finally {
      setRunningNodeIds((current) => current.filter((nodeId) => nodeId !== node.node_id));
    }
  }

  async function requestSuggestions(node: WritingNode, direction: "decompose" | "followup" | "evidence_gap" | "counterpoint") {
    if (!activeBoardId) {
      return;
    }
    const response = await suggestWritingQuestions(serviceToken, activeBoardId, { node_id: node.node_id, direction });
    setSuggestions((current) => ({ ...current, [node.node_id]: response.suggestions || [] }));
  }

  async function acceptSuggestion(node: WritingNode, suggestion: WritingQuestionSuggestion) {
    if (!activeBoardId) {
      return;
    }
    const created = await createWritingNode(serviceToken, activeBoardId, {
      node_type: "question",
      title: suggestion.question,
      body_markdown: suggestion.rationale || "",
      position: findOpenWritingPosition(
        writingNodes,
        {
          x: Number(node.position?.x || 0) + 380,
          y: Number(node.position?.y || 0) + 170
        },
        "question",
        { expanded: true }
      ),
      metadata: { expanded: true, suggestion }
    });
    if (created.node?.node_id) {
      await createWritingEdge(serviceToken, activeBoardId, {
        source_node_id: node.node_id,
        target_node_id: created.node.node_id,
        edge_type: suggestion.direction === "decompose" ? "decomposes_to" : "raises",
        label: "追问"
      });
    }
    setSuggestions((current) => ({ ...current, [node.node_id]: [] }));
    await boardQuery.refetch();
  }

  async function addAnswerToSection(node: WritingNode) {
    if (!activeBoardId || !selectedSectionId) {
      setWorkspaceMessage("请先创建或选择一个章节节点。");
      return;
    }
    await createWritingEdge(serviceToken, activeBoardId, {
      source_node_id: node.node_id,
      target_node_id: selectedSectionId,
      edge_type: "included_in",
      label: "纳入章节"
    });
    setWorkspaceMessage("答案已纳入当前章节。");
    await boardQuery.refetch();
  }

  async function composeSection() {
    if (!activeBoardId || !selectedSectionId) {
      setWorkspaceMessage("请先选择一个章节。");
      return;
    }
    const answerIds = writingEdges
      .filter((edge) => edge.edge_type === "included_in" && edge.target_node_id === selectedSectionId)
      .map((edge) => edge.source_node_id);
    const section = writingNodes.find((node) => node.node_id === selectedSectionId);
    const response = await composeWritingDraft(serviceToken, activeBoardId, {
      section_node_id: selectedSectionId,
      answer_node_ids: answerIds
    });
    const draftPosition = findOpenWritingPosition(
      writingNodes,
      {
        x: Number(section?.position?.x || 120) + 420,
        y: Number(section?.position?.y || 120) + 220
      },
      "draft",
      { expanded: true }
    );
    const draft = await createWritingNode(serviceToken, activeBoardId, {
      node_type: "draft",
      title: `草稿：${section?.title || "章节"}`,
      body_markdown: response.draft_markdown || "",
      position: draftPosition,
      source_refs: response.source_refs || [],
      citations: response.citations || [],
      metadata: { expanded: true, composed_from: answerIds, retrieval_used: response.retrieval_used === true }
    });
    if (draft.node?.node_id) {
      await createWritingEdge(serviceToken, activeBoardId, {
        source_node_id: selectedSectionId,
        target_node_id: draft.node.node_id,
        edge_type: "follows",
        label: "生成草稿"
      });
    }
    setWorkspaceMessage("章节草稿已生成。");
    await boardQuery.refetch();
  }

  async function copyBoardMarkdown() {
    const markdown = buildWritingExportMarkdown(board, writingNodes, writingEdges);
    await navigator.clipboard.writeText(markdown);
    setWorkspaceMessage("已复制写作网络 Markdown。");
  }

  const xyNodes: Node<WritingNodeData, "writingNode">[] = useMemo(
    () =>
      writingNodes.map((node) => ({
        id: node.node_id,
        type: "writingNode",
        position: {
          x: Number(node.position?.x || 80),
          y: Number(node.position?.y || 80)
        },
        data: {
          node,
          selected: selectedSectionId === node.node_id,
          running: runningNodeIds.includes(node.node_id),
          askPreview: askPreviews[node.node_id] || writingNodeLastAskPreview(node),
          suggestions: suggestions[node.node_id] || [],
          canAddToSection: Boolean(selectedSectionId && node.node_type === "answer"),
          onPatch: (nodeId, patch) => void patchNode(nodeId, patch),
          onRunAsk: (target) => void runAsk(target),
          onSuggest: (target, direction) => void requestSuggestions(target, direction),
          onAcceptSuggestion: (target, suggestion) => void acceptSuggestion(target, suggestion),
          onAddToSection: (target) => void addAnswerToSection(target),
          onDelete: (target) => void removeNode(target),
          onOpenEditor: (target) => {
            setEditingNodeId(target.node_id);
            setEditorMaximized(false);
          }
        }
      })),
    [selectedSectionId, runningNodeIds, askPreviews, suggestions, writingNodes]
  );
  const xyEdges: Edge[] = useMemo(
    () =>
      writingEdges.map((edge) => ({
        id: edge.edge_id,
        source: edge.source_node_id,
        target: edge.target_node_id,
        label: edge.label || writingEdgeLabel(edge.edge_type),
        type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed, width: 15, height: 15 },
        className: `writing-edge writing-edge-${edge.edge_type}`
      })),
    [writingEdges]
  );
  const [displayNodes, setDisplayNodes, onDisplayNodesChange] = useNodesState<WritingFlowNode>([]);

  useEffect(() => {
    setDisplayNodes((current) => {
      const currentById = new Map(current.map((node) => [node.id, node]));
      return xyNodes.map((nextNode) => {
        const currentNode = currentById.get(nextNode.id);
        if (!currentNode) {
          return nextNode;
        }
        const previousServerPosition = currentNode.data.node.position || {};
        const nextServerPosition = nextNode.data.node.position || {};
        const serverPositionChanged =
          Number(previousServerPosition.x || 80) !== Number(nextServerPosition.x || 80) ||
          Number(previousServerPosition.y || 80) !== Number(nextServerPosition.y || 80);
        return {
          ...nextNode,
          position: serverPositionChanged ? nextNode.position : currentNode.position
        };
      });
    });
  }, [setDisplayNodes, xyNodes]);

  if (boardsQuery.isLoading) {
    return <section className="main-workspace writing-surface"><div className="review-empty">正在加载写作网络...</div></section>;
  }

  if (!activeBoardId || projectManagerOpen) {
    return (
      <section className="main-workspace writing-surface writing-project-manager" aria-label="Writing Workspace">
        <div className="writing-start-panel" data-testid="writing-start-panel">
          <div>
            <span className="eyebrow">Writing Workspace</span>
            <h1>写作项目</h1>
            <p>每个项目是一块独立画布。问题节点有独立 session，连接到它的节点会作为结构化上下文传给 Ask PSKA。</p>
          </div>
          <textarea data-testid="writing-new-goal" value={newGoal} onChange={(event) => setNewGoal(event.target.value)} placeholder="我要写一篇关于……的文章/备忘录/报告，需要先弄清楚……" />
          <div className="writing-start-actions">
            <button className="primary" type="button" onClick={() => void createBoard()} data-testid="writing-create-board">
              新建空白画布
            </button>
            {activeBoardId ? <button type="button" onClick={() => setProjectManagerOpen(false)}>返回当前项目</button> : null}
          </div>
          {boardsQuery.isError ? <small>写作工作区暂时无法加载，请检查后端或登录状态。</small> : null}
          {workspaceMessage ? <small>{workspaceMessage}</small> : null}
        </div>
        <div className="writing-project-list" aria-label="写作项目列表" data-testid="writing-project-list">
          {boards.length ? boards.map((item) => (
            <article key={item.board_id} className={item.board_id === activeBoardId ? "active" : ""} data-testid="writing-project" data-board-id={item.board_id}>
              <button className="writing-project-card-main" type="button" onClick={() => openBoard(item.board_id)} data-testid="writing-open-board">
                <strong>{item.title || "未命名写作项目"}</strong>
                <p>{item.goal || "没有填写目标。"}</p>
                <small>{item.updated_at ? `更新于 ${formatReviewDate(item.updated_at)}` : item.board_id}</small>
              </button>
              <div className="writing-project-actions">
                <button type="button" className="danger" onClick={() => void removeBoard(item.board_id)} data-testid="writing-delete-board">删除</button>
              </div>
            </article>
          )) : <div className="review-empty compact">还没有写作项目。创建后会得到一块空白 Inquiry Graph 画布。</div>}
        </div>
      </section>
    );
  }

  return (
    <section className="main-workspace writing-surface" aria-label="Writing Workspace">
      <div className="writing-toolbar" data-testid="writing-toolbar">
        <select value={activeBoardId} onChange={(event) => setActiveBoardId(event.target.value)} aria-label="写作网络">
          {boards.map((item) => (
            <option value={item.board_id} key={item.board_id}>{item.title || item.board_id}</option>
          ))}
        </select>
        <button type="button" onClick={() => void addNode("question")} data-testid="writing-add-question">问题</button>
        <button type="button" onClick={() => void addNode("section")} data-testid="writing-add-section">章节</button>
        <button type="button" onClick={closeBoard} data-testid="writing-close-board">关闭项目</button>
        <button type="button" onClick={() => void copyBoardMarkdown()} data-testid="writing-export-markdown">导出 Markdown</button>
        <button type="button" onClick={onPinCurrent}>
          <Pin size={15} />
          {pinStatus === "saved" ? "已置顶" : pinStatus === "failed" ? "失败" : "置顶"}
        </button>
      </div>
      <div className="writing-board-title" data-testid="writing-board-title">
        <input value={board?.title || ""} onChange={(event) => void patchBoard({ title: event.target.value })} aria-label="标题" data-testid="writing-board-title-input" />
        <input value={board?.goal || ""} onChange={(event) => void patchBoard({ goal: event.target.value })} aria-label="目标" placeholder="写作目标" data-testid="writing-board-goal-input" />
      </div>
      <div className="writing-canvas-shell" data-testid="writing-canvas">
        <ReactFlow
          nodes={displayNodes}
          edges={xyEdges}
          nodeTypes={nodeTypes}
          onConnect={onConnect}
          onNodesChange={onDisplayNodesChange}
          onNodeDragStop={onNodeDragStop}
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={26} color="#ddd8cb" />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>
      <WritingComposer
        board={board}
        nodes={writingNodes}
        edges={writingEdges}
        selectedSectionId={selectedSectionId}
        onSelectSection={setSelectedSectionId}
        onCompose={() => void composeSection()}
        onCopy={() => void copyBoardMarkdown()}
        message={workspaceMessage}
        askPreview={runningNodeIds.length === 1 ? askPreviews[runningNodeIds[0]] : undefined}
        runningCount={runningNodeIds.length}
      />
      {editingNode ? (
        <WritingFloatingEditor
          node={editingNode}
          maximized={editorMaximized}
          onToggleMaximized={() => setEditorMaximized((current) => !current)}
          onCloseSave={(nodeId, patch) => void saveAndCloseEditor(nodeId, patch)}
        />
      ) : null}
    </section>
  );
}

function WritingCanvasNode({ data }: NodeProps<WritingFlowNode>) {
  const node = data.node;
  const timelineResult = data.askPreview;
  const timelineRawEvents = timelineResult ? agenticTraceEvents(timelineResult) : [];
  const hasTimeline = Boolean(timelineResult?.agent_steps?.length || timelineRawEvents.length);

  return (
    <div
      className={`writing-node writing-node-${node.node_type} ${data.selected ? "selected" : ""}`}
      data-testid="writing-node"
      data-node-id={node.node_id}
      data-node-type={node.node_type}
      data-node-title={node.title}
    >
      <Handle type="target" position={Position.Left} />
      <div className="writing-node-top">
        <span>{writingNodeLabel(node.node_type)}</span>
        <small>{data.running ? "运行中" : node.status || "idle"}</small>
      </div>
      <h3>{displayText(node.title, writingNodeDefaultTitle(node.node_type))}</h3>
      {node.body_markdown ? (
        <div
          className="writing-node-body-preview nodrag"
          onDoubleClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            data.onOpenEditor(node);
          }}
        >
          <MarkdownAnswer content={node.body_markdown} className="nodrag" />
        </div>
      ) : null}
      {hasTimeline ? (
        <div className="writing-node-timeline nodrag" data-testid="writing-node-timeline">
          <div className="writing-node-session">
            <span>Session</span>
            <code>{writingNodeSessionId(node)}</code>
          </div>
          <AskProcessTimeline steps={normalizeAskAgentSteps(timelineResult?.agent_steps)} rawEvents={timelineRawEvents} />
        </div>
      ) : null}
      {node.citations?.length || node.source_refs?.length ? (
        <div className="writing-node-citation-bar">
          <span>引用 {(node.citations || node.source_refs || []).length}</span>
        </div>
      ) : null}
      <div className="writing-node-actions nodrag">
        <button type="button" onClick={() => data.onOpenEditor(node)} data-testid="writing-node-toggle">编辑</button>
        {node.node_type === "question" ? <button type="button" onClick={() => data.onRunAsk(node)} disabled={data.running} data-testid="writing-node-ask">Ask</button> : null}
        {node.node_type === "question" || node.node_type === "answer" ? <button type="button" onClick={() => data.onSuggest(node, "followup")} data-testid="writing-node-followup">追问</button> : null}
        {node.node_type === "goal" || node.node_type === "question" ? <button type="button" onClick={() => data.onSuggest(node, "decompose")} data-testid="writing-node-decompose">拆解</button> : null}
        {node.node_type === "answer" && data.canAddToSection ? <button type="button" onClick={() => data.onAddToSection(node)} data-testid="writing-node-add-to-section">入章节</button> : null}
        <button type="button" onClick={() => data.onDelete(node)} data-testid="writing-node-delete">删除</button>
      </div>
      {data.suggestions.length ? (
        <div className="writing-suggestions nodrag" data-testid="writing-suggestions">
          {data.suggestions.map((suggestion, index) => (
            <button type="button" key={suggestion.suggestion_id || index} onClick={() => data.onAcceptSuggestion(node, suggestion)} data-testid="writing-accept-suggestion">
              {suggestion.question}
            </button>
          ))}
        </div>
      ) : null}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function WritingFloatingEditor({
  node,
  maximized,
  onToggleMaximized,
  onCloseSave
}: {
  node: WritingNode;
  maximized: boolean;
  onToggleMaximized: () => void;
  onCloseSave: (nodeId: string, patch: Partial<WritingNode>) => void;
}) {
  const [draftTitle, setDraftTitle] = useState(node.title);
  const [draftBody, setDraftBody] = useState(node.body_markdown || "");
  const closeRequested = useRef(false);

  useEffect(() => {
    setDraftTitle(node.title);
    setDraftBody(node.body_markdown || "");
    closeRequested.current = false;
  }, [node.node_id, node.title, node.body_markdown]);

  function closeAndSave() {
    if (closeRequested.current) {
      return;
    }
    closeRequested.current = true;
    onCloseSave(node.node_id, { title: draftTitle, body_markdown: draftBody });
  }

  return (
    <div className="writing-editor-layer" role="presentation">
      <section
        className={`writing-floating-editor ${maximized ? "maximized" : ""}`}
        aria-label="节点文本编辑器"
        data-testid="writing-floating-editor"
      >
        <header className="writing-floating-editor-bar">
          <span>{writingNodeLabel(node.node_type)}</span>
          <div>
            <button
              className="icon-button"
              type="button"
              onClick={onToggleMaximized}
              title={maximized ? "还原" : "最大化"}
              aria-label={maximized ? "还原" : "最大化"}
              data-testid="writing-editor-maximize"
            >
              {maximized ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </button>
            <button
              className="icon-button"
              type="button"
              onClick={closeAndSave}
              title="关闭并保存"
              aria-label="关闭并保存"
              data-testid="writing-editor-close"
            >
              <X size={16} />
            </button>
          </div>
        </header>
        <input
          className="writing-floating-title"
          value={draftTitle}
          onChange={(event) => setDraftTitle(event.target.value)}
          aria-label="节点标题"
          data-testid="writing-editor-title"
        />
        <textarea
          className="writing-floating-body"
          value={draftBody}
          onChange={(event) => setDraftBody(event.target.value)}
          aria-label="节点正文"
          data-testid="writing-editor-body"
        />
      </section>
    </div>
  );
}

function WritingComposer({
  board,
  nodes,
  edges,
  selectedSectionId,
  onSelectSection,
  onCompose,
  onCopy,
  message,
  askPreview,
  runningCount
}: {
  board?: WritingBoard;
  nodes: WritingNode[];
  edges: WritingEdge[];
  selectedSectionId: string;
  onSelectSection: (nodeId: string) => void;
  onCompose: () => void;
  onCopy: () => void;
  message: string;
  askPreview?: WorkspaceAskResponse;
  runningCount?: number;
}) {
  const sections = nodes.filter((node) => node.node_type === "section");
  const selectedEdges = edges.filter((edge) => edge.edge_type === "included_in" && edge.target_node_id === selectedSectionId);
  const selectedAnswerIds = new Set(selectedEdges.map((edge) => edge.source_node_id));
  const selectedAnswers = nodes.filter((node) => selectedAnswerIds.has(node.node_id));
  const drafts = nodes.filter((node) => node.node_type === "draft");
  return (
    <aside className="writing-composer" aria-label="文章结构" data-testid="writing-composer">
      <div>
        <span className="eyebrow">Composer</span>
        <h2>{board?.title || "写作网络"}</h2>
        <p>{board?.goal || "把问题网络收束为可引用草稿。"}</p>
      </div>
      <label>
        <span>当前章节</span>
        <select value={selectedSectionId} onChange={(event) => onSelectSection(event.target.value)} data-testid="writing-section-select">
          {sections.map((section) => (
            <option value={section.node_id} key={section.node_id}>{section.title}</option>
          ))}
        </select>
      </label>
      <div className="writing-composer-actions">
        <button className="primary" type="button" onClick={onCompose} data-testid="writing-compose-draft">生成章节草稿</button>
        <button type="button" onClick={onCopy} data-testid="writing-copy-markdown">复制 Markdown</button>
      </div>
      <div className="writing-composer-section">
        <strong>已纳入答案</strong>
        {selectedAnswers.length ? selectedAnswers.map((answer) => (
          <article key={answer.node_id} data-testid="writing-composer-answer">
            <span>{answer.title}</span>
            <small>{(answer.citations || answer.source_refs || []).length} 引用</small>
          </article>
        )) : <p>还没有答案节点进入当前章节。</p>}
      </div>
      {askPreview ? (
        <div className="writing-composer-section">
          <strong>正在运行</strong>
          <AskProcessTimeline steps={normalizeAskAgentSteps(askPreview.agent_steps)} rawEvents={agenticTraceEvents(askPreview)} />
        </div>
      ) : runningCount ? (
        <div className="writing-composer-section">
          <strong>正在运行</strong>
          <p>{runningCount} 个问题节点正在并行检索；展开对应节点查看各自事件流。</p>
        </div>
      ) : null}
      <div className="writing-composer-section">
        <strong>草稿节点</strong>
        {drafts.length ? drafts.slice(-3).map((draft) => (
          <article key={draft.node_id} data-testid="writing-composer-draft">
            <span>{draft.title}</span>
            <small>{trimText(draft.body_markdown || "", 90)}</small>
          </article>
        )) : <p>生成章节草稿后会出现在画布中。</p>}
      </div>
      {message ? <p className="writing-message">{message}</p> : null}
    </aside>
  );
}

function writingNodeSessionId(node: WritingNode) {
  const existing = node.metadata?.session_id;
  return typeof existing === "string" && existing.trim() ? existing : `writing:${node.board_id}:${node.node_id}`;
}

function writingNodeLastAsk(result: WorkspaceAskResponse, query: string, sessionId: string, scope: Record<string, unknown>) {
  return {
    query,
    session_id: sessionId,
    scope,
    route: result.route || {},
    timing: result.timing || {},
    agent_steps: (result.agent_steps || []).slice(-40),
    trace: {
      events: agenticTraceEvents(result).slice(-40),
      run_id: typeof result.trace?.run_id === "string" ? result.trace.run_id : undefined,
      session_id: typeof result.trace?.session_id === "string" ? result.trace.session_id : sessionId
    },
    saved_at: new Date().toISOString()
  };
}

function writingNodeLastAskPreview(node: WritingNode): WorkspaceAskResponse | undefined {
  const lastAsk = node.metadata?.last_ask;
  if (!lastAsk || typeof lastAsk !== "object" || Array.isArray(lastAsk)) {
    return undefined;
  }
  const data = lastAsk as Record<string, unknown>;
  return {
    ok: true,
    query: typeof data.query === "string" ? data.query : node.title,
    answer: "",
    route: isPlainObject(data.route) ? data.route as WorkspaceAskResponse["route"] : undefined,
    timing: isPlainObject(data.timing) ? data.timing as WorkspaceAskResponse["timing"] : undefined,
    agent_steps: Array.isArray(data.agent_steps) ? data.agent_steps as WorkspaceAskResponse["agent_steps"] : [],
    trace: isPlainObject(data.trace) ? data.trace : undefined
  };
}

function buildWritingAskScope(boardId: string, node: WritingNode, nodes: WritingNode[], edges: WritingEdge[], sessionId: string) {
  const connectedEdges = edges.filter((edge) => edge.source_node_id === node.node_id || edge.target_node_id === node.node_id);
  const connectedNodeIds = new Set<string>();
  connectedEdges.forEach((edge) => {
    connectedNodeIds.add(edge.source_node_id);
    connectedNodeIds.add(edge.target_node_id);
  });
  connectedNodeIds.delete(node.node_id);
  const connectedNodes = nodes.filter((item) => connectedNodeIds.has(item.node_id));
  const sourceItemIds = new Set<string>();
  [node, ...connectedNodes].forEach((item) => {
    [...(item.source_refs || []), ...(item.citations || [])].forEach((ref) => {
      const sourceItemId = typeof ref.source_item_id === "string" ? ref.source_item_id : "";
      if (sourceItemId) {
        sourceItemIds.add(sourceItemId);
      }
    });
  });
  return {
    board_id: boardId,
    node_id: node.node_id,
    session_id: sessionId,
    context_model: "connected_nodes_v1",
    context_rule: "directly connected writing nodes are included as structured context",
    context_nodes: connectedNodes.map(compactWritingNodeForScope),
    context_edges: connectedEdges.map((edge) => ({
      edge_id: edge.edge_id,
      source_node_id: edge.source_node_id,
      target_node_id: edge.target_node_id,
      edge_type: edge.edge_type,
      label: edge.label || writingEdgeLabel(edge.edge_type)
    })),
    source_item_ids: Array.from(sourceItemIds).slice(0, 20)
  };
}

function compactWritingNodeForScope(node: WritingNode) {
  return {
    node_id: node.node_id,
    node_type: node.node_type,
    title: node.title,
    body_markdown: trimText(node.body_markdown || "", 900),
    status: node.status || "idle",
    source_ref_count: (node.source_refs || []).length,
    citation_count: (node.citations || []).length
  };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function writingNodeDefaultTitle(type: WritingNodeType) {
  const labels: Record<WritingNodeType, string> = {
    goal: "写作目标",
    question: "待回答问题",
    answer: "证据回答",
    evidence: "证据",
    gap: "证据缺口",
    section: "章节",
    draft: "草稿"
  };
  return labels[type];
}

function writingNodeLabel(type: string) {
  return writingNodeDefaultTitle((["goal", "question", "answer", "evidence", "gap", "section", "draft"].includes(type) ? type : "question") as WritingNodeType);
}

function writingEdgeLabel(type: string) {
  const labels: Record<string, string> = {
    decomposes_to: "拆解",
    answered_by: "回答",
    supported_by: "支持",
    raises: "引出",
    conflicts_with: "冲突",
    included_in: "纳入",
    follows: "承接"
  };
  return labels[type] || type;
}

function buildWritingExportMarkdown(board: WritingBoard | undefined, nodes: WritingNode[], edges: WritingEdge[]) {
  const lines = [`# ${board?.title || "Writing Workspace"}`, ""];
  if (board?.goal) {
    lines.push(board.goal, "");
  }
  const sections = nodes.filter((node) => node.node_type === "section");
  const nodeById = new Map(nodes.map((node) => [node.node_id, node]));
  sections.forEach((section) => {
    lines.push(`## ${section.title}`, "");
    if (section.body_markdown) {
      lines.push(section.body_markdown, "");
    }
    const answerIds = edges.filter((edge) => edge.edge_type === "included_in" && edge.target_node_id === section.node_id).map((edge) => edge.source_node_id);
    answerIds.forEach((answerId) => {
      const answer = nodeById.get(answerId);
      if (!answer) {
        return;
      }
      lines.push(`### ${answer.title}`, "", answer.body_markdown || "", "");
      const refs = [...(answer.citations || []), ...(answer.source_refs || [])];
      if (refs.length) {
        lines.push("引用：");
        refs.slice(0, 8).forEach((ref) => lines.push(`- ${displayText(ref.title || ref.source_item_id || ref.chunk_id, "来源")}`));
        lines.push("");
      }
    });
  });
  const drafts = nodes.filter((node) => node.node_type === "draft");
  if (drafts.length) {
    lines.push("## Drafts", "");
    drafts.forEach((draft) => lines.push(`### ${draft.title}`, "", draft.body_markdown || "", ""));
  }
  return lines.join("\n").trim();
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
  const [pathQuery, setPathQuery] = useState("digest claims");
  const [pathResult, setPathResult] = useState<WorkspaceGraphPathResponse | null>(null);
  const [graphAskResult, setGraphAskResult] = useState<WorkspaceAskResponse | null>(null);
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
    setGraphAskResult(pendingAskResult(query));
    try {
      const payload = await askWorkspaceStream(query, serviceToken, "auto", "graph", ({ result: partial }) => {
        setGraphAskResult({ ...partial });
      });
      setGraphAskResult(payload);
      setPathResult(null);
      setPathStatus(payload.ok === false ? "error" : "success");
      setPathError(payload.error ? displaySearchError(payload.error) : "");
    } catch (err) {
      setPathStatus("error");
      setPathError(err instanceof Error ? err.message : "Ask PSKA 失败。");
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
            <form className="graph-path-search" onSubmit={(event) => void handleGraphPath(event)} aria-label="Ask PSKA">
              <Search size={15} />
              <input
                value={pathQuery}
                onChange={(event) => setPathQuery(event.target.value)}
                placeholder="向 PSKA 提问，证据会在右侧展开"
              />
              <button type="submit" disabled={pathStatus === "loading"}>
                {pathStatus === "loading" ? "查询中" : "Ask PSKA"}
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
                  {selectedNode.quality_tier ? <div><dt>质量门</dt><dd>{reviewQualityTierLabel(selectedNode.quality_tier)}</dd></div> : null}
                  {selectedNode.promotion_reason ? <div><dt>提升依据</dt><dd>{reviewPromotionReasonLabel(selectedNode.promotion_reason)}</dd></div> : null}
                  {selectedNode.support_kinds?.length ? (
                    <div><dt>支撑类型</dt><dd>{selectedNode.support_kinds.map((kind) => reviewSupportKindLabel(kind)).filter(Boolean).join(" · ")}</dd></div>
                  ) : null}
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
                <span className="eyebrow">Knowledge Graph</span>
                <h2>选择一个节点</h2>
                <p>点击 digest、claim、hyperedge 或 passage，查看它如何追溯到原文证据。</p>
              </>
            )}
            {graphAskResult ? <AskResult result={graphAskResult} pending={pathStatus === "loading"} /> : <GraphPathPanel result={pathResult} status={pathStatus} error={pathError} />}
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
        <span className="eyebrow">证据详情</span>
        <p>输入问题后，这里会显示 seeds、facts、passages 和 citations。</p>
      </div>
    );
  }
  if (status === "loading") {
    return (
      <div className="graph-path-panel">
        <span className="eyebrow">证据详情</span>
        <p>正在查询证据...</p>
      </div>
    );
  }
  if (status === "error" && !result) {
    return (
      <div className="graph-path-panel error-state">
        <span className="eyebrow">证据详情</span>
        <p>{error || "Ask PSKA 查询失败。"}</p>
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
      <span className="eyebrow">证据详情</span>
      <h3>{displayText(result?.query, "Ask PSKA")}</h3>
      <p>{result?.answer || result?.path_summary?.summary || "暂无路径摘要。"}</p>
      {error ? <p className="graph-path-warning">{error}</p> : null}
      {result?.mode || result?.agentic_service ? (
        <div className="graph-path-run">
          <span>{result.requires_agentic_service_online ? "深入分析" : "快速证据"}</span>
          {result.display_mode ? <span>{displayText(result.display_mode)}</span> : null}
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
          答案重写{result.agentic_repair.accepted ? "已采用" : "未采用"} · {displayText(result.agentic_repair.final_answer_mode || result.display_mode || result.mode)}
          {result.agentic_repair.repaired_answer_chars ? ` · ${result.agentic_repair.repaired_answer_chars} chars` : ""}
        </p>
      ) : null}
      {expansionDecisions.length ? (
        <div className="graph-path-section">
          <strong>Evidence Expansion</strong>
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
    addNode(chunkId, chunk.title || chunk.source_item_id || "Chunk", chunk.snippet || chunk.text || "资料片段", "chunks", "text");
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

type CanvasCardData = Record<string, unknown> & { title: string; body: string; icon: "text" | "doc" | "image" | "link"; kind?: string };
type CanvasFlowNode = Node<CanvasCardData, "pskaCard">;

function CanvasCardNode({ data }: NodeProps<CanvasFlowNode>) {
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
  if (surface === "writing") {
    return { title: "Writing Workspace", summary: "构造问题网络并组织可引用草稿。" };
  }
  if (surface === "canvas") {
    return { title: "画布工作区", summary: "查看画布工作区。" };
  }
  if (surface === "graph") {
    return { title: "Graph 工作区", summary: "查看真实 PSKA 图谱与候选关系。" };
  }
  if (surface === "corpus") {
  return { title: "资料库", summary: "查看已同步资料、可检索片段和资料位置。" };
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
