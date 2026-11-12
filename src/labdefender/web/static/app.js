// Entry point: checks that the page runs inside Telegram, loads the profile, starts routing.
import { api } from "./lib/api.js";
import { failure, h, mount, notice, title } from "./lib/dom.js";
import * as router from "./lib/router.js";
import { initData, setupChrome, webApp } from "./lib/telegram.js";
import { homeView } from "./views/home.js";
import { registerProfileRoutes } from "./views/profile.js";
import { registerReportRoutes } from "./views/reports.js";
import { registerSlotRoutes } from "./views/slots.js";
import { registerStudentRoutes } from "./views/student.js";
import { registerTeacherRoutes } from "./views/teacher.js";

const root = document.getElementById("app");
const SCREEN = /^(\/[a-z]+(\/\d+)?)+$/;

// Buttons under notifications open a screen directly: …/app/?screen=/review/s/42.
function launchScreen() {
  const screen = new URLSearchParams(location.search).get("screen");
  return screen && SCREEN.test(screen) ? screen : "/";
}

async function start() {
  const tg = webApp();
  if (!tg || !initData()) {
    mount(
      root,
      title("Откройте приложение из Telegram"),
      notice("Нажмите «Приложение» рядом с полем ввода в чате с ботом или отправьте боту /app."),
    );
    return;
  }
  tg.ready();
  tg.expand();
  setupChrome();
  let me;
  try {
    me = await api("/me");
  } catch (error) {
    mount(root, title("LabDefender"), failure(error, () => location.reload()));
    return;
  }
  // Shared by all screens: the profile, the root element and a one-time message.
  const ctx = { me, root, flash: null };
  router.route("/", (params, view) => homeView(ctx, view));
  registerSlotRoutes(ctx);
  registerStudentRoutes(ctx);
  registerTeacherRoutes(ctx);
  registerProfileRoutes(ctx);
  registerReportRoutes(ctx);
  router.start(launchScreen());
}

start().catch(() => mount(root, h("p", { class: "notice error" }, "Не удалось открыть приложение.")));
