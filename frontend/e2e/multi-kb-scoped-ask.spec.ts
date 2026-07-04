import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";

const frontendUrl = (process.env.PSKA_E2E_FRONTEND_URL || "http://127.0.0.1:5173").replace(/\/$/, "");
const authnodeUrl = (process.env.PSKA_E2E_AUTHNODE_URL || "http://127.0.0.1:8788").replace(/\/$/, "");
const tenantId = process.env.PSKA_E2E_TENANT_ID || "tenant_graphintell";
const userId = process.env.PSKA_E2E_USER_ID || "test_user";

type KnowledgeBase = {
  knowledge_base_id: string;
  name: string;
};

type TextSourceResponse = {
  knowledge_source?: { knowledge_source_id?: string };
  source_item_ids?: string[];
};

type ReviewHealthFixture = {
  topic: string;
  reviewItemId: string;
  sourceItemIds: string[];
};

test.setTimeout(300_000);

function resolvePassword() {
  if (process.env.PSKA_E2E_PASSWORD) {
    return process.env.PSKA_E2E_PASSWORD;
  }
  if (userId === "user_primary") {
    return "primary-local";
  }
  if (userId === "alice") {
    return "alice-local";
  }
  if (userId === "pska-local") {
    return "pska-local";
  }
  throw new Error(`Set PSKA_E2E_PASSWORD to run multi-KB scoped Ask e2e as ${tenantId}/${userId}`);
}

test("browser session scoped Ask does not leak citations across knowledge bases", async ({ page, request }) => {
  const marker = `PSKA_MULTI_KB_SCOPE_${Date.now()}_${randomUUID().slice(0, 8)}`;
  const alphaSecret = `ALPHA_ONLY_${marker}`;
  const betaSecret = `BETA_ONLY_${marker}`;
  const createdKnowledgeBaseIds: string[] = [];
  const createdSourceItemIds: string[] = [];
  let reviewFixture: ReviewHealthFixture | null = null;
  const bulkReviewFixtures: ReviewHealthFixture[] = [];

  await page.goto(await authnodeCallbackUrl(request));
  await expect(page.getByTestId("gateway-session")).toContainText(userId);
  await expect(page.getByTestId("gateway-session")).toContainText(tenantId);

  try {
    const alpha = await createKnowledgeBase(page, `Scoped Ask Alpha ${marker}`);
    const beta = await createKnowledgeBase(page, `Scoped Ask Beta ${marker}`);
    createdKnowledgeBaseIds.push(alpha.knowledge_base_id, beta.knowledge_base_id);

    const alphaSource = await createTextSource(page, alpha.knowledge_base_id, {
      title: `Scoped Ask Alpha source ${marker}`,
      text: `The scoped Ask answer for marker ${marker} is ${alphaSecret}. This source belongs only to the Alpha knowledge base.`
    });
    const betaSource = await createTextSource(page, beta.knowledge_base_id, {
      title: `Scoped Ask Beta source ${marker}`,
      text: `The scoped Ask answer for marker ${marker} is ${betaSecret}. This source belongs only to the Beta knowledge base.`
    });
    const alphaSourceItemId = requireOne(alphaSource.source_item_ids, "alpha source_item_id");
    const betaSourceItemId = requireOne(betaSource.source_item_ids, "beta source_item_id");
    createdSourceItemIds.push(alphaSourceItemId, betaSourceItemId);

    reviewFixture = await createReviewHealthFixture(page, alpha.knowledge_base_id, marker, createdSourceItemIds);

    await page.goto(frontendUrl, { waitUntil: "domcontentloaded" });
    await openWorkspace(page, "Today");
    await selectCurrentKnowledgeBase(page, alpha.knowledge_base_id);
    await expectKnowledgeBaseScopeMenuSearch(page, alpha, beta);
    await expectTodayReviewHealth(page, reviewFixture);
    await expectKnowledgeBaseReadinessPanel(page, alpha.name);
    await expectKnowledgeBaseDetailTabs(page);
    await openWorkspace(page, "Today");

    const snoozeReviewFixture = await createReviewHealthFixture(page, alpha.knowledge_base_id, marker, createdSourceItemIds);
    bulkReviewFixtures.push(
      await createReviewHealthFixture(page, alpha.knowledge_base_id, marker, createdSourceItemIds),
      await createReviewHealthFixture(page, alpha.knowledge_base_id, marker, createdSourceItemIds)
    );

    const query = `What is the scoped Ask answer for marker ${marker}?`;
    const result = await askViaBrowserSession(page, query, alpha.knowledge_base_id);
    const routeScope = result?.route?.scope_applied || result?.scope_applied || {};
    expect(routeScope.knowledge_base_ids).toEqual([alpha.knowledge_base_id]);
    expect(routeScope.knowledge_base_readiness).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          knowledge_base_id: alpha.knowledge_base_id,
          retrieval_ready: true
        })
      ])
    );
    expect(routeScope.knowledge_base_readiness_warnings).toEqual([]);

    const evidenceJson = JSON.stringify({
      citations: result?.citations || [],
      source_refs: result?.source_refs || [],
      source_windows: result?.source_windows || [],
      evidence: result?.evidence || {},
      route: result?.route || {},
      retrieval: result?.retrieval || {}
    });
    expect(evidenceJson).toContain(alphaSourceItemId);
    expect(evidenceJson).toContain(alpha.knowledge_base_id);
    expect(evidenceJson).toContain(alphaSecret);
    expect(evidenceJson).not.toContain(betaSourceItemId);
    expect(evidenceJson).not.toContain(beta.knowledge_base_id);
    expect(evidenceJson).not.toContain(betaSecret);

    await askViaBrowserComposer(page, query, {
      marker,
      alphaSecret,
      betaSecret,
      alphaSourceItemId
    });
    await askViaGraphWorkspace(page, query, {
      alphaSecret,
      betaSecret
    });
    await expectReviewSnoozeRestore(page, snoozeReviewFixture);
    await expectReviewBulkReject(page, bulkReviewFixtures);
    await expectReviewCenterHealth(page, reviewFixture);
  } finally {
    await cleanupFixtures(page, createdSourceItemIds, createdKnowledgeBaseIds);
    cleanupDatabaseResidue(marker, reviewFixture?.topic);
  }
});

async function authnodeCallbackUrl(request: APIRequestContext) {
  const response = await request.post(`${authnodeUrl}/login?local=1`, {
    form: {
      username: userId,
      tenant_id: tenantId,
      password: resolvePassword(),
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

async function api<T>(page: Page, path: string, init?: RequestInit): Promise<T> {
  return page.evaluate(
    async ({ requestPath, requestInit }) => {
      const response = await fetch(requestPath, {
        ...requestInit,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...(requestInit?.headers || {})
        }
      });
      if (!response.ok) {
        throw new Error(`${requestInit?.method || "GET"} ${requestPath} failed: ${response.status} ${await response.text()}`);
      }
      return response.json();
    },
    { requestPath: path, requestInit: init || {} }
  );
}

async function createKnowledgeBase(page: Page, name: string): Promise<KnowledgeBase> {
  const payload = await api<{ knowledge_base: KnowledgeBase }>(page, "/workspace/knowledge-bases", {
    method: "POST",
    body: JSON.stringify({ name, description: "temporary scoped Ask e2e fixture" })
  });
  return payload.knowledge_base;
}

async function createTextSource(
  page: Page,
  knowledgeBaseId: string,
  payload: { title: string; text: string }
): Promise<TextSourceResponse> {
  return api<TextSourceResponse>(page, "/workspace/sources/text", {
    method: "POST",
    body: JSON.stringify({
      ...payload,
      knowledge_base_id: knowledgeBaseId,
      digest_mode: "manual"
    })
  });
}

async function createReviewHealthFixture(
  page: Page,
  knowledgeBaseId: string,
  marker: string,
  createdSourceItemIds: string[]
): Promise<ReviewHealthFixture> {
  const topic = `rhtopic${randomUUID().replace(/-/g, "").slice(0, 8).toLowerCase()}`;
  const first = await createTextSource(page, knowledgeBaseId, {
    title: `${topic} ${marker}`,
    text: `Alpha linking evidence for ${marker}. The review health topic is ${topic}.`
  });
  const second = await createTextSource(page, knowledgeBaseId, {
    title: `${topic} ${marker}`,
    text: `Beta linking evidence for ${marker}. The review health topic is ${topic}.`
  });
  const sourceItemIds = [
    requireOne(first.source_item_ids, "first review fixture source_item_id"),
    requireOne(second.source_item_ids, "second review fixture source_item_id")
  ];
  createdSourceItemIds.push(...sourceItemIds);
  const linking = await api<{
    relationship_candidate_count?: number;
    review_items?: Array<{ review_item_id?: string; title?: string; proposal?: Record<string, unknown> }>;
  }>(page, "/workspace/digest/linking/run", {
    method: "POST",
    body: JSON.stringify({ source_item_ids: sourceItemIds, max_topics_per_source: 3 })
  });
  expect(linking.relationship_candidate_count || 0).toBeGreaterThan(0);
  const review = (linking.review_items || []).find((item) => JSON.stringify(item).includes(topic));
  if (!review?.review_item_id) {
    throw new Error(`Linking run did not create a review item for ${topic}: ${JSON.stringify(linking)}`);
  }
  return { topic, reviewItemId: review.review_item_id, sourceItemIds };
}

async function expectTodayReviewHealth(page: Page, fixture: ReviewHealthFixture) {
  const reviewCard = page.locator(".today-card.review-card").filter({ hasText: fixture.topic }).first();
  await expect(reviewCard).toBeVisible({ timeout: 45_000 });
  await expect(reviewCard.getByTestId("today-review-evidence-health")).toBeVisible();
  await expect(reviewCard.getByTestId("today-review-evidence-health")).toContainText("需复核");
}

async function expectKnowledgeBaseReadinessPanel(page: Page, knowledgeBaseName: string) {
  await openWorkspace(page, "资料库");
  const panel = page.getByTestId("knowledge-base-readiness-panel");
  await expect(panel).toBeVisible({ timeout: 45_000 });
  await expect(panel).toContainText(knowledgeBaseName);
  await expect(panel.getByTestId("knowledge-base-readiness-status")).toBeVisible();
  await expect(panel).toContainText("资料条目");
  await expect(panel).toContainText("向量覆盖");
  await expect(panel).toContainText("索引");
  await expect(panel).toContainText("最近同步");
  await expect(panel.getByTestId("knowledge-base-readiness-reason")).toContainText(/检索|处理|索引|资料/);
}

async function expectKnowledgeBaseDetailTabs(page: Page) {
  await openWorkspace(page, "资料库");
  const tabs = page.getByTestId("knowledge-base-detail-tabs");
  await expect(tabs).toContainText("资料");
  await expect(tabs).toContainText("Ask");
  await expect(tabs).toContainText("处理");
  await expect(tabs).toContainText("Digest");
  await expect(tabs).toContainText("Graph");
  await expect(tabs).toContainText("Writing");
  await expect(tabs).toContainText("设置");
  await expect(page.getByTestId("corpus-search-input")).toBeVisible();

  await page.getByTestId("knowledge-base-tab-ask").click();
  await expect(page.getByTestId("knowledge-base-search-input")).toBeVisible();
  await expect(page.locator(".kb-search-panel")).toContainText("证据搜索");

  await page.getByTestId("knowledge-base-tab-processing").click();
  await expect(page.locator(".chunk-preview-surface")).toBeVisible();
  await expect(page.locator(".corpus-advanced-details")).toBeVisible();

  await page.getByTestId("knowledge-base-tab-digest").click();
  await expect(page.locator(".digest-log-panel")).toBeVisible();

  await page.getByTestId("knowledge-base-tab-graph").click();
  await expect(page.getByTestId("knowledge-base-graph-panel")).toBeVisible();
  await expect(page.getByTestId("knowledge-base-open-graph")).toBeVisible();

  await page.getByTestId("knowledge-base-tab-writing").click();
  await expect(page.getByTestId("knowledge-base-writing-panel")).toBeVisible();
  await expect(page.getByTestId("knowledge-base-open-writing")).toBeVisible();

  await page.getByTestId("knowledge-base-tab-settings").click();
  await expect(page.locator(".kb-manage-strip")).toBeVisible();
  await expect(page.locator(".prompt-profile-panel")).toBeVisible();

  await page.getByTestId("knowledge-base-tab-sources").click();
  await expect(page.getByTestId("corpus-search-input")).toBeVisible();
}

async function expectReviewCenterHealth(page: Page, fixture: ReviewHealthFixture) {
  await openWorkspace(page, "Review");
  await page.getByTestId("review-filter-pending").click();
  const analytics = page.getByTestId("review-analytics");
  await expect(analytics).toBeVisible({ timeout: 45_000 });
  await expect(analytics).toContainText("队列态势");
  await expect(analytics).toContainText("待审");
  await expect(analytics).toContainText("缺证据");
  await expect(analytics.getByTestId("review-analytics-type").first()).toBeVisible();
  const reviewCard = page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId }).first();
  await expect(reviewCard).toBeVisible({ timeout: 45_000 });
  await expect(reviewCard).toContainText(fixture.topic);
  await expect(reviewCard.getByTestId("review-evidence-health")).toBeVisible();
  await expect(reviewCard.getByTestId("review-evidence-health")).toContainText("可审核");
  const remediation = reviewCard.getByTestId("review-remediation");
  await expect(remediation).toBeVisible();
  await expect(remediation).toContainText("处置建议");
  await expect(remediation).toContainText("批准并应用");
  await expectReviewEvidenceComparison(reviewCard, fixture);
  await expect(reviewCard.getByTestId("review-action-approve-apply")).toBeVisible();
  await reviewCard.getByTestId("review-action-approve-apply").click();
  await expect(page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId })).toHaveCount(0, { timeout: 45_000 });

  await page.getByTestId("review-filter-applied").click();
  const appliedCard = page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId }).first();
  await expect(appliedCard).toBeVisible({ timeout: 45_000 });
  await expect(appliedCard).toContainText(fixture.topic);
  await expect(appliedCard).toContainText(/Created graph relationship|已批准并应用|已应用|applied/);
  const decisionHistory = appliedCard.getByTestId("review-decision-history");
  await expect(decisionHistory).toBeVisible();
  await expect(decisionHistory).toContainText("决策记录");
  await expect(decisionHistory).toContainText("批准");
  await expect(decisionHistory).toContainText("写入长期知识");
  const lineage = appliedCard.getByTestId("review-application-lineage");
  await expect(lineage).toBeVisible();
  await expect(lineage).toContainText("应用 lineage");
  await expect(lineage).toContainText("写入目标");
  await expect(lineage).toContainText("Graph relationship");
  await expect(lineage).toContainText("hyperedge:");
  await expect(lineage).toContainText("证据");
  const openGraph = appliedCard.getByTestId("review-action-open-graph");
  await expect(openGraph).toBeVisible();
  await openGraph.click();
  const graphInspector = page.locator(".graph-inspector").filter({ hasText: "shared_topic" }).first();
  await expect(graphInspector).toBeVisible({ timeout: 60_000 });
  await expect(graphInspector).toContainText(fixture.topic);
  await expect(graphInspector.getByTestId("graph-citation-inspector")).toBeVisible();
}

async function expectReviewEvidenceComparison(reviewCard: Locator, fixture: ReviewHealthFixture) {
  const comparison = reviewCard.getByTestId("review-evidence-comparison");
  await expect(comparison).toBeVisible();
  await expect(comparison).toContainText("证据对比");
  await expect(comparison).toContainText("条引用");
  await comparison.locator("summary").click();

  const inspector = comparison.getByTestId("review-citation-inspector");
  await expect(inspector).toBeVisible();
  const fixtureRef = inspector.getByTestId("citation-inspector-ref").filter({ hasText: fixture.sourceItemIds[0] }).first();
  await expect(fixtureRef).toBeVisible({ timeout: 45_000 });
  await fixtureRef.click();
  await expect(inspector.getByTestId("ask-evidence-inspector")).toContainText(fixture.sourceItemIds[0]);
  await inspector.getByTestId("open-reader-pane").click();
  await expect(inspector.getByTestId("reader-pane")).toContainText(fixture.topic);
}

async function expectReviewSnoozeRestore(page: Page, fixture: ReviewHealthFixture) {
  await openWorkspace(page, "Review");
  await page.getByTestId("review-filter-pending").click();
  const reviewCard = page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId }).first();
  await expect(reviewCard).toBeVisible({ timeout: 45_000 });
  await expect(reviewCard.getByTestId("review-action-snooze")).toBeVisible();
  await reviewCard.getByTestId("review-action-snooze").click();
  await expect(page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId })).toHaveCount(0, { timeout: 45_000 });

  await page.getByTestId("review-filter-snoozed").click();
  const snoozedCard = page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId }).first();
  await expect(snoozedCard).toBeVisible({ timeout: 45_000 });
  await expect(snoozedCard).toContainText(fixture.topic);
  await expect(snoozedCard).toContainText("稍后");
  const snoozedHistory = snoozedCard.getByTestId("review-decision-history");
  await expect(snoozedHistory).toBeVisible();
  await expect(snoozedHistory).toContainText("决策记录");
  await expect(snoozedHistory).toContainText("稍后");
  await expect(snoozedCard.getByTestId("review-action-restore")).toBeVisible();
  await snoozedCard.getByTestId("review-action-restore").click();
  await expect(page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId })).toHaveCount(0, { timeout: 45_000 });

  await page.getByTestId("review-filter-pending").click();
  const restoredCard = page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId }).first();
  await expect(restoredCard).toBeVisible({ timeout: 45_000 });
  const restoredHistory = restoredCard.getByTestId("review-decision-history");
  await expect(restoredHistory).toBeVisible();
  await expect(restoredHistory).toContainText("稍后");
  await expect(restoredHistory).toContainText("恢复待审");
  await expect(restoredCard.getByTestId("review-action-approve")).toBeVisible();
}

async function expectReviewBulkReject(page: Page, fixtures: ReviewHealthFixture[]) {
  await openWorkspace(page, "Review");
  await page.getByTestId("review-filter-pending").click();
  for (const fixture of fixtures) {
    const reviewCard = page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId }).first();
    await expect(reviewCard).toBeVisible({ timeout: 45_000 });
    await reviewCard.getByTestId("review-select-item").check();
  }
  await expect(page.getByTestId("review-bulk-selection")).toContainText(`${fixtures.length} 已选择`);
  const comparison = page.getByTestId("review-selection-comparison");
  await expect(comparison).toBeVisible();
  await expect(comparison.getByTestId("review-comparison-count")).toContainText(`${fixtures.length} 个候选`);
  await expect(comparison.getByTestId("review-comparison-card")).toHaveCount(fixtures.length);
  for (const fixture of fixtures) {
    await expect(comparison).toContainText(fixture.topic);
  }
  await expect(page.getByTestId("review-bulk-reject")).toBeEnabled();
  await page.getByTestId("review-bulk-reject").click();
  await expect(page.getByTestId("review-bulk-message")).toContainText(`已处理 ${fixtures.length}/${fixtures.length} 条`, { timeout: 60_000 });
  for (const fixture of fixtures) {
    await expect(page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId })).toHaveCount(0, { timeout: 45_000 });
  }
  await page.getByTestId("review-filter-rejected").click();
  for (const fixture of fixtures) {
    const rejectedCard = page.locator(".review-center-item").filter({ hasText: fixture.reviewItemId }).first();
    await expect(rejectedCard).toBeVisible({ timeout: 45_000 });
    await expect(rejectedCard).toContainText(fixture.topic);
  }
}

async function openWorkspace(page: Page, label: string) {
  await page.getByRole("button", { name: new RegExp(label) }).first().click();
}

async function selectCurrentKnowledgeBase(page: Page, knowledgeBaseId: string) {
  const selector = page.locator(".kb-scope-chip select");
  await expect(selector).toBeVisible({ timeout: 45_000 });
  await expect(selector.locator(`option[value="${knowledgeBaseId}"]`)).toHaveCount(1, { timeout: 45_000 });
  await selector.selectOption(knowledgeBaseId);
  await expect(selector).toHaveValue(knowledgeBaseId);
}

async function expectKnowledgeBaseScopeMenuSearch(page: Page, alpha: KnowledgeBase, beta: KnowledgeBase) {
  const menu = page.locator(".kb-scope-menu");
  await menu.locator("summary").click();
  const panel = page.locator(".kb-scope-menu-panel");
  await expect(panel).toBeVisible();
  await panel.getByTestId("kb-scope-search").fill(alpha.name);
  await expect(panel.getByTestId("kb-scope-filter-summary")).toContainText("1 个匹配");
  const options = panel.getByTestId("kb-scope-option");
  await expect(options).toHaveCount(1);
  await expect(options.first()).toContainText(alpha.name);
  await expect(options.first()).toContainText(/可检索|待处理/);
  await expect(options.first()).toContainText(/资料/);
  await expect(panel).not.toContainText(beta.name);
  await panel.getByTestId("kb-scope-search").fill("");
  await expect(panel.getByTestId("kb-scope-option").filter({ hasText: beta.name })).toBeVisible();
  await menu.locator("summary").click();
}

async function askViaBrowserSession(page: Page, query: string, knowledgeBaseId: string): Promise<any> {
  return api(page, "/workspace/ask", {
    method: "POST",
    body: JSON.stringify({
      query,
      intent: "quick",
      surface: "e2e-multi-kb-scoped-ask",
      skip_intent_classifier: true,
      top_k: 8,
      scope: {
        mode: "hard",
        knowledge_base_ids: [knowledgeBaseId]
      }
    })
  });
}

async function askViaBrowserComposer(
  page: Page,
  query: string,
  expected: { marker: string; alphaSecret: string; betaSecret: string; alphaSourceItemId: string }
) {
  await page.getByTestId("today-ask-input").fill(query);
  await page.getByTestId("today-ask-submit").click();
  const result = page.getByTestId("ask-result").filter({ hasText: expected.alphaSecret }).last();
  await expect(result).toContainText(expected.alphaSecret, { timeout: 120_000 });
  await expect(result).not.toContainText(expected.betaSecret);
  await expect(result.getByTestId("ask-processing-timeline")).toContainText("检索");
  await expect(result.getByTestId("ask-processing-timeline")).toContainText("证据校验");
  await expect(page.getByTestId("today-ask-submit")).toBeEnabled({ timeout: 120_000 });

  const stableResult = page.getByTestId("ask-result").filter({ hasText: expected.alphaSecret }).last();
  await expect(stableResult).toContainText(expected.alphaSecret);
  await expect(stableResult.getByTestId("ask-scope-readiness")).toContainText(/范围可检索|可检索/);
  const alphaSourceRef = stableResult.getByTestId("ask-source-ref").filter({ hasText: expected.alphaSourceItemId }).first();
  await expect(alphaSourceRef).toBeVisible({ timeout: 45_000 });
  await alphaSourceRef.click();
  const evidenceInspector = stableResult.getByTestId("ask-evidence-inspector").first();
  await expect(evidenceInspector).toContainText(expected.alphaSecret);
  await evidenceInspector.getByTestId("open-reader-pane").click();
  const readerPane = evidenceInspector.getByTestId("reader-pane");
  await expect(readerPane).toContainText(expected.alphaSecret);
  const readerHighlight = readerPane.getByTestId("reader-highlight").first();
  await expect(readerHighlight).toContainText(expected.alphaSecret);
  await expect(readerPane).not.toContainText(expected.betaSecret);
  await selectLocatorText(readerHighlight);
  await expect(readerPane.getByTestId("reader-ask-selection")).toBeEnabled();
  await readerPane.getByTestId("reader-ask-selection").click();
  await expect(page.getByTestId("reader-focus-chip")).toBeVisible();
  await expect(page.getByTestId("today-ask-input")).toHaveValue(new RegExp(escapeRegExp(expected.alphaSourceItemId)));
  await expect(page.getByTestId("today-ask-input")).toHaveValue(new RegExp(escapeRegExp(expected.alphaSecret)));
  await saveAskResultToWriting(page, expected);
}

async function selectLocatorText(locator: Locator) {
  await locator.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
}

async function saveAskResultToWriting(page: Page, expected: { marker: string; alphaSecret: string; betaSecret: string }) {
  const result = page.getByTestId("ask-result").filter({ hasText: expected.alphaSecret }).last();
  const createBrief = result.getByTestId("ask-create-brief");
  await expect(createBrief).toBeEnabled({ timeout: 45_000 });
  await createBrief.click();
  await expect(page.getByTestId("writing-toolbar")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("writing-board-title-input")).toHaveValue(/Brief:/);
  const briefTitle = await page.getByTestId("writing-board-title-input").inputValue();
  const briefNode = page
    .locator('[data-testid="writing-node"][data-node-type="draft"], [data-testid="writing-node"][data-node-type="answer"]')
    .filter({ hasText: expected.alphaSecret })
    .first();
  await expect(briefNode).toBeVisible({ timeout: 45_000 });
  await expect(briefNode).not.toContainText(expected.betaSecret);
  await expect(page.getByTestId("writing-citation-inspector").first()).toBeVisible();
  await expectEvidenceBriefLibrary(page, briefTitle, expected);
}

async function expectEvidenceBriefLibrary(page: Page, briefTitle: string, expected: { marker: string; alphaSecret: string; betaSecret: string }) {
  await page.getByTestId("writing-close-board").click();
  const library = page.getByTestId("writing-brief-library");
  await expect(library).toBeVisible({ timeout: 45_000 });
  const briefCard = library.getByTestId("writing-brief-card").filter({ hasText: briefTitle }).first();
  await expect(briefCard).toBeVisible({ timeout: 45_000 });
  await briefCard.click();
  const detail = library.getByTestId("writing-brief-detail");
  await expect(detail).toContainText(briefTitle);
  await expect(detail.getByTestId("writing-brief-status")).toContainText("有效");
  await expect(detail.getByTestId("writing-brief-publish-status")).toContainText("Wiki 草稿");
  await expect(detail.getByTestId("writing-brief-regenerate")).toBeEnabled();
  await detail.getByTestId("writing-brief-publish").click();
  await expect(detail.getByTestId("writing-brief-publish-status")).toContainText("已发布到 Wiki", { timeout: 45_000 });
  const wikiResults = library.getByTestId("writing-brief-wiki-results");
  await expect(library.getByTestId("writing-brief-wiki-scope")).toContainText("已发布 Wiki");
  await expect(wikiResults.getByTestId("writing-brief-wiki-result").filter({ hasText: briefTitle }).first()).toBeVisible({ timeout: 45_000 });
  await library.getByTestId("writing-brief-wiki-search-input").fill(expected.alphaSecret);
  const wikiResult = wikiResults.getByTestId("writing-brief-wiki-result").filter({ hasText: briefTitle }).first();
  await expect(wikiResult).toBeVisible({ timeout: 45_000 });
  await expect(wikiResults).toContainText(expected.alphaSecret);
  await expect(wikiResults).not.toContainText(expected.betaSecret);
  await wikiResult.click();
  const wikiPage = library.getByTestId("writing-brief-wiki-page");
  await expect(wikiPage).toContainText(briefTitle, { timeout: 45_000 });
  await expect(wikiPage).toContainText(expected.alphaSecret);
  await expect(wikiPage).not.toContainText(expected.betaSecret);
  await expect(wikiPage.getByTestId("writing-brief-wiki-page-access")).toContainText("当前用户可见");
  await expect(wikiPage.getByTestId("writing-brief-wiki-page-open")).toBeVisible();
  const editedWikiBody = `Edited Wiki page body for ${expected.marker}: ${expected.alphaSecret}`;
  const contentEditor = wikiPage.getByTestId("writing-brief-wiki-content-editor");
  await expect(wikiPage.getByTestId("writing-brief-wiki-content-review-status")).toContainText("已同步发布");
  await contentEditor.getByTestId("writing-brief-wiki-content-body").fill(editedWikiBody);
  await contentEditor.getByRole("button", { name: /保存页面/ }).click();
  await expect(wikiPage.getByTestId("writing-brief-wiki-page-body")).toContainText(editedWikiBody, { timeout: 45_000 });
  await expect(wikiPage.getByTestId("writing-brief-wiki-content-review-status")).toContainText("待更新发布", { timeout: 45_000 });
  const secondWikiBody = `Second Wiki page body for ${expected.marker}: ${expected.alphaSecret}`;
  await contentEditor.getByTestId("writing-brief-wiki-content-body").fill(secondWikiBody);
  await contentEditor.getByRole("button", { name: /保存页面/ }).click();
  await expect(wikiPage.getByTestId("writing-brief-wiki-page-body")).toContainText(secondWikiBody, { timeout: 45_000 });
  await expect(wikiPage.getByTestId("writing-brief-wiki-content-review-status")).toContainText("待更新发布", { timeout: 45_000 });
  await wikiPage
    .getByTestId("writing-brief-wiki-revision")
    .filter({ hasText: `Edited Wiki page body for ${expected.marker}` })
    .first()
    .getByTestId("writing-brief-wiki-revision-restore")
    .click();
  await expect(wikiPage.getByTestId("writing-brief-wiki-page-body")).toContainText(editedWikiBody, { timeout: 45_000 });
  await expect(wikiPage.getByTestId("writing-brief-wiki-content-review-status")).toContainText("待更新发布", { timeout: 45_000 });
  await wikiPage.getByTestId("writing-brief-wiki-content-publish").click();
  await expect(wikiPage.getByTestId("writing-brief-wiki-content-review-status")).toContainText("已同步发布", { timeout: 45_000 });
  const taxonomyTag = `wiki-${expected.marker}`;
  const taxonomyEditor = wikiPage.getByTestId("writing-brief-wiki-taxonomy-editor");
  await taxonomyEditor.getByTestId("writing-brief-wiki-taxonomy-tags").fill(taxonomyTag);
  await taxonomyEditor.getByTestId("writing-brief-wiki-taxonomy-categories").fill("E2E");
  await taxonomyEditor.getByRole("button", { name: /保存分类/ }).click();
  await expect(wikiPage.getByTestId("writing-brief-wiki-taxonomy")).toContainText(taxonomyTag, { timeout: 45_000 });
  await expect(library.getByTestId("writing-brief-wiki-taxonomy-facets")).toContainText(taxonomyTag, { timeout: 45_000 });
  await library.getByTestId("writing-brief-wiki-taxonomy-filter").filter({ hasText: taxonomyTag }).click();
  await expect(library.getByTestId("writing-brief-wiki-scope")).toContainText("1 个匹配", { timeout: 45_000 });
  await expect(wikiResults.getByTestId("writing-brief-wiki-result").filter({ hasText: briefTitle }).first()).toBeVisible({ timeout: 45_000 });
  await detail.getByTestId("writing-brief-unpublish").click();
  await expect(detail.getByTestId("writing-brief-publish-status")).toContainText("Wiki 草稿", { timeout: 45_000 });
  await library.getByTestId("writing-brief-wiki-search-input").fill("");

  await library.getByTestId("writing-brief-show-inactive").check();
  await detail.getByTestId("writing-brief-expire").click();
  await expect(detail.getByTestId("writing-brief-status")).toContainText("已过期", { timeout: 45_000 });
  await detail.getByTestId("writing-brief-restore").click();
  await expect(detail.getByTestId("writing-brief-status")).toContainText("有效", { timeout: 45_000 });
  await detail.getByTestId("writing-brief-rollback").click();
  await expect(detail.getByTestId("writing-brief-status")).toContainText("已回滚", { timeout: 45_000 });
  await detail.getByTestId("writing-brief-restore").click();
  await expect(detail.getByTestId("writing-brief-status")).toContainText("有效", { timeout: 45_000 });

  await detail.getByTestId("writing-brief-regenerate").click();
  await expect(page.getByTestId("writing-toolbar")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("writing-board-title-input")).toHaveValue(/Brief:/);
  const regeneratedBriefNode = page
    .locator('[data-testid="writing-node"][data-node-type="draft"], [data-testid="writing-node"][data-node-type="answer"]')
    .filter({ hasText: expected.alphaSecret })
    .first();
  await expect(regeneratedBriefNode).toBeVisible({ timeout: 45_000 });
  await expect(regeneratedBriefNode).not.toContainText(expected.betaSecret);
}

async function askViaGraphWorkspace(
  page: Page,
  query: string,
  expected: { alphaSecret: string; betaSecret: string }
) {
  await openWorkspace(page, "Graph");
  await page.getByRole("button", { name: /Controls/ }).click();
  await page.locator(".graph-path-search input").fill(query);
  await page.locator(".graph-path-search button[type='submit']").click();
  const result = page.getByTestId("graph-ask-result-panel").last();
  await expect(result).toContainText(expected.alphaSecret, { timeout: 120_000 });
  await expect(result).not.toContainText(expected.betaSecret);
  await expect(result.getByTestId("graph-path-evidence-health")).toBeVisible();
  await expect(result.getByTestId("ask-processing-timeline")).toContainText("证据校验");
  await saveGraphAskResultToWriting(page, expected);
  await saveSelectedGraphNodeToWriting(page, expected);
}

async function saveGraphAskResultToWriting(page: Page, expected: { alphaSecret: string; betaSecret: string }) {
  const result = page.getByTestId("graph-ask-result-panel").filter({ hasText: expected.alphaSecret }).last();
  const saveToWriting = result.getByTestId("graph-ask-save-writing");
  await expect(saveToWriting).toBeEnabled({ timeout: 45_000 });
  await saveToWriting.click();
  await expect(page.getByTestId("writing-toolbar")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("writing-board-title-input")).toHaveValue(/Graph Brief:/);
  const graphAnswerNode = page
    .locator('[data-testid="writing-node"][data-node-type="answer"], [data-testid="writing-node"][data-node-type="evidence"]')
    .filter({ hasText: expected.alphaSecret })
    .first();
  await expect(graphAnswerNode).toBeVisible({ timeout: 45_000 });
  await expect(graphAnswerNode).not.toContainText(expected.betaSecret);
  await expect(page.getByTestId("writing-citation-inspector").first()).toBeVisible();
}

async function saveSelectedGraphNodeToWriting(page: Page, expected: { alphaSecret: string; betaSecret: string }) {
  await openWorkspace(page, "Graph");
  await ensureGraphControlsOpen(page);
  await page.getByTestId("graph-local-search-input").fill(expected.alphaSecret);
  await page.getByTestId("graph-local-search-subgraph").click();
  const inspector = page.locator(".graph-inspector").filter({ hasText: expected.alphaSecret }).first();
  await expect(inspector).toBeVisible({ timeout: 60_000 });
  const saveToWriting = inspector.getByTestId("graph-node-save-writing");
  await expect(saveToWriting).toBeEnabled({ timeout: 45_000 });
  await saveToWriting.click();
  await expect(page.getByTestId("writing-toolbar")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("writing-board-title-input")).toHaveValue(/Graph Node:/);
  const graphEvidenceNode = page
    .locator('[data-testid="writing-node"][data-node-type="evidence"]')
    .filter({ hasText: expected.alphaSecret })
    .first();
  await expect(graphEvidenceNode).toBeVisible({ timeout: 45_000 });
  await expect(graphEvidenceNode).not.toContainText(expected.betaSecret);
  await expect(page.getByTestId("writing-citation-inspector").first()).toBeVisible();
}

async function ensureGraphControlsOpen(page: Page) {
  const localSearch = page.getByTestId("graph-local-search-input");
  if (!(await localSearch.isVisible().catch(() => false))) {
    await page.getByRole("button", { name: /Controls/ }).click();
  }
  await expect(localSearch).toBeVisible({ timeout: 45_000 });
}

async function cleanupFixtures(page: Page, sourceItemIds: string[], knowledgeBaseIds: string[]) {
  if (sourceItemIds.length) {
    await api(page, "/workspace/documents/delete", {
      method: "POST",
      body: JSON.stringify({
        source_item_ids: sourceItemIds,
        execute: true,
        hard_delete: true,
        reason: "multi-kb scoped Ask e2e cleanup"
      })
    }).catch(() => undefined);
  }
  for (const knowledgeBaseId of knowledgeBaseIds) {
    await api(page, `/workspace/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`, {
      method: "DELETE",
      body: JSON.stringify({ reason: "multi-kb scoped Ask e2e cleanup" })
    }).catch(() => undefined);
  }
}

function cleanupDatabaseResidue(marker: string, extraMarker?: string) {
  const databaseUrl = process.env.PSKA_E2E_DATABASE_URL || "postgresql:///pska";
  const tenant = sqlLiteral(tenantId);
  const owner = sqlLiteral(userId);
  const markers = [marker, extraMarker].filter((value): value is string => Boolean(value));
  const markerArray = `array[${markers.map((value) => sqlLiteral(`%${value}%`)).join(", ")}]`;
  const sourceMarkerClause = `(title like any(${markerArray}) or content_text like any(${markerArray}))`;
  const sql = `
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}
), target_kbs as (
  select knowledge_base_id from knowledge_bases
  where tenant_id = ${tenant} and owner_user_id = ${owner} and name like any(${markerArray})
)
delete from knowledge_base_source_items
where tenant_id = ${tenant}
  and (source_item_id in (select source_item_id from target_items)
       or knowledge_base_id in (select knowledge_base_id from target_kbs));
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}
), target_reviews as (
  select review_item_id from review_items
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (
      title like any(${markerArray})
      or proposal::text like any(${markerArray})
      or exists (
        select 1 from target_items
        where review_items.proposal::text like '%' || target_items.source_item_id || '%'
      )
    )
), target_topics as (
  select topic_id from knowledge_topics
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (label like any(${markerArray}) or normalized_label like any(${markerArray}) or metadata::text like any(${markerArray}))
  union
  select topic_id from topic_mentions
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and source_item_id in (select source_item_id from target_items)
)
delete from artifact_supports
where tenant_id = ${tenant}
  and owner_user_id = ${owner}
  and (
    source_item_id in (select source_item_id from target_items)
    or artifact_id in (select review_item_id from target_reviews)
    or topic_id in (select topic_id from target_topics)
  );
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}
), target_topics as (
  select topic_id from knowledge_topics
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (label like any(${markerArray}) or normalized_label like any(${markerArray}) or metadata::text like any(${markerArray}))
)
delete from topic_mentions
where tenant_id = ${tenant}
  and owner_user_id = ${owner}
  and (
    source_item_id in (select source_item_id from target_items)
    or topic_id in (select topic_id from target_topics)
    or mention_text like any(${markerArray})
    or metadata::text like any(${markerArray})
  );
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}
), target_hyperedges as (
  select hyperedge_id from hyperedges
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (
      evidence_text like any(${markerArray})
      or source_refs::text like any(${markerArray})
      or exists (
        select 1 from target_items
        where hyperedges.source_refs::text like '%' || target_items.source_item_id || '%'
      )
    )
)
delete from hyperedge_members
where hyperedge_id in (select hyperedge_id from target_hyperedges);
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}
), target_hyperedges as (
  select hyperedge_id from hyperedges
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (
      evidence_text like any(${markerArray})
      or source_refs::text like any(${markerArray})
      or exists (
        select 1 from target_items
        where hyperedges.source_refs::text like '%' || target_items.source_item_id || '%'
      )
    )
)
delete from hyperedges
where hyperedge_id in (select hyperedge_id from target_hyperedges);
delete from entities
where tenant_id = ${tenant}
  and owner_user_id = ${owner}
  and (label like any(${markerArray}) or metadata::text like any(${markerArray}))
  and not exists (
    select 1 from hyperedge_members
    where hyperedge_members.entity_id = entities.entity_id
  );
delete from review_items
where tenant_id = ${tenant}
  and owner_user_id = ${owner}
  and (title like any(${markerArray}) or proposal::text like any(${markerArray}));
delete from knowledge_topics
where tenant_id = ${tenant}
  and owner_user_id = ${owner}
  and (label like any(${markerArray}) or normalized_label like any(${markerArray}) or metadata::text like any(${markerArray}));
with target_writing_boards as (
  select board_id from writing_boards
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (title like any(${markerArray}) or goal like any(${markerArray}) or metadata::text like any(${markerArray}))
  union
  select board_id from writing_nodes
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (
      title like any(${markerArray})
      or body_markdown like any(${markerArray})
      or source_refs::text like any(${markerArray})
      or citations::text like any(${markerArray})
      or metadata::text like any(${markerArray})
    )
)
delete from writing_edges
where tenant_id = ${tenant}
  and owner_user_id = ${owner}
  and board_id in (select board_id from target_writing_boards);
with target_writing_boards as (
  select board_id from writing_boards
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (title like any(${markerArray}) or goal like any(${markerArray}) or metadata::text like any(${markerArray}))
  union
  select board_id from writing_nodes
  where tenant_id = ${tenant}
    and owner_user_id = ${owner}
    and (
      title like any(${markerArray})
      or body_markdown like any(${markerArray})
      or source_refs::text like any(${markerArray})
      or citations::text like any(${markerArray})
      or metadata::text like any(${markerArray})
    )
)
delete from writing_nodes
where tenant_id = ${tenant}
  and owner_user_id = ${owner}
  and board_id in (select board_id from target_writing_boards);
delete from writing_boards
where tenant_id = ${tenant}
  and owner_user_id = ${owner}
  and (title like any(${markerArray}) or goal like any(${markerArray}) or metadata::text like any(${markerArray}));
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}
)
delete from passage_windows where tenant_id = ${tenant} and source_item_id in (select source_item_id from target_items);
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}
)
delete from chunks where tenant_id = ${tenant} and source_item_id in (select source_item_id from target_items);
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}
)
delete from documents where tenant_id = ${tenant} and source_item_id in (select source_item_id from target_items);
delete from source_items
where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause};
delete from knowledge_sources
where tenant_id = ${tenant} and owner_user_id = ${owner} and (name like any(${markerArray}) or uri like any(${markerArray}));
delete from knowledge_bases
where tenant_id = ${tenant} and owner_user_id = ${owner} and name like any(${markerArray});
select
  (select count(*) from knowledge_bases where tenant_id = ${tenant} and owner_user_id = ${owner} and name like any(${markerArray})) as knowledge_bases,
  (select count(*) from knowledge_sources where tenant_id = ${tenant} and owner_user_id = ${owner} and (name like any(${markerArray}) or uri like any(${markerArray}))) as knowledge_sources,
  (select count(*) from source_items where tenant_id = ${tenant} and owner_user_id = ${owner} and ${sourceMarkerClause}) as source_items,
  (select count(*) from documents where tenant_id = ${tenant} and body like any(${markerArray})) as documents,
  (select count(*) from chunks where tenant_id = ${tenant} and text like any(${markerArray})) as chunks,
  (select count(*) from passage_windows where tenant_id = ${tenant} and text like any(${markerArray})) as passage_windows,
  (select count(*) from review_items where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like any(${markerArray}) or proposal::text like any(${markerArray}))) as review_items,
  (select count(*) from knowledge_topics where tenant_id = ${tenant} and owner_user_id = ${owner} and (label like any(${markerArray}) or normalized_label like any(${markerArray}) or metadata::text like any(${markerArray}))) as knowledge_topics,
  (select count(*) from topic_mentions where tenant_id = ${tenant} and owner_user_id = ${owner} and (mention_text like any(${markerArray}) or metadata::text like any(${markerArray}))) as topic_mentions,
  (select count(*) from artifact_supports where tenant_id = ${tenant} and owner_user_id = ${owner} and (artifact_id like any(${markerArray}) or metadata::text like any(${markerArray}))) as artifact_supports,
  (select count(*) from writing_boards where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like any(${markerArray}) or goal like any(${markerArray}) or metadata::text like any(${markerArray}))) as writing_boards,
  (select count(*) from writing_nodes where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like any(${markerArray}) or body_markdown like any(${markerArray}) or source_refs::text like any(${markerArray}) or citations::text like any(${markerArray}) or metadata::text like any(${markerArray}))) as writing_nodes,
  (select count(*) from writing_edges where tenant_id = ${tenant} and owner_user_id = ${owner} and metadata::text like any(${markerArray})) as writing_edges,
  (select count(*) from entities where tenant_id = ${tenant} and owner_user_id = ${owner} and (label like any(${markerArray}) or metadata::text like any(${markerArray}))) as entities,
  (select count(*) from hyperedges where tenant_id = ${tenant} and owner_user_id = ${owner} and (evidence_text like any(${markerArray}) or source_refs::text like any(${markerArray}))) as hyperedges,
  (select count(*) from hyperedge_members join entities on entities.entity_id = hyperedge_members.entity_id where entities.tenant_id = ${tenant} and entities.owner_user_id = ${owner} and (entities.label like any(${markerArray}) or entities.metadata::text like any(${markerArray}))) as hyperedge_members;
`;
  const output = execFileSync("psql", ["-X", "-d", databaseUrl, "-A", "-F", ",", "-q", "-c", sql], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"]
  });
  const residue = output
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => /^0(?:,0){15}$/.test(line));
  if (!residue) {
    throw new Error(`multi-KB scoped Ask e2e cleanup left residue for marker ${marker}: ${output}`);
  }
}

function sqlLiteral(value: string) {
  return `'${value.replace(/'/g, "''")}'`;
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function requireOne(values: string[] | undefined, label: string) {
  const value = values?.[0];
  if (!value) {
    throw new Error(`Missing ${label}`);
  }
  return value;
}
