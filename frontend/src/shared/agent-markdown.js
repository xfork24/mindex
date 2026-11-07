"use strict";

// 智能助手回复的 Markdown 渲染：marked 解析 → DOMPurify 白名单净化 → 写入目标元素。
// 这是助手面板唯一写入 HTML 的地方，且只写入净化后的结果；依赖未加载时回退为纯文本。
(() => {
  const TAGS = [
    "p",
    "br",
    "strong",
    "em",
    "del",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "a",
    "img",
  ];
  const SAFE_URL = /^(?:https?:|\/(?!\/)|#)/i;
  // 图片只放行本站 /api/ 地址：外链图片会泄露浏览信息，也可能被用作追踪像素。
  const SAFE_IMAGE = /^\/api\/[^\s]*$/;
  let configured = false;

  function configure() {
    if (configured || !window.DOMPurify) {
      return;
    }
    configured = true;
    // 链接：外部地址新窗口打开并切断 opener；站内相对地址保持同窗口。其余协议的 href 直接去掉。
    window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "IMG") {
        const src = node.getAttribute("src") || "";
        if (!SAFE_IMAGE.test(src) || src.includes("//") || src.includes("..")) {
          node.remove();
          return;
        }
        node.setAttribute("loading", "lazy");
        return;
      }
      if (node.tagName !== "A") {
        return;
      }
      const href = node.getAttribute("href") || "";
      if (!SAFE_URL.test(href)) {
        node.removeAttribute("href");
        return;
      }
      if (/^https?:/i.test(href)) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
  }

  function html(text) {
    if (!window.marked || !window.DOMPurify) {
      return null;
    }
    configure();
    const raw = window.marked.parse(String(text ?? ""), { gfm: true, breaks: true, async: false });
    return window.DOMPurify.sanitize(raw, {
      ALLOWED_TAGS: TAGS,
      ALLOWED_ATTR: ["href", "title", "align", "target", "rel", "src", "alt", "loading"],
      ALLOWED_URI_REGEXP: SAFE_URL,
    });
  }

  function render(target, text) {
    const clean = html(text);
    target.classList.toggle("agent-markdown", clean !== null);
    if (clean === null) {
      target.textContent = String(text ?? "");
      return target;
    }
    target.innerHTML = clean;
    return target;
  }

  window.MindexMarkdown = { render, html };
})();
