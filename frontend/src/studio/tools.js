/* 创作 · 第三方工具（/studio/tools）：登记 HTTP / MCP 工具源、维护工具白名单与上传授权、填写密钥（只写）、测试连接。
   经典脚本，依赖 shared/*（MindexI18n、MindexApi、MindexFeedback）。全部经 /api/studio/tool-sources
   （与 CLI、智能助手、MCP 同一接口）；Web 与桌面版共用（不使用 window.prompt）。 */
"use strict";
const query = new URLSearchParams(location.search);
const page = { items: [], selected: null, creating: false, entries: [], secrets: {}, job: query.get("job") };
const $ = (id) => document.getElementById(id);
const api = (url, method = "GET", body) => MindexApi.json(url, method, body);
const sourceUrl = (id, suffix = "") => `/api/studio/tool-sources/${encodeURIComponent(id)}${suffix}`;
const TEMPLATES = {
  http: { base_url: "https://example.com", headers: { Authorization: "secret:token" }, submit_path: "/tools/{tool}/jobs",
    poll_path: "/jobs/{id}", poll_interval: 2, id_field: "id", status_field: "status", done: ["succeeded", "completed", "done", "success"],
    failed: ["failed", "error", "cancelled"], result_field: "output.url", error_field: "error", tools_path: null },
  "mcp-http": { url: "https://example.com/mcp", headers: {} },
  "mcp-stdio": { command: "/usr/local/bin/my-mcp-server", args: [], env: {}, cwd: null }
};
const CONNECTION_KEY = { http: "http", "mcp-http": "mcp_http", "mcp-stdio": "stdio" };

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) MindexI18n.setText(node, text);
  if (cls) node.className = cls;
  return node;
}
function notice(text, error = false) {
  const value = error ? MindexI18n.system(text) : text;
  if (globalThis.MindexFeedback) return MindexFeedback.show("notice", value, error ? "error" : "info");
  MindexI18n.setText($("notice"), value);
}
const failed = (error) => notice(error.message, true);
const outputLabel = (output) => MindexI18n.msg(output === "image" ? "ui.invoke_output_image" : "ui.invoke_output_video");

function renderList() {
  const box = $("tool-list");
  box.replaceChildren();
  if (!page.items.length) {
    box.append(el("p", MindexI18n.msg("ui.tools_list_empty"), "muted template-group-empty"));
    return;
  }
  const list = el("ul", null, "template-items");
  for (const item of page.items) {
    const li = el("li");
    const entry = el("button", null, "job-item template-item");
    entry.type = "button";
    entry.dataset.id = item.id;
    if (page.selected?.id === item.id) entry.classList.add("active");
    entry.append(el("div", item.name, "job-title"));
    const parts = [item.protocol, MindexI18n.msg("ui.tools_count", { p0: item.tools.length })];
    if (item.missing_secrets.length) parts.push(MindexI18n.msg("ui.tools_missing_secrets"));
    entry.append(el("div", parts.join(" · "), item.missing_secrets.length ? "job-sub warn-text" : "job-sub"));
    entry.addEventListener("click", () => select(item.id).catch(failed));
    li.append(entry);
    list.append(li);
  }
  box.append(list);
}
function control(label, value, type, onChange, extra = {}) {
  const wrap = el("label", label, extra.check ? "check" : "form-field");
  const input = document.createElement(extra.choices ? "select" : "input");
  if (extra.choices) for (const [v, text] of extra.choices) {
    const option = el("option", text);
    option.value = v;
    input.append(option);
  } else input.type = type;
  if (type === "checkbox") input.checked = Boolean(value);else input.value = value ?? "";
  for (const key of ["min", "max", "step", "maxLength"]) if (extra[key] != null) input[key] = extra[key];
  input.addEventListener(type === "checkbox" || extra.choices ? "change" : "input", () =>
    onChange(type === "checkbox" ? input.checked : type === "number" ? Number(input.value) : input.value));
  wrap.append(input);
  return wrap;
}
function renderEntries() {
  const box = $("tool-entries");
  box.replaceChildren(el("h3", MindexI18n.msg("ui.tools_whitelist")));
  page.entries.forEach((entry, index) => {
    const row = el("div", "", "template-fields");
    row.dataset.toolEntry = String(index);
    const set = (key) => (value) => {entry[key] = value;};
    row.append(
      control(MindexI18n.msg("ui.tools_entry_name"), entry.name, "text", set("name"), { maxLength: 128 }),
      control(MindexI18n.msg("ui.tools_entry_label"), entry.label || "", "text", (v) => {entry.label = v || null;}, { maxLength: 60 }),
      control(MindexI18n.msg("ui.tools_entry_output"), entry.output, "text", set("output"),
      { choices: [["video", outputLabel("video")], ["image", outputLabel("image")]] }),
      control(MindexI18n.msg("ui.tools_entry_timeout"), entry.timeout, "number", set("timeout"), { min: 5, max: 3600 }),
      control(MindexI18n.msg("ui.tools_entry_max_mb"), entry.max_mb, "number", set("max_mb"), { min: 1, max: 2000 }),
      control(MindexI18n.msg("ui.tools_entry_upload"), entry.allow_upload, "checkbox", set("allow_upload"), { check: true }));
    const remove = el("button", MindexI18n.msg("ui.tools_entry_remove"), "quiet");
    remove.type = "button";
    remove.addEventListener("click", () => {page.entries.splice(index, 1);renderEntries();});
    row.append(remove);
    box.append(row);
  });
  const add = el("button", MindexI18n.msg("ui.tools_entry_add"), "quiet");
  add.type = "button";
  add.addEventListener("click", () => {
    page.entries.push({ name: "", label: null, output: "video", allow_upload: false, timeout: 600, max_mb: 500 });
    renderEntries();
  });
  box.append(add, el("p", MindexI18n.msg("ui.tools_whitelist_help"), "muted"));
}
function renderSecrets(item) {
  const box = $("tool-secrets");
  box.replaceChildren(el("h3", MindexI18n.msg("ui.tools_secrets")));
  const known = new Map((item?.secrets || []).map((entry) => [entry.name, entry.set]));
  for (const name of Object.keys(page.secrets)) if (!known.has(name)) known.set(name, false);
  for (const [name, set] of known) {
    const wrap = el("label", MindexI18n.msg(set ? "ui.tools_secret_set" : "ui.tools_secret_unset", { p0: name }), "form-field");
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "off";
    input.dataset.toolSecret = name;
    input.value = page.secrets[name] || "";
    input.addEventListener("input", () => {page.secrets[name] = input.value;});
    wrap.append(input);
    box.append(wrap);
  }
  const nameInput = document.createElement("input");
  nameInput.maxLength = 40;
  MindexI18n.setAttr(nameInput, "placeholder", MindexI18n.msg("ui.tools_secret_name"));
  MindexI18n.setAttr(nameInput, "aria-label", MindexI18n.msg("ui.tools_secret_name"));
  const add = el("button", MindexI18n.msg("ui.tools_secret_add"), "quiet");
  add.type = "button";
  add.addEventListener("click", () => {
    const name = nameInput.value.trim();
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(name)) return notice(MindexI18n.msg("ui.tools_secret_name_invalid"), true);
    page.secrets[name] = page.secrets[name] || "";
    renderSecrets(item);
  });
  const row = el("div", "", "detail-actions");
  row.append(nameInput, add);
  box.append(row, el("p", MindexI18n.msg("ui.tools_secrets_help"), "muted"));
}
function connectionHelp() {
  MindexI18n.setText($("tf-connection-help"), MindexI18n.msg(`ui.tools_connection_help_${$("tf-protocol").value.replace("-", "_")}`));
}
function renderDetail() {
  const item = page.selected;
  const visible = Boolean(item || page.creating);
  $("tool-empty").hidden = visible;
  $("tool-form").hidden = !visible;
  if (!visible) return;
  MindexI18n.setText($("tool-title"), item ? item.name : MindexI18n.msg("ui.tools_new"));
  $("tf-id").value = item?.id || "";
  $("tf-id").disabled = Boolean(item);
  $("tf-name").value = item?.name || "";
  $("tf-protocol").value = item?.protocol || "http";
  $("tf-hosts").value = (item?.artifact_hosts || []).join(", ");
  $("tf-connection").value = JSON.stringify(item?.connection || TEMPLATES.http, null, 2);
  $("tool-delete").hidden = !item;
  $("tool-test").disabled = !item;
  $("tool-error").hidden = true;
  $("tool-test-result").replaceChildren();
  connectionHelp();
  renderEntries();
  renderSecrets(item);
}
async function select(id) {
  page.creating = false;
  page.selected = await api(sourceUrl(id));
  page.entries = JSON.parse(JSON.stringify(page.selected.tools));
  page.secrets = {};
  history.replaceState(null, "", `/studio/tools?id=${encodeURIComponent(id)}${page.job ? `&job=${encodeURIComponent(page.job)}` : ""}`);
  renderList();
  renderDetail();
}
async function load(preferred) {
  page.items = (await api("/api/studio/tool-sources")).items;
  renderList();
  const id = preferred || page.selected?.id;
  if (id && page.items.some((item) => item.id === id)) await select(id);
}
function body() {
  const protocol = $("tf-protocol").value;
  let connection;
  try {
    connection = JSON.parse($("tf-connection").value);
  } catch {
    throw new Error(MindexI18n.msg("ui.tools_connection_invalid"));
  }
  const secrets = Object.fromEntries(Object.entries(page.secrets).filter(([, value]) => value));
  return { name: $("tf-name").value.trim(), protocol, [CONNECTION_KEY[protocol]]: connection,
    tools: page.entries.map((entry) => ({ ...entry, name: entry.name.trim() })),
    artifact_hosts: $("tf-hosts").value.split(/[,，\s]+/).map((host) => host.trim()).filter(Boolean),
    ...(Object.keys(secrets).length ? { secrets } : {}) };
}
async function save(event) {
  event?.preventDefault();
  $("tool-error").hidden = true;
  const id = page.selected?.id || $("tf-id").value.trim();
  try {
    const item = await api(sourceUrl(id), "PUT", body());
    page.creating = false;
    await load(item.id);
    notice(MindexI18n.msg("ui.tools_saved"));
  } catch (error) {
    MindexI18n.setText($("tool-error"), error.message);
    $("tool-error").hidden = false;
  }
}
async function test() {
  const box = $("tool-test-result");
  box.replaceChildren(el("p", MindexI18n.msg("ui.tools_testing")));
  const result = await api(sourceUrl(page.selected.id, "/test"), "POST");
  box.replaceChildren();
  if (!result.ok) return box.append(el("p", MindexI18n.msg("ui.tools_test_failed", { p0: MindexI18n.system(result.error) }), "warn-text"));
  box.append(el("p", MindexI18n.msg("ui.tools_test_ok", { p0: result.elapsed_ms, p1: result.tools ? result.tools.length : "—" })));
  if (result.missing?.length) box.append(el("p", MindexI18n.msg("ui.tools_test_missing", { p0: result.missing.join("、") }), "warn-text"));
  const known = new Set(page.entries.map((entry) => entry.name));
  for (const tool of result.tools || []) {
    if (known.has(tool.name)) continue;
    const row = el("div", "", "detail-actions");
    const add = el("button", MindexI18n.msg("ui.tools_discovered_add", { p0: tool.name }), "quiet");
    add.type = "button";
    add.addEventListener("click", () => {
      page.entries.push({ name: tool.name, label: null, description: tool.description || null, output: "video",
        allow_upload: false, timeout: 600, max_mb: 500 });
      renderEntries();
      row.remove();
    });
    row.append(add, el("span", tool.description || "", "muted"));
    box.append(row);
  }
}
async function remove() {
  if (!window.confirm(MindexI18n.render(MindexI18n.msg("ui.tools_delete_confirm", { p0: page.selected.name })))) return;
  await api(sourceUrl(page.selected.id), "DELETE");
  page.selected = null;
  await load();
  renderDetail();
  notice(MindexI18n.msg("ui.tools_deleted"));
}

$("tool-form").addEventListener("submit", (event) => save(event).catch(failed));
$("tool-test").addEventListener("click", () => test().catch(failed));
$("tool-delete").addEventListener("click", () => remove().catch(failed));
$("tf-protocol").addEventListener("change", () => {
  const protocol = $("tf-protocol").value;
  const current = page.selected?.protocol === protocol ? page.selected.connection : TEMPLATES[protocol];
  $("tf-connection").value = JSON.stringify(current, null, 2);
  connectionHelp();
});
$("tool-new").addEventListener("click", () => {
  page.selected = null;
  page.creating = true;
  page.entries = [{ name: "", label: null, output: "video", allow_upload: false, timeout: 600, max_mb: 500 }];
  page.secrets = {};
  renderList();
  renderDetail();
  $("tf-id").focus();
});
$("reload").addEventListener("click", () => load().then(() => notice(MindexI18n.msg("ui.tools_reloaded"))).catch(failed));
window.addEventListener("mindex:languagechange", () => {renderList();if (page.selected || page.creating) {renderEntries();renderSecrets(page.selected);connectionHelp();}});
window.addEventListener("mindex:agent-mutation", (event) => {
  const detail = event.detail || {};
  if (detail.domain === "studio" && detail.tool_sources) load().catch(() => {});
});
window.MindexAgentContext = () => ({ page: "studio", section: "tools", tool_source_id: page.selected?.id || null });
window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
  if (focus.job_id && !passive) location.href = `/studio?job=${encodeURIComponent(focus.job_id)}`;
  return true;
};
if (page.job) $("studio-jobs-tab").href = `/studio?job=${encodeURIComponent(page.job)}`;
load(query.get("id")).catch(failed);
