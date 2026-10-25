// 创作页脚本按 studio.html 中的加载顺序拼接成一段源码，供按函数片段运行真实代码的 .cjs 测试使用。
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const STUDIO = path.join(ROOT, "frontend/src/studio");

function studioScripts() {
  const html = fs.readFileSync(path.join(STUDIO, "studio.html"), "utf8");
  const files = [...html.matchAll(/\/assets\/studio\/([\w-]+\.js)/g)].map((m) => m[1]);
  if (!files.length) throw new Error("studio.html 中没有创作页脚本");
  return files.map((name) => path.join(STUDIO, name));
}

function studioSource() {
  return studioScripts().map((file) => fs.readFileSync(file, "utf8")).join("");
}

module.exports = { studioScripts, studioSource };
