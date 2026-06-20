import { create } from "zustand";
import type { BrainState, WorkspaceMode } from "./types";

type WorkspaceStore = {
  mode: WorkspaceMode;
  leftCollapsed: boolean;
  activeDocumentId: string;
  documentText: string;
  selectedText: string;
  serviceToken: string;
  brain: BrainState;
  setMode: (mode: WorkspaceMode) => void;
  toggleLeft: () => void;
  setDocumentText: (documentText: string) => void;
  setSelectedText: (selectedText: string) => void;
  setServiceToken: (serviceToken: string) => void;
  setBrain: (brain: Partial<BrainState>) => void;
};

const initialBrain: BrainState = {
  relatedKnowledge: [
    {
      id: "fastreact",
      title: "FastReAct 架构",
      score: 92,
      snippet: "Agentic 规划保留在 PSKA 之外；PSKA 负责检索、ACL、来源引用和记忆写入。",
      source: "architecture-status"
    },
    {
      id: "hipporag",
      title: "HippoRAG2 笔记",
      score: 88,
      snippet: "长期图检索结合实体链接、证据片段和 PageRank 风格的多跳遍历。",
      source: "retrieval design"
    },
    {
      id: "runtime",
      title: "Agent 运行时设计",
      score: 85,
      snippet: "工具执行应可审计、感知引用，并受本地服务契约约束。",
      source: "runtime draft"
    }
  ],
  entities: ["FastReAct", "工具运行时", "OpenAI Responses API", "记忆层"],
  timeline: [
    { id: "timeline-1", age: "3 个月前", title: "Agent 运行时草稿", detail: "梳理编排边界和 trace 捕获方式。" },
    { id: "timeline-2", age: "5 个月前", title: "工具调度设计", detail: "比较后台任务、重试策略和预算控制。" },
    { id: "timeline-3", age: "8 个月前", title: "GraphRAG 架构", detail: "把实体、事实和证据映射为可遍历图谱。" }
  ],
  connections: [
    { id: "conn-1", label: "FastReAct", relation: "通过它规划" },
    { id: "conn-2", label: "工具分发器", relation: "通过它执行" },
    { id: "conn-3", label: "工作流引擎", relation: "协调" }
  ],
  status: "idle",
  lastTrigger: "pause",
  updatedAt: null
};

export const useWorkspaceStore = create<WorkspaceStore>((set) => ({
  mode: "today",
  leftCollapsed: false,
  activeDocumentId: "agent-runtime",
  documentText:
    "Agent 运行时\n\nPSKA 应该在用户写作时退到旁边。工作台观察当前块，在用户暂停后检索相关知识，并展示证据，但不会自动插入内容。\n\n待讨论问题：\n- 文档模式和画布模式之间，上下文刷新应该如何衔接？\n- 哪些图谱关系值得提升为长期记忆？",
  selectedText: "",
  serviceToken: window.sessionStorage.getItem("pska_service_token") || "",
  brain: initialBrain,
  setMode: (mode) => set({ mode }),
  toggleLeft: () => set((state) => ({ leftCollapsed: !state.leftCollapsed })),
  setDocumentText: (documentText) => set({ documentText }),
  setSelectedText: (selectedText) => set({ selectedText }),
  setServiceToken: (serviceToken) => {
    window.sessionStorage.setItem("pska_service_token", serviceToken);
    set({ serviceToken });
  },
  setBrain: (brain) => set((state) => ({ brain: { ...state.brain, ...brain } }))
}));
