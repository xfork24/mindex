const { between } = require("./source_slices.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const search = fs.readFileSync("frontend/src/search/app.js", "utf8");
const people = fs.readFileSync("frontend/src/persons/persons.js", "utf8");
(async () => {
  let statusResolve;
  let filtersResolve;
  let populated = false;
  const requests = [];
  const ctx = vm.createContext({
    $: () => ({ disabled: false, replaceChildren() {}, append() {} }),
    api: (url) => {
      requests.push(url);
      return new Promise((resolve) => {
        if (url.includes("/filters")) {
          filtersResolve = resolve;
        } else {
          statusResolve = resolve;
        }
      });
    },
    fillOptions() {},
    searchGroupLink: {
      async refresh() {
        populated = true;
      },
    },
    element() {},
    notice() {},
    MindexI18n: { msg: (x) => x, setText() {}, system: () => "" },
  });
  vm.runInContext(
    "let registeredPeople=[],registeredSources=[],sourceScope=null;" +
      between(search, "async function refresh()", '$("refresh").onclick'),
    ctx,
  );
  const pending = vm.runInContext("refresh()", ctx);
  assert.equal(requests.length, 2, "start both requests immediately");
  filtersResolve({ entities: [{ id: "actor" }], sources: [{ id: "video" }] });
  await tick();
  assert.equal(populated, true, "filters populate before model health finishes");
  assert.equal(vm.runInContext("registeredSources[0].id", ctx), "video");
  statusResolve({ sources: [], segment_counts: [], runs: [], model_health: { ready: true } });
  await pending;

  let catalogueResolve;
  let galleryStarted = false;
  const personCtx = vm.createContext({
    api: () =>
      new Promise((resolve) => {
        catalogueResolve = resolve;
      }),
    loadImportGallery: () => {
      galleryStarted = true;
      return Promise.resolve();
    },
    notice() {},
    fillSelectors() {},
    renderCatalogue() {},
    renderTasks() {},
    entityFields() {},
    $: () => ({ replaceChildren() {} }),
  });
  vm.runInContext(
    "const state={};" + between(people, "async function refresh()", "// 从原片选取参考"),
    personCtx,
  );
  const personPending = vm.runInContext("refresh()", personCtx);
  assert.equal(galleryStarted, true, "gallery does not wait for full catalogue");
  catalogueResolve({ entities: [], references: [], sources: [], releases: [] });
  await personPending;
  console.log("Search filters and gallery load independently of slow unrelated requests");
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
