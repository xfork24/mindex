/* 素材管理 · 音乐库（/admin/music）：导入带授权信息的音乐、查看节奏分析、修改授权与情绪标签、删除。
   经典脚本，依赖 shared/*（MindexI18n、MindexApi、MindexFeedback）。全部经 /api/studio/music-library
   （与 CLI、智能助手、MCP 同一接口）；Web 与桌面版共用（不使用 window.prompt）。 */
"use strict";
const query = new URLSearchParams(location.search);
const page = { items: [], moods: [], selected: null };
const $ = (id) => document.getElementById(id);
const api = (url, method = "GET", body) => MindexApi.json(url, method, body);
// 组筛选与素材页/检索页同款：可搜索多选列表框，默认全选（=不限），部分选择后过滤左侧曲目列表。
const musicGroups = MindexGroups.picker("music", [], () => load(), "ui.groups_music_filter", {
  listbox: true,
  defaultAll: true,
});
$("music-group-filter").append(musicGroups.wrap);
const trackUrl = (id, suffix = "") =>
  `/api/studio/music-library/${encodeURIComponent(id)}${suffix}`;
const MAX_MOODS = 3;

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) MindexI18n.setText(node, text);
  if (cls) node.className = cls;
  return node;
}
function notice(text, error = false) {
  const value = error ? MindexI18n.system(text) : text;
  if (globalThis.MindexFeedback)
    return MindexFeedback.show("notice", value, error ? "error" : "info");
  MindexI18n.setText($("notice"), value);
}
const failed = (error) => notice(error.message, true);
const moodLabel = (key) => MindexI18n.msg(`ui.music_mood_${key}`);
const seconds = (value) => (value == null ? "—" : `${Number(value).toFixed(1)}s`);

// ------------------------------------------------------------ 列表
function renderMoodFilter() {
  const select = $("music-mood");
  const current = select.value;
  select.replaceChildren();
  const all = el("option", MindexI18n.msg("ui.music_mood_all"));
  all.value = "";
  select.append(all);
  for (const mood of page.moods) {
    const option = el("option", moodLabel(mood.key));
    option.value = mood.key;
    select.append(option);
  }
  select.value = current;
}
function renderList() {
  const box = $("music-list");
  box.replaceChildren();
  if (!page.items.length) {
    box.append(el("p", MindexI18n.msg("ui.music_list_empty"), "muted template-group-empty"));
    return;
  }
  const list = el("ul", null, "template-items");
  for (const item of page.items) {
    const li = el("li");
    const entry = el("button", null, "job-item template-item");
    entry.type = "button";
    entry.dataset.id = item.id;
    if (page.selected?.id === item.id) entry.classList.add("active");
    entry.append(el("div", item.title, "job-title"));
    const parts = [
      seconds(item.duration),
      item.bpm
        ? `${Math.round(item.bpm)} BPM`
        : MindexI18n.msg(`ui.music_status_${item.analysis_status}`),
      (item.moods || []).map(moodLabel).join("、"),
    ].filter(Boolean);
    if (item.expired) parts.push(MindexI18n.msg("ui.music_library_expired"));
    entry.append(el("div", parts.join(" · "), item.expired ? "job-sub warn-text" : "job-sub"));
    entry.addEventListener("click", () => select(item.id).catch(failed));
    li.append(entry);
    list.append(li);
  }
  box.append(list);
}
async function load(preferred) {
  const params = new URLSearchParams();
  if ($("music-search").value.trim()) params.set("q", $("music-search").value.trim());
  if ($("music-mood").value) params.set("mood", $("music-mood").value);
  for (const id of musicGroups.values()) params.append("music_group_ids", id);
  const result = await api(`/api/studio/music-library${params.size ? `?${params}` : ""}`);
  page.items = result.items;
  page.moods = result.moods;
  renderMoodFilter();
  renderList();
  const id = preferred || page.selected?.id;
  if (id)
    await select(id).catch(() => {
      page.selected = null;
      renderDetail();
    });
}

// ------------------------------------------------------------ 详情
function renderMoods() {
  const box = $("music-moods");
  box.querySelectorAll("label").forEach((node) => node.remove());
  const chosen = new Set(page.selected.moods || []);
  for (const mood of page.moods) {
    const wrap = el("label", "", "check");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = mood.key;
    input.checked = chosen.has(mood.key);
    input.addEventListener("change", () => {
      const checked = [...box.querySelectorAll("input:checked")];
      if (checked.length > MAX_MOODS) {
        input.checked = false;
        notice(MindexI18n.msg("ui.music_moods_limit"), true);
      }
    });
    wrap.append(input, el("span", moodLabel(mood.key)));
    box.append(wrap);
  }
}
function renderDetail() {
  const item = page.selected;
  $("music-empty").hidden = Boolean(item);
  $("music-view").hidden = !item;
  if (!item) return;
  MindexGroups.membership($("music-view"), "music", item);
  MindexI18n.setText($("music-title"), item.title);
  MindexI18n.setText(
    $("music-tags"),
    item.expired
      ? MindexI18n.msg("ui.music_library_expired")
      : item.expires_at
        ? MindexI18n.msg("ui.music_expires_on", { p0: item.expires_at })
        : MindexI18n.msg("ui.music_no_expiry"),
  );
  $("music-audio").src = trackUrl(item.id, "/file");
  $("music-download").href = trackUrl(item.id, "/file?download=1");
  const status = MindexI18n.msg(`ui.music_status_${item.analysis_status}`);
  MindexI18n.setText(
    $("music-analysis"),
    item.analysis_status === "ready"
      ? MindexI18n.msg("ui.music_analysis_ready", {
          p0: item.bpm,
          p1: item.beat_count,
          p2: item.energy,
          p3: item.brightness,
          p4: seconds(item.duration),
        }) + (item.stale ? ` · ${MindexI18n.msg("ui.music_analysis_stale")}` : "")
      : `${status}${item.analysis_error ? ` · ${MindexI18n.system(item.analysis_error)}` : ""}`,
  );
  $("mf-title").value = item.title;
  $("mf-artist").value = item.artist || "";
  $("mf-source").value = item.source;
  $("mf-license").value = item.license;
  $("mf-attribution").value = item.attribution;
  $("mf-expires").value = item.expires_at || "";
  $("mf-tags").value = (item.tags || []).join(", ");
  renderMoods();
  MindexI18n.setText(
    $("music-meta"),
    MindexI18n.msg("ui.music_meta", { p0: item.id.slice(0, 12), p1: item.created_at }),
  );
}
async function select(id) {
  page.selected = await api(trackUrl(id));
  history.replaceState(null, "", `/admin/music?id=${encodeURIComponent(id)}`);
  renderList();
  renderDetail();
}
async function save(event) {
  event?.preventDefault();
  if (!page.selected) return;
  $("music-error").hidden = true;
  const body = {
    title: $("mf-title").value.trim(),
    artist: $("mf-artist").value.trim(),
    source: $("mf-source").value.trim(),
    license: $("mf-license").value.trim(),
    attribution: $("mf-attribution").value.trim(),
    expires_at: $("mf-expires").value || null,
    tags: $("mf-tags").value,
    moods: [...$("music-moods").querySelectorAll("input:checked")].map((input) => input.value),
  };
  try {
    page.selected = await api(trackUrl(page.selected.id), "PUT", body);
  } catch (error) {
    MindexI18n.setText($("music-error"), error.message);
    $("music-error").hidden = false;
    return;
  }
  await load(page.selected.id);
  notice(MindexI18n.msg("ui.music_saved_track"));
}
async function analyze() {
  if (!page.selected) return;
  $("music-analyze").disabled = true;
  notice(MindexI18n.msg("ui.music_analyzing"));
  try {
    const item = await api(trackUrl(page.selected.id, "/analyze"), "POST", {
      ai: true,
      retag: false,
    });
    await load(item.id);
    notice(MindexI18n.msg("ui.music_analyzed", { p0: item.bpm }));
  } finally {
    $("music-analyze").disabled = false;
  }
}
async function remove() {
  if (
    !page.selected ||
    !window.confirm(
      MindexI18n.render(MindexI18n.msg("ui.music_delete_confirm", { p0: page.selected.title })),
    )
  )
    return;
  await api(trackUrl(page.selected.id), "DELETE");
  page.selected = null;
  await load();
  renderDetail();
  notice(MindexI18n.msg("ui.music_deleted"));
}
async function importTrack(event) {
  event.preventDefault();
  $("music-import-error").hidden = true;
  const file = $("mi-file").files?.[0];
  const required = ["mi-source", "mi-license", "mi-attribution"].filter(
    (id) => !$(id).value.trim(),
  );
  if (!file || required.length) {
    MindexI18n.setText($("music-import-error"), MindexI18n.msg("ui.music_import_required"));
    $("music-import-error").hidden = false;
    return;
  }
  const form = new FormData();
  form.append("file", file, file.name);
  for (const [key, id] of [
    ["title", "mi-title"],
    ["artist", "mi-artist"],
    ["source", "mi-source"],
    ["license", "mi-license"],
    ["attribution", "mi-attribution"],
    ["expires_at", "mi-expires"],
    ["tags", "mi-tags"],
  ])
    form.append(key, $(id).value.trim());
  $("mi-submit").disabled = true;
  notice(MindexI18n.msg("ui.music_importing"));
  try {
    const item = await api("/api/studio/music-library", "POST", form);
    $("music-import").reset();
    await load(item.id);
    notice(
      MindexI18n.msg(item.duplicate ? "ui.music_import_duplicate" : "ui.music_imported", {
        p0: item.title,
      }),
    );
  } catch (error) {
    MindexI18n.setText($("music-import-error"), error.message);
    $("music-import-error").hidden = false;
  } finally {
    $("mi-submit").disabled = false;
  }
}

$("music-import").addEventListener("submit", (event) => importTrack(event).catch(failed));
$("music-form").addEventListener("submit", (event) => save(event).catch(failed));
$("music-analyze").addEventListener("click", () => analyze().catch(failed));
$("music-delete").addEventListener("click", () => remove().catch(failed));
$("music-mood").addEventListener("change", () => load().catch(failed));
let searchTimer = null;
$("music-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => load().catch(failed), 250);
});
$("reload").addEventListener("click", () =>
  load()
    .then(() => notice(MindexI18n.msg("ui.music_reloaded")))
    .catch(failed),
);
window.addEventListener("mindex:languagechange", () => {
  renderMoodFilter();
  renderList();
  if (page.selected) renderDetail();
});
window.addEventListener("mindex:groups-changed", () => {
  void musicGroups.refresh();
});
window.addEventListener("mindex:agent-mutation", (event) => {
  const detail = event.detail || {};
  if (detail.groups || (detail.domain === "studio" && detail.music_library)) {
    load().catch(() => {});
    void musicGroups.refresh();
  }
});
window.MindexAgentContext = () => ({
  page: "music",
  track_id: page.selected?.id || null,
  music_group_ids: musicGroups.values(),
});
window.MindexAgentFocus = async (focus) => {
  if (focus.music_group_ids) {
    musicGroups.setValues(focus.music_group_ids);
    await load();
  }
  window.MindexAgent?.highlight($("music-list"));
  return true;
};
load(query.get("id")).catch(failed);
