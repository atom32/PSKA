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
