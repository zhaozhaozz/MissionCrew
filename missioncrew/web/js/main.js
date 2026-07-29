/* ---- 侧栏宽度可调:拖拽手柄,宽度跨会话记忆,双击恢复默认 ---- */
(() => {
  const sidebar = document.getElementById("sidebar");
  const resizer = document.getElementById("side-resizer");
  const clamp = w => Math.min(Math.max(w, 170), 480);
  const saved = parseInt(localStorage.getItem("mc.sidebarWidth") || "", 10);
  if (saved) sidebar.style.width = clamp(saved) + "px";
  resizer.addEventListener("dblclick", () => {
    sidebar.style.width = "220px";
    localStorage.removeItem("mc.sidebarWidth");
  });
  resizer.addEventListener("mousedown", e => {
    e.preventDefault();
    resizer.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    const move = ev => { sidebar.style.width = clamp(ev.clientX) + "px"; };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      resizer.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      localStorage.setItem("mc.sidebarWidth", parseInt(sidebar.style.width, 10));
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
})();

/* ---- 移动端:侧栏改抽屉,点遮罩或选中条目后自动收起 ---- */
(() => {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  const toggle = document.getElementById("sidebar-toggle");
  const mobileLayout = matchMedia("(max-width: 820px)");
  const setOpen = open => {
    sidebar.classList.toggle("open", open);
    backdrop.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
  };
  window.toggleMobileSidebar = () => setOpen(!sidebar.classList.contains("open"));
  window.closeMobileSidebar = () => setOpen(false);
  // 选中频道/面板/角色或点击导航后收起抽屉,展开/折叠分区不收起
  sidebar.addEventListener("click", e => {
    if (mobileLayout.matches
        && e.target.closest(".side-item, .side-nav, .role-chip, .sec-act .icon-btn"))
      closeMobileSidebar();
  });
  mobileLayout.addEventListener("change", m => { if (!m.matches) setOpen(false); });
})();

loadOverview();
pollRuntimeStatus();
setInterval(pollMessages, 2000);
setInterval(pollConfigChat, 2000);
setInterval(pollRuntimeStatus, 10000);
setInterval(() => loadOverview().catch(() => {}), 8000);  // 服务重启间隙静默跳过
