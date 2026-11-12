let backAction;
let mainAction;
let mainBusy = false;

export function webApp() {
  return window.Telegram?.WebApp;
}

export function initData() {
  return webApp()?.initData || "";
}

function client() {
  const app = webApp();
  return app?.platform && app.platform !== "unknown" ? app : null;
}

function backButton() {
  const app = client();
  return app?.isVersionAtLeast("6.1") ? app.BackButton : null;
}

function runBack() {
  backAction?.();
}

function runMain() {
  if (!mainBusy) mainAction?.();
}

export function setupChrome() {
  backButton()?.onClick(runBack);
  client()?.MainButton.onClick(runMain);
  document.getElementById("back").addEventListener("click", runBack);
  document.getElementById("main-button").addEventListener("click", runMain);
}

export function setBackAction(action) {
  backAction = action;
  const nativeButton = backButton();
  document.getElementById("back-row").hidden = Boolean(nativeButton) || !action;
  if (action) nativeButton?.show();
  else nativeButton?.hide();
}

export function setMainAction(label, action) {
  mainAction = action;
  setMainBusy(false);
  const nativeButton = client()?.MainButton;
  document.getElementById("main-action").hidden = Boolean(nativeButton) || !label;
  if (label) {
    document.getElementById("main-button").textContent = label;
    nativeButton?.setText(label);
    nativeButton?.show();
  } else {
    nativeButton?.hide();
  }
}

export function setMainBusy(busy) {
  mainBusy = busy;
  document.getElementById("main-button").disabled = busy;
  const nativeButton = client()?.MainButton;
  if (busy) nativeButton?.showProgress();
  else {
    nativeButton?.hideProgress();
    nativeButton?.enable();
  }
}

export function confirm(message) {
  const app = client();
  if (app?.isVersionAtLeast("6.2")) {
    return new Promise((resolve) => app.showConfirm(message, resolve));
  }
  return Promise.resolve(window.confirm(message));
}

export function haptic(type) {
  const app = client();
  if (app?.isVersionAtLeast("6.1")) app.HapticFeedback.notificationOccurred(type);
}

export function closeApp() {
  client()?.close();
}

export function openLink(value) {
  const url = new URL(value);
  if (url.protocol !== "http:" && url.protocol !== "https:") return;
  const app = client();
  if (app?.isVersionAtLeast("6.1")) app.openLink(url.href);
  else window.open(url.href, "_blank", "noopener,noreferrer");
}

export function share(link, text) {
  const url = new URL("https://t.me/share/url");
  url.search = new URLSearchParams({ url: link, text });
  const app = client();
  if (app?.isVersionAtLeast("6.1")) app.openTelegramLink(url.href);
  else window.open(url.href, "_blank", "noopener,noreferrer");
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
