// Teacher: group → its slots and approved works without a booking → slot queue.
// A slot belongs to a group; each place in it is one work: student, assignment and attempt.
import { api } from "../lib/api.js";
import { button, failure, h, item, loading, mount, pagedList, section, title } from "../lib/dom.js";
import { go, route } from "../lib/router.js";
import { confirm, haptic } from "../lib/telegram.js";
import { firstPageThen, pickGroup, slotBadge, slotLabel, takeFlash } from "./common.js";
import { newSlotView } from "./new-slot.js";

export function registerSlotRoutes(ctx) {
  route("/slots", (params, view) => groupsView(ctx, view));
  route("/slots/g/:group", (params, view) => groupSlotsView(ctx, view, params.group));
  route("/slots/g/:group/new", (params, view) => newSlotView(ctx, view, params.group));
  // Links made before slots belonged to groups name an assignment: open its group.
  route("/slots/a/:assignment", (params, view) => assignmentRedirect(ctx, view, params.assignment));
  route("/slots/s/:slot", (params, view) => queueView(ctx, view, params.slot));
}

function groupsView(ctx, view) {
  pickGroup(ctx, view, {
    heading: "Слоты и очередь",
    back: () => go("/"),
    open: (group) => `/slots/g/${group.id}`,
  });
}

async function assignmentRedirect(ctx, view, assignmentId) {
  mount(ctx.root, loading());
  try {
    const card = await api(`/assignments/${assignmentId}`);
    if (view.alive()) go(`/slots/g/${card.group.id}`, { replace: true });
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, () => assignmentRedirect(ctx, view, assignmentId)));
  }
}

export function workLabel(work) {
  return `${work.assignment_title} · попытка №${work.attempt_number}`;
}

async function groupSlotsView(ctx, view, groupId) {
  view.back(() => go("/slots"));
  const flash = takeFlash(ctx);
  mount(ctx.root, loading());
  let first;
  try {
    first = await api(`/groups/${groupId}/slots?offset=0`);
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, () => groupSlotsView(ctx, view, groupId)));
    return;
  }
  if (!view.alive()) return;
  view.main("Новый слот", () => go(`/slots/g/${groupId}/new`));
  mount(
    ctx.root,
    title(first.group.name, "Слоты защиты группы · время московское"),
    flash,
    h("p", { class: "hint" }, "В слот записывается любая допущенная работа группы, каждая — на своё место."),
    section(
      "Слоты защиты",
      pagedList(
        firstPageThen(first, (offset) => api(`/groups/${groupId}/slots?offset=${offset}`)),
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
      "Допущенные работы без записи",
      pagedList(
        (offset) => api(`/groups/${groupId}/unassigned?offset=${offset}`),
        (work) =>
          h(
            "div",
            { class: "row" },
            h("div", { class: "item-title" }, work.name),
            h("div", { class: "item-subtitle" }, workLabel(work)),
          ),
        { empty: "Все допущенные работы записаны." },
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
  const { slot, group } = first;
  view.back(() => go(`/slots/g/${group.id}`));
  const errors = h("div", {});

  async function change(action) {
    errors.replaceChildren();
    try {
      const result = await api(`/slots/${slotId}/${action}`, { method: "POST" });
      ctx.flash =
        action === "close"
          ? "Слот закрыт для новых записей."
          : result.reopened
            ? "Слот снова открыт. Студенты с допущенными работами без записи получат уведомление."
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
      go(`/slots/g/${group.id}`, { replace: true });
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
    title(slotLabel(slot), group.name),
    flash,
    h("p", {}, `Записано: ${slot.occupied} из ${slot.capacity}. ${status}`),
    errors,
    h("div", { class: "actions" }, actions),
    section(
      "Очередь",
      pagedList(
        firstPageThen(first, (offset) => api(`/slots/${slotId}/queue?offset=${offset}`)),
        (member) =>
          h(
            "div",
            { class: "row" },
            h("div", { class: "item-title" }, `${member.position}. ${member.name}`),
            h(
              "div",
              { class: "item-subtitle" },
              [workLabel(member), member.username && "@" + member.username].filter(Boolean).join(" · "),
            ),
          ),
        { empty: "Пока никто не записался." },
      ),
    ),
  );
}
