// One attempt: content, status, comment. A teacher approves or returns a pending attempt;
// a stored file is sent to the user's chat with the bot (the server never downloads it).
import { api } from "../lib/api.js";
import { button, failure, h, loading, mount, notice, section, title } from "../lib/dom.js";
import { go } from "../lib/router.js";
import { confirm, haptic, openLink } from "../lib/telegram.js";
import { takeFlash, textBlock } from "./common.js";

const RETURN_LIMIT = 4000;

// `parent(detail)` is where Back leads; `after` is where a finished review goes.
export async function attemptView(ctx, view, submissionId, { parent, after }) {
  view.back(() => history.back());
  const flash = takeFlash(ctx);
  mount(ctx.root, loading());
  let detail;
  try {
    detail = await api(`/submissions/${submissionId}`);
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, () => attemptView(ctx, view, submissionId, { parent, after })));
    return;
  }
  if (!view.alive()) return;
  const { submission, assignment, group } = detail;
  view.back(() => go(parent(detail)));
  const teacher = ctx.me.role === "teacher";
  const errors = h("div", {});
  const done = h("div", {});

  function reload(message) {
    ctx.flash = message;
    haptic("success");
    if (after) go(after(detail), { replace: true });
    else attemptView(ctx, view, submissionId, { parent, after });
  }

  async function sendFile(control) {
    errors.replaceChildren();
    control.disabled = true;
    try {
      await api(`/submissions/${submissionId}/send-file`, { method: "POST" });
      haptic("success");
      done.replaceChildren(notice("Файл отправлен в чат с ботом.", "success"));
    } catch (error) {
      haptic("error");
      errors.replaceChildren(failure(error));
    } finally {
      control.disabled = false;
    }
  }

  async function approve() {
    if (!(await confirm("Допустить работу? Студент получит уведомление и сможет записаться на защиту."))) return;
    errors.replaceChildren();
    try {
      await api(`/submissions/${submissionId}/approve`, { method: "POST" });
      reload("Работа допущена. Студенту отправлено уведомление.");
    } catch (error) {
      haptic("error");
      errors.replaceChildren(failure(error));
    }
  }

  function returnForm() {
    const comment = h("textarea", { rows: "5", maxlength: String(RETURN_LIMIT), placeholder: "Что нужно исправить" });
    const formErrors = h("div", {});
    let busy = false;
    const send = async () => {
      const text = comment.value.trim();
      if (!text) {
        formErrors.replaceChildren(notice("Нужен комментарий для студента.", "error"));
        return;
      }
      if (busy) return;
      busy = true;
      formErrors.replaceChildren();
      try {
        await api(`/submissions/${submissionId}/return`, { method: "POST", body: { comment: text } });
        reload("Работа возвращена. Студенту отправлено уведомление с комментарием.");
      } catch (error) {
        busy = false;
        haptic("error");
        formErrors.replaceChildren(failure(error));
      }
    };
    // The comment and its errors in one column, the button below with the usual gap.
    return section(
      "Вернуть на доработку",
      h("div", { class: "stack" }, comment, formErrors),
      h("div", { class: "actions" }, button("Вернуть с комментарием", send)),
    );
  }

  let content;
  if (submission.has_file) {
    const control = button("Прислать файл в чат", () => sendFile(control));
    content = h("div", { class: "stack" }, h("p", {}, `Файл ${submission.kind_label}`), control);
  } else if (submission.kind === "link" && /^https?:\/\//i.test(submission.content || "")) {
    content = h(
      "div",
      { class: "stack" },
      textBlock(submission.content),
      button("Открыть ссылку", () => openLink(submission.content)),
    );
  } else {
    content = textBlock(submission.content || "");
  }

  const student = submission.student_username
    ? `${submission.student_name} (@${submission.student_username})`
    : submission.student_name;
  mount(
    ctx.root,
    title(`Попытка №${submission.attempt_number}`, `${assignment.title} · ${group.name}`),
    flash,
    h(
      "dl",
      { class: "summary" },
      teacher && [h("dt", {}, "Студент"), h("dd", {}, student)],
      h("dt", {}, "Статус"),
      h("dd", {}, submission.status_label),
      h("dt", {}, "Отправлено"),
      h("dd", {}, `${submission.submitted_label} (Москва)`),
      h("dt", {}, "Тип"),
      h("dd", {}, submission.kind_label),
    ),
    section("Решение", content),
    done,
    submission.comment && section("Комментарий преподавателя", textBlock(submission.comment)),
    errors,
    detail.can_review && h("div", { class: "actions" }, button("Допустить", approve, "primary")),
    detail.can_review && returnForm(),
  );
}
