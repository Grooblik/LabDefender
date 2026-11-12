// Reports: a discipline table (teacher), a student's report (teacher: own disciplines only;
// student: own report everywhere). The same tables come to the chat as CSV files.
import { api } from "../lib/api.js";
import { button, failure, h, loading, mount, notice, section, title } from "../lib/dom.js";
import { go, route } from "../lib/router.js";
import { haptic } from "../lib/telegram.js";
import { pickGroup } from "./common.js";

export function registerReportRoutes(ctx) {
  route("/reports", (params, view) =>
    pickGroup(ctx, view, { heading: "Отчёты", back: () => go("/"), open: (group) => `/groups/${group.id}/report` }),
  );
  route("/groups/:group/report", (params, view) => groupReportView(ctx, view, params.group));
  route("/students/:student/report", (params, view) =>
    studentReportView(ctx, view, `/students/${params.student}/report`),
  );
  route("/report", (params, view) => studentReportView(ctx, view, "/me/report"));
}

const LEGEND = "✅ допущено (дата) · ⏳ на проверке (дата) · 🔄 на доработке (×попыток) · — не сдано · 🗓 запись на защиту";

// Sends the CSV of the report to the chat with the bot; the result is shown in `place`.
function fileButton(path, place) {
  let busy = false;
  return button("Прислать таблицу в чат", async () => {
    if (busy) return;
    busy = true;
    place.replaceChildren();
    try {
      await api(path, { method: "POST" });
      haptic("success");
      place.replaceChildren(notice("Файл CSV отправлен в чат с ботом. Он открывается в Excel.", "success"));
    } catch (error) {
      haptic("error");
      place.replaceChildren(failure(error));
    } finally {
      busy = false;
    }
  });
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

async function groupReportView(ctx, view, groupId) {
  view.back(() => go(`/groups/${groupId}`));
  const data = await load(ctx, view, `/groups/${groupId}/report`, () => groupReportView(ctx, view, groupId));
  if (!data) return;
  const { assignments, rows, totals } = data;
  const detail = h("div", {});
  const sent = h("div", {});

  function show(row, assignment, cell) {
    detail.replaceChildren(notice(`${row.student.name} · ${assignment.title}: ${cell.full}`, "info"));
  }

  const table = h(
    "table",
    { class: "report" },
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", {}, "Студент"),
        assignments.map((assignment) => h("th", { title: assignment.title }, assignment.title)),
        h("th", {}, "Итого"),
      ),
    ),
    h(
      "tbody",
      {},
      rows.map((row) =>
        h(
          "tr",
          {},
          h(
            "th",
            { scope: "row" },
            h(
              "button",
              { type: "button", class: "link", onClick: () => go(`/students/${row.student.id}/report`) },
              row.student.name,
            ),
            row.student.username && h("div", { class: "item-subtitle" }, "@" + row.student.username),
          ),
          row.cells.map((cell, index) =>
            h(
              "td",
              { class: "cell-" + (cell.status || "none"), onClick: () => show(row, assignments[index], cell) },
              cell.short,
            ),
          ),
          h("td", {}, `${row.approved}/${assignments.length}`),
        ),
      ),
    ),
    h(
      "tfoot",
      {},
      h(
        "tr",
        {},
        h("th", { scope: "row" }, "Допущено"),
        totals.map((total) => h("td", {}, `${total.approved}/${rows.length}`)),
        h("td", {}, ""),
      ),
    ),
  );

  mount(
    ctx.root,
    title("Отчёт по дисциплине", `${data.group.name} · студентов ${rows.length}, заданий ${assignments.length}`),
    rows.length && assignments.length
      ? [
          h("div", { class: "table-wrap" }, table),
          h("p", { class: "hint" }, "Нажмите на ячейку — внизу появятся подробности; на имя — отчёт студента."),
          detail,
          h("p", { class: "hint" }, LEGEND),
        ]
      : notice(rows.length ? "В группе пока нет заданий." : "В группе пока нет студентов.", "info"),
    h("div", { class: "actions" }, fileButton(`/groups/${groupId}/report/file`, sent)),
    sent,
  );
}

async function studentReportView(ctx, view, path) {
  view.back(() => (ctx.me.role === "teacher" ? history.back() : go("/")));
  const data = await load(ctx, view, path, () => studentReportView(ctx, view, path));
  if (!data) return;
  const sent = h("div", {});
  const own = path === "/me/report";
  mount(
    ctx.root,
    title(
      own ? "Мой отчёт" : data.student.name,
      own ? "Все дисциплины" : ["Ваши дисциплины", data.student.username && "@" + data.student.username].filter(Boolean).join(" · "),
    ),
    data.disciplines.length
      ? data.disciplines.map((discipline) =>
          section(
            `${discipline.group.name} · допущено ${discipline.approved} из ${discipline.total}`,
            discipline.rows.length
              ? h(
                  "div",
                  { class: "list" },
                  discipline.rows.map((row) =>
                    h(
                      "div",
                      { class: "row" },
                      h("div", { class: "item-title" }, `${row.short.split(" ")[0]} ${row.assignment.title}`),
                      h("div", { class: "item-subtitle" }, row.full),
                    ),
                  ),
                )
              : h("p", { class: "hint" }, "Заданий пока нет."),
          ),
        )
      : notice(own ? "Вы пока не состоите в группах." : "Нет общих дисциплин.", "info"),
    h("div", { class: "actions" }, fileButton(`${path}/file`, sent)),
    sent,
  );
}
