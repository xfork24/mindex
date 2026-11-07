/* 创作页 · 交付：多画幅导出（快速派生 / 重新渲染）与“构建全部画幅”。后续的平台封面、发布元数据与发布包也在这里。
   全部经方案 PATCH 与构建接口（与 CLI studio export、智能助手 set_exports / run_build 同一接口）；Web 与桌面版共用。
   按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
function exportsOf(p) {
  return p.options.exports || [];
}
function finalOf(p, key) {
  return ver((p.finals || {})[key]);
}
function renderDelivery() {
  const box = $("delivery-body");
  if (!box) return;
  const p = plan();
  const key = JSON.stringify([
    scopeKey(),
    p.options.canvas,
    p.options.exports,
    p.options.publish,
    p.options.delivery,
    p.finals,
    p.final,
    state.data.revision,
    Object.values(state.data.plans)
      .filter((item) => item.variant_of?.plan === p.id)
      .map((item) => [item.id, item.final]),
  ]);
  if (state.deliveryKey === key) return;
  state.deliveryKey = key;
  pauseMedia(box);
  box.replaceChildren();
  renderDeliveryConfig(box, p);
  renderDeliveryResults(box, p);
}
// 构建全部画幅：子方案（重新渲染）在前、主方案在后，一次构建完成；预检清单按方案分组列出要重建的节点数。
async function buildAllCanvases() {
  const checked = await api(`${planBase()}/preflight?all_canvases=true`);
  const rebuild = checked.nodes.filter((node) => node.action === "rebuild").length;
  const blocked = checked.nodes.filter((node) => node.action === "blocked").length;
  if (
    !window.confirm(
      MindexI18n.render(
        MindexI18n.msg("ui.delivery_build_all_confirm", { p0: rebuild, p1: blocked }),
      ),
    )
  )
    return;
  await api(`${planBase()}/build`, "POST", { token: checked.token, all_canvases: true });
  await refresh();
}

// ------------------------------------------------------------ 平台发布：平台、封面、发布元数据与发布包
function platformResult(p, id) {
  const card = el("div", "", "publish-result");
  card.dataset.publishPlatform = id;
  card.append(el("h4", p.options.publish?.platform_specs?.[id]?.name || id));
  const cover = finalOf(p, `cover:${id}`),
    meta = finalOf(p, `metadata:${id}`),
    pack = finalOf(p, `package:${id}`);
  if (!meta && !cover && !pack) {
    card.append(el("p", MindexI18n.msg("ui.publish_not_built"), "muted"));
    return card;
  }
  if (cover?.files?.cover?.url) {
    const img = document.createElement("img");
    img.src = cover.files.cover.url;
    img.className = "publish-cover";
    MindexI18n.setAttr(img, "alt", MindexI18n.msg("ui.publish_cover_alt"));
    card.append(img);
  }
  if (meta?.data) {
    const value = {
      title: meta.data.title,
      description: meta.data.body ?? "",
      tags: (meta.data.tags || []).join("，"),
    };
    const title = field(MindexI18n.msg("ui.publish_meta_title"), value.title);
    const description = field(
      MindexI18n.msg("ui.publish_meta_description"),
      value.description,
      "textarea",
    );
    description.input.rows = 4;
    const tags = field(MindexI18n.msg("ui.publish_meta_tags"), value.tags);
    card.append(title.wrap, description.wrap, tags.wrap);
    if (meta.data.chapters?.length)
      card.append(
        el(
          "p",
          MindexI18n.msg("ui.publish_meta_chapters", { p0: meta.data.chapters.length }),
          "muted",
        ),
      );
    if (meta.data.notes?.length)
      card.append(
        el("p", meta.data.notes.map((note) => MindexI18n.system(note)).join("；"), "warn-text"),
      );
    card.append(
      el(
        "p",
        MindexI18n.msg(
          meta.data.origin === "manual" ? "ui.publish_meta_manual" : "ui.publish_meta_generated",
        ),
        "muted",
      ),
    );
    card.append(
      mutationButton(MindexI18n.msg("ui.publish_meta_save"), async () => {
        const body = {
          revision: state.data.revision,
          title: title.input.value.trim(),
          description: description.input.value,
          tags: tags.input.value
            .split(/[,，、\s]+/)
            .map((tag) => tag.trim())
            .filter(Boolean),
        };
        await accept(await api(`${planBase()}/publish-metadata/${id}`, "PATCH", body));
        notice(MindexI18n.msg("ui.publish_meta_saved"));
      }),
    );
  }
  if (pack?.data?.notes?.length)
    card.append(
      el("p", pack.data.notes.map((note) => MindexI18n.system(note)).join("；"), "warn-text"),
    );
  if (pack?.files?.zip?.url) {
    const download = el(
      "a",
      MindexI18n.msg("ui.publish_download", {
        p0: Math.round(((pack.data?.bytes || 0) / 1024 / 1024) * 10) / 10,
      }),
      "quiet nav-link",
    );
    download.href = `${pack.files.zip.url}?download=true`;
    download.setAttribute("download", "");
    card.append(download);
    if (pack.data?.mismatch)
      card.append(el("p", MindexI18n.msg("ui.publish_mismatch"), "warn-text"));
  }
  return card;
}

let deliveryTemplateItems = null;
let deliveryTemplateLoading = false;
function renderDeliveryConfig(box, p) {
  const { node, t, defaults, render } = DeliveryEditor;
  const row = el("div", "", "detail-actions");
  const select = document.createElement("select");
  select.setAttribute("aria-label", MindexI18n.render(t("templates")));
  if (!deliveryTemplateItems && !deliveryTemplateLoading) {
    deliveryTemplateLoading = true;
    Promise.all([api("/api/studio/delivery-templates"), DeliveryEditor.loadPlatforms(true)])
      .then(([data]) => {
        deliveryTemplateItems = data.items;
        state.deliveryKey = null;
        renderDelivery();
      })
      .catch((error) => notice(error.message))
      .finally(() => {
        deliveryTemplateLoading = false;
      });
  }
  for (const item of deliveryTemplateItems || []) {
    const option = document.createElement("option");
    option.value = item.id;
    if (item.builtin) MindexI18n.setText(option, t("base"));
    else option.textContent = item.name;
    select.append(option);
  }
  if (
    p.options.delivery &&
    deliveryTemplateItems &&
    !deliveryTemplateItems.some((item) => item.id === p.options.delivery.template_id)
  ) {
    const option = document.createElement("option");
    option.value = p.options.delivery.template_id;
    option.textContent = p.options.delivery.template_name;
    option.disabled = true;
    select.append(option);
  }
  select.value = p.options.delivery?.template_id || "base";
  const applyButton = mutationButton(t("apply"), async () => {
    const item = deliveryTemplateItems?.find((x) => x.id === select.value);
    if (!item || !confirm(MindexI18n.render(t("overwrite")))) return;
    await accept(
      await api(planBase(), "PATCH", {
        revision: state.data.revision,
        options: {
          ...p.options,
          delivery: { template_id: item.id, template_name: item.name, config: item.config },
        },
      }),
    );
  });
  applyButton.disabled = !deliveryTemplateItems;
  const manage = node("a", "manage", "quiet nav-link");
  manage.href = `/studio/delivery-templates?job=${encodeURIComponent(state.id)}`;
  row.append(select, applyButton, manage);
  box.append(row);
  let cfg;
  if (p.options.delivery) cfg = p.options.delivery.config;
  else {
    cfg = defaults();
    cfg.exports = [
      { canvas: p.options.canvas || "16:9", mode: "derive", fit: p.options.fit || "pad" },
      ...(p.options.exports || []),
    ];
    cfg.publish = p.options.publish || null;
  }
  const details = node("details");
  details.append(node("summary", "snapshot"));
  box.append(details);
  const read = render(details, cfg, {
    candidates: async (platform) => {
      if (!platform) throw new Error(MindexI18n.render(MindexI18n.msg("ui.publish_need_platform")));
      return (await api(`${planBase()}/cover-candidates?platform=${platform}`)).items;
    },
    upload: async (file) => {
      const body = new FormData();
      body.append("file", file);
      body.append("kind", "image");
      body.append("purpose", "source");
      return api(`${planBase()}/query-media`, "POST", body);
    },
  });
  details.append(
    mutationButton(t("save"), async () => {
      const delivery = {
        ...(p.options.delivery || {
          template_id: "base",
          template_name: MindexI18n.render(t("snapshot")),
        }),
        config: read(),
      };
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          options: { ...p.options, delivery },
        }),
      );
      notice(t("saved"));
    }),
  );
  box.append(
    mutationButton(t("build"), () =>
      exportsOf(p).some((item) => item.mode === "rerender") ? buildAllCanvases() : preflight(false),
    ),
  );
}
function renderDeliveryResults(box, p) {
  for (const item of exportsOf(p)) {
    const made = finalOf(p, `export:${item.canvas}`);
    if (!made?.files?.mp4?.url) continue;
    const section = el("div", "", "delivery-result");
    section.append(el("h3", item.canvas), media("video", made.files.mp4.url));
    const download = el("a", MindexI18n.msg("ui.delivery_download"), "quiet nav-link");
    download.href = `${made.files.mp4.url}?download=true`;
    download.setAttribute("download", "");
    section.append(download);
    box.append(section);
  }
  for (const id of p.options.publish?.platforms || []) box.append(platformResult(p, id));
}
