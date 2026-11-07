// Shared sidebar control for the material library and creation workspace.
for (const button of document.querySelectorAll("[data-sidebar-toggle]")) {
  const panel = document.getElementById(button.getAttribute("aria-controls"));
  const workspace = button.closest(".workspace");
  const storageKey = `mindex.sidebar.${button.dataset.sidebarToggle}`;
  function setCollapsed(collapsed) {
    panel.hidden = collapsed;
    workspace.classList.toggle("sidebar-collapsed", collapsed);
    button.setAttribute("aria-expanded", String(!collapsed));
    const label = MindexI18n.msg(collapsed ? "ui.sidebar_expand" : "ui.sidebar_collapse");
    MindexI18n.setAttr(button, "aria-label", label);
    MindexI18n.setAttr(button, "title", label);
  }
  let collapsed = false;
  try {
    collapsed = localStorage.getItem(storageKey) === "collapsed";
  } catch {}
  setCollapsed(collapsed);
  button.addEventListener("click", () => {
    collapsed = !collapsed;
    setCollapsed(collapsed);
    try {
      localStorage.setItem(storageKey, collapsed ? "collapsed" : "expanded");
    } catch {}
    // The material list sizes itself from its current position in the viewport.
    window.dispatchEvent(new Event("resize"));
  });
}
