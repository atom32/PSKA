import { create } from "zustand";
import type { BrainState, WorkspaceMode } from "./types";

type WorkspaceStore = {
  mode: WorkspaceMode;
  leftCollapsed: boolean;
  activeDocumentId: string;
  documentText: string;
  selectedText: string;
  serviceToken: string;
  tenantId: string;
  userId: string;
  representedUserId: string;
  currentKnowledgeBaseId: string;
  selectedKnowledgeBaseIds: string[];
  knowledgeBaseScopeMode: "current" | "all" | "selected" | "attachments";
  brain: BrainState;
  setMode: (mode: WorkspaceMode) => void;
  toggleLeft: () => void;
  setDocumentText: (documentText: string) => void;
  setSelectedText: (selectedText: string) => void;
  setServiceToken: (serviceToken: string) => void;
  setTenantId: (tenantId: string) => void;
  setUserId: (userId: string) => void;
  setRepresentedUserId: (representedUserId: string) => void;
  setCurrentKnowledgeBaseId: (knowledgeBaseId: string) => void;
  setSelectedKnowledgeBaseIds: (knowledgeBaseIds: string[]) => void;
  setKnowledgeBaseScopeMode: (mode: "current" | "all" | "selected" | "attachments") => void;
  setBrain: (brain: Partial<BrainState>) => void;
  clearIdentity: () => void;
};

const initialBrain: BrainState = {
  relatedKnowledge: [],
  entities: [],
  timeline: [],
  connections: [],
  status: "idle",
  lastTrigger: "pause",
  updatedAt: null,
  error: null
};

const storedStringArray = (key: string): string[] => {
  try {
    const value = JSON.parse(window.sessionStorage.getItem(key) || "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
  } catch {
    return [];
  }
};

export const useWorkspaceStore = create<WorkspaceStore>((set) => ({
  mode: "today",
  leftCollapsed: false,
  activeDocumentId: "draft",
  documentText: "",
  selectedText: "",
  serviceToken: window.sessionStorage.getItem("pska_service_token") || "",
  tenantId: window.sessionStorage.getItem("pska_tenant_id") || "tenant_default",
  userId: window.sessionStorage.getItem("pska_user_id") || "user_primary",
  representedUserId: window.sessionStorage.getItem("pska_represented_user_id") || "",
  currentKnowledgeBaseId: window.sessionStorage.getItem("pska_current_knowledge_base_id") || "",
  selectedKnowledgeBaseIds: storedStringArray("pska_selected_knowledge_base_ids"),
  knowledgeBaseScopeMode: (window.sessionStorage.getItem("pska_knowledge_base_scope_mode") as "current" | "all" | "selected" | "attachments" | null) || "current",
  brain: initialBrain,
  setMode: (mode) => set({ mode }),
  toggleLeft: () => set((state) => ({ leftCollapsed: !state.leftCollapsed })),
  setDocumentText: (documentText) => set({ documentText }),
  setSelectedText: (selectedText) => set({ selectedText }),
  setServiceToken: (serviceToken) => {
    window.sessionStorage.setItem("pska_service_token", serviceToken);
    set({ serviceToken });
  },
  setTenantId: (tenantId) => {
    window.sessionStorage.setItem("pska_tenant_id", tenantId);
    set({ tenantId });
  },
  setUserId: (userId) => {
    window.sessionStorage.setItem("pska_user_id", userId);
    set((state) => ({ userId, representedUserId: state.representedUserId || userId }));
  },
  setRepresentedUserId: (representedUserId) => {
    window.sessionStorage.setItem("pska_represented_user_id", representedUserId);
    set({ representedUserId });
  },
  setCurrentKnowledgeBaseId: (currentKnowledgeBaseId) => {
    window.sessionStorage.setItem("pska_current_knowledge_base_id", currentKnowledgeBaseId);
    set({ currentKnowledgeBaseId, selectedKnowledgeBaseIds: currentKnowledgeBaseId ? [currentKnowledgeBaseId] : [] });
    window.sessionStorage.setItem("pska_selected_knowledge_base_ids", JSON.stringify(currentKnowledgeBaseId ? [currentKnowledgeBaseId] : []));
  },
  setSelectedKnowledgeBaseIds: (selectedKnowledgeBaseIds) => {
    window.sessionStorage.setItem("pska_selected_knowledge_base_ids", JSON.stringify(selectedKnowledgeBaseIds));
    set({ selectedKnowledgeBaseIds });
  },
  setKnowledgeBaseScopeMode: (knowledgeBaseScopeMode) => {
    window.sessionStorage.setItem("pska_knowledge_base_scope_mode", knowledgeBaseScopeMode);
    set({ knowledgeBaseScopeMode });
  },
  setBrain: (brain) => set((state) => ({ brain: { ...state.brain, ...brain } })),
  clearIdentity: () => {
    window.sessionStorage.removeItem("pska_service_token");
    window.sessionStorage.removeItem("pska_tenant_id");
    window.sessionStorage.removeItem("pska_user_id");
    window.sessionStorage.removeItem("pska_represented_user_id");
    set({
      serviceToken: "",
      tenantId: "tenant_default",
      userId: "user_primary",
      representedUserId: "",
      currentKnowledgeBaseId: "",
      selectedKnowledgeBaseIds: [],
      knowledgeBaseScopeMode: "current"
    });
  }
}));
