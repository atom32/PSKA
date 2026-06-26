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
  brain: BrainState;
  setMode: (mode: WorkspaceMode) => void;
  toggleLeft: () => void;
  setDocumentText: (documentText: string) => void;
  setSelectedText: (selectedText: string) => void;
  setServiceToken: (serviceToken: string) => void;
  setTenantId: (tenantId: string) => void;
  setUserId: (userId: string) => void;
  setRepresentedUserId: (representedUserId: string) => void;
  setBrain: (brain: Partial<BrainState>) => void;
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
  setBrain: (brain) => set((state) => ({ brain: { ...state.brain, ...brain } }))
}));
