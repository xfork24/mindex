// Prettier 负责排版；这里检查排版工具不会修复的语言级规范。
export default [
  {
    ignores: [
      "**/node_modules/**",
      "desktop/build/**",
      "desktop/dist/**",
      "frontend/src/shared/messages.js",
      "frontend/src/shared/marked.umd.js",
      "frontend/src/shared/purify.min.js",
    ],
  },
  {
    files: ["frontend/src/**/*.js", "desktop/**/*.js", "tests/*.cjs", "scripts/*.cjs"],
    rules: {
      "curly": ["error", "all"],
      "no-var": "error",
      "prefer-const": "error",
      "one-var": ["error", "never"],
      "eqeqeq": ["error", "always", { null: "ignore" }],
      "camelcase": ["error", { properties: "never", ignoreDestructuring: true }],
      "no-new-wrappers": "error",
      "no-with": "error",
      "no-debugger": "error",
      "no-const-assign": "error",
      "no-class-assign": "error",
      "no-duplicate-case": "error",
      "no-unreachable": "error",
    },
  },
  {
    // 经典脚本共享顶层绑定。合并检查会在完整作用域里启用 prefer-const。
    files: ["frontend/src/studio/studio-*.js"],
    rules: { "prefer-const": "off" },
  },
];
