// 片段测试必须明确找到两端，防止 indexOf 返回 -1 后静默运行错误的源码范围。
const assert = require("node:assert/strict");

function between(source, start, end, from = 0) {
  assert.ok(from >= 0, "找不到源码片段的起始搜索位置");
  const first = source.indexOf(start, from);
  assert.ok(first >= 0, `找不到源码片段起点：${start}`);
  const last = source.indexOf(end, first + start.length);
  assert.ok(last > first, `找不到源码片段终点：${end}`);
  return source.slice(first, last);
}

module.exports = { between };
