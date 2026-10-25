const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = require("./studio_source.cjs").studioSource();
const calls = [];
let releaseList, releaseArtifacts;
const listResponse = new Promise(resolve => { releaseList = resolve; });
const artifactsResponse = new Promise(resolve => { releaseArtifacts = resolve; });
const nodes = {"job-list": {replaceChildren() {}, append() {}}};
const sandbox = {
  URLSearchParams, Promise, Map, Date,
  location: {search: "?job=selected"}, history: {replaceState() {}},
  state: {token: 0}, clearTimeout() {}, closeDialog() {},
  $: id => nodes[id], MindexI18n: {date() {}, msg() {}},
  button() { return {append() {}}; }, el() { return {}; },
  fail(error) { throw error; }, notice() {},
  async api(url) { calls.push(url); return listResponse; },
  async refresh() { calls.push("workspace"); },
  async loadArtifacts() { calls.push("artifacts"); return artifactsResponse; },
  async loadRecallOptions() { calls.push("options"); },
  async loadSources() { calls.push("sources"); },
};
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf("async function loadJobs("), source.indexOf("async function loadRecallOptions(")), sandbox);
vm.runInContext(source.slice(source.indexOf("async function boot("), source.indexOf("boot().catch(fail)")), sandbox);
(async () => {
  const boot = sandbox.boot();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, ["sources", "/api/studio/jobs", "options", "workspace", "artifacts"]);
  assert.equal(sandbox.state.id, "selected");
  releaseList({items: [{id: "selected", title: "Selected", created_at: "2026-10-18"}]});
  await boot; // Artifacts remain pending and cannot block the main workspace.
  assert.equal(calls.filter(call => call === "/api/studio/jobs").length, 1);
  releaseArtifacts();
  console.log("Direct links load immediately; lists and artifacts do not block the workspace.");
})().catch(error => { console.error(error); process.exitCode = 1; });
