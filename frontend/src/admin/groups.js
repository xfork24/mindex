(function () {
  "use strict";
  const $ = (id) => document.getElementById(id),
    api = window.MindexApi.json;
  let selected = null,
    systemGroup = false,
    page = 1,
    total = 0,
    detailRequest = 0,
    libraryRequest = 0;
  const kind = () => $("groups-kind").value;
  const text = (id, key, params) => MindexI18n.setText($(id), MindexI18n.msg(key, params));
  const run = (action) => async (event) => {
    event?.preventDefault();
    MindexI18n.setText($("groups-error"), "");
    try {
      await action();
    } catch (error) {
      MindexI18n.setText($("groups-error"), MindexI18n.system(error.message));
    }
  };
  MindexMultiSearch.attach($("groups-results"));
  const query = $("groups-results").closest(".multi-select").querySelector(".multi-search");
  query.id = "groups-query";
  function actions() {
    $("groups-delete").disabled = !selected || systemGroup;
    $("groups-name").disabled =
      $("groups-description").disabled =
      $("groups-form").querySelector("[type=submit]").disabled =
        systemGroup;
    $("groups-add").disabled =
      !selected ||
      systemGroup ||
      $("groups-results").disabled ||
      !$("groups-results").selectedOptions.length;
    $("groups-remove").disabled =
      !selected || systemGroup || !$("groups-members").querySelector("input:checked");
    const visible = [...$("groups-results").options].filter((option) => !option.hidden).length;
    text("groups-library-count", "ui.groups_library_count", {
      p0: visible,
      p1: $("groups-results").selectedOptions.length,
    });
  }
  function markSelected() {
    for (const button of $("groups-list").querySelectorAll("button")) {
      button.classList.toggle("selected", button.dataset.groupId === selected);
      button.setAttribute("aria-pressed", String(button.dataset.groupId === selected));
    }
    actions();
  }
  function showMembers(rows) {
    $("groups-members").replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      MindexI18n.setText(
        empty,
        MindexI18n.msg(selected ? "ui.groups_members_empty" : "ui.groups_select_first"),
      );
      $("groups-members").append(empty);
    }
    for (const item of rows) {
      const label = document.createElement("label");
      label.className = "group-member";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = item.id;
      input.addEventListener("change", actions);
      const name = document.createElement("span");
      name.textContent = item.name || item.title || item.id;
      label.append(input, name);
      $("groups-members").append(label);
    }
    actions();
  }
  async function detail(id) {
    const request = ++detailRequest,
      currentKind = kind();
    selected = id;
    markSelected();
    let group = await api(`/api/admin/groups/${id}?page=${page}`);
    if (request !== detailRequest || currentKind !== kind()) return;
    const last = Math.max(1, Math.ceil(group.member_count / group.page_size));
    if (page > last) {
      page = last;
      group = await api(`/api/admin/groups/${id}?page=${page}`);
    }
    if (request !== detailRequest || currentKind !== kind()) return;
    total = group.member_count;
    systemGroup = Boolean(group.system);
    const name = group.system
      ? MindexI18n.render(MindexI18n.msg("ui.groups_ungrouped"))
      : group.name;
    $("groups-name").value = name;
    $("groups-description").value = group.system
      ? MindexI18n.render(MindexI18n.msg("ui.groups_ungrouped_help"))
      : group.description;
    text("groups-member-title", "ui.groups_member_title", { p0: name, p1: total });
    showMembers(group.items);
    $("groups-prev").disabled = page <= 1;
    $("groups-next").disabled = page >= last;
    $("groups-member-page").textContent = `${page} / ${last}`;
    window.MindexGroupContext = { group_id: id, type: currentKind };
  }
  async function list(autoSelect = false) {
    const currentKind = kind(),
      { items } = await api(`/api/admin/groups?type=${currentKind}`);
    if (currentKind !== kind()) return [];
    $("groups-list").replaceChildren();
    for (const group of items) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "group-item";
      button.dataset.groupId = group.id;
      if (group.system)
        MindexI18n.setText(
          button,
          MindexI18n.msg("ui.groups_ungrouped_count", { p0: group.member_count }),
        );
      else button.textContent = `${group.name} (${group.member_count})`;
      button.onclick = run(async () => {
        page = 1;
        await detail(group.id);
      });
      $("groups-list").append(button);
    }
    markSelected();
    if (autoSelect && items.length) {
      page = 1;
      await detail(items[0].id);
    }
    return items;
  }
  function reset() {
    ++detailRequest;
    selected = null;
    systemGroup = false;
    page = 1;
    total = 0;
    $("groups-form").reset();
    text("groups-member-title", "ui.groups_members");
    showMembers([]);
    $("groups-member-page").textContent = "";
    $("groups-prev").disabled = $("groups-next").disabled = true;
    window.MindexGroupContext = { type: kind() };
    markSelected();
  }
  async function loadLibrary() {
    const request = ++libraryRequest,
      currentKind = kind(),
      chosen = new Set([...$("groups-results").selectedOptions].map((option) => option.value));
    $("groups-results").disabled = true;
    actions();
    text("groups-library-status", "ui.groups_loading_library");
    const items = [];
    try {
      for (let libraryPage = 1; ; libraryPage++) {
        const result = await api(
          currentKind === "source"
            ? `/api/admin/sources?visibility=all&page_size=100&page=${libraryPage}`
            : "/api/studio/music-library",
        );
        if (request !== libraryRequest || currentKind !== kind()) return;
        items.push(...result.items);
        if (currentKind !== "source" || items.length >= result.total || !result.items.length) break;
      }
      $("groups-results").replaceChildren();
      for (const item of MindexMultiSearch.sort(
        items.map((item) => ({ ...item, name: item.name || item.title })),
      )) {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.name;
        option.selected = chosen.has(item.id);
        $("groups-results").append(option);
      }
      MindexMultiSearch.refilter($("groups-results"));
      text(
        "groups-library-status",
        items.length ? "ui.groups_library_help" : "ui.groups_library_empty",
      );
    } finally {
      if (request === libraryRequest) {
        $("groups-results").disabled = false;
        actions();
      }
    }
  }
  $("groups-kind").onchange = run(async () => {
    reset();
    $("groups-results").replaceChildren();
    query.value = "";
    $("groups-results").dataset.query = "";
    await Promise.all([list(true), loadLibrary()]);
  });
  $("groups-new").onclick = reset;
  $("groups-form").onsubmit = run(async () => {
    const group = await api(
      "/api/admin/groups" + (selected ? `/${selected}` : ""),
      selected ? "PATCH" : "POST",
      { type: kind(), name: $("groups-name").value, description: $("groups-description").value },
    );
    await detail(group.id);
    await list();
  });
  $("groups-delete").onclick = run(async () => {
    if (!selected || !confirm(MindexI18n.render(MindexI18n.msg("ui.groups_confirm")))) return;
    await api(`/api/admin/groups/${selected}`, "DELETE");
    reset();
    await list(true);
  });
  $("groups-results").onchange = actions;
  query.addEventListener("input", actions);
  $("groups-select-visible").onclick = () => {
    for (const option of $("groups-results").options) if (!option.hidden) option.selected = true;
    actions();
  };
  $("groups-clear-selection").onclick = () => {
    for (const option of $("groups-results").options) option.selected = false;
    actions();
  };
  for (const [button, suffix] of [
    ["groups-add", ""],
    ["groups-remove", "/remove"],
  ]) {
    $(button).onclick = run(async () => {
      if (!selected) return;
      const group = selected;
      const ids = suffix
        ? [...$("groups-members").querySelectorAll("input:checked")].map((input) => input.value)
        : [...$("groups-results").selectedOptions].map((option) => option.value);
      $(button).disabled = true;
      try {
        for (let start = 0; start < ids.length; start += 1000)
          await api(`/api/admin/groups/${group}/members${suffix}`, "POST", {
            member_ids: ids.slice(start, start + 1000),
          });
        if (selected !== group) return;
        if (!suffix) for (const option of $("groups-results").options) option.selected = false;
        await detail(group);
        await list();
      } finally {
        actions();
      }
    });
  }
  $("groups-prev").onclick = run(async () => {
    if (selected && page > 1) {
      page--;
      await detail(selected);
    }
  });
  $("groups-next").onclick = run(async () => {
    if (selected && page * 48 < total) {
      page++;
      await detail(selected);
    }
  });
  window.addEventListener("mindex:agent-mutation", (event) => {
    if (!event.detail?.groups) return;
    run(async () => {
      const items = await list();
      if (selected && items.some((group) => group.id === selected)) await detail(selected);
      else {
        reset();
        await list(true);
      }
    })();
  });
  window.addEventListener("mindex:languagechange", () => {
    if (systemGroup && selected) run(() => detail(selected))();
  });
  window.MindexAgentContext = () => ({ ...window.MindexGroupContext });
  window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
    if (passive && focus.group_id !== selected) return true;
    if (focus.group_id) {
      const group = await api(`/api/admin/groups/${focus.group_id}`);
      if (kind() !== group.type) {
        $("groups-kind").value = group.type;
        await loadLibrary();
      }
      page = 1;
      await detail(focus.group_id);
      await list();
    }
    return true;
  };
  reset();
  run(async () => {
    await Promise.all([list(true), loadLibrary()]);
  })();
})();
