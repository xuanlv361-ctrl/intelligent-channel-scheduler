import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "../web/node_modules/playwright/index.mjs";

const baseUrl = process.env.CONSOLE_URL ?? "http://127.0.0.1:5174";
const outputDir = resolve("docs/screenshots/historical-replay");
await mkdir(outputDir, { recursive: true });

const chromeCandidates = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`,
];
const executablePath = chromeCandidates.find(existsSync);
if (!executablePath) throw new Error("chrome_not_installed");
const browser = await chromium.launch({ headless: true, executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
try {
  await page.goto(`${baseUrl}/observability/replay`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "运行历史回放" }).waitFor();
  await page.getByRole("button", { name: "运行历史回放" }).click();
  await page.getByText("真实 UAT 日志 · 国内 UAT", { exact: true }).waitFor();
  await page.getByText("REQ-5CC62E68D9D3493296D1D81F035F28A6", { exact: true }).waitFor();
  const visibleText = await page.locator(".historical-replay-page").innerText();
  if (/Demo|演示样本/.test(visibleText)) {
    throw new Error("historical replay unexpectedly rendered demo data");
  }
  await page.screenshot({
    path: resolve(outputDir, "historical-replay-desktop.png"),
    fullPage: true,
  });

  await page.getByText("REQ-5CC62E68D9D3493296D1D81F035F28A6", { exact: true }).click();
  await page.getByRole("button", { name: "关闭详情" }).waitFor();
  await page.screenshot({
    path: resolve(outputDir, "historical-replay-request-drawer.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "关闭详情" }).click();

  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  if (overflow.document > overflow.viewport || overflow.body > overflow.viewport) {
    throw new Error(`mobile horizontal overflow: ${JSON.stringify(overflow)}`);
  }
  await page.screenshot({
    path: resolve(outputDir, "historical-replay-mobile-390.png"),
    fullPage: true,
  });
  process.stdout.write(JSON.stringify({ outputDir, overflow, demoData: false }));
} finally {
  await browser.close();
}
