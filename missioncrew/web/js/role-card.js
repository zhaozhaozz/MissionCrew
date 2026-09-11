/* ---- 角色悬停卡片:指针停在带 data-role-card 的元素上,弹出该角色的完整档案 ----
   聊天角色栏的按钮只放得下「@id 显示名」,定位/偏好/能力/执行组合要到项目设置才看得全;
   这里把它们做成悬停卡片,描述按 Markdown 排版(与文档预览同一渲染器)。触发元素只需带
   data-role-card="<角色 id>";卡片全页唯一、挂在 body 下 fixed 定位,贴触发元素上方弹出,
   上方放不下时翻到下方;指针挪进卡片本身不收起,长描述可以滚动阅读。 */
const ROLE_CARD_SHOW_DELAY = 220;    // 扫过角色栏不弹,停住才弹
const ROLE_CARD_SWITCH_DELAY = 70;   // 卡片已开着时在相邻角色间切换要跟手
const ROLE_CARD_HIDE_DELAY = 160;    // 留出把指针从按钮挪进卡片的时间
let roleCardTimer = 0;
let roleCardAnchor = null;           // 非空即卡片正开着,指向当前触发元素

function roleCardElement() {
  let card = document.getElementById("role-hover-card");
  if (card) return card;
  card = document.createElement("div");
  card.id = "role-hover-card";
  card.className = "role-hover-card";
  card.setAttribute("role", "tooltip");
  card.hidden = true;
  card.addEventListener("pointerenter", () => clearTimeout(roleCardTimer));
  card.addEventListener("pointerleave", scheduleHideRoleCard);
  document.body.appendChild(card);
  return card;
}

function roleCardHtml(role) {
  const isOrchestrator = projObj()?.orchestrator_role_id === role.id;
  const disabled = role.enabled === false;
  const backend = (overview.backends || []).find(item => item.id === role.runtime_id);
  const execution = role.runtime_id
    ? [backend?.name || role.runtime_id, role.model || "CLI 默认模型",
       role.effort ? `effort ${role.effort}` : ""].filter(Boolean).map(esc).join(" / ")
    : `<span class="muted">未绑定 runtime</span>`;
  const abilities = (role.capabilities || []).map(key =>
    `<span class="pill">${esc(traitMeta.abilities[key] || key)}</span>`).join("");
  const description = String(role.description || "").trim();
  return `
    <div class="role-hover-card-head">
      <span class="role-dot" style="background:${esc(role.color || "#888")}"></span>
      <b>@${esc(role.id)}</b>
      ${role.name ? `<span class="role-hover-card-name">${esc(role.name)}</span>` : ""}
      ${isOrchestrator ? `<span class="pill" style="color:var(--warn);border-color:var(--warn)">主控</span>` : ""}
      ${role.usage_linkage_enabled ? `<span class="pill">用量联动</span>` : ""}
      ${disabled ? `<span class="pill" title="${esc(roleDisabledReason(role))}">${
          role.usage_auto_disabled ? "用量停用" : "停用"}</span>` : ""}
    </div>
    <dl class="role-hover-card-meta">
      <dt>执行</dt><dd>${execution}</dd>
      <dt>能力</dt><dd>${abilities || `<span class="muted">—</span>`}</dd>
      <dt>偏好</dt><dd>${role.preference ? esc(role.preference) : `<span class="muted">—</span>`}</dd>
    </dl>
    <div class="role-hover-card-desc markdown-body">${description
      ? miniMarkdown(description) : `<p class="muted">尚未填写角色描述</p>`}</div>`;
}

function showRoleCard(anchor) {
  const role = projRoles().find(item => item.id === anchor.dataset.roleCard);
  if (!role || !document.contains(anchor)) return;
  const card = roleCardElement();
  card.innerHTML = roleCardHtml(role);
  roleCardAnchor = anchor;
  card.hidden = false;
  positionRoleCard(card, anchor);
}

/* 先量卡片自然高度:上方放得下(或上方比下方宽裕)就贴按钮上沿向上摆,否则贴下沿向下摆;
   高度按所选一侧的可用空间封顶,超出部分由描述区滚动。左缘对齐按钮,超出视口时向内收。 */
function positionRoleCard(card, anchor) {
  const gap = 8, margin = 12;
  const rect = anchor.getBoundingClientRect();
  const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
  const viewportHeight = document.documentElement.clientHeight || window.innerHeight;
  const spaceAbove = rect.top - gap - margin;
  const spaceBelow = viewportHeight - rect.bottom - gap - margin;
  card.style.maxHeight = "";
  const above = card.offsetHeight <= spaceAbove || spaceAbove >= spaceBelow;
  card.style.maxHeight = `${Math.max(above ? spaceAbove : spaceBelow, 120)}px`;
  const top = above ? rect.top - gap - card.offsetHeight : rect.bottom + gap;
  const left = Math.max(margin, Math.min(rect.left, viewportWidth - card.offsetWidth - margin));
  card.style.top = `${Math.max(top, margin)}px`;
  card.style.left = `${left}px`;
}

function hideRoleCard() {
  clearTimeout(roleCardTimer);
  roleCardTimer = 0;
  roleCardAnchor = null;
  const card = document.getElementById("role-hover-card");
  if (card) card.hidden = true;
}

function scheduleHideRoleCard() {
  clearTimeout(roleCardTimer);
  roleCardTimer = setTimeout(hideRoleCard, ROLE_CARD_HIDE_DELAY);
}

function scheduleShowRoleCard(anchor, delay) {
  clearTimeout(roleCardTimer);
  roleCardTimer = setTimeout(() => showRoleCard(anchor), delay);
}

const roleCardTrigger = target => target?.closest?.("[data-role-card]") || null;

// 只响应鼠标/触控笔:触摸没有悬停,点按直接走按钮本身的动作
document.addEventListener("pointerover", event => {
  if (event.pointerType === "touch") return;
  const trigger = roleCardTrigger(event.target);
  if (!trigger) return;
  clearTimeout(roleCardTimer);   // 回到触发元素上:撤销待执行的收起
  if (trigger === roleCardAnchor) return;
  scheduleShowRoleCard(trigger, roleCardAnchor ? ROLE_CARD_SWITCH_DELAY : ROLE_CARD_SHOW_DELAY);
});
document.addEventListener("pointerout", event => {
  const trigger = roleCardTrigger(event.target);
  if (!trigger) return;
  const next = event.relatedTarget;
  if (next && (trigger.contains(next) || roleCardElement().contains(next))) return;
  scheduleHideRoleCard();
});
// 键盘 Tab 到按钮上同样展示;鼠标点按得到的焦点不算(:focus-visible 为假)
document.addEventListener("focusin", event => {
  const trigger = roleCardTrigger(event.target);
  if (trigger && trigger.matches(":focus-visible")) { clearTimeout(roleCardTimer); showRoleCard(trigger); }
});
document.addEventListener("focusout", event => {
  if (roleCardAnchor && roleCardTrigger(event.target) === roleCardAnchor) hideRoleCard();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && roleCardAnchor) hideRoleCard();
});
// 点按触发元素(插入 @)或卡片以外任何地方都收起;卡片内点击(选文字/滚动条)不受影响
document.addEventListener("click", event => {
  if (roleCardTrigger(event.target) || (roleCardAnchor && !roleCardElement().contains(event.target)))
    hideRoleCard();
});
// 页面任一滚动容器滚动或视口变化后按钮位置会变,直接收起,避免卡片悬在错位处
document.addEventListener("scroll", event => {
  if (roleCardAnchor && !roleCardElement().contains(event.target)) hideRoleCard();
}, true);
window.addEventListener("resize", () => { if (roleCardAnchor) hideRoleCard(); });
