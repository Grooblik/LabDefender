// Teacher: group → assignment → slots and approved students without a booking → slot queue.
import { api } from "../lib/api.js";
import { button, failure, h, item, loading, mount, pagedList, section, title } from "../lib/dom.js";
import { go, route } from "../lib/router.js";
import { confirm, haptic } from "../lib/telegram.js";
import {
  firstPageThen,
  pickAssignment,
  pickGroup,
  slotBadge,
  slotLabel,
  takeFlash,
} from "./common.js";
import { newSlotView } from "./new-slot.js";

export function registerSlotRoutes(ctx) {
  route("/slots", (params, view) => groupsView(ctx, view));
  route("/slots/g/:group", (params, view) => assignmentsView(ctx, view, params.group));
  route("/slots/a/:assignment", (params, view) => assignmentView(ctx, view, params.assignment));
  route("/slots/a/:assignment/new", (params, view) => newSlotView(ctx, view, params.assignment));
  route("/slots/s/:slot", (params, view) => queueView(ctx, view, params.slot));
}

function groupsView(ctx, view) {
  pickGroup(ctx, view, {
    heading: "Слоты и очередь",
    back: () => go("/"),
    open: (group) => `/slots/g/${group.id}`,
  });
}

function assignmentsView(ctx, view, groupId) {
  pickAssignment(ctx, view, groupId, {
    back: () => go("/slots"),
    open: (assignment) => `/slots/a/${assignment.id}`,
    subtitle: (assignment) => `Формат сдачи: ${assignment.format_label}`,
  });
}

async function assignmentView(ctx, view, assignmentId) {
  view.back(() => go("/slots"));
  const flash = takeFlash(ctx);
  mount(ctx.root, loading());
  let first;
  try {
    first = await api(`/assignments/${assignmentId}/slots?offset=0`);
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, () => assignmentView(ctx, view, assignmentId)));
    return;
  }
  if (!view.alive()) return;
  const assignment = first.assignment;
  view.back(() => go(`/slots/g/${assignment.group_id}`));
  view.main("Новый слот", () => go(`/slots/a/${assignmentId}/new`));
  mount(
    ctx.root,
    title(assignment.title, `Формат сдачи: ${assignment.format_label} · время московское`),
    flash,
    section(
      "Слоты защиты",
      pagedList(
        firstPageThen(first, (offset) => api(`/assignments/${assignmentId}/slots?offset=${offset}`)),
        (slot) =>
          item({
            text: slotLabel(slot),
            subtitle: `Записано: ${slot.occupied} из ${slot.capacity}`,
            badge: slotBadge(slot),
            tone: slot.is_past ? "muted" : null,
            onClick: () => go(`/slots/s/${slot.id}`),
          }),
        { empty: "Слотов пока нет. Создайте первый кнопкой «Новый слот»." },
      ),
    ),
    section(
      "Допущены, но не записаны",
      pagedList(
        (offset) => api(`/assignments/${assignmentId}/unassigned?offset=${offset}`),
        (student) => h("p", { class: "row" }, student.name),
        { empty: "Все допущенные студенты записаны." },
      ),
    ),
  );
}

async function queueView(ctx, view, slotId) {
  view.back(() => go("/slots"));
  const flash = takeFlash(ctx);
  mount(ctx.root, loading());
  let first;
  try {
    first = await api(`/slots/${slotId}/queue?offset=0`);
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, () => queueView(ctx, view, slotId)));
    return;
  }
  if (!view.alive()) return;
  const { slot, assignment } = first;
  view.back(() => go(`/slots/a/${assignment.id}`));
  const errors = h("div", {});

  async function change(action) {
    errors.replaceChildren();
    try {
      const result = await api(`/slots/${slotId}/${action}`, { method: "POST" });
      ctx.flash =
        action === "close"
          ? "Слот закрыт для новых записей."
          : result.reopened
            ? "Слот снова открыт. Допущенные студенты без записи получат уведомление."
            : "Слот уже открыт.";
      haptic("success");
      queueView(ctx, view, slotId);
    } catch (error) {
      haptic("error");
      errors.replaceChildren(failure(error));
    }
  }

  async function cancel() {
    if (!(await confirm("Отменить пустой слот? Вернуть его будет нельзя."))) return;
    errors.replaceChildren();
    try {
      await api(`/slots/${slotId}`, { method: "DELETE" });
      ctx.flash = "Пустой слот отменён.";
      haptic("success");
      go(`/slots/a/${assignment.id}`, { replace: true });
    } catch (error) {
      haptic("error");
      errors.replaceChildren(failure(error));
    }
  }

  const status = slot.is_past
    ? "Слот уже прошёл."
    : slot.is_open
      ? "Открыт для записи."
      : "Закрыт для новых записей.";
  const actions = [];
  if (!slot.is_past) {
    actions.push(
      slot.is_open
        ? button("Закрыть для записи", () => change("close"))
        : button("Открыть для записи", () => change("reopen")),
    );
  }
  if (slot.occupied === 0) actions.push(button("Отменить слот", cancel, "danger"));
  actions.push(button("Обновить", () => queueView(ctx, view, slotId)));

  mount(
    ctx.root,
    title(slotLabel(slot), assignment.title),
    flash,
    h("p", {}, `Записано: ${slot.occupied} из ${slot.capacity}. ${status}`),
    errors,
    h("div", { class: "actions" }, actions),
    section(
      "Очередь",
      pagedList(
        firstPageThen(first, (offset) => api(`/slots/${slotId}/queue?offset=${offset}`)),
        (member) => h("p", { class: "row" }, `${member.position}. ${member.name}`),
        { empty: "Пока никто не записался." },
      ),
    ),
  );
}
