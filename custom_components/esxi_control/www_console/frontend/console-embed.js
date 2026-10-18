/*!
 * ESXi Control —— Home Assistant 前端悬浮窗模块
 *
 * 通过 homeassistant.components.frontend.add_extra_js_url 注入到 HA 前端。
 * 两类入口、两个**互相独立**的悬浮窗（可同时打开，互不替换）：
 *
 *   /esxi_control/console?...  → 虚拟机设备页「访问」：那一台 VM 的 WebMKS 控制台
 *   /esxi_control/admin?...    → 主机设备页「访问」：主机级虚拟机管理（建机/改配/删除）
 *
 * 拦截设备页「访问」链接的点击（链接在 ha-button 的 Shadow DOM 里，用
 * composedPath 穿透），阻止整页跳转，改为在 HA 页面内弹出可拖动、可缩放的
 * 悬浮窗，窗内用 iframe 原样加载签名页面。
 *
 * 纯原生 JS + Shadow DOM，不依赖 HA 前端任何内部实现，升级风险最小。
 */
(() => {
  "use strict";

  // pathname → 窗口类型。各类型窗口彼此独立（容器、iframe、生命周期分开）
  const PAGE_KINDS = {
    "/esxi_control/console": "console",
    "/esxi_control/admin": "admin",
  };
  const HOST_ID = "esxi-float-host";

  const KIND_CONF = {
    console: {
      title: "虚拟机控制台",
      width: "min(1120px, 94vw)",
      height: "min(820px, 86vh)",
    },
    admin: {
      title: "虚拟机管理",
      width: "min(1280px, 96vw)",
      height: "min(900px, 92vh)",
    },
  };

  const CSS = `
    .ec-window {
      position: fixed;
      z-index: 1;
      display: none;
      flex-direction: column;
      min-width: 320px;
      min-height: 240px;
      background: #11151c;
      border: 1px solid #2f3c4e;
      border-radius: 12px;
      box-shadow: 0 14px 48px rgba(0, 0, 0, .55);
      overflow: hidden;
      resize: both;
      pointer-events: auto;
    }
    .ec-window.show { display: flex; }
    .ec-window.ec-flash { animation: ec-pop .35s ease; }
    @keyframes ec-pop {
      0% { transform: scale(.97); }
      60% { transform: scale(1.012); }
      100% { transform: scale(1); }
    }
    .ec-head {
      display: flex;
      align-items: center;
      gap: 8px;
      height: 42px;
      padding: 0 10px;
      background: #1b222d;
      border-bottom: 1px solid #2f3c4e;
      cursor: move;
      touch-action: none;
      color: #e7edf5;
      flex: 0 0 auto;
    }
    .ec-grip { color: #6b7787; font-size: 14px; letter-spacing: -2px; }
    .ec-title { font-size: 13.5px; font-weight: 600; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis; }
    .ec-spacer { flex: 1; }
    .ec-btn {
      appearance: none;
      border: 1px solid #2f3c4e;
      background: #202a38;
      color: #e7edf5;
      border-radius: 7px;
      width: 30px;
      height: 28px;
      font-size: 15px;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
      padding: 0;
    }
    .ec-btn:hover { background: #2a3646; border-color: #2f9bff; }
    .ec-x:hover { background: #5a2327; border-color: #f0564a; }
    .ec-body { flex: 1 1 auto; position: relative; background: #000; }
    .ec-body iframe { position: absolute; inset: 0; width: 100%; height: 100%;
      border: 0; background: #fff; }
    .ec-window[data-kind=console] .ec-body { background: #000; }
    .ec-window[data-kind=console] .ec-body iframe { background: #000; }
  `;

  // HA 设备页的「访问」渲染为 <ha-button href target="_blank">，其内部的 <a>
  // 位于 ha-button 的 Shadow DOM 中；下拉菜单里的备用 <a> 也在卡片组件的
  // shadow root 里。普通 closest("a") 穿不过 shadow 边界，必须走
  // composedPath() 拿到事件穿过的全部节点（含 shadow 内部）。
  function linkFromEvent(e) {
    let path = null;
    try { path = typeof e.composedPath === "function" ? e.composedPath() : null; }
    catch (_) { path = null; }
    if (!path || !path.length) {
      path = [];
      let n = e.target;
      while (n) {
        path.push(n);
        const root = n.getRootNode ? n.getRootNode() : null;
        n = n.parentNode || (root && root.host) || null;
      }
    }
    for (const node of path) {
      if (!node || node.nodeType !== 1) { continue; }  // 只看 Element
      let href = null;
      if (node.tagName === "A") { href = node.href; }
      if (!href && typeof node.getAttribute === "function") {
        href = node.getAttribute("href");  // ha-button 的 href 属性
      }
      if (!href) { continue; }
      let u;
      try { u = new URL(href, location.href); } catch (_) { continue; }
      const kind = PAGE_KINDS[u.pathname];
      // 只拦截两类页面本身（静态资源 / 票据 / WS / API 路径一律放行）
      if (kind && u.searchParams.has("authSig")) {
        // 关键：主机地址一律以**浏览器当前地址栏**为准，而不是设备链接里存的
        // 那个 HA 自探测/历史学习地址（端口映射、反向代理或换过访问 IP 后，
        // 链接里的 host 往往不可达）。authSig 只签 path+query 不签 host，
        // 换 host 后签名依然有效；页面内票据 / API 请求都是相对路径，
        // iframe 一旦走地址栏 host，内部全部自动跟随。
        try { u = new URL(u.pathname + u.search, window.location.origin); }
        catch (_) { /* 解析失败就用原链接 */ }
        return { kind, url: u, node };
      }
    }
    return null;
  }

  // 把命中的链接原地改写为地址栏主机地址：连 Ctrl/中键的原生新标签、
  //   ha-button 内部可能触发的 window.open 也一并覆盖到（不拦截默认行为）。
  function rewriteLinkToLocation(match) {
    if (!match) { return; }
    try {
      if (match.node.getAttribute && match.node.getAttribute("href")) {
        match.node.setAttribute("href", match.url.href);
      }
    } catch (_) { /* 只读 DOM 改不动时静默，悬浮窗路径仍可用 */ }
  }

  function ensureHost() {
    let host = document.getElementById(HOST_ID);
    if (host) { return host; }
    host = document.createElement("div");
    host.id = HOST_ID;
    // 容器本身不挡鼠标，只有悬浮窗本体接收事件 —— 非模态，
    // 窗体外仍可正常操作 Home Assistant
    host.style.cssText =
      "position:fixed;inset:0;z-index:999999;pointer-events:none";
    const shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);
    document.body.appendChild(host);
    return host;
  }

  function centerWindow(win) {
    const w = win.offsetWidth || Math.min(1120, window.innerWidth * 0.94);
    const h = win.offsetHeight || Math.min(820, window.innerHeight * 0.86);
    // 两类窗口错开一点，同时打开时不会完全重叠
    const offset = win.dataset.kind === "admin" ? 26 : 0;
    win.style.left = Math.max(8, (window.innerWidth - w) / 2 + offset) + "px";
    win.style.top = Math.max(8, (window.innerHeight - h) / 2 - 10 - offset) + "px";
  }

  function enableDrag(win, head) {
    head.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".ec-btn")) { return; }
      e.preventDefault();
      const offX = e.clientX - win.offsetLeft;
      const offY = e.clientY - win.offsetTop;
      const move = (ev) => {
        const x = Math.max(-win.offsetWidth + 120,
          Math.min(window.innerWidth - 60, ev.clientX - offX));
        const y = Math.max(0,
          Math.min(window.innerHeight - 40, ev.clientY - offY));
        win.style.left = x + "px";
        win.style.top = y + "px";
      };
      const up = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    });
  }

  function buildWindow(root, kind) {
    const conf = KIND_CONF[kind];
    const win = document.createElement("div");
    win.className = "ec-window";
    win.dataset.kind = kind;
    win.style.width = conf.width;
    win.style.height = conf.height;
    win.innerHTML = `
      <div class="ec-head">
        <span class="ec-grip">⠿</span>
        <span class="ec-title">${conf.title}</span>
        <span class="ec-spacer"></span>
        <button type="button" class="ec-btn" data-act="reload" title="重新加载">↻</button>
        <a class="ec-btn ec-link" target="_blank" rel="noopener" title="在新标签页打开">↗</a>
        <button type="button" class="ec-btn ec-x" data-act="close" title="关闭">✕</button>
      </div>
      <div class="ec-body"><iframe
          allow="fullscreen; clipboard-read; clipboard-write"
          allowfullscreen></iframe></div>`;
    root.appendChild(win);

    const frame = win.querySelector("iframe");
    centerWindow(win);
    enableDrag(win, win.querySelector(".ec-head"));

    win.querySelector('[data-act="reload"]').addEventListener("click", () => {
      // 票据一次性：整页重载才会重新申请
      frame.src = frame.dataset.url;
    });
    win.querySelector('[data-act="close"]').addEventListener("click", () => {
      // 先换地址，确保窗内控制台 WebSocket 立即释放
      frame.src = "about:blank";
      frame.dataset.url = "";
      win.classList.remove("show");
    });
    return win;
  }

  function openFloat(kind, url) {
    const host = ensureHost();
    const root = host.shadowRoot;
    // 每类窗口一个独立实例：控制台与管理窗可以同时存在、各自刷新/关闭
    let win = root.querySelector(`.ec-window[data-kind="${kind}"]`);
    if (!win) {
      win = buildWindow(root, kind);
    }
    const frame = win.querySelector("iframe");
    if (frame.dataset.url !== url.href) {
      frame.dataset.url = url.href;
      frame.src = url.href;
      win.querySelector(".ec-link").href = url.href;
    }
    win.classList.add("show");
    win.classList.remove("ec-flash");
    // 强制 reflow 以重放动画
    void win.offsetWidth;
    win.classList.add("ec-flash");
    // 管理窗后打开时置于最上
    win.style.zIndex = String(10 + (kind === "admin" ? 1 : 0));
  }

  // 捕获阶段拦截（document 早于 shadow 内部的目标元素收到事件）：
  //   preventDefault 阻止 <a> 默认导航/开新标签；
  //   stopPropagation 阻止事件继续进入 shadow 到达 ha-button 自身的
  //   @click 处理器（_deviceActionClicked，可能 window.open）。
  //   Ctrl/Cmd/Shift/Alt 点击及中键仍走浏览器原生"新标签打开"。
  function onClick(e) {
    const match = linkFromEvent(e);
    if (!match) { return; }
    // 即便不拦截（Ctrl/Cmd/Shift/Alt、中键走浏览器原生新标签），也先把链接
    //   原地改成地址栏主机地址，保证任何打开方式都不走 HA 探测的旧 host
    rewriteLinkToLocation(match);
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) {
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    openFloat(match.kind, match.url);
  }
  document.addEventListener("click", onClick, true);

  // ---- 地址栏来源心跳 ----
  // 设备「访问」链接 / 远程地址传感器必须是浏览器当前实际能到达 HA 的地址
  //   （上级路由 / 端口映射下，HA 自探测的容器内网地址不可达）。服务端无法
  //   主动知道地址栏，这里在前端每次加载时同源上报一次 location 的 origin；
  //   页面重新获得焦点（从别的标签/网络切回来）时节流补报，服务端只在地址
  //   变化时落盘并重签链接，重复上报零负担。
  //
  // ⚠️ 这个接口是要鉴权的（服务端视图 requires_auth），**裸 fetch 会 401**：
  //   HA 会把未鉴权的请求记成一次失败登录（"Login attempt or request with
  //   invalid authentication"），攒够次数直接封掉用户自己的 IP。所以这里必须
  //   带上 HA 前端的登录态；拿不到就**不发请求**（宁可不学地址，也不能封 IP）。
  const ORIGIN_PATH = "/esxi_control/api/console_origin";
  const ORIGIN_HEADERS = { "Cache-Control": "no-store" };
  let lastOriginReport = 0;
  let originRetries = 0;

  // 取一个「已登录」的 fetch：优先 HA 前端的 hass.fetchWithAuth（自带 Bearer 且会刷新令牌），
  //   退路是 hass.auth.data.access_token 自己拼头 —— 两者都拿不到返回 null。
  function authFetcher() {
    try {
      const root = document.querySelector("home-assistant");
      const hass = root && root.hass;
      if (!hass) { return null; }
      if (typeof hass.fetchWithAuth === "function") {
        return (url, init) => hass.fetchWithAuth(url, init);
      }
      const token = hass.auth && hass.auth.data && hass.auth.data.access_token;
      if (token) {
        return (url, init) => fetch(url, {
          ...init,
          headers: { ...(init && init.headers), Authorization: "Bearer " + token }
        });
      }
    } catch (_) { /* 取不到就当作未登录 */ }
    return null;
  }

  function reportOrigin() {
    const now = Date.now();
    if (now - lastOriginReport < 30000) { return; }
    const fetcher = authFetcher();
    if (!fetcher) {
      // HA 前端还没起来（本模块是 add_extra_js_url 注入的，常先于 hass 就绪）：
      //   有限次重试，别无限轮询。
      if (originRetries < 10) {
        originRetries += 1;
        setTimeout(reportOrigin, 1500);
      }
      return;
    }
    originRetries = 0;
    lastOriginReport = now;
    try {
      fetcher(ORIGIN_PATH, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: ORIGIN_HEADERS
      }).catch(() => {});
    } catch (_) { /* 页面正在卸载等场景，静默 */ }
  }
  reportOrigin();
  window.addEventListener("focus", reportOrigin);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { reportOrigin(); }
  });
  window.addEventListener("pageshow", reportOrigin);

  // 加载标记：F12 Console 输入 __esxiFloatEmbed 可确认模块是否生效
  window.__esxiFloatEmbed = "0.10.6";
})();
