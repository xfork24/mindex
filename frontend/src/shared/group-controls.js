(function () {
  "use strict";
  const api = (...args) => window.MindexApi.json(...args);
  // 同页共享目录与在途请求。成员随目录加载，筛选切换不再等待解析接口。
  const catalogues = new Map();
  function catalogue(kind, fresh = false) {
    if (fresh && !catalogues.get(kind)?.pending) {
      catalogues.delete(kind);
    }
    const cached = catalogues.get(kind);
    if (cached && (cached.pending || Date.now() - cached.time < 15000)) {
      return cached.request;
    }
    const entry = { pending: true, time: 0 };
    entry.request = api(
      `/api/admin/groups?type=${kind}${kind === "source" ? "&include_members=true" : ""}`,
    )
      .then((result) => {
        entry.pending = false;
        entry.time = Date.now();
        return result;
      })
      .catch((error) => {
        if (catalogues.get(kind) === entry) {
          catalogues.delete(kind);
        }
        throw error;
      });
    catalogues.set(kind, entry);
    return entry.request;
  }
  for (const event of ["mindex:groups-changed", "mindex:agent-mutation"]) {
    window.addEventListener(event, (e) => {
      if (event === "mindex:agent-mutation" && !e.detail?.groups) {
        return;
      }
      catalogues.clear();
    });
  }
  // defaultAll：筛选类控件默认全选；全选视为“不限”（values 返回空，不写入筛选/保存值），
  // 与任务创建“全选=不限”的既有约定一致。归属编辑（membership）不传该选项。
  function picker(
    kind,
    selected = [],
    changed = () => {},
    key,
    { listbox = false, includeSystem = true, defaultAll = false } = {},
  ) {
    const wrap = document.createElement("fieldset");
    wrap.className = listbox ? "group-picker group-picker-listbox" : "group-picker";
    wrap.dataset.groupKind = kind;
    const caption = document.createElement("legend");
    caption.className = "group-caption";
    MindexI18n.setText(caption, MindexI18n.msg(key || "ui.groups_select"));
    const options = document.createElement("div");
    options.className = "group-options";
    const status = document.createElement("p");
    status.className = "group-status muted";
    status.setAttribute("role", "status");
    wrap.append(caption, options, status);
    const select = listbox ? document.createElement("select") : null;
    if (select) {
      select.multiple = true;
      MindexI18n.setAttr(select, "aria-label", MindexI18n.msg(key || "ui.groups_select"));
      options.append(select);
      MindexMultiSearch.attach(select);
    }
    const listeners = new Set();
    let selectedIds = [...new Set(selected)];
    let items = [];
    let disabled = false;
    let loading = true;
    let loaded = false;
    let refreshGeneration = 0;
    const coversAll = (ids) =>
      defaultAll && items.length > 0 && items.every((group) => ids.includes(group.id));
    const values = () => (coversAll(selectedIds) ? [] : [...selectedIds]);
    function syncDisabled() {
      wrap.dataset.loading = loading ? "1" : "0";
      for (const input of options.querySelectorAll("input")) {
        input.disabled = disabled || loading;
      }
      if (select) {
        select.disabled =
          disabled || loading || ![...select.options].some((option) => option.value);
      }
    }
    let allSelectedBefore = false;
    function syncAll() {
      const all = options.querySelector("[data-group-all]");
      allSelectedBefore =
        items.length > 0 && items.every((group) => selectedIds.includes(group.id));
      if (all) {
        if (select) {
          all.selected = allSelectedBefore;
        } else {
          all.checked = allSelectedBefore;
        }
      }
    }
    async function notifyChange(event) {
      const all = options.querySelector("[data-group-all]");
      const marked = all && (select ? all.selected : all.checked);
      const real = select
        ? [...select.selectedOptions].filter((option) => option.value !== "__all__" && option.value)
        : [...options.querySelectorAll("input:checked:not([data-group-all])")];
      const chooseAll = marked && (!allSelectedBefore || !real.length || event?.target === all);
      const clearAll = !select && event?.target === all && !marked;
      if (chooseAll || clearAll) {
        const ids = chooseAll ? items.map((group) => group.id) : [];
        for (const node of options.querySelectorAll(select ? "option" : "input")) {
          if (node === all) {
            continue;
          }
          if (select) {
            node.selected = ids.includes(node.value);
          } else {
            node.checked = ids.includes(node.value);
          }
        }
      }
      selectedIds = select
        ? [...select.selectedOptions]
            .map((option) => option.value)
            .filter((id) => id && id !== "__all__")
        : [...options.querySelectorAll("input:checked:not([data-group-all])")].map(
            (entry) => entry.value,
          );
      syncAll();
      try {
        await changed(values());
        await Promise.all([...listeners].map((listener) => listener()));
      } catch (error) {
        MindexI18n.setText(status, MindexI18n.system(error.message));
      }
    }
    if (select) {
      select.addEventListener("change", notifyChange);
    }
    function draw() {
      if (select) {
        select.replaceChildren();
      } else {
        options.replaceChildren();
      }
      const known = new Set(items.map((group) => group.id));
      const rows = loaded
        ? [
            ...items,
            ...selectedIds.filter((id) => !known.has(id)).map((id) => ({ id, missing: true })),
          ]
        : [];
      if (!rows.length && (loading || loaded)) {
        const empty = document.createElement(select ? "option" : "span");
        if (select) {
          empty.value = "";
          empty.disabled = true;
        }
        empty.className = "group-empty muted";
        MindexI18n.setText(
          empty,
          MindexI18n.msg(loading ? "ui.groups_loading" : "ui.groups_none_available"),
        );
        (select || options).append(empty);
      }
      for (const group of rows) {
        if (select) {
          const option = document.createElement("option");
          option.value = group.id;
          option.selected = selectedIds.includes(group.id);
          if (group.missing) {
            MindexI18n.setText(option, MindexI18n.msg("ui.groups_missing_choice"));
          } else if (group.system) {
            MindexI18n.setText(
              option,
              MindexI18n.msg("ui.groups_ungrouped_count", { p0: group.member_count }),
            );
          } else {
            option.textContent = `${group.name} (${group.member_count})`;
          }
          select.append(option);
          continue;
        }
        const label = document.createElement("label");
        label.className = "group-option";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = group.id;
        input.checked = selectedIds.includes(group.id);
        const name = document.createElement("span");
        if (group.missing) {
          MindexI18n.setText(name, MindexI18n.msg("ui.groups_missing_choice"));
          label.title = group.id;
        } else {
          // 组名称是用户内容，保留原文；只有控件文案使用翻译绑定。
          if (group.system) {
            MindexI18n.setText(
              name,
              MindexI18n.msg("ui.groups_ungrouped_count", { p0: group.member_count }),
            );
          } else {
            name.textContent = `${group.name} (${group.member_count})`;
          }
        }
        label.append(input, name);
        input.addEventListener("change", notifyChange);
        options.append(label);
      }
      if (includeSystem && items.length) {
        const all = document.createElement(select ? "option" : "input");
        all.value = "__all__";
        all.dataset.groupAll = "true";
        if (select) {
          MindexI18n.setText(all, MindexI18n.msg("ui.groups_all"));
          select.prepend(all);
        } else {
          all.type = "checkbox";
          const label = document.createElement("label");
          label.className = "group-option";
          const name = document.createElement("span");
          MindexI18n.setText(name, MindexI18n.msg("ui.groups_all"));
          label.append(all, name);
          options.prepend(label);
          all.addEventListener("change", notifyChange);
        }
      }
      syncAll();
      if (select) {
        MindexMultiSearch.refilter(select);
      }
      syncDisabled();
    }
    async function refresh({ fresh = true } = {}) {
      const current = ++refreshGeneration;
      loading = true;
      syncDisabled();
      // defaultAll：从未选择过则取全选默认；原选择已覆盖全部组时刷新后保持全选（新增组不改变“不限”）。
      const keepAll =
        defaultAll && items.length > 0 && items.every((group) => selectedIds.includes(group.id));
      try {
        const result = await catalogue(kind, fresh);
        if (current !== refreshGeneration) {
          return;
        }
        items = result.items.filter((group) => includeSystem || !group.system);
        loaded = true;
        if (defaultAll && (keepAll || selectedIds.length === 0)) {
          selectedIds = items.map((group) => group.id);
        }
        MindexI18n.setText(status, "");
      } catch (error) {
        if (current !== refreshGeneration) {
          return;
        }
        loaded = false;
        MindexI18n.setText(status, MindexI18n.system(error.message));
      } finally {
        if (current === refreshGeneration) {
          loading = false;
          draw();
        }
      }
    }
    draw();
    const ready = refresh({ fresh: false });
    return {
      wrap,
      ready,
      values,
      refresh,
      resolveSources: () => {
        if (!loaded) {
          throw new Error(MindexI18n.msg("ui.groups_loading"));
        }
        const ids = values();
        if (!ids.length) {
          return null;
        }
        const allowed = new Set();
        for (const id of ids) {
          const group = items.find((item) => item.id === id);
          // 已删除的选择不能扩大为全库。
          if (!group) {
            throw new Error(MindexI18n.msg("ui.groups_missing_choice"));
          }
          for (const member of group.member_ids || []) {
            allowed.add(member);
          }
        }
        return allowed;
      },
      onChange: (listener) => listeners.add(listener),
      setDisabled: (value) => {
        disabled = value;
        syncDisabled();
      },
      setValues: (ids) => {
        const next = [...new Set(ids)];
        // defaultAll 控件里清空选择等于回到“不限”默认：显示全选，values() 仍返回空。
        selectedIds = defaultAll && !next.length ? items.map((group) => group.id) : next;
        draw();
      },
    };
  }
  function membership(host, kind, item) {
    host.querySelector("[data-group-membership]")?.remove();
    const control = picker(
      kind,
      (item.groups || []).filter((group) => !group.system).map((group) => group.id),
      async (ids) => {
        control.setDisabled(true);
        try {
          const result = await api(`/api/admin/group-memberships/${kind}/${item.id}`, "PUT", {
            group_ids: ids,
          });
          item.groups = result.groups;
          updateUncategorized();
          await control.refresh();
          MindexI18n.setText(message, "");
          window.dispatchEvent(
            new CustomEvent("mindex:groups-changed", { detail: { kind, id: item.id } }),
          );
        } catch (error) {
          MindexI18n.setText(message, MindexI18n.system(error.message));
          control.setValues(
            (item.groups || []).filter((group) => !group.system).map((group) => group.id),
          );
        } finally {
          control.setDisabled(false);
        }
      },
      undefined,
      { includeSystem: false },
    );
    control.wrap.dataset.groupMembership = item.id;
    const message = document.createElement("span");
    message.setAttribute("role", "alert");
    const uncategorized = document.createElement("span");
    uncategorized.className = "group-uncategorized muted";
    MindexI18n.setText(uncategorized, MindexI18n.msg("ui.groups_ungrouped"));
    function updateUncategorized() {
      uncategorized.hidden = !(item.groups || []).some((group) => group.system);
    }
    updateUncategorized();
    control.wrap.append(uncategorized, message);
    host.append(control.wrap);
  }
  async function resolveSources(ids) {
    if (!ids.length) {
      return null;
    }
    try {
      const result = await api("/api/admin/groups/resolve", "POST", { source_group_ids: ids });
      return result.source_ids === null ? null : new Set(result.source_ids);
    } catch (error) {
      // 空组在管理/Agent 接口中是明确错误；联动控件则显示空列表，不能扩大范围。
      if (
        error.status === 422 &&
        (error.data?.message_key === "ui.groups_empty_resolve" ||
          error.detail === "所选组与素材范围没有成员，请重新选择")
      ) {
        return new Set();
      }
      throw error;
    }
  }
  function linkSources(
    control,
    select,
    { entries, selection, fill, onScope = () => {}, updated = () => {}, onError = () => {} },
  ) {
    let generation = 0;
    async function refresh() {
      const current = ++generation;
      select.disabled = true;
      try {
        // 目录未就绪时只等首个请求；后续切换同步使用缓存的成员关系。
        await control.ready;
        const allowed = control.resolveSources
          ? control.resolveSources()
          : await resolveSources(control.values());
        if (current !== generation || !select.isConnected) {
          return;
        }
        onScope(allowed);
        const choices = entries().filter((item) => allowed === null || allowed.has(item.id));
        const chosen = selection();
        fill(choices, new Set(chosen === null ? choices.map((item) => item.id) : chosen));
        updated(choices);
        select.disabled = ![...select.options].some((option) => option.value !== "__all__");
      } catch (error) {
        if (current !== generation) {
          return;
        }
        onScope(new Set());
        fill([], new Set());
        updated([]);
        select.disabled = true;
        onError(error);
      }
    }
    control.onChange(refresh);
    async function reload({ fresh = true } = {}) {
      if (!select.isConnected) {
        return;
      }
      if (control.refresh) {
        await control.refresh({ fresh });
      }
      await refresh();
    }
    if (control.refresh) {
      window.addEventListener("mindex:groups-changed", () => {
        void reload();
      });
      window.addEventListener("mindex:agent-mutation", (event) => {
        if (event.detail?.groups) {
          void reload();
        }
      });
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
          void reload();
        }
      });
    }
    return { refresh: reload };
  }
  window.MindexGroups = { picker, membership, resolveSources, linkSources };
})();
