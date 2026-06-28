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
const sourceTitle = process.env.PSKA_E2E_SOURCE_TITLE || "helio-company-brief.md";
const reportPath = process.env.PSKA_E2E_REPORT_PATH || "";

test.setTimeout(900_000);

test("workspace files to corpus to parallel writing draft", async ({ page, request }) => {
  const callbackUrl = await authnodeCallbackUrl(request);
  await page.goto(callbackUrl);
  await expect(page.getByTestId("gateway-session")).toContainText(userId);
  await expect(page.getByTestId("gateway-session")).toContainText(tenantId);

  await openWorkspace(page, "语料库");
  await expect(page.getByTestId("corpus-source-list")).toContainText(sourceTitle, { timeout: 45_000 });

  await openWorkspace(page, "写作");
  await createBoard(page);

  const questions = [
    {
      title: "What is Helio Forge Systems and why is it being considered?",
      body: "Summarize the company, stage, product, and reserve decision context using only PSKA evidence."
    },
    {
      title: "Which Q3 reserve allocation criteria does Helio satisfy or miss?",
      body: "Compare Helio against the policy dimensions and keep support evidence separate from gaps."
    },
    {
      title: "What customer traction evidence supports shortlist inclusion?",
      body: "Look for pilots, expansions, concentration risk, and commercial proof."
    },
    {
      title: "What financial and technical risks argue against inclusion?",
      body: "Find the strongest counter-evidence across unit economics, runway, reliability, and sales motion."
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
  expect(result.draft_text).not.toMatch(/FastReAct|MCP|tool_call|GraphRAG/i);

  if (reportPath) {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, JSON.stringify({ ok: true, ...result }, null, 2), "utf-8");
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
  await page.getByRole("button", { name: new RegExp(label) }).first().click();
}

async function createBoard(page: Page) {
  const startPanel = page.getByTestId("writing-start-panel");
  const toolbar = page.getByTestId("writing-toolbar");
  if (!(await toolbar.isVisible().catch(() => false))) {
    await expect(startPanel).toBeVisible({ timeout: 45_000 });
    await page.getByTestId("writing-new-goal").fill(
      `Write an evidence-backed reserve allocation memo for Helio Forge Systems. ${marker}`
    );
    await page.getByTestId("writing-create-board").click();
  }
  await expect(toolbar).toBeVisible({ timeout: 45_000 });
  await page.getByTestId("writing-board-title-input").fill(`Helio Forge Systems memo ${marker}`);
  await page.getByTestId("writing-board-goal-input").fill(
    `Build an inquiry graph before drafting the reserve allocation memo. ${marker}`
  );
  await page.getByTestId("writing-board-goal-input").press("Tab");
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
  await node.getByTestId("writing-node-title-input").fill(title);
  await node.getByTestId("writing-node-body-input").fill(body);
  await node.getByTestId("writing-node-body-input").press("Tab");
  await page.waitForTimeout(300);
}

async function runQuestionsInParallel(page: Page, questionCount: number) {
  const questions = page.locator('[data-testid="writing-node"][data-node-type="question"]');
  await expect(questions).toHaveCount(questionCount, { timeout: 30_000 });
  const ids = await questionNodeIds(page);
  await runQuestionNodesViaSession(page, ids.slice(0, questionCount));
  await page.reload();
  await ensureWritingBoardOpen(page);
  await expect(page.locator('[data-testid="writing-node"][data-node-type="answer"]')).toHaveCount(questionCount, {
    timeout: 240_000
  });
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
  if (await toolbar.isVisible().catch(() => false)) {
    return;
  }
  await expect(page.getByTestId("writing-project-list")).toBeVisible({ timeout: 45_000 });
  const project = page.getByTestId("writing-project").filter({ hasText: marker }).first();
  await expect(project).toBeVisible({ timeout: 45_000 });
  await project.getByTestId("writing-open-board").click();
  await expect(toolbar).toBeVisible({ timeout: 45_000 });
}

async function runQuestionNodesViaSession(page: Page, nodeIds: string[]) {
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
      const scope = {
        board_id: board.board_id,
        node_id: node.node_id,
        session_id: sessionId,
        context_model: "connected_nodes_v1",
        context_rule: "directly connected writing nodes are included as structured context",
        context_edges: connectedEdges,
        context_nodes: contextNodes,
        source_item_ids: Array.from(new Set(contextNodes.flatMap((item: any) =>
          [...(item.citations || []), ...(item.source_refs || [])].map((ref: any) => ref.source_item_id).filter(Boolean)
        )))
      };
      const result: any = { answer: "", citations: [], source_refs: [], agent_steps: [], timing: {}, evidence: {}, trace: {} };
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
        } else if (event === "evidence") {
          result.evidence = data.evidence || result.evidence;
          result.citations = Array.isArray(data.citations) ? data.citations : result.citations;
          result.source_refs = result.evidence?.source_refs || result.citations;
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
          metadata: { route: result.route || {}, timing: result.timing || {}, session_id: sessionId, source_question_id: node.node_id, expanded: true }
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
            metadata: { expanded: false }
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
            last_ask: {
              query,
              route: result.route || {},
              scope,
              trace: result.trace || {},
              timing: result.timing || {},
              agent_steps: result.agent_steps || [],
              saved_at: new Date().toISOString(),
              session_id: sessionId
            }
          }
        })
      });
      return answerNode.node.node_id;
    };

    const answerNodeIds = await Promise.all(targets.map((node: any) => askStream(node)));
    return { answerNodeIds };
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
        metadata: { expanded: true, e2e_marker: markerValue }
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
    const nodes = detail.nodes || [];
    const answerNodes = nodes.filter((node: any) => node.node_type === "answer");
    const draftNodes = nodes.filter((node: any) => node.node_type === "draft");
    const questionNodes = nodes.filter((node: any) => node.node_type === "question");
    const draftText = draftNodes.map((node: any) => node.body_markdown || "").join("\n\n");
    const citationCount = nodes.reduce((total: number, node: any) => {
      return total + (node.citations || []).length + (node.source_refs || []).length;
    }, 0);
    const timelineCount = questionNodes.filter((node: any) => node.metadata?.last_ask?.agent_steps?.length).length;
    return {
      board_id: board.board_id,
      node_count: nodes.length,
      answer_count: answerNodes.length,
      draft_count: draftNodes.length,
      draft_length: draftText.length,
      draft_text: draftText,
      citation_count: citationCount,
      timeline_count: timelineCount
    };
  }, { markerValue: marker });
}
