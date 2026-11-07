/* Select paired LLM content for display only. Never translate user text or alter API data. */
(function () {
  "use strict";
  const bindings = new WeakMap();
  const translations = new Map();
  const labels = {
    动作: "ui.d9d9827827e1",
    场景: "ui.625b392c7b8a",
    人物: "ui.b755b68373c8",
    对象: "ui.539cb3b0a662",
    人物情绪: "ui.6e8e0c3eeeac",
    氛围: "ui.6b8f229d5eb9",
    景别: "ui.6867f7b4537a",
    角度: "ui.96ba4a436593",
    镜头运动: "ui.0118461d56e8",
  };
  const current = () => window.MindexI18n.language;
  function select(value, language = current()) {
    const text = String(value ?? "");
    if (language === "en" && translations.has(text)) {
      return translations.get(text);
    }
    // The analysis prompt defines Chinese / English within each string. Ordinary
    // slashes, Chinese alternatives, URLs and old monolingual values stay intact.
    for (const match of text.matchAll(/\s+\/\s+/g)) {
      const zh = text.slice(0, match.index).trim();
      const en = text.slice(match.index + match[0].length).trim();
      if (
        /[\u3400-\u9fff]/.test(zh) &&
        /^[^\p{L}\p{N}]*[A-Za-z0-9]/u.test(en) &&
        !/^https?:\/\//i.test(en)
      ) {
        return language === "en" ? en : zh;
      }
    }
    return text;
  }
  function view(value, language = current()) {
    const sections = [];
    for (const line of String(value ?? "").split(/\r?\n/)) {
      const match = line.match(/^([^：]+)：(.*)$/);
      if (match && labels[match[1]]) {
        sections.push({ label: match[1], value: match[2] });
      } else if (sections.length) {
        sections[sections.length - 1].value += "\n" + line;
      } else {
        sections.push({ value: line });
      }
    }
    return sections
      .map((section) => {
        if (!section.label) {
          return select(section.value, language);
        }
        const key = labels[section.label];
        const label =
          MindexI18n.catalog?.[key]?.[language] ?? MindexI18n.render(MindexI18n.msg(key));
        const values = section.value.split("；").map((item) => select(item.trim(), language));
        return (
          label + (language === "en" ? ": " : "：") + values.join(language === "en" ? "; " : "；")
        );
      })
      .join("\n");
  }
  function update(node) {
    const item = bindings.get(node);
    if (!item) {
      return;
    }
    const language = current();
    const value = Array.isArray(item.value)
      ? item.value.map((text) => select(text, language)).join(language === "en" ? "; " : "；")
      : item.view
        ? view(item.value, language)
        : select(item.value, language);
    const label = item.label
      ? MindexI18n.render(item.label) + (language === "en" ? ": " : "：")
      : "";
    MindexI18n.setText(node, label + value);
  }
  function setText(node, value, options = {}) {
    bindings.set(node, { value, ...options });
    node.setAttribute("data-analysis-text", "");
    update(node);
    return node;
  }
  window.addEventListener("mindex:languagechange", () => {
    document.querySelectorAll("[data-analysis-text]").forEach(update);
  });
  function addTranslations(values) {
    for (const [source, english] of Object.entries(values)) {
      if (typeof english === "string" && english.trim() && !/[\u3400-\u9fff]/.test(english)) {
        translations.set(source, english);
      }
    }
    document.querySelectorAll("[data-analysis-text]").forEach(update);
  }
  window.MindexAnalysisText = { select, view, setText, addTranslations };
})();
