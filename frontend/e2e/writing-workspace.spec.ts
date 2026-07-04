import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const frontendUrl = (process.env.PSKA_E2E_FRONTEND_URL || "http://127.0.0.1:5173").replace(/\/$/, "");
const authnodeUrl = (process.env.PSKA_E2E_AUTHNODE_URL || "http://127.0.0.1:8788").replace(/\/$/, "");
const tenantId = process.env.PSKA_E2E_TENANT_ID || "tenant_default";
const userId = process.env.PSKA_E2E_USER_ID || "user_primary";
const password =
  process.env.PSKA_E2E_PASSWORD ||
  (userId === "user_primary" ? "primary-local" : userId === "alice" ? "alice-local" : "pska-local");
const marker = process.env.PSKA_E2E_MARKER || `pska-writing-e2e-${Date.now()}`;
const sourceTitle = process.env.PSKA_E2E_SOURCE_TITLE || `writing-scope-fixture-${marker}.md`;
const fixtureSecret = `WRITING_SCOPE_${marker.replace(/[^a-zA-Z0-9]/g, "_")}`;
const reportPath = process.env.PSKA_E2E_REPORT_PATH || "";

test.setTimeout(900_000);

test("workspace files to corpus to parallel writing draft", async ({ page, request }) => {
  let fixtureSourceItemId: string | undefined;
  let fixtureKnowledgeBaseId: string | undefined;
  const callbackUrl = await authnodeCallbackUrl(request);
  await page.goto(callbackUrl);
  await expect(page.getByTestId("gateway-session")).toContainText(userId);
  await expect(page.getByTestId("gateway-session")).toContainText(tenantId);

  try {
    const fixture = await createWritingSourceFixture(page, sourceTitle, writingFixtureText(marker, fixtureSecret));
    fixtureSourceItemId = fixture.sourceItemId;
    fixtureKnowledgeBaseId = fixture.knowledgeBaseId;

    await page.reload();
    await expect(page.getByTestId("gateway-session")).toContainText(userId);
    await expect(page.getByTestId("gateway-session")).toContainText(tenantId);

    await openWorkspace(page, "资料库");
    await page.getByRole("combobox", { name: "当前知识库" }).selectOption(fixture.knowledgeBaseId);
    await page.getByTestId("knowledge-base-tab-sources").click();
    await page.getByTestId("corpus-search-input").fill(sourceTitle);
    await expect(page.getByTestId("document-lifecycle-list")).toContainText(sourceTitle, { timeout: 45_000 });

    await openWorkspace(page, "写作");
    await createBoard(page, fixture.knowledgeBaseId, fixture.knowledgeBaseName);

    const questions = [
      {
        title: `What is the reserve recommendation for ${marker}?`,
        body: `Use only the PSKA fixture evidence. Include the marker ${fixtureSecret}, candidate name, and recommendation.`
      },
      {
        title: `Who owns diligence for ${marker} and what is the reserve ceiling?`,
        body: "Extract the named owner, Q3 reserve ceiling, and the exact condition that must be satisfied before allocation."
      },
      {
        title: `What traction evidence supports ${marker}?`,
        body: "Separate renewal, expansion, and customer proof evidence from unsupported claims."
      },
      {
        title: `What risks argue against approving ${marker} now?`,
        body: "Find the strongest counter-evidence across concentration, reliability, and compliance readiness."
      }
    ];
    for (const question of questions) {
      await createQuestion(page, question.title, question.body);
    }

    await runQuestionsInParallel(page, questions.length);
    const initialAnswerCount = await page.locator('[data-testid="writing-node"][data-node-type="answer"]').count();
    expect(initialAnswerCount).toBeGreaterThanOrEqual(questions.length);
    await expect(page.locator('[data-testid="writing-node-timeline"]').first()).toBeVisible();

    const followupNodeId = await createConnectedFollowupNode(page);
    await page.reload();
    await ensureWritingBoardOpen(page);
    const followupNode = page.locator(`[data-testid="writing-node"][data-node-id="${followupNodeId}"]`);
    await expect(followupNode).toBeVisible();
    await runQuestionNodesViaSession(page, [followupNodeId]);
    await page.reload();
    await ensureWritingBoardOpen(page);
    await expect(page.locator('[data-testid="writing-node"][data-node-type="answer"]')).toHaveCount(initialAnswerCount + 1, {
      timeout: 240_000
    });

    await addAnswersToSection(page, Math.min(initialAnswerCount + 1, 5));
    await page.getByTestId("writing-compose-draft").click();
    await expect(page.locator('[data-testid="writing-node"][data-node-type="draft"]')).toHaveCount(1, { timeout: 45_000 });

    const result = await collectBoardResult(page);
    expect(result.answer_count).toBeGreaterThanOrEqual(questions.length + 1);
    expect(result.draft_length).toBeGreaterThan(200);
    expect(result.citation_count).toBeGreaterThan(0);
    expect(result.timeline_count).toBeGreaterThanOrEqual(questions.length);
    expect(result.health_signal_count).toBeGreaterThanOrEqual(questions.length);
    expect(result.board_kb_count).toBeGreaterThan(0);
    expect(result.writing_ask_scoped_count).toBeGreaterThanOrEqual(questions.length + 1);
    expect(result.combined_text).toContain(fixtureSecret);
    expect(result.draft_text).not.toMatch(/FastReAct|MCP|tool_call|GraphRAG/i);

    if (reportPath) {
      fs.mkdirSync(path.dirname(reportPath), { recursive: true });
      fs.writeFileSync(reportPath, JSON.stringify({ ok: true, ...result }, null, 2), "utf-8");
    }
  } finally {
    await softDeleteSourceItemIfPossible(page, fixtureSourceItemId);
    await archiveKnowledgeBaseIfPossible(page, fixtureKnowledgeBaseId);
  }
});

async function authnodeCallbackUrl(request: APIRequestContext) {
  const response = await request.post(`${authnodeUrl}/login?local=1`, {
    form: {
      username: userId,
      tenant_id: tenantId,
      password,
      target: "pska",
      return_to: `${frontendUrl}/auth/callback`,
      next: "/"
    },
    maxRedirects: 0
  });
  if (response.status() !== 302) {
    throw new Error(`AuthNode login failed with HTTP ${response.status()}: ${await response.text()}`);
  }
  const location = response.headers().location;
  if (!location) {
    throw new Error("AuthNode login did not return a callback Location");
  }
  return location;
}

async function openWorkspace(page: Page, label: string) {
  const navigationLabel = label === "语料库" ? "资料库" : label;
  await page.getByRole("button", { name: new RegExp(navigationLabel) }).first().click();
}

function writingFixtureText(markerValue: string, secret: string) {
  return [
    `Writing scope fixture marker: ${secret}.`,
    `Reserve candidate: Solara Meridian Labs for ${markerValue}.`,
    "Recommendation: keep Solara Meridian Labs on the watchlist only, not approved for immediate allocation.",
    "Diligence owner: Mira Chen.",
    "Q3 reserve ceiling: 760000 USD.",
    "Condition before allocation: complete SOC2 evidence review and confirm pilot renewal evidence from Northstar Bank.",
    "Traction evidence: Northstar Bank pilot renewal, Atlas Health expansion intent, and three production workflow references.",
    "Risks: customer concentration, unverified reliability telemetry, and incomplete compliance evidence.",
    "Next diligence: collect reliability logs, validate concentration exposure, and attach the SOC2 evidence memo before any reserve release."
  ].join("\n");
}

async function createWritingSourceFixture(page: Page, title: string, text: string): Promise<{ sourceItemId?: string; knowledgeBaseId: string; knowledgeBaseName?: string }> {
  return page.evaluate(async ({ fixtureTitle, fixtureText, markerValue }) => {
    const api = async (path: string, init?: RequestInit) => {
      const response = await fetch(path, {
        ...init,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...(init?.headers || {})
        }
      });
      if (!response.ok) {
        throw new Error(`${init?.method || "GET"} ${path} failed: ${response.status} ${await response.text()}`);
      }
      return response.json();
    };
    const createdKnowledgeBase = await api("/workspace/knowledge-bases", {
      method: "POST",
      body: JSON.stringify({
        name: `Writing E2E ${markerValue}`,
        description: "Temporary isolated knowledge base for Writing scope e2e coverage.",
        kb_type: "document"
      })
    });
    const knowledgeBase = createdKnowledgeBase.knowledge_base;
    if (!knowledgeBase?.knowledge_base_id) {
      throw new Error("Writing fixture could not find a writable knowledge base");
    }
    const source = await api("/workspace/sources/text", {
      method: "POST",
      body: JSON.stringify({
        title: fixtureTitle,
        text: fixtureText,
        knowledge_base_id: knowledgeBase.knowledge_base_id,
        digest_mode: "manual"
      })
    });
    return {
      sourceItemId: Array.isArray(source.source_item_ids) ? source.source_item_ids[0] : undefined,
      knowledgeBaseId: knowledgeBase.knowledge_base_id,
      knowledgeBaseName: typeof knowledgeBase.name === "string" ? knowledgeBase.name : undefined
    };
  }, { fixtureTitle: title, fixtureText: text, markerValue: marker });
}

async function softDeleteSourceItemIfPossible(page: Page, sourceItemId: string | undefined) {
  if (!sourceItemId) {
    return;
  }
  try {
    await page.evaluate(async (id) => {
      const response = await fetch("/workspace/documents/delete", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          source_item_ids: [id],
          execute: true,
          restore: false,
          hard_delete: false,
          reason: "writing workspace e2e cleanup"
        })
      });
      if (!response.ok) {
        throw new Error(`Cleanup delete failed with HTTP ${response.status}: ${await response.text()}`);
      }
    }, sourceItemId);
  } catch {
    // Keep the primary test failure visible; cleanup is best effort.
  }
}

async function archiveKnowledgeBaseIfPossible(page: Page, knowledgeBaseId: string | undefined) {
  if (!knowledgeBaseId) {
    return;
  }
  try {
    await page.evaluate(async (id) => {
      const response = await fetch(`/workspace/knowledge-bases/${encodeURIComponent(id)}`, {
        method: "DELETE",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({})
      });
      if (!response.ok) {
        throw new Error(`Cleanup knowledge base failed with HTTP ${response.status}: ${await response.text()}`);
      }
    }, knowledgeBaseId);
  } catch {
    // Keep the primary test failure visible; cleanup is best effort.
  }
}

async function createBoard(page: Page, knowledgeBaseId: string, knowledgeBaseName?: string) {
  const startPanel = page.getByTestId("writing-start-panel");
  const toolbar = page.getByTestId("writing-toolbar");
  await expect(page.locator('[data-testid="writing-toolbar"], [data-testid="writing-start-panel"]').first()).toBeVisible({
    timeout: 45_000
  });
  if (await toolbar.isVisible().catch(() => false)) {
    await page.getByTestId("writing-close-board").click();
  }
  await expect(startPanel).toBeVisible({ timeout: 45_000 });
  await page.getByTestId("writing-new-goal").fill(
    `Write an evidence-backed reserve allocation memo for ${marker}.`
  );
  await page.getByTestId("writing-create-board").click();
  await expect(toolbar).toBeVisible({ timeout: 45_000 });
  await page.getByTestId("writing-board-title-input").fill(`Writing fixture memo ${marker}`);
  await page.getByTestId("writing-board-goal-input").fill(
    `Build an inquiry graph before drafting the reserve allocation memo for ${marker}.`
  );
  await page.getByTestId("writing-board-goal-input").press("Tab");
  await expect(page.getByTestId("writing-board-scope")).toBeVisible();
  await bindCurrentWritingBoardToKnowledgeBase(page, knowledgeBaseId, knowledgeBaseName);
  await page.reload();
  await ensureWritingBoardOpen(page);
  await expect(page.getByTestId("writing-board-scope")).not.toHaveText("全部资料库", { timeout: 30_000 });
}

async function bindCurrentWritingBoardToKnowledgeBase(page: Page, knowledgeBaseId: string, knowledgeBaseName?: string) {
  await page.evaluate(async ({ markerValue, targetKnowledgeBaseId, targetKnowledgeBaseName }) => {
    const api = async (path: string, init?: RequestInit) => {
      const response = await fetch(path, {
        ...init,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...(init?.headers || {})
        }
      });
      if (!response.ok) {
        throw new Error(`${init?.method || "GET"} ${path} failed: ${response.status} ${await response.text()}`);
      }
      return response.json();
    };
    const boardsResponse = await api("/workspace/writing/boards?limit=50");
    const board = (boardsResponse.boards || []).find((item: any) => `${item.title || ""} ${item.goal || ""}`.includes(markerValue));
    if (!board?.board_id) {
      throw new Error("E2E writing board was not found while binding knowledge base scope");
    }
    const detail = await api(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}`);
    const metadata = detail.board?.metadata && typeof detail.board.metadata === "object" && !Array.isArray(detail.board.metadata)
      ? detail.board.metadata
      : {};
    const knowledgeBaseScope = {
      mode: "hard",
      knowledge_base_ids: [targetKnowledgeBaseId],
      ...(targetKnowledgeBaseName ? { knowledge_base_name: targetKnowledgeBaseName } : {})
    };
    const updated = await api(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        metadata: {
          ...metadata,
          knowledge_base_ids: [targetKnowledgeBaseId],
          knowledge_base_scope: knowledgeBaseScope
        }
      })
    });
    const updatedIds = Array.isArray(updated.board?.metadata?.knowledge_base_ids)
      ? updated.board.metadata.knowledge_base_ids
      : updated.board?.metadata?.knowledge_base_scope?.knowledge_base_ids;
    if (!Array.isArray(updatedIds) || !updatedIds.includes(targetKnowledgeBaseId)) {
      throw new Error(`E2E writing board scope was not persisted for ${targetKnowledgeBaseId}`);
    }
  }, { markerValue: marker, targetKnowledgeBaseId: knowledgeBaseId, targetKnowledgeBaseName: knowledgeBaseName || "" });
}

async function createQuestion(page: Page, title: string, body: string) {
  const beforeIds = await questionNodeIds(page);
  await page.getByTestId("writing-add-question").click();
  await expect(page.locator('[data-testid="writing-node"][data-node-type="question"]')).toHaveCount(beforeIds.length + 1, {
    timeout: 30_000
  });
  const afterIds = await questionNodeIds(page);
  const nodeId = afterIds.find((id) => !beforeIds.includes(id));
  if (!nodeId) {
    throw new Error("Could not identify newly created question node");
  }
  const node = writingNodeById(page, nodeId);
  await expect(node).toBeVisible();
  await expect(node.getByTestId("writing-node-ask-scope")).toBeVisible();
  await expect(node.getByTestId("writing-node-ask-scope")).not.toHaveText("全部资料库", { timeout: 20_000 });
  await node.getByTestId("writing-node-toggle").click();
  const editor = page.getByTestId("writing-floating-editor");
  await expect(editor).toBeVisible({ timeout: 20_000 });
  await editor.getByTestId("writing-editor-title").fill(title, { timeout: 20_000 });
  await editor.getByTestId("writing-editor-body").fill(body, { timeout: 20_000 });
  await editor.getByTestId("writing-editor-close").click();
  await expect(editor).toBeHidden({ timeout: 20_000 });
  await expect(node).toContainText(title, { timeout: 20_000 });
  await page.waitForTimeout(300);
}

async function runQuestionsInParallel(page: Page, questionCount: number) {
  const questions = page.locator('[data-testid="writing-node"][data-node-type="question"]');
  await expect(questions).toHaveCount(questionCount, { timeout: 30_000 });
  await expect(page.getByTestId("writing-node-ask-scope")).toHaveCount(questionCount, { timeout: 30_000 });
  const ids = await questionNodeIds(page);
  const runResult = await runQuestionNodesViaSession(page, ids.slice(0, questionCount));
  if (runResult.boardKnowledgeBaseIds.length) {
    expect(runResult.scopedQuestionCount).toBe(questionCount);
  }
  await page.reload();
  await ensureWritingBoardOpen(page);
  await expect(page.locator('[data-testid="writing-node"][data-node-type="answer"]')).toHaveCount(questionCount, {
    timeout: 240_000
  });
  await expect(page.getByTestId("writing-node-ask-health").first()).toBeVisible({ timeout: 45_000 });
  expect(await page.getByTestId("writing-node-ask-health").count()).toBeGreaterThanOrEqual(questionCount);
}

async function questionNodeIds(page: Page) {
  return page.locator('[data-testid="writing-node"][data-node-type="question"]').evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute("data-node-id") || "").filter(Boolean)
  );
}

function writingNodeById(page: Page, nodeId: string) {
  return page.locator(`[data-testid="writing-node"][data-node-id="${nodeId}"]`);
}

async function ensureWritingBoardOpen(page: Page) {
  await openWorkspace(page, "写作");
  const toolbar = page.getByTestId("writing-toolbar");
  await expect(page.locator('[data-testid="writing-toolbar"], [data-testid="writing-project-list"]').first()).toBeVisible({
    timeout: 45_000
  });
  if ((await toolbar.isVisible().catch(() => false)) && await currentWritingBoardContainsMarker(page)) {
    return;
  }
  if (await toolbar.isVisible().catch(() => false)) {
    await page.getByTestId("writing-close-board").click();
  }
  await expect(page.getByTestId("writing-project-list")).toBeVisible({ timeout: 45_000 });
  const project = page.getByTestId("writing-project").filter({ hasText: marker }).first();
  await expect(project).toBeVisible({ timeout: 45_000 });
  await project.getByTestId("writing-open-board").click();
  await expect(toolbar).toBeVisible({ timeout: 45_000 });
  await expect(page.getByTestId("writing-board-title-input")).toHaveValue(new RegExp(marker), { timeout: 20_000 });
}

async function currentWritingBoardContainsMarker(page: Page) {
  const title = await page.getByTestId("writing-board-title-input").inputValue({ timeout: 1_000 }).catch(() => "");
  const goal = await page.getByTestId("writing-board-goal-input").inputValue({ timeout: 1_000 }).catch(() => "");
  return `${title} ${goal}`.includes(marker);
}

async function runQuestionNodesViaSession(page: Page, nodeIds: string[]): Promise<{ answerNodeIds: string[]; boardKnowledgeBaseIds: string[]; scopedQuestionCount: number }> {
  return page.evaluate(async ({ markerValue, targetNodeIds }) => {
    const api = async (path: string, init?: RequestInit) => {
      const response = await fetch(path, {
        ...init,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...(init?.headers || {})
        }
      });
      if (!response.ok) {
        throw new Error(`${init?.method || "GET"} ${path} failed: ${response.status} ${await response.text()}`);
      }
      return response.json();
    };
    const boardsResponse = await api("/workspace/writing/boards?limit=50");
    const board = (boardsResponse.boards || []).find((item: any) => `${item.title || ""} ${item.goal || ""}`.includes(markerValue));
    if (!board?.board_id) {
      throw new Error("E2E writing board was not found before running Ask");
    }
    const detail = await api(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}`);
    const isRecord = (value: any) => Boolean(value && typeof value === "object" && !Array.isArray(value));
    const boardDetail = detail.board || board;
    const boardMetadata = isRecord(boardDetail.metadata) ? boardDetail.metadata : {};
    const rawBoardScope = isRecord(boardMetadata.knowledge_base_scope) ? boardMetadata.knowledge_base_scope : {};
    const scopeIds = Array.isArray(rawBoardScope.knowledge_base_ids) ? rawBoardScope.knowledge_base_ids.filter((item: any) => typeof item === "string" && item.length > 0) : [];
    const metadataIds = Array.isArray(boardMetadata.knowledge_base_ids) ? boardMetadata.knowledge_base_ids.filter((item: any) => typeof item === "string" && item.length > 0) : [];
    const boardKnowledgeBaseIds = Array.from(new Set([...scopeIds, ...metadataIds]));
    const boardKnowledgeBaseScope = boardKnowledgeBaseIds.length
      ? { ...rawBoardScope, mode: "hard", knowledge_base_ids: boardKnowledgeBaseIds }
      : Object.keys(rawBoardScope).length
        ? { ...rawBoardScope, knowledge_base_ids: [] }
        : {};
    const nodes = detail.nodes || [];
    const edges = detail.edges || [];
    const nodeById = new Map(nodes.map((node: any) => [node.node_id, node]));
    const targets = targetNodeIds.map((nodeId: string) => nodeById.get(nodeId)).filter(Boolean);
    if (targets.length !== targetNodeIds.length) {
      throw new Error(`Only found ${targets.length} of ${targetNodeIds.length} requested question nodes`);
    }

    const askStream = async (node: any) => {
      const query = [node.title, node.body_markdown].filter(Boolean).join("\n").trim();
      const sessionId = `writing:${board.board_id}:${node.node_id}`;
      const connectedEdges = edges.filter((edge: any) => edge.source_node_id === node.node_id || edge.target_node_id === node.node_id);
      const contextNodes = connectedEdges
        .map((edge: any) => nodeById.get(edge.source_node_id === node.node_id ? edge.target_node_id : edge.source_node_id))
        .filter(Boolean)
        .map((item: any) => ({
          node_id: item.node_id,
          node_type: item.node_type,
          title: item.title,
          body_markdown: item.body_markdown,
          citations: item.citations || [],
          source_refs: item.source_refs || [],
          quality_signals: item.quality_signals || {}
        }));
      const scopedSourceItemIds = Array.from(new Set(contextNodes.flatMap((item: any) =>
        [...(item.citations || []), ...(item.source_refs || [])].map((ref: any) => ref.source_item_id).filter(Boolean)
      )));
      const scope = {
        ...(boardKnowledgeBaseIds.length ? { mode: "hard", knowledge_base_ids: boardKnowledgeBaseIds } : {}),
        board_id: board.board_id,
        node_id: node.node_id,
        session_id: sessionId,
        context_model: "connected_nodes_v1",
        context_rule: "directly connected writing nodes are included as structured context",
        context_edges: connectedEdges,
        context_nodes: contextNodes,
        ...(scopedSourceItemIds.length ? { source_item_ids: scopedSourceItemIds } : {})
      };
      const result: any = {
        answer: "",
        citations: [],
        source_refs: [],
        agent_steps: [],
        progress: [],
        timing: {},
        evidence: {},
        evidence_check: {},
        quality_signals: {},
        trace: {}
      };
      const response = await fetch("/workspace/ask/stream", {
        method: "POST",
        headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
        body: JSON.stringify({ query, intent: "auto", surface: "writing", session_id: sessionId, scope, top_k: 8 })
      });
      if (!response.ok || !response.body) {
        throw new Error(`Ask stream failed for ${node.node_id}: ${response.status} ${await response.text()}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const applyFrame = (frame: string) => {
        const lines = frame.split(/\r?\n/);
        let event = "message";
        const dataLines: string[] = [];
        for (const line of lines) {
          if (line.startsWith("event:")) event = line.slice("event:".length).trim();
          if (line.startsWith("data:")) dataLines.push(line.slice("data:".length).trim());
        }
        if (!dataLines.length) return;
        const data = JSON.parse(dataLines.join("\n"));
        if (event === "route") {
          result.route = data.route || result.route;
          result.timing = { ...(result.timing || {}), ...(data.timing || {}) };
        } else if (event === "agent_step") {
          if (data.step) result.agent_steps.push(data.step);
          result.timing = { ...(result.timing || {}), ...(data.timing || {}) };
        } else if (event === "progress") {
          if (data.progress) result.progress.push(data.progress);
          result.timing = { ...(result.timing || {}), ...(data.timing || {}) };
        } else if (event === "evidence") {
          result.evidence = data.evidence || result.evidence;
          result.citations = Array.isArray(data.citations) ? data.citations : result.citations;
          result.source_refs = result.evidence?.source_refs || result.citations;
          result.quality_signals = data.quality_signals || result.quality_signals;
        } else if (event === "evidence_check") {
          result.evidence_check = data.evidence_check || result.evidence_check;
          result.quality_signals = data.quality_signals || result.quality_signals;
        } else if (event === "answer_delta") {
          result.answer = `${result.answer || ""}${typeof data.delta === "string" ? data.delta : ""}`;
          if (typeof data.time_to_first_answer_ms === "number") {
            result.timing = { ...(result.timing || {}), time_to_first_answer_ms: data.time_to_first_answer_ms };
          }
        } else if (event === "trace") {
          result.trace = data.trace || result.trace;
          result.agentic_service = data.agentic_service || result.agentic_service;
        } else if (event === "done") {
          result.ok = data.ok !== false;
          result.timing = { ...(result.timing || {}), ...(data.timing || {}) };
          result.evidence_check = data.evidence_check || result.evidence_check;
          result.quality_signals = data.quality_signals || result.quality_signals;
        } else if (event === "error") {
          result.ok = false;
          result.error = data.error || "Ask PSKA stream failed";
        }
      };
      while (true) {
        const { value, done } = await reader.read();
        if (value) {
          buffer += decoder.decode(value, { stream: !done });
          let boundary = buffer.indexOf("\n\n");
          while (boundary !== -1) {
            applyFrame(buffer.slice(0, boundary));
            buffer = buffer.slice(boundary + 2);
            boundary = buffer.indexOf("\n\n");
          }
        }
        if (done) {
          buffer += decoder.decode();
          if (buffer.trim()) applyFrame(buffer);
          break;
        }
      }
      if (result.ok === false) {
        throw new Error(result.error || `Ask PSKA failed for ${node.node_id}`);
      }
      const answerText = result.answer || "PSKA did not return visible answer text.";
      const answerNode = await api(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}/nodes`, {
        method: "POST",
        body: JSON.stringify({
          node_type: "answer",
          title: `回答：${(node.title || query).slice(0, 42)}`,
          body_markdown: answerText,
          position: { x: Number(node.position?.x || 0) + 390, y: Number(node.position?.y || 0) },
          status: "complete",
          citations: result.citations || [],
          source_refs: result.source_refs || [],
          quality_signals: result.quality_signals || {},
          metadata: { route: result.route || {}, timing: result.timing || {}, session_id: sessionId, source_question_id: node.node_id, expanded: true, knowledge_base_scope: boardKnowledgeBaseScope }
        })
      });
      await api(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}/edges`, {
        method: "POST",
        body: JSON.stringify({
          source_node_id: node.node_id,
          target_node_id: answerNode.node.node_id,
          edge_type: "answered_by",
          label: "回答"
        })
      });
      const refsById = new Map();
      for (const ref of [...(result.citations || []), ...(result.source_refs || [])]) {
        const key = ref.source_item_id || ref.title || JSON.stringify(ref);
        if (key && !refsById.has(key)) refsById.set(key, ref);
      }
      const refs = Array.from(refsById.values());
      if (refs.length) {
        const evidenceNode = await api(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}/nodes`, {
          method: "POST",
          body: JSON.stringify({
            node_type: "evidence",
            title: `证据 ${refs.length}`,
            body_markdown: refs.slice(0, 5).map((ref: any, index: number) => `${index + 1}. ${ref.title || ref.source_item_id}`).join("\n"),
            position: { x: Number(node.position?.x || 0) + 790, y: Number(node.position?.y || 0) - 80 },
            citations: result.citations || [],
            source_refs: result.source_refs || [],
            metadata: { expanded: false, knowledge_base_scope: boardKnowledgeBaseScope }
          })
        });
        await api(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}/edges`, {
          method: "POST",
          body: JSON.stringify({
            source_node_id: answerNode.node.node_id,
            target_node_id: evidenceNode.node.node_id,
            edge_type: "supported_by",
            label: "证据"
          })
        });
      }
      await api(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}/nodes/${encodeURIComponent(node.node_id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          status: "complete",
          metadata: {
            ...(node.metadata || {}),
            session_id: sessionId,
            knowledge_base_scope: boardKnowledgeBaseScope,
            last_ask: {
              query,
              route: result.route || {},
              scope,
              trace: result.trace || {},
              timing: result.timing || {},
              agent_steps: result.agent_steps || [],
              progress: result.progress || [],
              evidence_check: result.evidence_check || {},
              quality_signals: result.quality_signals || {},
              citations: (result.citations || []).slice(0, 20),
              source_refs: (result.source_refs || []).slice(0, 20),
              source_windows: (result.source_windows || []).slice(0, 20),
              no_answer_reasons: result.no_answer_reasons || result.evidence_check?.no_answer_reasons || result.evidence?.no_answer_reasons || [],
              saved_at: new Date().toISOString(),
              session_id: sessionId
            }
          }
        })
      });
      return answerNode.node.node_id;
    };

    const answerNodeIds = await Promise.all(targets.map((node: any) => askStream(node)));
    return {
      answerNodeIds,
      boardKnowledgeBaseIds,
      scopedQuestionCount: boardKnowledgeBaseIds.length ? answerNodeIds.length : 0
    };
  }, { markerValue: marker, targetNodeIds: nodeIds });
}

async function createConnectedFollowupNode(page: Page) {
  return page.evaluate(async ({ markerValue }) => {
    const boardsResponse = await fetch("/workspace/writing/boards?limit=50", { headers: { Accept: "application/json" } });
    const boards = await boardsResponse.json();
    const board = (boards.boards || []).find((item: any) => `${item.title || ""} ${item.goal || ""}`.includes(markerValue));
    if (!board?.board_id) {
      throw new Error("E2E writing board was not found");
    }
    const detailResponse = await fetch(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}`, {
      headers: { Accept: "application/json" }
    });
    const detail = await detailResponse.json();
    const isRecord = (value: any) => Boolean(value && typeof value === "object" && !Array.isArray(value));
    const boardMetadata = isRecord((detail.board || board).metadata) ? (detail.board || board).metadata : {};
    const rawBoardScope = isRecord(boardMetadata.knowledge_base_scope) ? boardMetadata.knowledge_base_scope : {};
    const scopeIds = Array.isArray(rawBoardScope.knowledge_base_ids) ? rawBoardScope.knowledge_base_ids.filter((item: any) => typeof item === "string" && item.length > 0) : [];
    const metadataIds = Array.isArray(boardMetadata.knowledge_base_ids) ? boardMetadata.knowledge_base_ids.filter((item: any) => typeof item === "string" && item.length > 0) : [];
    const boardKnowledgeBaseIds = Array.from(new Set([...scopeIds, ...metadataIds]));
    const boardKnowledgeBaseScope = boardKnowledgeBaseIds.length
      ? { ...rawBoardScope, mode: "hard", knowledge_base_ids: boardKnowledgeBaseIds }
      : Object.keys(rawBoardScope).length
        ? { ...rawBoardScope, knowledge_base_ids: [] }
        : {};
    const answers = (detail.nodes || []).filter((node: any) => node.node_type === "answer");
    if (answers.length < 2) {
      throw new Error("Need at least two answers before creating connected follow-up");
    }
    const nodeResponse = await fetch(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}/nodes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        node_type: "question",
        title: "What final recommendation follows from the connected answers?",
        body_markdown: "Use the directly connected answer nodes as context. State conditions, gaps, and next diligence steps.",
        position: { x: 860, y: 640 },
        metadata: { expanded: true, e2e_marker: markerValue, knowledge_base_scope: boardKnowledgeBaseScope }
      })
    });
    const created = await nodeResponse.json();
    if (!created.node?.node_id) {
      throw new Error("Follow-up node was not created");
    }
    for (const answer of answers.slice(0, 3)) {
      await fetch(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}/edges`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_node_id: answer.node_id,
          target_node_id: created.node.node_id,
          edge_type: "raises",
          label: "context"
        })
      });
    }
    return created.node.node_id as string;
  }, { markerValue: marker });
}

async function addAnswersToSection(page: Page, limit: number) {
  await page.evaluate(async ({ markerValue, maxAnswers }) => {
    const boardsResponse = await fetch("/workspace/writing/boards?limit=50", { headers: { Accept: "application/json" } });
    const boards = await boardsResponse.json();
    const board = (boards.boards || []).find((item: any) => `${item.title || ""} ${item.goal || ""}`.includes(markerValue));
    if (!board?.board_id) {
      throw new Error("E2E writing board was not found while adding answers to section");
    }
    const detailResponse = await fetch(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}`, {
      headers: { Accept: "application/json" }
    });
    const detail = await detailResponse.json();
    const section = (detail.nodes || []).find((node: any) => node.node_type === "section");
    const answers = (detail.nodes || []).filter((node: any) => node.node_type === "answer").slice(0, maxAnswers);
    if (!section?.node_id || !answers.length) {
      throw new Error("Need a section and at least one answer before composing");
    }
    const existing = new Set(
      (detail.edges || [])
        .filter((edge: any) => edge.edge_type === "included_in" && edge.target_node_id === section.node_id)
        .map((edge: any) => edge.source_node_id)
    );
    await Promise.all(answers.filter((answer: any) => !existing.has(answer.node_id)).map((answer: any) =>
      fetch(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}/edges`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_node_id: answer.node_id,
          target_node_id: section.node_id,
          edge_type: "included_in",
          label: "纳入章节"
        })
      })
    ));
  }, { markerValue: marker, maxAnswers: limit });
  await page.reload();
  await ensureWritingBoardOpen(page);
  await expect(page.getByTestId("writing-composer-answer").first()).toBeVisible({ timeout: 20_000 });
}

async function collectBoardResult(page: Page) {
  return page.evaluate(async ({ markerValue }) => {
    const boardsResponse = await fetch("/workspace/writing/boards?limit=50", { headers: { Accept: "application/json" } });
    const boards = await boardsResponse.json();
    const board = (boards.boards || []).find((item: any) => `${item.title || ""} ${item.goal || ""}`.includes(markerValue));
    if (!board?.board_id) {
      throw new Error("E2E writing board was not found while collecting result");
    }
    const detailResponse = await fetch(`/workspace/writing/boards/${encodeURIComponent(board.board_id)}`, {
      headers: { Accept: "application/json" }
    });
    const detail = await detailResponse.json();
    const isRecord = (value: any) => Boolean(value && typeof value === "object" && !Array.isArray(value));
    const boardDetail = detail.board || board;
    const boardMetadata = isRecord(boardDetail.metadata) ? boardDetail.metadata : {};
    const rawBoardScope = isRecord(boardMetadata.knowledge_base_scope) ? boardMetadata.knowledge_base_scope : {};
    const scopeIds = Array.isArray(rawBoardScope.knowledge_base_ids) ? rawBoardScope.knowledge_base_ids.filter((item: any) => typeof item === "string" && item.length > 0) : [];
    const metadataIds = Array.isArray(boardMetadata.knowledge_base_ids) ? boardMetadata.knowledge_base_ids.filter((item: any) => typeof item === "string" && item.length > 0) : [];
    const boardKnowledgeBaseIds = Array.from(new Set([...scopeIds, ...metadataIds]));
    const nodes = detail.nodes || [];
    const answerNodes = nodes.filter((node: any) => node.node_type === "answer");
    const draftNodes = nodes.filter((node: any) => node.node_type === "draft");
    const questionNodes = nodes.filter((node: any) => node.node_type === "question");
    const draftText = draftNodes.map((node: any) => node.body_markdown || "").join("\n\n");
    const combinedText = nodes.map((node: any) => `${node.title || ""}\n${node.body_markdown || ""}`).join("\n\n");
    const citationCount = nodes.reduce((total: number, node: any) => {
      return total + (node.citations || []).length + (node.source_refs || []).length;
    }, 0);
    const timelineCount = questionNodes.filter((node: any) => node.metadata?.last_ask?.agent_steps?.length).length;
    const healthSignalCount = nodes.filter((node: any) =>
      Object.keys(node.quality_signals || {}).length > 0 || Object.keys(node.metadata?.last_ask?.quality_signals || {}).length > 0
    ).length;
    const writingAskScopedCount = questionNodes.filter((node: any) => {
      const ids = Array.isArray(node.metadata?.last_ask?.scope?.knowledge_base_ids) ? node.metadata.last_ask.scope.knowledge_base_ids : [];
      return boardKnowledgeBaseIds.length > 0 && boardKnowledgeBaseIds.every((knowledgeBaseId: string) => ids.includes(knowledgeBaseId));
    }).length;
    return {
      board_id: board.board_id,
      node_count: nodes.length,
      answer_count: answerNodes.length,
      draft_count: draftNodes.length,
      draft_length: draftText.length,
      draft_text: draftText,
      combined_text: combinedText,
      citation_count: citationCount,
      timeline_count: timelineCount,
      health_signal_count: healthSignalCount,
      board_kb_count: boardKnowledgeBaseIds.length,
      writing_ask_scoped_count: writingAskScopedCount
    };
  }, { markerValue: marker });
}
