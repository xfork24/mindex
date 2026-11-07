const { between } = require("./source_slices.cjs");
// persons.js 处理进度轮询：链式 setTimeout、页面隐藏暂停、失败指数退避（5s 起翻倍，上限 60s）、成功复位。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("frontend/src/persons/persons.js", "utf8");
const block = between(source, "// 处理进度轮询", "// 智能助手：定位");
(async () => {
  const timers = [];
  let listener = null;
  let outcome = "ok";
  let calls = 0;
  let notices = 0;
  let inflight = null;
  const document = {
    hidden: false,
    addEventListener: (name, fn) => {
      if (name === "visibilitychange") {
        listener = fn;
      }
    },
  };
  const ctx = vm.createContext({
    document,
    $: () => ({ hidden: false }),
    notice: () => {
      notices += 1;
    },
    refresh: () => {
      calls += 1;
      return new Promise((resolve, reject) => {
        inflight = { resolve, reject };
      });
    },
    setTimeout: (fn, delay) => {
      const timer = { fn, delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout: (timer) => {
      if (timer) {
        timer.cleared = true;
      }
    },
  });
  vm.runInContext(block, ctx);
  const live = () => timers.filter((t) => !t.cleared);
  const fire = async () => {
    const timer = live().at(-1);
    timer.cleared = true;
    const pending = timer.fn();
    await new Promise(setImmediate);
    return { pending }; // 包一层：async 函数直接返回 Promise 会等它完成
  };
  const settle = async ({ pending }) => {
    if (outcome === "ok") {
      inflight.resolve();
    } else {
      inflight.reject(new Error("down"));
    }
    await pending;
  };
  assert.deepEqual(
    live().map((t) => t.delay),
    [5000],
    "first poll after 5s",
  );
  let pending = await fire();
  assert.equal(live().length, 0, "no next poll while refresh is in flight");
  await settle(pending);
  assert.deepEqual(
    live().map((t) => t.delay),
    [5000],
  );
  outcome = "fail";
  const delays = [];
  for (let i = 0; i < 6; i += 1) {
    pending = await fire();
    await settle(pending);
    delays.push(live().at(-1).delay);
  }
  assert.deepEqual(
    delays,
    [10000, 20000, 40000, 60000, 60000, 60000],
    "exponential backoff capped at 60s",
  );
  assert.equal(notices, 6, "failures are reported");
  outcome = "ok";
  pending = await fire();
  await settle(pending);
  assert.equal(live().at(-1).delay, 5000, "success resets backoff");
  document.hidden = true;
  listener();
  assert.equal(live().length, 0, "hidden page pauses polling");
  document.hidden = false;
  listener();
  assert.deepEqual(
    live().map((t) => t.delay),
    [0],
    "visible page refreshes immediately",
  );
  assert.equal(calls, 8);
  console.log("persons polling backoff checks passed");
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
