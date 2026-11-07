const assert = require("node:assert/strict");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
(async () => {
  const [url, file, screenshot] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  const uploads = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("response", (r) => {
    if (r.request().method() === "PUT" && !r.url().startsWith(url)) {
      uploads.push(r.status());
    }
  });
  try {
    await page.goto(url + "/admin");
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/api/uploads/capabilities")),
      page.locator("#add-source").click(),
    ]);
    await page.locator("#upload-transport").selectOption("s3");
    const imported = page.waitForResponse((r) => r.url().includes("/import-upload/"));
    await page.locator("#upload-files").setInputFiles(file);
    // 代理在 PUT 被 S3 接收后断开：用户显式续传，GET 状态恢复该分片，不重做上传。
    await page.locator("#upload-results button").first().waitFor();
    await page.locator("#upload-results button").first().click();
    const response = await imported;
    assert.equal(response.status(), 201, await response.text());
    const assetId = response.url().split("/import-upload/")[1];
    const seek = await page.evaluate(async (id) => {
      const video = document.createElement("video");
      video.muted = true;
      video.preload = "metadata";
      document.body.append(video);
      video.src = "/api/uploads/" + id + "/content";
      await new Promise((resolve, reject) => {
        video.onloadedmetadata = resolve;
        video.onerror = () => reject(new Error("媒体元数据不可读"));
      });
      if (video.duration < 70) {
        throw new Error("长视频样本不足 70 秒");
      }
      const done = new Promise((resolve, reject) => {
        video.onseeked = resolve;
        video.onerror = () => reject(new Error("视频 seek 失败"));
      });
      video.currentTime = 60;
      await done;
      return { duration: video.duration, time: video.currentTime };
    }, assetId);
    assert.ok(Math.abs(seek.time - 60) < 0.1);
    await page.screenshot({ path: screenshot, fullPage: true });
    assert.deepEqual(errors, []);
    process.stdout.write(
      JSON.stringify({
        cors_upload: true,
        source_import: true,
        page_errors: errors,
        resumed: true,
        seek_seconds: seek.time,
        duration: seek.duration,
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
