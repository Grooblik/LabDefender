// New slot: date → start → duration → places → confirmation, like the chat wizard.
// The server gives the calendar and the free times of a day; it also validates the result.
import { api, newKey } from "../lib/api.js";
import { button, failure, h, loading, mount, notice, section, title } from "../lib/dom.js";
import { go } from "../lib/router.js";
import { haptic, setMainBusy } from "../lib/telegram.js";

const MAX_CAPACITY = 2147483647;

// A slot belongs to a group: students book any approved work of the group into it.
export function newSlotView(ctx, view, groupId) {
  const draft = { day: null, start: null, end: null, capacity: null };
  const steps = []; // Step history for the Back button inside the wizard.
  let groupName = "";

  function enter(step) {
    steps.push(step);
    step();
  }

  function replace(step) {
    steps.pop();
    enter(step);
  }

  function back() {
    steps.pop();
    const previous = steps.pop();
    if (previous) enter(previous);
    else go(`/slots/g/${groupId}`);
  }

  function screen(step, ...children) {
    view.main(null);
    mount(
      ctx.root,
      title("Новый слот", [groupName, step].filter(Boolean).join(" · ")),
      ...children,
    );
  }

  async function load(path, retry) {
    screen("", loading());
    try {
      return await api(path);
    } catch (error) {
      if (view.alive()) screen("", failure(error, retry));
      return null;
    }
  }

  async function dateStep(page) {
    const calendar = await load(`/groups/${groupId}/calendar?page=${page}`, () =>
      dateStep(page),
    );
    if (!calendar || !view.alive()) return;
    groupName = calendar.group.name;
    const cells = calendar.weekdays.map((name) => h("span", { class: "weekday" }, name));
    for (let index = 0; index < calendar.leading; index += 1) {
      cells.push(h("span", { class: "cell empty" }));
    }
    for (const day of calendar.days) {
      const label = day.day === 1 ? `${day.day}.${String(day.month).padStart(2, "0")}` : String(day.day);
      cells.push(
        h(
          "button",
          {
            type: "button",
            class: "cell" + (day.marked ? " marked" : "") + (day.today ? " today" : ""),
            onClick: () => enter(() => startStep(day.date)),
          },
          label,
        ),
      );
    }
    screen(
      "дата",
      h("p", { class: "hint" }, `${calendar.range_label}, время московское. Точка — на дату уже есть слоты.`),
      h("div", { class: "calendar" }, cells),
      h(
        "div",
        { class: "pager" },
        page > 0 && button("← Раньше", () => replace(() => dateStep(page - 1))),
        page < calendar.pages - 1 && button("Позже →", () => replace(() => dateStep(page + 1))),
      ),
    );
  }

  async function startStep(date) {
    const day = await load(`/groups/${groupId}/day?date=${date}`, () => startStep(date));
    if (!day || !view.alive()) return;
    draft.day = day;
    const busy = day.busy.length
      ? "Уже есть: " + day.busy.map((slot) => slot.time_label).join(", ") + "."
      : "Других слотов группы на этот день нет.";
    const children = [h("p", {}, `${day.date_label}, ${day.weekday}`), h("p", { class: "hint" }, busy)];
    if (day.template) {
      const template = day.template;
      children.push(
        button(
          `Как в прошлый раз: ${template.time_label} · мест: ${template.capacity}`,
          () => {
            Object.assign(draft, { start: template.start, end: template.end, capacity: template.capacity });
            enter(confirmStep);
          },
          "secondary wide",
        ),
      );
    }
    if (day.starts.length) {
      children.push(
        section(
          "Начало",
          h(
            "div",
            { class: "chips" },
            day.starts.map((start) =>
              button(start.time, () => {
                draft.start = start.time;
                enter(() => durationStep(start));
              }, "chip"),
            ),
          ),
        ),
      );
    } else {
      children.push(notice("Свободного времени в сетке нет — укажите время вручную.", "info"));
    }
    children.push(customTime());
    screen("время начала", ...children);
  }

  function customTime() {
    const start = h("input", { type: "time", step: "300", "aria-label": "Начало" });
    const end = h("input", { type: "time", step: "300", "aria-label": "Окончание" });
    const errors = h("div", {});
    return section(
      "Другое время",
      h("div", { class: "inputs" }, h("label", {}, "с ", start), h("label", {}, "до ", end)),
      errors,
      button("Далее", () => {
        if (!start.value || !end.value || start.value >= end.value) {
          errors.replaceChildren(notice("Укажите начало и окончание; окончание позже начала.", "error"));
          return;
        }
        draft.start = start.value.slice(0, 5);
        draft.end = end.value.slice(0, 5);
        enter(capacityStep);
      }),
    );
  }

  function durationStep(start) {
    screen(
      "длительность",
      h("p", {}, `${draft.day.date_label}, начало в ${start.time}`),
      h(
        "div",
        { class: "list" },
        start.durations.map((duration) =>
          button(`${duration.label} · до ${duration.end}`, () => {
            draft.end = duration.end;
            enter(capacityStep);
          }, "secondary wide"),
        ),
      ),
    );
  }

  function capacityStep() {
    const input = h("input", { type: "number", min: "1", step: "1", inputmode: "numeric", placeholder: "Число" });
    const errors = h("div", {});
    screen(
      "места",
      h("p", {}, `${draft.day.date_label}, ${draft.start}–${draft.end}`),
      h(
        "div",
        { class: "chips" },
        draft.day.capacities.map((value) =>
          button(String(value), () => {
            draft.capacity = value;
            enter(confirmStep);
          }, "chip"),
        ),
      ),
      section(
        "Другое число",
        h(
          "div",
          { class: "inputs" },
          input,
          button("Далее", () => {
            const value = Number(input.value);
            if (!Number.isInteger(value) || value < 1 || value > MAX_CAPACITY) {
              errors.replaceChildren(notice("Нужно целое положительное число мест.", "error"));
              return;
            }
            draft.capacity = value;
            enter(confirmStep);
          }),
        ),
        errors,
      ),
    );
  }

  function confirmStep() {
    const key = newKey();
    const errors = h("div", {});
    let busy = false;
    screen(
      "проверка",
      section(
        "Проверьте слот",
        h(
          "dl",
          { class: "summary" },
          h("dt", {}, "Дата"),
          h("dd", {}, `${draft.day.date_label}, ${draft.day.weekday}`),
          h("dt", {}, "Время"),
          h("dd", {}, `${draft.start}–${draft.end} (Москва)`),
          h("dt", {}, "Мест"),
          h("dd", {}, String(draft.capacity)),
        ),
      ),
      errors,
      h("p", { class: "hint" }, "Студенты группы с допущенными работами без записи получат уведомление о новом слоте."),
    );
    view.main("Создать слот", async () => {
      if (busy) return;
      busy = true;
      setMainBusy(true);
      errors.replaceChildren();
      try {
        const result = await api(`/groups/${groupId}/slots`, {
          method: "POST",
          key,
          body: { date: draft.day.date, start: draft.start, end: draft.end, capacity: draft.capacity },
        });
        haptic("success");
        ctx.flash = "Слот создан. Студенты с допущенными работами без записи получат уведомление.";
        go(`/slots/s/${result.slot.id}`, { replace: true });
      } catch (error) {
        haptic("error");
        busy = false;
        setMainBusy(false);
        errors.replaceChildren(failure(error));
      }
    });
  }

  view.back(back);
  enter(() => dateStep(0));
}
