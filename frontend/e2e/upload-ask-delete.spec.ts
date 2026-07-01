import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";

const frontendUrl = (process.env.PSKA_E2E_FRONTEND_URL || "http://127.0.0.1:5173").replace(/\/$/, "");
const authnodeUrl = (process.env.PSKA_E2E_AUTHNODE_URL || "http://127.0.0.1:8788").replace(/\/$/, "");
const tenantId = process.env.PSKA_E2E_TENANT_ID || "tenant_default";
const userId = process.env.PSKA_E2E_USER_ID || "user_primary";
const password =
  process.env.PSKA_E2E_PASSWORD ||
  (userId === "user_primary" ? "primary-local" : userId === "alice" ? "alice-local" : "pska-local");

type WorkspaceDocument = {
  source_item_id?: string;
  title?: string;
  lifecycle_status?: string;
};

test.setTimeout(240_000);

test("browser upload spreadsheet ask and soft delete removes evidence", async ({ page, request }) => {
  const marker = `PSKA_E2E_ANNUAL_REPORT_${Date.now()}_${randomUUID().slice(0, 8)}`;
  const reportValue = `ARR-${marker.slice(-8).toUpperCase()}`;
  const filename = `${marker}.xlsx`;
  const workbookPath = path.join(os.tmpdir(), filename);
  await writeMinimalXlsx(workbookPath, {
    sheetName: "Annual Report",
    rows: [
      ["Marker", "Metric", "Value", "Comment"],
      [marker, "Annual recurring revenue", reportValue, "browser upload deletion regression"]
    ]
  });
  let sourceItemId: string | undefined;

  try {
    await page.goto(await authnodeCallbackUrl(request));
    await expect(page.getByTestId("gateway-session")).toContainText(userId);
    await expect(page.getByTestId("gateway-session")).toContainText(tenantId);

    await openWorkspace(page, "资料库");
    await page.getByTestId("corpus-upload-digest-toggle").uncheck();
    await page.getByTestId("corpus-upload-input").setInputFiles(workbookPath);
    await page.getByTestId("corpus-upload-submit").click();
    await expect(page.getByTestId("corpus-upload-submit")).toBeEnabled({ timeout: 90_000 });
    await expect(page.getByTestId("corpus-operation")).toContainText(/已完成|入库\s*1/, { timeout: 45_000 });

    const uploadedDocument = await waitForDocument(page, filename, "active");
    sourceItemId = uploadedDocument.source_item_id;
    expect(sourceItemId).toBeTruthy();
    await page.getByRole("button", { name: /刷新视图/ }).click();
    await page.getByTestId("corpus-search-input").fill(filename);
    const card = page.getByTestId("document-lifecycle-card").filter({ hasText: filename });
    await expect(card).toBeVisible({ timeout: 45_000 });
    await expect(card).toContainText("active");
    await expect(card).toHaveAttribute("data-source-item-id", sourceItemId as string);

    await openWorkspace(page, "Today");
    await page.getByTestId("today-ask-input").fill(`${marker} 的 Annual recurring revenue 是多少？`);
    await page.getByTestId("today-ask-submit").click();
    await expect(page.locator("body")).toContainText(reportValue, { timeout: 90_000 });

    const beforeDelete = await askViaBrowserSession(page, `${marker} 的 Annual recurring revenue 是多少？`);
    expect(JSON.stringify(beforeDelete)).toContain(reportValue);
    expect(JSON.stringify(beforeDelete)).toContain(sourceItemId as string);

    await openWorkspace(page, "资料库");
    await page.getByTestId("corpus-search-input").fill(filename);
    const deleteCard = page.getByTestId("document-lifecycle-card").filter({ hasText: filename });
    await expect(deleteCard).toBeVisible({ timeout: 45_000 });
    await deleteCard.getByTestId("document-soft-delete").click();
    await waitForDocument(page, filename, "deleted");
    await expect(deleteCard).toContainText("deleted", { timeout: 45_000 });

    const afterDelete = await askViaBrowserSession(page, `${marker} 的 Annual recurring revenue 是多少？`);
    const afterDeleteJson = JSON.stringify(afterDelete);
    expect(afterDeleteJson).not.toContain(reportValue);
    expect(afterDeleteJson).not.toContain(sourceItemId as string);
  } finally {
    fs.rmSync(workbookPath, { force: true });
    await softDeleteDocumentIfPossible(page, sourceItemId, filename);
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

async function waitForDocument(page: Page, filename: string, lifecycleStatus?: string): Promise<WorkspaceDocument> {
  const deadline = Date.now() + 60_000;
  let lastSeen = "";
  while (Date.now() < deadline) {
    const documents = await listWorkspaceDocuments(page);
    const document = findDocumentByFilename(documents, filename);
    if (document && (!lifecycleStatus || document.lifecycle_status === lifecycleStatus)) {
      return document;
    }
    lastSeen = documents
      .slice(0, 8)
      .map((item) => `${item.title || item.source_item_id || "<untitled>"}:${item.lifecycle_status || "unknown"}`)
      .join(", ");
    await page.waitForTimeout(1000);
  }
  throw new Error(`Document ${filename} did not reach ${lifecycleStatus || "any"} state. Last seen: ${lastSeen}`);
}

function findDocumentByFilename(documents: WorkspaceDocument[], filename: string): WorkspaceDocument | undefined {
  return documents.find((item) => item.title === filename);
}

async function listWorkspaceDocuments(page: Page): Promise<WorkspaceDocument[]> {
  return page.evaluate(async () => {
    const response = await fetch("/workspace/documents/data?include_deleted=true&limit=500", {
      headers: { Accept: "application/json" }
    });
    if (!response.ok) {
      throw new Error(`Documents fetch failed with HTTP ${response.status}: ${await response.text()}`);
    }
    const payload = await response.json();
    return payload.documents || [];
  });
}

async function softDeleteDocumentIfPossible(page: Page, sourceItemId: string | undefined, filename: string) {
  try {
    let cleanupSourceItemId = sourceItemId;
    if (!cleanupSourceItemId) {
      cleanupSourceItemId = findDocumentByFilename(await listWorkspaceDocuments(page), filename)?.source_item_id;
    }
    if (!cleanupSourceItemId) {
      return;
    }
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
          reason: "e2e upload-delete cleanup"
        })
      });
      if (!response.ok) {
        throw new Error(`Cleanup delete failed with HTTP ${response.status}: ${await response.text()}`);
      }
    }, cleanupSourceItemId);
  } catch {
    // The primary test failure should stay visible; cleanup is best effort.
  }
}

async function askViaBrowserSession(page: Page, query: string) {
  return page.evaluate(async (askQuery) => {
    const response = await fetch("/workspace/ask", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        query: askQuery,
        intent: "quick",
        surface: "e2e-upload-delete",
        top_k: 8
      })
    });
    if (!response.ok) {
      throw new Error(`Ask failed with HTTP ${response.status}: ${await response.text()}`);
    }
    return response.json();
  }, query);
}

async function writeMinimalXlsx(
  outputPath: string,
  {
    sheetName,
    rows
  }: {
    sheetName: string;
    rows: string[][];
  }
) {
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "pska-xlsx-"));
  const sheetRows = rows
    .map((row, rowIndex) => {
      const cells = row
        .map((value, columnIndex) => {
          const ref = `${columnName(columnIndex + 1)}${rowIndex + 1}`;
          return `<c r="${ref}" t="inlineStr"><is><t>${xmlEscape(value)}</t></is></c>`;
        })
        .join("");
      return `<row r="${rowIndex + 1}">${cells}</row>`;
    })
    .join("");
  const sheetXml =
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
    `<sheetData>${sheetRows}</sheetData></worksheet>`;
  writeFixtureFile(
    tempDir,
    "[Content_Types].xml",
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
      "</Types>"
  );
  writeFixtureFile(
    tempDir,
    "_rels/.rels",
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
      "</Relationships>"
  );
  writeFixtureFile(
    tempDir,
    "xl/workbook.xml",
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' +
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
      `<sheets><sheet name="${xmlEscape(sheetName)}" sheetId="1" r:id="rId1"/></sheets>` +
      "</workbook>"
  );
  writeFixtureFile(
    tempDir,
    "xl/_rels/workbook.xml.rels",
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
      "</Relationships>"
  );
  writeFixtureFile(tempDir, "xl/worksheets/sheet1.xml", sheetXml);
  fs.rmSync(outputPath, { force: true });
  execFileSync("zip", ["-qr", outputPath, "."], { cwd: tempDir });
  fs.rmSync(tempDir, { recursive: true, force: true });
}

function writeFixtureFile(root: string, relativePath: string, content: string) {
  const filePath = path.join(root, relativePath);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, content, "utf-8");
}

function columnName(index: number) {
  let value = "";
  while (index > 0) {
    const remainder = (index - 1) % 26;
    value = String.fromCharCode(65 + remainder) + value;
    index = Math.floor((index - 1) / 26);
  }
  return value;
}

function xmlEscape(value: string) {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
