import { chromium } from "playwright";
import fs from "node:fs/promises";

const baseUrl = process.env.APP_BASE_URL ?? "http://127.0.0.1:5174";
const outputDir = new URL("../../docs/screenshots/agent-skill-final/", import.meta.url);
await fs.mkdir(outputDir, { recursive: true });

const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: "zh-CN" });
const page = await context.newPage();
await page.goto(`${baseUrl}/integrations/agent-skill`, { waitUntil: "domcontentloaded", timeout: 30_000 });
await page.getByRole("heading", { name: "Agent Skill" }).waitFor();
await page.getByRole("button", { name: "测试台" }).click();
await page.getByLabel("Skill", { exact: true }).selectOption("mcp-builder");
const history = page.getByLabel("历史调用结果");
await history.waitFor();
const successValue = await history.locator("option").evaluateAll(options =>
  options.find(option => option.textContent?.includes("成功"))?.getAttribute("value") ?? "",
);
if (!successValue) throw new Error("No persisted successful MCP Builder invocation was available.");
await history.selectOption(successValue);
await page.getByText("Skill调用成功").waitFor();
const pathOf = name => new URL(name, outputDir).pathname.replace(/^\/(?=[A-Za-z]:)/, "");
await page.screenshot({ path: pathOf("agent-skill-testbench-success-desktop-20260806.png"), fullPage: true });

await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(400);
await page.screenshot({ path: pathOf("agent-skill-testbench-success-mobile-390x844-20260806.png"), fullPage: true });
const overflow = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
if (overflow.scrollWidth > overflow.width) throw new Error(`Mobile horizontal overflow: ${overflow.scrollWidth} > ${overflow.width}`);
await browser.close();
console.log(JSON.stringify({ success: true, invocation_id: successValue, mobile_overflow: overflow }));
