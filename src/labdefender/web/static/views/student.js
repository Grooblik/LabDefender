// Student: assignments with statuses, answering, attempts, and the defense queue.
import { api, newKey } from "../lib/api.js";
import { button, failure, h, item, loading, mount, notice, pagedList, section, title } from "../lib/dom.js";
import { go, route } from "../lib/router.js";
import { closeApp, confirm, haptic, setMainBusy } from "../lib/telegram.js";
import { attemptView } from "./attempt.js";
import {
  pickAssignment,
  pickGroup,
  slotBadge,
  slotLabel,
  takeFlash,
  textBlock,
} from "./common.js";

const TEXT_LIMIT = 4096;

export function registerStudentRoutes(ctx) {
  route("/tasks", (params, view) =>
    pickGroup(ctx, view, { heading: "Задания", back: () => go("/"), open: (group) => `/tasks/g/${group.id}` }),
  );
  route("/tasks/g/:group", (params, view) =>
    pickAssignment(ctx, view, params.group, {
      back: () => go("/tasks"),
      open: (assignment) => `/tasks/a/${assignment.id}`,
      subtitle: (assignment) => `${assignment.status_label} · ${assignment.format_label}`,
    }),
  );
  route("/tasks/a/:assignment", (params, view) => assignmentView(ctx, view, params.assignment));
  route("/tasks/a/:assignment/answer", (params, view) => answerView(ctx, view, params.assignment));
  route("/tasks/a/:assignment/attempts", (params, view) => attemptsView(ctx, view, params.assignment));
  route("/tasks/s/:submission", (params, view) =>
    attemptView(ctx, view, params.submission, {
      parent: (detail) => `/tasks/a/${detail.assignment.id}/attempts`,
    }),
  );
  route("/defense", (params, view) =>
    pickGroup(ctx, view, {
      heading: "Защита и очередь",
      back: () => go("/"),
      open: (group) => `/defense/g/${group.id}`,
    }),
  );
  route("/defense/g/:group", (params, view) =>
    pickAssignment(ctx, view, params.group, {
      back: () => go("/defense"),
      open: (assignment) => `/defense/a/${assignment.id}`,
      subtitle: (assignment) => assignment.status_label,
    }),
  );
  route("/defense/a/:assignment", (params, view) => defenseView(ctx, view, params.assignment));
}

async function load(ctx, view, path, retry) {
  mount(ctx.root, loading());
  try {
    const data = await api(path);
    return view.alive() ? data : null;
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, retry));
    return null;
  }
}

async function assignmentView(ctx, view, assignmentId) {
  view.back(() => go("/tasks"));
  const flash = takeFlash(ctx);
  const card = await load(ctx, view, `/assignments/${assignmentId}`, () => assignmentView(ctx, view, assignmentId));
  if (!card) return;
  const { assignment, group, latest } = card;
  view.back(() => go(`/tasks/g/${group.id}`));
  const errors = h("div", {});

  async function requestFile(control) {
    errors.replaceChildren();
    control.disabled = true;
    try {
      await api(`/assignments/${assignmentId}/file-request`, { method: "POST", key: newKey() });
      haptic("success");
      mount(
        ctx.root,
        title("Отправьте файл в чат"),
        notice(
          `Бот написал вам в чат: пришлите туда один PDF или ZIP до 20 МиБ для «${assignment.title}» ` +
            "и подтвердите отправку кнопкой под сообщением.",
          "success",
        ),
        button("Перейти в чат", closeApp, "primary wide"),
        button("Вернуться к заданию", () => assignmentView(ctx, view, assignmentId)),
      );
    } catch (error) {
      control.disabled = false;
      haptic("error");
      errors.replaceChildren(failure(error));
    }
  }

  const actions = [];
  if (card.can_submit) {
    actions.push(
      button(latest ? "Пересдать текстом или ссылкой" : "Ответить текстом или ссылкой", () =>
        go(`/tasks/a/${assignmentId}/answer`),
      "primary wide"),
    );
    const fileButton = button("Отправить файл PDF/ZIP через чат", () => requestFile(fileButton), "secondary wide");
    actions.push(fileButton);
  }
  if (latest) actions.push(button("Мои попытки", () => go(`/tasks/a/${assignmentId}/attempts`), "secondary wide"));
  if (card.approved) actions.push(button("Защита и очередь", () => go(`/defense/a/${assignmentId}`), "secondary wide"));

  mount(
    ctx.root,
    title(assignment.title, `${group.name} · формат сдачи: ${assignment.format_label}`),
    flash,
    section(
      "Статус",
      h(
        "p",
        {},
        latest ? `${card.status_label} · попытка №${latest.attempt_number} (${latest.kind_label})` : card.status_label,
      ),
      latest && latest.comment && textBlock(`Комментарий преподавателя:\n${latest.comment}`),
    ),
    errors,
    h("div", { class: "stack" }, actions),
    section("Условие", textBlock(assignment.description)),
  );
}

async function answerView(ctx, view, assignmentId) {
  view.back(() => go(`/tasks/a/${assignmentId}`));
  const card = await load(ctx, view, `/assignments/${assignmentId}`, () => answerView(ctx, view, assignmentId));
  if (!card) return;
  if (!card.can_submit) {
    mount(ctx.root, title(card.assignment.title), notice("Сейчас сдавать не нужно: попытка уже на проверке или допущена.", "info"));
    return;
  }
  const field = h("textarea", { rows: "10", maxlength: String(TEXT_LIMIT), placeholder: "Текст решения или ссылка https://…" });
  const errors = h("div", {});
  const key = newKey();
  let busy = false;
  mount(
    ctx.root,
    title(card.latest ? "Пересдача" : "Ответ", card.assignment.title),
    h("p", { class: "hint" }, "Текст до 4096 символов или одна ссылка, начиная с http:// или https://. Файл PDF/ZIP — кнопкой на карточке задания, через чат."),
    field,
    errors,
  );
  view.main("Отправить на проверку", async () => {
    if (busy) return;
    if (!field.value.trim()) {
      errors.replaceChildren(notice("Напишите решение или вставьте ссылку.", "error"));
      return;
    }
    if (!(await confirm("Отправить решение на проверку? Изменить его после отправки нельзя."))) return;
    busy = true;
    setMainBusy(true);
    errors.replaceChildren();
    try {
      const result = await api(`/assignments/${assignmentId}/submissions`, {
        method: "POST",
        key,
        body: { text: field.value, expected_previous_id: card.latest ? card.latest.id : null },
      });
      haptic("success");
      ctx.flash = `Попытка №${result.submission.attempt_number} отправлена на проверку.`;
      go(`/tasks/a/${assignmentId}`, { replace: true });
    } catch (error) {
      busy = false;
      setMainBusy(false);
      haptic("error");
      errors.replaceChildren(failure(error));
    }
  });
}

function attemptsView(ctx, view, assignmentId) {
  view.back(() => go(`/tasks/a/${assignmentId}`));
  mount(
    ctx.root,
    title("Мои попытки"),
    pagedList(
      (offset) => api(`/assignments/${assignmentId}/submissions?offset=${offset}`),
      (attempt) =>
        item({
          text: `Попытка №${attempt.attempt_number} · ${attempt.status_label}`,
          subtitle: `${attempt.submitted_label} · ${attempt.kind_label}`,
          onClick: () => go(`/tasks/s/${attempt.id}`),
        }),
      { empty: "Попыток пока нет." },
    ),
  );
}

async function defenseView(ctx, view, assignmentId) {
  view.back(() => go("/defense"));
  const flash = takeFlash(ctx);
  const data = await load(ctx, view, `/assignments/${assignmentId}/booking`, () => defenseView(ctx, view, assignmentId));
  if (!data) return;
  const { assignment, booking } = data;
  view.back(() => go(`/defense/g/${assignment.group_id}`));
  const errors = h("div", {});
  const version = {
    expected_entry_id: booking ? booking.entry_id : null,
    expected_order_at: booking ? booking.order_at : null,
  };

  async function act(path, question, message, extra = {}) {
    if (!(await confirm(question))) return;
    errors.replaceChildren();
    try {
      await api(path, { method: "POST", body: { ...version, ...extra } });
      ctx.flash = message;
      haptic("success");
      defenseView(ctx, view, assignmentId);
    } catch (error) {
      haptic("error");
      errors.replaceChildren(failure(error));
    }
  }

  function choose(slot) {
    const switching = booking !== null;
    const question =
      `${switching ? "Сменить слот на" : "Записаться на"} ${slot.date_label} · ${slot.time_label} (Москва)?` +
      (switching ? " В новом слоте вы окажетесь в конце очереди." : "");
    act(`/assignments/${assignmentId}/enroll`, question, "Запись сохранена.", { slot_id: slot.id });
  }

  const current = booking
    ? section(
        "Ваша запись",
        h(
          "dl",
          { class: "summary" },
          h("dt", {}, "Слот"),
          h("dd", {}, `${slotLabel(booking.slot)} (Москва)`),
          h("dt", {}, "Позиция"),
          h("dd", {}, String(booking.position)),
          h("dt", {}, "Записано"),
          h("dd", {}, `${booking.occupied} из ${booking.slot.capacity}`),
        ),
        booking.can_move &&
          h(
            "div",
            { class: "actions" },
            button("В конец очереди", () =>
              act(
                `/assignments/${assignmentId}/move-to-end`,
                "Перенести вашу запись в конец очереди этого слота?",
                "Запись перенесена в конец очереди.",
              ),
            ),
          ),
      )
    : notice("Допуск получен. Выберите слот защиты.", "info");

  mount(
    ctx.root,
    title("Защита", assignment.title),
    flash,
    current,
    errors,
    section(
      booking ? "Сменить слот" : "Слоты защиты",
      pagedList(
        (offset) => api(`/assignments/${assignmentId}/slots?offset=${offset}`),
        (slot) => {
          const mine = booking && booking.slot.id === slot.id;
          const full = slot.occupied >= slot.capacity;
          return item({
            text: slotLabel(slot),
            subtitle: `Записано: ${slot.occupied} из ${slot.capacity}`,
            badge: mine ? "ваш слот" : slotBadge(slot),
            disabled: mine || full || !slot.is_open,
            onClick: () => choose(slot),
          });
        },
        { empty: "Свободных слотов пока нет. Когда преподаватель откроет слот, придёт уведомление." },
      ),
    ),
  );
}

