import { failure, mount } from "./dom.js";
import { setBackAction, setMainAction } from "./telegram.js";

const routes = [];
let activeView;
let renderedHash;

export function route(pattern, handler) {
  routes.push({ parts: pattern.slice(1).split("/"), handler });
}

function findRoute(path) {
  const parts = path.slice(1).split("/");
  for (const { parts: pattern, handler } of routes) {
    if (pattern.length !== parts.length) continue;
    const params = {};
    const matches = pattern.every((part, index) => {
      if (!part.startsWith(":")) return part === parts[index];
      if (!/^\d+$/.test(parts[index])) return false;
      params[part.slice(1)] = parts[index];
      return true;
    });
    if (matches) return { handler, params };
  }
  return null;
}

async function render() {
  renderedHash = location.hash;
  const target = findRoute(renderedHash.slice(1)) || findRoute("/");
  setBackAction(null);
  setMainAction(null);
  const view = {
    alive: () => activeView === view,
    back: (action) => { if (view.alive()) setBackAction(action); },
    main: (label, action) => { if (view.alive()) setMainAction(label, action); },
  };
  activeView = view;
  try {
    await target.handler(target.params, view);
  } catch (error) {
    if (view.alive()) {
      mount(document.getElementById("app"), failure(error, render));
    }
  }
}

export function go(path, { replace = false } = {}) {
  const hash = `#${path}`;
  if (hash !== location.hash) {
    if (replace) history.replaceState(null, "", hash);
    else history.pushState(null, "", hash);
  }
  void render();
}

export function start(path) {
  const sync = () => {
    if (location.hash !== renderedHash) void render();
  };
  window.addEventListener("popstate", sync);
  window.addEventListener("hashchange", sync);
  go(path, { replace: true });
}
