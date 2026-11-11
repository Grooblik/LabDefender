// Pieces shared by the screens: one-time messages, group and assignment pickers, labels.
import { api } from "../lib/api.js";
import { h, item, mount, notice, pagedList, title } from "../lib/dom.js";
import { go } from "../lib/router.js";

// A message for the next screen, e.g. "Слот создан" after the creation screen is left.
export function takeFlash(ctx) {
  const text = ctx.flash;
  ctx.flash = null;
  return text ? notice(text, "success") : null;
}

// Reuses an already loaded first page instead of requesting it twice.
export function firstPageThen(first, loader) {
  let used = false;
  return (offset) => {
    if (offset === 0 && !used) {
      used = true;
      return Promise.resolve(first);
    }
    return loader(offset);
  };
}

export function slotLabel(slot) {
  return `${slot.date_label}, ${slot.weekday} · ${slot.time_label}`;
}

export function slotBadge(slot) {
  if (slot.is_past) return "прошёл";
  if (!slot.is_open) return "закрыт";
  if (slot.occupied >= slot.capacity) return "мест нет";
  return null;
}

export function pickGroup(ctx, view, { heading, back, open }) {
  view.back(back);
  mount(
    ctx.root,
    title(heading, "Выберите группу"),
    takeFlash(ctx),
    pagedList(
      (offset) => api(`/groups?offset=${offset}`),
      (group) => item({ text: group.name, onClick: () => go(open(group)) }),
      {
        empty:
          ctx.me.role === "teacher"
            ? "Групп пока нет. Создайте первую в разделе «👥 Группы»."
            : "Вы пока не состоите в группах. Откройте ссылку-приглашение преподавателя.",
      },
    ),
  );
}

export function pickAssignment(ctx, view, groupId, { back, open, subtitle }) {
  view.back(back);
  const header = title("Задания группы", "Выберите задание");
  mount(
    ctx.root,
    header,
    takeFlash(ctx),
    pagedList(
      async (offset) => {
        const page = await api(`/groups/${groupId}/assignments?offset=${offset}`);
        if (view.alive()) header.querySelector("h1").textContent = page.group.name;
        return page;
      },
      (assignment) =>
        item({ text: assignment.title, subtitle: subtitle(assignment), onClick: () => go(open(assignment)) }),
      { empty: "В группе пока нет заданий." },
    ),
  );
}

// Multi-line text from users, shown as is (text nodes only).
export function textBlock(text) {
  return h("div", { class: "text-block" }, text);
}
