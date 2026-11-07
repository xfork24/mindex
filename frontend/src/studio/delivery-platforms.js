/* Persistent platform catalogue. Delivery templates and plans own separate rule snapshots. */
"use strict";
window.DeliveryPlatforms = (() => {
  function mount(host, changed) {
    const { node, t } = DeliveryEditor;
    const api = (path = "", method = "GET", body) =>
      MindexApi.json(`/api/studio/platforms${path}`, method, body);
    host.append(node("h2", "platform_management"), node("p", "platform_management_help", "muted"));
    const list = node("div", null, "delivery-platform-list");
    const form = node("form", null, "delivery-platform-form");
    const fields = node("div", null, "delivery-fields");
    const actions = node("div", null, "detail-actions");
    const notice = node("p", null, "warn-text");
    notice.setAttribute("role", "status");
    const create = node("button", "add_platform", "quiet");
    create.type = "button";
    const save = node("button", "save", "submit");
    save.type = "submit";
    const remove = node("button", "delete_platform", "quiet");
    remove.type = "button";
    actions.append(save, remove);
    form.append(fields, actions);
    host.append(create, list, form, notice);
    let current = null;
    let draft = null;
    const descriptors = [
      ["id", "platform_id"],
      ["name", "platform_name"],
      ["canvas", "check_canvas", ["16:9", "9:16", "1:1"]],
      ["cover.width", "cover_width", "number"],
      ["cover.height", "cover_height", "number"],
      ["cover.format", "cover_format", ["jpeg", "png"]],
      ["cover.max_kb", "cover_max_kb", "number"],
      ["title.max_chars", "title_limit", "number"],
      ["description.max_chars", "description_limit", "number"],
      ["tags.max_count", "tag_count", "number"],
      ["tags.max_chars_each", "tag_length", "number"],
      ["tags.max_total_chars", "tag_total", "number", true],
      ["tags.hashtag", "tag_hashtag", "checkbox"],
      ["chapters.enabled", "chapters_enabled", "checkbox"],
      ["chapters.min_count", "chapters_count", "number"],
      ["chapters.first_at_zero", "chapters_zero", "checkbox"],
      ["chapters.min_seconds", "chapters_seconds", "number"],
      ["language", "platform_language"],
    ];
    function edit(item) {
      current = item?.id || null;
      draft = structuredClone(
        item || {
          id: "",
          name: "",
          canvas: "16:9",
          cover: { width: 1280, height: 720, format: "jpeg", max_kb: 2048 },
          title: { max_chars: 100 },
          description: { max_chars: 5000 },
          tags: { max_count: 30, max_chars_each: 30, max_total_chars: null, hashtag: false },
          chapters: { enabled: true, min_count: 2, first_at_zero: true, min_seconds: 5 },
          language: "zh",
        },
      );
      fields.replaceChildren();
      notice.textContent = "";
      remove.hidden = !current;
      for (const [path, key, type, optional] of descriptors) {
        const parts = path.split(".");
        const parent = parts.length === 1 ? draft : draft[parts[0]];
        const prop = parts.at(-1);
        const label = node("label", null, `field ${type === "checkbox" ? "delivery-check" : ""}`);
        label.append(node("span", key));
        const input = document.createElement(Array.isArray(type) ? "select" : "input");
        input.dataset.platformField = path;
        if (Array.isArray(type)) {
          for (const value of type) {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = value;
            input.append(option);
          }
        } else {
          input.type = type || "text";
        }
        if (type === "checkbox") {
          input.checked = parent[prop];
        } else {
          input.value = parent[prop] ?? "";
        }
        if (type === "number") {
          input.min = "1";
          input.step = path === "chapters.min_seconds" ? "any" : "1";
        }
        if (type !== "checkbox") {
          input.required = !optional;
        }
        if (path === "id") {
          input.disabled = !!current;
          input.pattern = "[a-z][a-z0-9-]{0,63}";
          input.maxLength = 64;
        }
        input.addEventListener("input", () => {
          parent[prop] =
            type === "checkbox"
              ? input.checked
              : type === "number"
                ? input.value === "" && optional
                  ? null
                  : Number(input.value)
                : input.value;
        });
        label.append(input);
        fields.append(label);
      }
      for (const button of list.children) {
        button.setAttribute("aria-current", String(button.dataset.id === current));
      }
    }
    async function load(id = current) {
      const items = (await api()).items;
      list.replaceChildren();
      for (const item of items) {
        const button = document.createElement("button");
        button.className = "quiet";
        button.type = "button";
        button.textContent = item.name;
        button.dataset.id = item.id;
        button.onclick = () => edit(item);
        list.append(button);
      }
      edit(items.find((item) => item.id === id) || items[0] || null);
    }
    create.onclick = () => edit(null);
    form.onsubmit = async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        const item = await api(current ? `/${current}` : "", current ? "PUT" : "POST", draft);
        await load(item.id);
        await changed();
        notice.replaceChildren(node("span", "saved"));
      } catch (error) {
        notice.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    };
    remove.onclick = async () => {
      if (!current || !confirm(MindexI18n.render(t("delete_platform_confirm")))) {
        return;
      }
      remove.disabled = true;
      try {
        await api(`/${current}`, "DELETE");
        await load();
        await changed();
      } catch (error) {
        notice.textContent = error.message;
      } finally {
        remove.disabled = false;
      }
    };
    load().catch((error) => {
      notice.textContent = error.message;
    });
  }
  return { mount };
})();
