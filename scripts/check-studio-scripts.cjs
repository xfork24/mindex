// 按页面顺序检查创作页的共享作用域，避免单文件自动修复误判跨文件的重新赋值。
const fs = require("node:fs");
const path = require("node:path");
const { ESLint } = require("eslint");

/** 检查完整的经典脚本作用域；返回用于 CI 的退出码。 */
async function main() {
  const root = path.resolve(__dirname, "..");
  const studio = path.join(root, "frontend/src/studio");
  const html = fs.readFileSync(path.join(studio, "studio.html"), "utf8");
  const files = [...html.matchAll(/\/assets\/studio\/([\w-]+\.js)/g)].map((match) => match[1]);
  if (!files.length) {
    throw new Error("studio.html 中找不到创作页脚本");
  }
  const source = files.map((name) => fs.readFileSync(path.join(studio, name), "utf8")).join("\n");
  const eslint = new ESLint({
    cwd: root,
    overrideConfig: { rules: { "prefer-const": "error" } },
  });
  const results = await eslint.lintText(source, {
    filePath: "frontend/src/studio/__combined__.js",
  });
  const formatter = await eslint.loadFormatter("stylish");
  const output = formatter.format(results);
  if (output) {
    process.stderr.write(output);
  }
  return results.some((result) => result.errorCount || result.warningCount) ? 1 : 0;
}

main().then(
  (code) => {
    process.exitCode = code;
  },
  (error) => {
    console.error(error);
    process.exitCode = 1;
  },
);
