import { closeApp } from "./telegram.js";

export function h(tag, attributes, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "onClick") {
      element.addEventListener("click", value);
    } else if (value !== false && value != null) {
      element.setAttribute(name, value === true ? "" : String(value));
    }
  }
  append(element, children);
  return element;
}

function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || typeof child === "boolean") continue;
    parent.append(child instanceof Node ? child : String(child));
  }
}

export function mount(root, ...children) {
  root.replaceChildren();
  append(root, children);
}

export function title(text, subtitle) {
  return h("header", { class: "title" }, h("h1", {}, text),
    subtitle && h("p", { class: "hint" }, subtitle));
}

export function section(heading, ...children) {
  return h("section", { class: "section" }, h("h2", {}, heading), children);
}

export function button(text, onClick, style = "secondary") {
  return h("button", { type: "button", class: style, onClick }, text);
}

export function item({ text, subtitle, badge, tone, disabled, onClick }) {
  return h(
    "button", { type: "button", class: tone ? `item ${tone}` : "item", disabled, onClick },
    h("span", { class: "item-text" },
      h("span", { class: "item-title" }, text),
      subtitle && h("span", { class: "item-subtitle" }, subtitle)),
    badge && h("span", { class: "badge" }, badge),
    !disabled && h("span", { class: "chevron", "aria-hidden": "true" }, "›"),
  );
}

export function notice(text, tone = "info") {
  return h("p", { class: `notice ${tone}`, role: tone === "error" ? "alert" : "status" }, text);
}

export function loading() {
  return h("p", { class: "hint", role: "status" }, "Загрузка…");
}

export function failure(error, retry) {
  const expired = error?.status === 401;
  const text = expired ? "Сеанс истёк. Откройте приложение снова из чата с ботом."
    : error?.message || "Не удалось загрузить данные.";
  return h("div", {}, notice(text, "error"),
    expired ? button("Закрыть приложение", closeApp)
      : retry && button("Повторить", retry));
}

export function pagedList(loadPage, renderItem, { empty = "Записей пока нет." } = {}) {
  const list = h("div", { class: "list" });
  const controls = h("div", { class: "pager" });
  const root = h("div", {}, list, controls);
  let offset = 0;

  async function load() {
    const pending = loading();
    controls.replaceChildren(pending);
    try {
      const page = await loadPage(offset);
      if (offset === 0 && page.items.length === 0) {
        list.append(h("p", { class: "hint" }, empty));
      } else {
        list.append(...page.items.map(renderItem));
      }
      offset = page.offset + page.items.length;
      controls.replaceChildren();
      if (page.has_next) controls.append(button("Показать ещё", load));
    } catch (error) {
      controls.replaceChildren(failure(error, load));
    }
  }

  void load();
  return root;
}
