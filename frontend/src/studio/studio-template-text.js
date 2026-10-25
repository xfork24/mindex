/* 创作 · 文案模板的显示文字（创作页与模板管理页共用）。经典脚本，顶层声明共享全局作用域；只依赖 MindexI18n。 */
"use strict";
const TEMPLATE_CATEGORY = {
  general: "ui.template_category_general",
  history: "ui.template_category_history",
  commentary: "ui.template_category_commentary",
  film: "ui.template_category_film",
  science: "ui.template_category_science",
  interview: "ui.template_category_interview"
};
const TEMPLATE_KIND = { outline: "ui.template_kind_outline", rewrite: "ui.template_kind_rewrite" };
const BUILTIN_TEMPLATE_TEXT = {
  base: ["ui.template_name_base", "ui.template_desc_base"],
  history: ["ui.template_name_history", "ui.template_desc_history"],
  hot: ["ui.template_name_hot", "ui.template_desc_hot"],
  film: ["ui.template_name_film", "ui.template_desc_film"],
  science: ["ui.template_name_science", "ui.template_desc_science"],
  interview: ["ui.template_name_interview", "ui.template_desc_interview"]
};
// 内置模板的名称与说明走文案目录（中英双语）；用户模板的名称、分类是用户内容，原样显示。
function templateName(item) {
  const text = item.builtin && BUILTIN_TEMPLATE_TEXT[item.id];
  return text ? MindexI18n.msg(text[0]) : item.name;
}
function templateDescription(item) {
  const text = item.builtin && BUILTIN_TEMPLATE_TEXT[item.id];
  return text ? MindexI18n.msg(text[1]) : item.description || "";
}
function templateCategory(item) {
  return TEMPLATE_CATEGORY[item.category] ? MindexI18n.msg(TEMPLATE_CATEGORY[item.category]) : item.category;
}
function templateKind(kind) {
  return MindexI18n.msg(TEMPLATE_KIND[kind] || TEMPLATE_KIND.outline);
}
function templateLabel(item) {
  return MindexI18n.msg("ui.script_template_option", { p0: templateCategory(item), p1: templateName(item) });
}
// 模板管理页地址：带上要打开的模板与来源任务，便于从管理页回到原任务。
function templatesPageUrl(templateId, jobId) {
  const params = new URLSearchParams();
  if (templateId) params.set("id", templateId);
  if (jobId) params.set("job", jobId);
  const query = params.toString();
  return "/studio/templates" + (query ? `?${query}` : "");
}
