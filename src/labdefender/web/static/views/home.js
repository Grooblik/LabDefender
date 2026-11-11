import { h, item, mount, section, title } from "../lib/dom.js";
import { go } from "../lib/router.js";
import { takeFlash } from "./common.js";

const SECTIONS = {
  teacher: [
    { text: "📥 На проверку", subtitle: "Новые решения во всех группах", path: "/review" },
    { text: "📝 Задания", subtitle: "Публикация, попытки студентов", path: "/work" },
    { text: "🗓 Слоты и очередь", subtitle: "Календарь, новые слоты, очереди", path: "/slots" },
    { text: "👥 Группы", subtitle: "Новая группа, ссылка-приглашение", path: "/groups" },
    { text: "🔔 Уведомления", subtitle: "Какие уведомления присылать", path: "/notifications" },
  ],
  student: [
    { text: "📚 Задания", subtitle: "Условия, статусы, сдача решений", path: "/tasks" },
    { text: "🗓 Защита и очередь", subtitle: "Запись на слот, позиция в очереди", path: "/defense" },
  ],
};

function profileEntry(me) {
  const parts = ["Имя", "Telegram ID"];
  if (me.role === "student") parts.push("доступ преподавателя");
  if (me.admin) parts.push("выдача роли");
  return { text: "👤 Профиль", subtitle: parts.join(", "), path: "/profile" };
}

export function homeView(ctx, view) {
  const sections = [...(SECTIONS[ctx.me.role] || SECTIONS.student), profileEntry(ctx.me)];
  mount(
    ctx.root,
    title("LabDefender", `${ctx.me.name} · ${ctx.me.role_label}`),
    takeFlash(ctx),
    section(
      "Разделы",
      h(
        "div",
        { class: "list" },
        sections.map((entry) => item({ text: entry.text, subtitle: entry.subtitle, onClick: () => go(entry.path) })),
      ),
    ),
    h(
      "p",
      { class: "hint" },
      "Файлы PDF/ZIP отправляются в чат с ботом. Все разделы есть и там — кнопками под полем ввода.",
    ),
  );
  view.back(null);
}
