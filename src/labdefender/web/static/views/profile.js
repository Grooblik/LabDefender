// Profile for both roles: Telegram ID, the teacher code, the role grant (administrators only)
// and the teacher's notification settings.
import { api } from "../lib/api.js";
import { button, failure, h, item, loading, mount, notice, section, title } from "../lib/dom.js";
import { go, route } from "../lib/router.js";
import { confirm, copyText, haptic, setMainBusy } from "../lib/telegram.js";
import { takeFlash } from "./common.js";

const CODE_LIMIT = 4096;
const NAME_LIMIT = 128;
const TELEGRAM_ID = /^\d{1,19}$/;

export function registerProfileRoutes(ctx) {
  route("/profile", (params, view) => profileView(ctx, view));
  route("/profile/name", (params, view) => nameView(ctx, view));
  route("/profile/teacher", (params, view) => teacherCodeView(ctx, view));
  route("/profile/grant", (params, view) => grantView(ctx, view));
  route("/notifications", (params, view) => notificationsView(ctx, view));
}

function profileView(ctx, view) {
  view.back(() => go("/"));
  const { me } = ctx;
  const id = String(me.telegram_id);
  const copied = h("div", {});
  const blocks = [
    title("Профиль", `${me.name} · ${me.role_label}`),
    takeFlash(ctx),
    section(
      "Имя",
      item({
        text: me.name,
        subtitle: me.custom_name ? `Своё имя · в Telegram: ${me.telegram_name}` : "Из Telegram",
        badge: "изменить",
        onClick: () => go("/profile/name"),
      }),
      h("p", { class: "hint" }, "Так вас видят в списках группы, очереди и уведомлениях."),
    ),
    section(
      "Telegram ID",
      h("p", { class: "link-text" }, id),
      h(
        "div",
        { class: "actions" },
        button("Скопировать", async () => {
          const ok = await copyText(id);
          copied.replaceChildren(notice(ok ? "ID скопирован." : "Скопируйте ID вручную.", ok ? "success" : "info"));
        }),
      ),
      copied,
      h("p", { class: "hint" }, "Этот ID нужен администратору, чтобы выдать роль преподавателя."),
    ),
  ];
  if (me.role === "student") {
    blocks.push(
      section(
        "Доступ преподавателя",
        me.can_register
          ? item({ text: "🎓 Я преподаватель", subtitle: "Ввести код преподавателя", onClick: () => go("/profile/teacher") })
          : h(
              "p",
              { class: "hint" },
              "Регистрация по коду отключена. Роль может выдать администратор — отправьте ему свой Telegram ID.",
            ),
      ),
      section(
        "Уведомления",
        h(
          "p",
          { class: "hint" },
          "Студентам уведомления приходят всегда: новые задания, результат проверки и новые слоты защиты.",
        ),
      ),
    );
  } else {
    blocks.push(
      section(
        "Уведомления",
        item({ text: "🔔 Уведомления", subtitle: "Какие уведомления присылать", onClick: () => go("/notifications") }),
      ),
    );
  }
  if (me.admin) {
    blocks.push(
      section(
        "Администратор",
        item({ text: "👤 Выдать роль преподавателя", subtitle: "По Telegram ID", onClick: () => go("/profile/grant") }),
      ),
    );
  }
  mount(ctx.root, ...blocks);
}

function nameView(ctx, view) {
  view.back(() => go("/profile"));
  const input = h("input", {
    type: "text",
    autocomplete: "name",
    maxlength: String(NAME_LIMIT),
    placeholder: "Имя и фамилия",
  });
  input.value = ctx.me.name;
  const errors = h("div", {});
  let busy = false;

  async function change(request, flash) {
    if (busy) return;
    busy = true;
    setMainBusy(true);
    errors.replaceChildren();
    try {
      ctx.me = await request();
      haptic("success");
      ctx.flash = flash(ctx.me);
      if (view.alive()) go("/profile", { replace: true });
    } catch (error) {
      busy = false;
      haptic("error");
      if (!view.alive()) return;
      setMainBusy(false);
      errors.replaceChildren(failure(error));
    }
  }

  function save() {
    if (!input.value.trim()) {
      errors.replaceChildren(notice("Введите имя.", "error"));
      return;
    }
    change(
      () => api("/me/name", { method: "PUT", body: { name: input.value } }),
      (me) => `Имя сохранено: «${me.name}».`,
    );
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") save();
  });
  mount(
    ctx.root,
    title("Имя", "Так вас видят преподаватель и студенты"),
    h("label", { class: "field" }, "Имя и фамилия", input),
    h(
      "p",
      { class: "hint" },
      "Можно указать имя, если в Telegram оно другое. Username из Telegram показывается рядом и не меняется.",
    ),
    ctx.me.custom_name &&
      h(
        "div",
        { class: "actions" },
        button(`Вернуть имя из Telegram: ${ctx.me.telegram_name}`, () =>
          change(
            () => api("/me/name", { method: "DELETE" }),
            (me) => `Имя из Telegram восстановлено: «${me.name}».`,
          ),
        ),
      ),
    errors,
  );
  view.main("Сохранить", save);
}

function teacherCodeView(ctx, view) {
  view.back(() => go("/profile"));
  // The code is a secret: hidden while typed, never kept after a request.
  const code = h("input", {
    type: "password",
    autocomplete: "off",
    maxlength: String(CODE_LIMIT),
    placeholder: "Код преподавателя",
  });
  const errors = h("div", {});
  let busy = false;
  async function submit() {
    if (busy) return;
    if (!code.value.trim()) {
      errors.replaceChildren(notice("Введите код преподавателя.", "error"));
      return;
    }
    busy = true;
    setMainBusy(true);
    errors.replaceChildren();
    const value = code.value;
    code.value = "";
    try {
      const result = await api("/teacher/register", { method: "POST", body: { code: value } });
      ctx.me = result.me;
      haptic("success");
      ctx.flash = result.changed
        ? "Доступ преподавателя открыт. Меню в чате с ботом тоже обновлено."
        : "У вас уже есть доступ преподавателя.";
      if (view.alive()) go("/", { replace: true });
    } catch (error) {
      busy = false;
      haptic("error");
      if (!view.alive()) return;
      setMainBusy(false);
      errors.replaceChildren(failure(error));
      code.focus();
    }
  }
  code.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit();
  });
  mount(
    ctx.root,
    title("Доступ преподавателя", "Код выдаёт администратор курса"),
    h("label", { class: "field" }, "Код", code),
    h("p", { class: "hint" }, "После пяти неверных попыток ввод блокируется на 10 минут."),
    errors,
  );
  view.main("Подтвердить", submit);
}

function grantView(ctx, view) {
  view.back(() => go("/profile"));
  const input = h("input", {
    type: "text",
    inputmode: "numeric",
    autocomplete: "off",
    maxlength: "19",
    placeholder: "Например, 123456789",
  });
  const result = h("div", {});
  let busy = false;
  async function submit() {
    if (busy) return;
    const value = input.value.trim();
    if (!TELEGRAM_ID.test(value)) {
      result.replaceChildren(notice("Нужен числовой Telegram ID.", "error"));
      return;
    }
    if (!(await confirm(`Выдать роль преподавателя пользователю с ID ${value}?`))) return;
    busy = true;
    setMainBusy(true);
    result.replaceChildren();
    try {
      const answer = await api("/admin/grant", { method: "POST", body: { telegram_id: value } });
      haptic("success");
      if (!view.alive()) return;
      input.value = "";
      result.replaceChildren(
        notice(
          answer.changed ? `Роль преподавателя выдана (ID ${value}).` : `У пользователя ${value} уже есть роль преподавателя.`,
          "success",
        ),
      );
    } catch (error) {
      haptic("error");
      if (view.alive()) result.replaceChildren(failure(error));
    } finally {
      busy = false;
      if (view.alive()) setMainBusy(false);
    }
  }
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit();
  });
  mount(
    ctx.root,
    title("Роль преподавателя", "Только для администраторов из ADMIN_IDS"),
    h("label", { class: "field" }, "Telegram ID пользователя", input),
    h(
      "p",
      { class: "hint" },
      "Пользователь узнаёт свой ID в профиле приложения или командой /id в чате. " +
        "Сначала он должен открыть бота или приложение.",
    ),
    result,
  );
  view.main("Выдать роль", submit);
}

async function notificationsView(ctx, view) {
  view.back(() => go("/"));
  mount(ctx.root, loading());
  let state;
  try {
    state = await api("/notifications");
  } catch (error) {
    if (view.alive()) mount(ctx.root, failure(error, () => notificationsView(ctx, view)));
    return;
  }
  if (!view.alive()) return;
  let busy = false;
  const list = h("div", { class: "list" });
  const bulk = h("div", { class: "actions" });
  const status = h("div", {});

  function draw() {
    list.replaceChildren(
      ...state.categories.map((category) =>
        item({
          text: category.label,
          subtitle: category.enabled ? "Присылать" : "Не присылать",
          badge: category.enabled ? "✅ вкл." : "🔕 выкл.",
          onClick: () => change(category.key, !category.enabled),
        }),
      ),
    );
    const enabled = state.categories.map((category) => category.enabled);
    bulk.replaceChildren(
      ...[
        enabled.some(Boolean) && button("Выключить все", () => change("all", false)),
        enabled.some((value) => !value) && button("Включить все", () => change("all", true)),
      ].filter(Boolean),
    );
  }

  async function change(category, enabled) {
    if (busy) return;
    busy = true;
    status.replaceChildren();
    try {
      state = await api("/notifications", { method: "PUT", body: { category, enabled } });
      if (!view.alive()) return;
      draw();
      status.replaceChildren(notice("Настройки уведомлений сохранены.", "success"));
    } catch (error) {
      if (view.alive()) status.replaceChildren(failure(error));
    } finally {
      busy = false;
    }
  }

  draw();
  mount(
    ctx.root,
    title("Уведомления", "Нажмите на категорию, чтобы включить или выключить её"),
    list,
    bulk,
    status,
    h(
      "p",
      { class: "hint" },
      "Под каждым уведомлением в чате есть «🔕 Не присылать такие». Студентам уведомления приходят всегда.",
    ),
  );
}
