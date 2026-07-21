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

loadOverview();
setInterval(pollMessages, 2000);
setInterval(pollConfigChat, 2000);
setInterval(pollRuntimeStatus, 1000);
setInterval(() => loadOverview().catch(() => {}), 8000);  // 服务重启间隙静默跳过
