import { expect, test, type FrameLocator, type Page } from "@playwright/test";

type Fixture = { id: string; token: string };

async function docs(page: Page, art: Fixture): Promise<Record<string, Record<string, unknown>>> {
  return (await page.request.get(`/__e2e/docs/${art.id}`)).json();
}

async function artifactState(page: Page, art: Fixture): Promise<{ status: string; notified_at: number | null }> {
  return (await page.request.get(`/__e2e/artifact/${art.id}`)).json();
}

// Each test gets its own artifact, so none depends on what another left in the store.
async function open(page: Page): Promise<{ art: Fixture; app: FrameLocator }> {
  const art: Fixture = await (await page.request.post("/__e2e/artifact")).json();
  await page.goto(`/a/${art.id}`);
  return { art, app: page.frameLocator("iframe") };
}

test("a document saved in the page is persisted and echoed back through onSnapshot", async ({ page }) => {
  const { art, app } = await open(page);
  await app.locator("#name").fill("Ada");
  await app.locator("#save").click();
  await expect(app.locator("#snapshot")).toContainText('"answers":{"name":"Ada"}');
  await expect.poll(async () => (await docs(page, art))["form/answers"]).toEqual({ name: "Ada" });
});

test("a write made outside the page reaches it over the event stream", async ({ page }) => {
  const { art, app } = await open(page);
  await expect(app.locator("#snapshot")).toHaveText("{}");
  const res = await page.request.post(`/api/${art.id}/db/set`, {
    headers: { "x-artifact": art.token },
    data: { path: "form/remote", data: { from: "agent" } },
  });
  expect(res.ok()).toBe(true);
  await expect(app.locator("#snapshot")).toContainText('"remote":{"from":"agent"}');
});

test("submit saves the answers as submitted and notifies, in one request", async ({ page }) => {
  const { art, app } = await open(page);
  await app.locator("#name").fill("Ada");
  await app.locator("#submit").click();
  await expect.poll(async () => (await artifactState(page, art)).notified_at).not.toBeNull();
  expect((await docs(page, art))["form/answers"]).toEqual({ name: "Ada", status: "submitted" });
  expect((await artifactState(page, art)).status).toBe("active");
});

test("notify on its own records the time and saves nothing", async ({ page }) => {
  const { art, app } = await open(page);
  await app.locator("#notify").click();
  await expect.poll(async () => (await artifactState(page, art)).notified_at).not.toBeNull();
  expect(await docs(page, art)).toEqual({});
});

test("add generates an id and saves under the collection", async ({ page }) => {
  const { art, app } = await open(page);
  await app.locator("#add").click();
  await expect(app.locator("#result")).toHaveText(/^[0-9a-f]{20}$/);
  const id = await app.locator("#result").textContent();
  expect((await docs(page, art))[`items/${id}`]).toEqual({ n: 1 });
});

test("a malformed path is refused by the server, with its code", async ({ page }) => {
  const { app } = await open(page);
  await app.locator("#bad").click();
  await expect(app.locator("#result")).toHaveText("invalid_argument");
});

test("a lost event stream is reported, not silent", async ({ page }) => {
  await page.route("**/api/*/events*", (route) => route.abort());
  const warned = page.waitForEvent("console", (m) => m.text().includes("event stream lost"));
  await open(page);
  expect((await warned).type()).toBe("warning");
});

test("clicking a link opens it in a new tab and leaves the artifact in place", async ({ page }) => {
  const { app } = await open(page);
  const popup = page.waitForEvent("popup");
  await app.locator("#link").click();
  expect((await popup).url()).toContain("/__e2e/landing");
  await expect(app.locator("#snapshot")).toBeVisible();
});

test("a link's own click handler still runs when the shim routes the link", async ({ page }) => {
  const { app } = await open(page);
  const popup = page.waitForEvent("popup");
  await app.locator("#tapped").click();
  await expect(app.locator("#taps")).toHaveText("1");
  expect((await popup).url()).toContain("/__e2e/landing");
});

test("window.open opens a new tab", async ({ page }) => {
  const { app } = await open(page);
  const popup = page.waitForEvent("popup");
  await app.locator("#open").click();
  expect((await popup).url()).toContain("/__e2e/landing");
});

test("the page runs sandboxed: no web storage", async ({ page }) => {
  const { app } = await open(page);
  await expect(app.locator("#sandbox")).toHaveText('{"storage":"SecurityError"}');
});
