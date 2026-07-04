import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
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

    await page.goto(frontendUrl, { waitUntil: "domcontentloaded" });
    await openWorkspace(page, "Today");
    await selectCurrentKnowledgeBase(page, alpha.knowledge_base_id);

    const query = `What is the scoped Ask answer for marker ${marker}?`;
    const result = await askViaBrowserSession(page, query, alpha.knowledge_base_id);
    const routeScope = result?.route?.scope_applied || result?.scope_applied || {};
    expect(routeScope.knowledge_base_ids).toEqual([alpha.knowledge_base_id]);

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
      alphaSecret,
      betaSecret,
      alphaSourceItemId
    });
  } finally {
    await cleanupFixtures(page, createdSourceItemIds, createdKnowledgeBaseIds);
    cleanupDatabaseResidue(marker);
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
  expected: { alphaSecret: string; betaSecret: string; alphaSourceItemId: string }
) {
  await page.getByTestId("today-ask-input").fill(query);
  await page.getByTestId("today-ask-submit").click();
  const result = page.getByTestId("ask-result").last();
  await expect(result).toContainText(expected.alphaSecret, { timeout: 120_000 });
  await expect(result).not.toContainText(expected.betaSecret);
  await expect(page.getByTestId("ask-evidence-inspector").last()).toContainText(expected.alphaSecret);
  await page.getByTestId("open-reader-pane").last().click();
  await expect(page.getByTestId("reader-pane").last()).toContainText(expected.alphaSecret);
  await expect(page.getByTestId("reader-highlight").last()).toContainText(expected.alphaSecret);
  await expect(page.getByTestId("reader-pane").last()).not.toContainText(expected.betaSecret);
  await page.getByTestId("ask-from-evidence").last().click();
  await expect(page.getByTestId("reader-focus-chip")).toBeVisible();
  await expect(page.getByTestId("today-ask-input")).toHaveValue(new RegExp(escapeRegExp(expected.alphaSourceItemId)));
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

function cleanupDatabaseResidue(marker: string) {
  const databaseUrl = process.env.PSKA_E2E_DATABASE_URL || "postgresql:///pska";
  const tenant = sqlLiteral(tenantId);
  const owner = sqlLiteral(userId);
  const markerLike = sqlLiteral(`%${marker}%`);
  const sql = `
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like ${markerLike} or content_text like ${markerLike})
), target_kbs as (
  select knowledge_base_id from knowledge_bases
  where tenant_id = ${tenant} and owner_user_id = ${owner} and name like ${markerLike}
)
delete from knowledge_base_source_items
where tenant_id = ${tenant}
  and (source_item_id in (select source_item_id from target_items)
       or knowledge_base_id in (select knowledge_base_id from target_kbs));
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like ${markerLike} or content_text like ${markerLike})
)
delete from passage_windows where tenant_id = ${tenant} and source_item_id in (select source_item_id from target_items);
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like ${markerLike} or content_text like ${markerLike})
)
delete from chunks where tenant_id = ${tenant} and source_item_id in (select source_item_id from target_items);
with target_items as (
  select source_item_id from source_items
  where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like ${markerLike} or content_text like ${markerLike})
)
delete from documents where tenant_id = ${tenant} and source_item_id in (select source_item_id from target_items);
delete from source_items
where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like ${markerLike} or content_text like ${markerLike});
delete from knowledge_sources
where tenant_id = ${tenant} and owner_user_id = ${owner} and (name like ${markerLike} or uri like ${markerLike});
delete from knowledge_bases
where tenant_id = ${tenant} and owner_user_id = ${owner} and name like ${markerLike};
select
  (select count(*) from knowledge_bases where tenant_id = ${tenant} and owner_user_id = ${owner} and name like ${markerLike}) as knowledge_bases,
  (select count(*) from knowledge_sources where tenant_id = ${tenant} and owner_user_id = ${owner} and (name like ${markerLike} or uri like ${markerLike})) as knowledge_sources,
  (select count(*) from source_items where tenant_id = ${tenant} and owner_user_id = ${owner} and (title like ${markerLike} or content_text like ${markerLike})) as source_items,
  (select count(*) from documents where tenant_id = ${tenant} and body like ${markerLike}) as documents,
  (select count(*) from chunks where tenant_id = ${tenant} and text like ${markerLike}) as chunks,
  (select count(*) from passage_windows where tenant_id = ${tenant} and text like ${markerLike}) as passage_windows;
`;
  const output = execFileSync("psql", ["-X", "-d", databaseUrl, "-A", "-F", ",", "-q", "-c", sql], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"]
  });
  const residue = output
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => /^0,0,0,0,0,0$/.test(line));
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
