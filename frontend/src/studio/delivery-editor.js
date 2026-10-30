/* Shared delivery configuration form for templates and independent plan snapshots. */
"use strict";
window.DeliveryEditor = (() => {
  let catalogue = [];
  async function loadPlatforms(force = false) {
    if (!catalogue.length || force) catalogue = (await MindexApi.json("/api/studio/platforms")).items;
    return catalogue;
  }
  const t = key => MindexI18n.msg(`ui.dt_${key}`);
  function node(tag, key, cls) {
    const n = document.createElement(tag);
    if (key) MindexI18n.setText(n, t(key));
    if (cls) n.className = cls;
    return n;
  }
  function defaults() { return {exports: ["16:9", "9:16", "1:1"].map(canvas => ({canvas, mode: "derive", fit: "crop"})), publish: null, cover_asset: null, rules: {}, package: {cover: true, srt: true, vtt: true, metadata_json: true, metadata_txt: true, filename: "{job_id}-{platform}"}, checks: {canvas: "warn", cover: "warn", metadata: "warn"}}; }
  function render(host, input, {readonly = false, upload, candidates} = {}) {
    const cfg = structuredClone(input);
    const wrapper = node("fieldset", null, "delivery-config-fields"); wrapper.disabled = readonly; host.append(wrapper);
    function section(key) { const box = node("section", null, "delivery-exports delivery-fields"); box.append(node("h3", key)); wrapper.append(box); return box; }
    function field(box, key, value, change, choices, type = "text") {
      const label = node("label", null, `field ${type === "checkbox" ? "delivery-check" : type === "textarea" ? "delivery-long" : ""}`); label.append(node("span", key));
      const control = document.createElement(choices ? "select" : type === "textarea" ? "textarea" : "input");
      if (choices) for (const [val, title] of choices) { const opt = node("option", title); opt.value = val; control.append(opt); }
      else if (type !== "textarea") control.type = type;
      if (type === "checkbox") control.checked = !!value; else control.value = value ?? "";
      control.addEventListener(type === "checkbox" || choices ? "change" : "input", () => change(type === "checkbox" ? control.checked : control.value));
      label.append(control); box.append(label); return control;
    }
    const exportsBox = section("exports");
    const strategies = [["crop", "crop"], ["pad", "pad"], ["rerender", "rerender"]];
    const strategy = field(exportsBox, "strategy", "", value => {
      cfg.exports = ["16:9", "9:16", "1:1"].map(canvas => ({canvas, mode: value === "rerender" ? "rerender" : "derive", fit: value === "pad" ? "pad" : "crop"}));
      refreshExports();
    }, [["", "strategy"], ...strategies]);
    strategy.setAttribute("aria-label", MindexI18n.render(t("strategy")));
    const rows = node("div", null, "delivery-export-grid"); exportsBox.append(rows);
    function refreshExports() {
      rows.replaceChildren();
      for (const canvas of ["16:9", "9:16", "1:1"]) {
        const row = node("div", null, "delivery-export-card"); const heading = node("strong"); heading.textContent = canvas; row.append(heading); rows.append(row);
        let item = cfg.exports.find(x => x.canvas === canvas);
        const val = item ? item.mode === "rerender" ? "rerender" : item.fit : "";
        field(row, "strategy", val, value => { cfg.exports = cfg.exports.filter(x => x.canvas !== canvas); if (value) cfg.exports.push({canvas, mode: value === "rerender" ? "rerender" : "derive", fit: value === "pad" ? "pad" : "crop"}); refreshExports(); }, [["", "off"], ...strategies]);
        if (item?.mode === "rerender") field(row, "fit", item.fit, value => {item.fit = value;}, [["crop", "fit_crop"], ["pad", "fit_pad"]]);
      }
    }
    refreshExports();
    const pub = cfg.publish || {platforms: [], cover: {source: "frame", text: true}};
    pub.platform_specs ||= {};
    const platforms = section("platforms");
    const rules = node("section", null, "delivery-exports delivery-fields");
    function drawRules() {
      rules.replaceChildren(node("h3", "rules"));
      for (const id of pub.platforms) {
        const item = pub.platform_specs[id] || catalogue.find(x=>x.id === id);
        const details = node("details", null, "delivery-rule delivery-fields");
        const summary = node("summary"); summary.textContent = item?.name || id; details.append(summary); rules.append(details);
        const rule = cfg.rules[id] ||= {title_instructions: "", description_instructions: "", fixed_tags: [], footer: ""};
        for (const key of ["title_instructions", "description_instructions", "fixed_tags", "footer"]) field(details, key, key === "fixed_tags" ? (rule[key] || []).join(", ") : rule[key], value => {rule[key] = key === "fixed_tags" ? value.split(/[,，、\n]+/).map(x=>x.trim()).filter(Boolean) : value;}, null, "textarea");
      }
    }
    function drawPlatforms() {
      platforms.replaceChildren(node("h3", "platforms"));
      const ids = [...new Set([...catalogue.map(x=>x.id), ...pub.platforms])];
      for (const id of ids) {
        const live = catalogue.find(x=>x.id === id);
        const stored = pub.platform_specs[id];
        const group = node("div", null, "delivery-platform-choice"); platforms.append(group);
        const checkbox = field(group, null, pub.platforms.includes(id), checked => {
          pub.platforms = pub.platforms.filter(x=>x !== id);
          if (checked && live) {pub.platforms.push(id); pub.platform_specs[id] = structuredClone(live);}
          else delete pub.platform_specs[id];
          cfg.publish = pub.platforms.length ? pub : null;
          drawPlatforms(); drawRules();
        }, null, "checkbox");
        checkbox.parentNode.firstChild.textContent = stored?.name || live?.name || id;
        if (!live) group.append(node("span", "platform_deleted", "muted"));
        if (live && pub.platforms.includes(id)) {
          const update = node("button", "update_platform", "quiet"); update.type = "button";
          update.disabled = readonly || JSON.stringify(live) === JSON.stringify(stored);
          update.onclick = () => {pub.platform_specs[id] = structuredClone(live); drawPlatforms(); drawRules();}; group.append(update);
        }
      }
    }
    drawPlatforms(); drawRules();
    const coverBox = section("cover"), cover = pub.cover;
    function coverField(key, type = "text") {return field(coverBox, key, cover[key], value => {cover[key] = value || null;}, null, type);}
    const source = field(coverBox, "source", cover.source, value => {cover.source = value;}, ["frame", "aigc", "upload"].map(x => [x,x]));
    const time = field(coverBox, "time", cover.time, value => {cover.time = value === "" ? null : Number(value);}, null, "number"); time.min = "0"; time.step = "0.1";
    if (candidates) {
      const pick = document.createElement("button"); pick.type = "button"; pick.className = "quiet";
      MindexI18n.setText(pick, MindexI18n.msg("ui.publish_cover_candidates"));
      const gallery = node("div", null, "cover-candidates");
      pick.addEventListener("click", async () => {
        pick.disabled = true;
        try {
          const values = await candidates(pub.platforms[0]); gallery.replaceChildren();
          for (const item of values) {
            const choice = document.createElement("button"); choice.type = "button"; choice.className = "quiet cover-candidate";
            const img = document.createElement("img"); img.src = item.url;
            MindexI18n.setAttr(img, "alt", MindexI18n.msg("ui.publish_cover_at", {p0: item.time}));
            const caption = document.createElement("span"); caption.textContent = `${item.time}s`; choice.append(img, caption);
            choice.onclick = () => {cover.source = "frame"; cover.time = item.time; source.value = "frame"; time.value = item.time;}; gallery.append(choice);
          }
        } catch (error) {gallery.textContent = error.message;} finally {pick.disabled = readonly;}
      });
      coverBox.append(pick, gallery);
    }
    coverField("prompt", "textarea").maxLength = 1500; coverField("title").maxLength = 40; coverField("subtitle").maxLength = 60;
    field(coverBox, "text", cover.text, value => {cover.text = value;}, null, "checkbox");
    const imageInfo = node("p", cfg.cover_asset || cover.media_id ? "image_ready" : null, "muted"); coverBox.append(imageInfo);
    if (upload) {
      const image = field(coverBox, "image", "", () => {}, null, "file"); image.accept = "image/png,image/jpeg,image/webp";
      image.addEventListener("change", async () => {
        if (!image.files[0]) return;
        image.disabled = true;
        try { const result = await upload(image.files[0]); cfg.cover_asset = result.asset || null; cover.media_id = result.media_id || null; cover.source = "upload"; source.value = "upload"; MindexI18n.setText(imageInfo, t("image_ready")); }
        catch (error) { imageInfo.textContent = error.message; } finally { image.disabled = readonly; }
      });
      const clear = node("button", "clear_image", "quiet"); clear.type = "button"; clear.onclick = () => {cfg.cover_asset = null; cover.media_id = null; image.value = ""; imageInfo.textContent = "";}; coverBox.append(clear);
    }
    wrapper.append(rules);
    const pack = section("package"); pack.append(node("p", "package_help", "muted"));
    for (const key of ["cover", "srt", "vtt", "metadata_json", "metadata_txt"]) {
      const label = key === "srt" || key === "vtt" ? null : key;
      const control = field(pack, label, cfg.package[key], value => {cfg.package[key] = value;}, null, "checkbox");
      if (!label) control.parentNode.firstChild.textContent = key.toUpperCase();
    }
    field(pack, "filename", cfg.package.filename, value => {cfg.package.filename = value;}); pack.append(node("p", "filename_help", "muted"));
    const checks = section("checks");
    for (const key of ["canvas", "cover", "metadata"]) field(checks, `check_${key}`, cfg.checks[key], value => {cfg.checks[key] = value;}, [["warn", "warn"], ["block", "block"]]);
    return () => {cfg.publish = pub.platforms.length ? pub : null; return structuredClone(cfg);};
  }
  return {render, defaults, t, node, loadPlatforms};
})();
