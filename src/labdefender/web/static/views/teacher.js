// Teacher: review inbox, groups with invitations, assignments with their attempts.
import { api, newKey } from "../lib/api.js";
import { button, failure, h, item, loading, mount, notice, pagedList, section, title } from "../lib/dom.js";
import { go, route } from "../lib/router.js";
import { confirm, copyText, haptic, setMainBusy, share } from "../lib/telegram.js";
import { attemptView } from "./attempt.js";
import { pickAssignment, pickGroup, takeFlash, textBlock } from "./common.js";

const NAME_LIMIT = 128;
const TITLE_LIMIT = 256;
const DESCRIPTION_LIMIT = 4000;

export function registerTeacherRoutes(ctx) {
  route("/review", (params, view) => reviewView(ctx, view));
  route("/review/s/:submission", (params, view) =>
    attemptView(ctx, view, params.submission, { parent: () => "/review", after: () => "/review" }),
  );
  route("/groups", (params, view) => groupsView(ctx, view));
  route("/groups/new", (params, view) => newGroupView(ctx, view));
  route("/groups/:group", (params, view) => groupView(ctx, view, params.group));
  route("/groups/:group/students", (params, view) => studentsView(ctx, view, params.group));
  route("/work", (params, view) =>
    pickGroup(ctx, view, { heading: "Задания", back: () => go("/"), open: (group) => `/work/g/${group.id}` }),
  );
  route("/work/g/:group", (params, view) => {
    pickAssignment(ctx, view, params.group, {
      back: () => go("/work"),
      open: (assignment) => `/work/a/${assignment.id}`,
      subtitle: (assignment) => `Формат сдачи: ${assignment.format_label}`,
    });
    view.main("Новое задание", () => go(`/work/g/${params.group}/new`));
  });
  route("/work/g/:group/new", (params, view) => newAssignmentView(ctx, view, params.group));
  route("/work/a/:assignment", (params, view) => assignmentView(ctx, view, params.assignment));
  route("/work/s/:submission", (params, view) =>
    attemptView(ctx, view, params.submission, { parent: (detail) => `/work/a/${detail.assignment.id}` }),
  );
}

function reviewView(ctx, view) {
  view.back(() => go("/"));
  mount(
    ctx.root,
    title("На проверку", "Сначала самые давние попытки"),
    takeFlash(ctx),
    pagedList(
      (offset) => api(`/review?offset=${offset}`),
      (attempt) =>
        item({
          text: `${attempt.student_name} · ${attempt.assignment.title}`,
          subtitle: `${attempt.group_name} · попытка №${attempt.attempt_number} · ${attempt.kind_label} · ${attempt.submitted_label}`,
          onClick: () => go(`/review/s/${attempt.id}`),
        }),
      { empty: "Новых решений нет." },
    ),
  );
}

function groupsView(ctx, view) {
  view.back(() => go("/"));
  view.main("Новая группа", () => go("/groups/new"));
  mount(
    ctx.root,
    title("Группы"),
    takeFlash(ctx),
    pagedList(
      (offset) => api(`/groups?offset=${offset}`),
      (group) => item({ text: group.name, onClick: () => go(`/groups/${group.id}`) }),
      { empty: "Групп пока нет. Создайте первую кнопкой «Новая группа»." },
    ),
  );
}

function newGroupView(ctx, view) {
  view.back(() => go("/groups"));
  const name = h("input", { type: "text", maxlength: String(NAME_LIMIT), placeholder: "Например, ИВТ-101" });
  const errors = h("div", {});
  const key = newKey();
  let busy = false;
  mount(ctx.root, title("Новая группа"), h("label", { class: "field" }, "Название", name), errors);
  view.main("Создать группу", async () => {
    if (busy) return;
    if (!name.value.trim()) {
      errors.replaceChildren(notice("Введите название группы.", "error"));
      return;
    }
    busy = true;
    setMainBusy(true);
    errors.replaceChildren();
    try {
      const result = await api("/groups", { method: "POST", key, body: { name: name.value } });
      haptic("success");
      ctx.flash = "Группа создана. Отправьте студентам ссылку-приглашение.";
      go(`/groups/${result.group.id}`, { replace: true });
    } catch (error) {
      busy = false;
      setMainBusy(false);
      haptic("error");
      errors.replaceChildren(failure(error));
    }
  });
}

async function groupView(ctx, view, groupId) {
  view.back(() => go("/groups"));
  const flash = takeFlash(ctx);
  mount(ctx.root, loading());
  let card;
  try {
    card = await api(`/groups/${groupId}`);
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, () => groupView(ctx, view, groupId)));
    return;
  }
  if (!view.alive()) return;
  const copied = h("div", {});
  const link = card.invite_link;
  mount(
    ctx.root,
    title(card.group.name, `Студентов: ${card.members}`),
    flash,
    section(
      "Ссылка-приглашение",
      h("p", { class: "link-text" }, link),
      h(
        "div",
        { class: "actions" },
        button("Поделиться", () => share(link, `Вступайте в группу «${card.group.name}» в LabDefender`), "primary"),
        button("Скопировать", async () => {
          const ok = await copyText(link);
          copied.replaceChildren(notice(ok ? "Ссылка скопирована." : "Скопируйте ссылку вручную.", ok ? "success" : "info"));
        }),
      ),
      copied,
    ),
    section(
      "Действия",
      h(
        "div",
        { class: "list" },
        item({ text: "👥 Посмотреть студентов", onClick: () => go(`/groups/${groupId}/students`) }),
        item({ text: "📝 Задания группы", onClick: () => go(`/work/g/${groupId}`) }),
        item({ text: "➕ Новое задание", onClick: () => go(`/work/g/${groupId}/new`) }),
        item({ text: "🗓 Слоты и очередь", onClick: () => go(`/slots/g/${groupId}`) }),
      ),
    ),
  );
}

// Names as the students set them (or from Telegram); usernames always come from Telegram.
function studentsView(ctx, view, groupId) {
  view.back(() => go(`/groups/${groupId}`));
  const header = title("Студенты группы", "Имя — как его указал студент, username — из Telegram");
  mount(
    ctx.root,
    header,
    pagedList(
      async (offset) => {
        const page = await api(`/groups/${groupId}/members?offset=${offset}`);
        if (view.alive()) header.querySelector("h1").textContent = page.group.name;
        return page;
      },
      (student) =>
        h(
          "div",
          { class: "row" },
          h("div", { class: "item-title" }, student.name),
          h("div", { class: "item-subtitle" }, student.username ? "@" + student.username : "нет username"),
        ),
      { empty: "В группе пока нет студентов. Отправьте им ссылку-приглашение." },
    ),
  );
}

function newAssignmentView(ctx, view, groupId) {
  view.back(() => go(`/work/g/${groupId}`));
  const formats = [
    ["online", "Онлайн-сдача"],
    ["in_person", "Очная сдача"],
  ];
  let format = null;
  const formatButtons = formats.map(([value, label]) =>
    button(label, () => {
      format = value;
      formatButtons.forEach((control, index) => control.classList.toggle("selected", formats[index][0] === value));
    }, "chip"),
  );
  const heading = h("input", { type: "text", maxlength: String(TITLE_LIMIT), placeholder: "Например, ЛР 1. Сортировки" });
  const description = h("textarea", { rows: "8", maxlength: String(DESCRIPTION_LIMIT), placeholder: "Условие задания" });
  const errors = h("div", {});
  const key = newKey();
  let busy = false;
  mount(
    ctx.root,
    title("Новое задание"),
    section("Формат сдачи", h("div", { class: "chips" }, formatButtons)),
    h("label", { class: "field" }, "Название", heading),
    h("label", { class: "field" }, "Условие", description),
    h("p", { class: "hint" }, "После публикации студенты группы получат уведомление."),
    errors,
  );
  view.main("Опубликовать", async () => {
    if (busy) return;
    const problems = [];
    if (!format) problems.push("выберите формат сдачи");
    if (!heading.value.trim()) problems.push("введите название");
    if (!description.value.trim()) problems.push("введите условие");
    if (problems.length) {
      errors.replaceChildren(notice("Чтобы опубликовать, " + problems.join(", ") + ".", "error"));
      return;
    }
    if (!(await confirm("Опубликовать задание? Студенты группы получат уведомление."))) return;
    busy = true;
    setMainBusy(true);
    errors.replaceChildren();
    try {
      const result = await api(`/groups/${groupId}/assignments`, {
        method: "POST",
        key,
        body: { title: heading.value, description: description.value, format },
      });
      haptic("success");
      ctx.flash = "Задание опубликовано. Студенты группы получат уведомление.";
      go(`/work/a/${result.assignment.id}`, { replace: true });
    } catch (error) {
      busy = false;
      setMainBusy(false);
      haptic("error");
      errors.replaceChildren(failure(error));
    }
  });
}

async function assignmentView(ctx, view, assignmentId) {
  view.back(() => go("/work"));
  const flash = takeFlash(ctx);
  mount(ctx.root, loading());
  let card;
  try {
    card = await api(`/assignments/${assignmentId}`);
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, () => assignmentView(ctx, view, assignmentId)));
    return;
  }
  if (!view.alive()) return;
  const { assignment, group } = card;
  view.back(() => go(`/work/g/${group.id}`));
  mount(
    ctx.root,
    title(assignment.title, `${group.name} · формат сдачи: ${assignment.format_label}`),
    flash,
    h("div", { class: "actions" }, button("🗓 Слоты защиты", () => go(`/slots/a/${assignmentId}`))),
    section(
      "Попытки студентов",
      pagedList(
        (offset) => api(`/assignments/${assignmentId}/submissions?offset=${offset}`),
        (attempt) =>
          item({
            text: `${attempt.student_name} · №${attempt.attempt_number}`,
            subtitle: `${attempt.status_label} · ${attempt.kind_label} · ${attempt.submitted_label}`,
            onClick: () => go(`/work/s/${attempt.id}`),
          }),
        { empty: "Решений пока нет." },
      ),
    ),
    section("Условие", textBlock(assignment.description)),
  );
}
