/* ---------------- 通用请求 ---------------- */
async function api(method, url, body) {
  const r = await fetch(url, {
    method, headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    toast(err.detail || `请求失败 (${r.status})`, "error", 5000);   // 非阻塞报错,不打断当前页面
    throw new Error("api");
  }
  return r.json();
}

let traitMeta = { abilities: {}, tiers: [] };
async function ensureTraits() {
  if (!Object.keys(traitMeta.abilities).length)
    traitMeta = await (await fetch("/api/traits")).json();
}

