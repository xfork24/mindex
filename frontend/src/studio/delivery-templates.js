"use strict";
(() => {
  const {t, node, render} = DeliveryEditor;
  const $ = id => document.getElementById(id);
  const api = (url, method = "GET", body) => MindexApi.json(url, method, body);
  const base = "/api/studio/delivery-templates";
  const job = new URLSearchParams(location.search).get("job");
  if (job && $("studio-jobs-tab")) $("studio-jobs-tab").href = `/studio?job=${encodeURIComponent(job)}`;
  let items = [], selected, read;
  const report = error => {$("notice").textContent = error.message;};
  function select(item) {
    selected = item; $("delivery-template-name").value = item.name;
    $("delivery-template-name").disabled = item.builtin;
    $("delivery-template-help").replaceChildren(item.builtin ? node("span", "readonly") : node("span"));
    $("delivery-template-save").disabled = item.builtin; $("delivery-template-delete").disabled = item.builtin;
    $("delivery-template-editor").replaceChildren();
    read = render($("delivery-template-editor"), item.config, {readonly: item.builtin, upload: async file => {
      const body = new FormData(); body.append("file", file);
      const asset = await api("/api/studio/brand-assets", "POST", body);
      return {asset: Object.fromEntries(["sha256", "suffix", "kind", "name"].map(k => [k, asset[k]]))};
    }});
    for (const button of $("delivery-template-list").children) button.setAttribute("aria-current", String(button.dataset.id === item.id));
  }
  async function load(id = selected?.id) {
    await DeliveryEditor.loadPlatforms(true);
    items = (await api(base)).items; $("delivery-template-list").replaceChildren();
    for (const item of items) {const button = node("button", null, "quiet"); button.type = "button"; if (item.builtin) MindexI18n.setText(button, t("base")); else button.textContent = item.name; button.dataset.id = item.id; button.onclick = () => select(item); $("delivery-template-list").append(button);}
    select(items.find(x=>x.id === id) || items[0]);
  }
  function labels() {
    document.title = `Mindex · ${MindexI18n.render(t("templates"))}`;
    for (const [id,key] of Object.entries({"delivery-title":"templates", "delivery-intro":"intro", "name-label":"name", "delivery-template-copy":"copy", "delivery-template-delete":"remove", "delivery-template-save":"save"})) MindexI18n.setText($(id),t(key));
  }
  $("delivery-template-form").onsubmit = async event => {event.preventDefault(); try {const item = await api(`${base}/${selected.id}`, "PUT", {name: $("delivery-template-name").value, config: read()}); await load(item.id); $("notice").replaceChildren(node("span", "saved"));} catch(error) {report(error);}};
  $("delivery-template-copy").onclick = async () => {const name = prompt(MindexI18n.render(t("copy_name")), selected.name + " copy"); if (!name?.trim()) return; try {const item = await api(`${base}/${selected.id}/copy`, "POST", {name: name.trim()}); await load(item.id);} catch(error) {report(error);}};
  $("delivery-template-delete").onclick = async () => {if (!confirm(MindexI18n.render(t("delete_confirm")))) return; try {await api(`${base}/${selected.id}`, "DELETE"); await load();} catch(error) {report(error);}};
  $("reload").onclick = () => load().catch(report);
  window.addEventListener("mindex:languagechange", labels);
  DeliveryPlatforms.mount($("delivery-platform-management"), async () => {
    const pending = selected && !selected.builtin ? {...selected, name: $("delivery-template-name").value, config: read()} : null;
    await load();
    if (pending) select(pending);
  });
  labels(); load().catch(report);
})();
