import { initData } from "./telegram.js";

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function api(path, { method = "GET", body, key } = {}) {
  const headers = { Authorization: `tma ${initData()}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;

  let response;
  try {
    response = await fetch(`../api${path}`, {
      method,
      headers,
      body: JSON.stringify(body),
      redirect: "error",
    });
  } catch {
    throw new ApiError(0, "Не удалось связаться с ботом. Попробуйте ещё раз.");
  }

  const data = await response.json().catch(() => null);
  if (!response.ok || data === null) {
    throw new ApiError(response.status, data?.error || "Не удалось выполнить запрос.");
  }
  return data;
}

export function newKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
