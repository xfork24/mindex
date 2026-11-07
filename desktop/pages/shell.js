"use strict";
// 外壳本地页面：读取查询参数显示状态；按钮把 hash 设为 #command=<命令>，由主进程执行。
(() => {
  const params = new URLSearchParams(location.search);
  const text = (id, value) => {
    const node = document.getElementById(id);
    node.textContent = value || "";
    return node;
  };
  const error = params.get("mode") === "error";
  document.documentElement.lang = params.get("lang") || "zh-CN";
  document.title = text("title", params.get("title")).textContent || "Mindex";
  text("message", params.get("message"));
  document.getElementById("spinner").hidden = error;
  const detail = params.get("detail");
  if (detail) {
    text("detail", detail).hidden = false;
  }
  if (error) {
    document.getElementById("actions").hidden = false;
    for (const id of ["retry", "logs", "quit"]) {
      text(id, params.get(id));
    }
    document.getElementById("retry").focus();
  }
  document.querySelectorAll("[data-command]").forEach((button) => {
    button.addEventListener("click", () => {
      // 先清空再设置，保证重复点击同一按钮也会触发页内导航。
      history.replaceState(null, "", location.pathname + location.search);
      location.hash = `command=${button.dataset.command}`;
    });
  });
})();
