"use strict";
/* Agent Space — the team's control tower for agents.
   Vanilla JS, no build step, no external resources (CSP: script-src/style-src 'self'; no inline styles,
   so motion is SVG animation elements and CSS classes; only CSS variables are set through the CSSOM). */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const app = $("#app"),
    modal = $("#modal");

let me = null,
    harnesses = [],
    route = "start",
    refreshTimer = null,
    workPoll = null,
    pageEpoch = 0,
    syncedAt = null;
let workspace = { devices: [], own: [], shared: [] };
const view = { directory: { filter: "all", query: "" } };

/* ------------------------------------------------------------------ basics */

const ICONS = {
    radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12 18 6"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    agent: '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M8.5 13h.01M15.5 13h.01M9 17h6"/>',
    device: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    plug: '<path d="M9 7V3M15 7V3M6 7h12v4a6 6 0 0 1-12 0Z"/><path d="M12 17v4"/>',
    book: '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2Z"/><path d="M4 19V5M9 7h6"/>',
    users: '<path d="M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20"/><circle cx="10" cy="8" r="3.5"/><path d="M20 20v-1.5a3.5 3.5 0 0 0-2.5-3.35M15.5 4.6a3.5 3.5 0 0 1 0 6.8"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    back: '<path d="M19 12H5m5-5-5 5 5 5"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    lock: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c4.5 4.8 4.5 13.2 0 18-4.5-4.8-4.5-13.2 0-18Z"/>',
    refresh:
        '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5L20 16m0 4v-4h-4"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 8-8M16 7l2 2"/>',
    logout: '<path d="M9 20H5V4h4M15 16l4-4-4-4M19 12H9"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    spark: '<path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4Z"/>',
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    wrench: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4Z"/>',
    bolt: '<path d="M13 3 5 14h6l-1 7 8-11h-6Z"/>',
};
function icon(name, cls = "") {
    return `<svg class="icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.agent}</svg>`;
}
function esc(s) {
    return String(s ?? "").replace(
        /[&<>"']/g,
        (c) =>
            ({
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
            })[c]
    );
}
function toast(text, tone = "") {
    const t = $("#toast");
    t.textContent = text;
    t.className = "show " + tone;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => {
        t.className = "";
    }, 3200);
}
async function api(path, method = "GET", body) {
    const r = await fetch(path, {
        method,
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json",
            ...(me ? { "X-CSRF-Token": me.csrf } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
    });
    let d = {};
    try {
        d = await r.json();
    } catch {
        /* an empty or non-JSON answer */
    }
    if (!r.ok) {
        const error = new Error(d.error || "请求失败，稍后再试");
        error.status = r.status;
        throw error;
    }
    return d;
}
async function copy(text) {
    try {
        await navigator.clipboard.writeText(text);
    } catch {
        const t = document.createElement("textarea");
        t.value = text;
        document.body.append(t);
        t.select();
        const ok = document.execCommand("copy");
        t.remove();
        if (!ok) throw new Error("浏览器不让复制，展开全文手动复制吧。");
    }
    toast("已复制", "ok");
    return true;
}
function guard(fn) {
    return (...a) =>
        Promise.resolve(fn(...a)).catch((e) => toast(e.message, "bad"));
}
function on(sel, event, fn, root = document) {
    $$(sel, root).forEach((el) =>
        el.addEventListener(
            event,
            guard((e) => fn(el, e))
        )
    );
}
function clock(d) {
    return d
        ? d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
        : "—";
}
function dateOf(t) {
    return t ? new Date(t * 1000).toLocaleDateString("zh-CN") : "—";
}
function ago(t) {
    if (!t) return "还没上线过";
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 90) return "刚刚在线";
    if (s < 3600) return Math.round(s / 60) + " 分钟前在线";
    if (s < 86400) return Math.round(s / 3600) + " 小时前在线";
    return Math.round(s / 86400) + " 天前在线";
}
function initials(name) {
    return esc((name || "?").trim().slice(0, 1).toUpperCase());
}
function shortAid(aid) {
    return aid ? esc(aid.slice(0, 10) + "…" + aid.slice(-6)) : "";
}
// Heartbeat clocks move on every poll; a refresh that only saw those has nothing to redraw.
function stable(x) {
    return JSON.stringify(x, (k, v) =>
        k === "last_seen" || k === "sync_at" ? undefined : v
    );
}
function clip(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/* ------------------------------------------------------------------ states */

function executorName(a) {
    if (a?.executor === "custom") return "自定义软件";
    if (a?.executor === "claude") return "Claude Code";
    return (
        harnesses.find((h) => h.id === (a?.executor || "codex"))?.name ||
        "没选软件"
    );
}
function executorNote(id) {
    return (
        {
            codex: "那台电脑上要装好并登录 Codex。",
            pi: "那台电脑上要装好 Pi，并配好模型。",
            workbuddy:
                "通过 WorkBuddy 官方接口连电脑上的助理，需要在那台电脑上授权。",
            deepseek: "用 DeepSeek Harness 的命令行模式，模型在那台电脑上配。",
            custom: "自己的命令行工具也行：从标准输入读任务，把结果写到标准输出。",
        }[id] || "选那台电脑上已经装好的软件。"
    );
}
/* label · tone · what is going on · what to do */
function stateInfo(a) {
    const device = a.kind === "device";
    const table = device
        ? {
              pending: [
                  "待连接",
                  "warn",
                  "还没连上这台电脑。",
                  "复制连接说明，到这台电脑的助手里粘贴。",
              ],
              bound: [
                  "待上线",
                  "info",
                  "身份连上了，连接程序还没跑起来。",
                  "在这台电脑上启动连接程序。",
              ],
              ready: [
                  "在线",
                  "ok",
                  "在线，可以从这台电脑发任务。",
                  "去智能体列表挑一个。",
              ],
              offline: [
                  "离线",
                  "off",
                  "没有近期节点连通确认；具体原因未提供。",
                  "看看那台电脑开着没、连接程序还在不在。",
              ],
              disabled: [
                  "已停用",
                  "off",
                  "这台电脑被停用了。",
                  "找空间管理员问问。",
              ],
          }
        : {
              pending: [
                  "待连接",
                  "warn",
                  "资料存好了，还没连上执行的电脑。",
                  "复制连接说明，到那台电脑的助手里粘贴。",
              ],
              bound: [
                  "待上线",
                  "info",
                  "身份连上了，运行程序还没跑起来。",
                  "在执行的电脑上启动连接程序。",
              ],
              ready: [
                  "可用",
                  "ok",
                  "连接正常，可以接任务。",
                  "派个小任务试试。",
              ],
              offline: [
                  "离线",
                  "off",
                  "没有近期节点连通确认；具体原因未提供。",
                  "看看那台电脑开着没。",
              ],
              policy_pending: [
                  "门户独占策略待核验",
                  "warn",
                  "尚未确认门户独占准入；既有授权链路是否可用须按发起身份核对。",
                  "查看节点上报、原任务接收记录及本机诊断；不要仅凭此状态扩大权限。",
              ],
              executor_pending: [
                  "执行器待核验",
                  "warn",
                  "节点尚未确认执行器就绪，不代表软件未安装。",
                  "核对本机自检结果、执行器配置和状态更新时间；已有任务运行时不要贸然重装或重启。",
              ],
              disabled: [
                  "已停用",
                  "off",
                  "暂时不能接任务。",
                  "找所有者或管理员问问。",
              ],
          };
    const [label, tone, meaning, next] = table[a.status] || [
        "待检查",
        "warn",
        "状态不明。",
        "打开详情看看。",
    ];
    return { label, tone, meaning, next };
}
function led(a) {
    const s = stateInfo(a);
    return `<span class="led ${s.tone}" title="${esc(s.meaning)}"><i></i>${s.label}</span>`;
}
function pill(a) {
    const s = stateInfo(a);
    return `<span class="pill ${s.tone}"><i></i>${s.label}</span>`;
}
function bigStatus(a) {
    const s = stateInfo(a);
    return `<span class="big-status ${s.tone}">${s.label}</span>`;
}
function sendReady(a) {
    return !!a && a.send_ready === true && !!a.aid && (a.enabled === true || a.enabled === 1) &&
        (a.node_connected === true || a.node_connected === 1) && evidenceFresh(a.last_seen);
}
function receiveReady(a) {
    return !!a && a.kind === "agent" && a.receive_ready === true && a.status === "ready" && sendReady(a);
}
function callers() {
    return [...workspace.devices, ...workspace.own].filter(
        (a) => a.is_owner === true && sendReady(a)
    );
}
function catalog() {
    return [
        ...new Map(
            [...workspace.own, ...workspace.shared].map((a) => [a.id, a])
        ).values(),
    ].filter((a) => a.kind === "agent");
}
function readyAgents() {
    return catalog().filter(receiveReady);
}
function recordApi(a) {
    return "/api/" + (a.kind === "device" ? "devices" : "agents");
}
function findRecord(id) {
    return [...workspace.devices, ...catalog()].find((a) => a.id === id);
}
function byReadiness(a, b) {
    return (
        Number(b.status === "ready") - Number(a.status === "ready") ||
        Number(b.long_task) - Number(a.long_task) ||
        a.name.localeCompare(b.name, "zh")
    );
}

/* ------------------------------------------------------------------ dialog */

function openDialog({ title, sub = "", body, size = "", kicker = "" }) {
    clearInterval(workPoll);
    workPoll = null;
    modal.className = size;
    modal.setAttribute("aria-labelledby", "dlg-title");
    modal.innerHTML = `<div class="dlg-head"><div>${kicker ? `<p class="kicker">${kicker}</p>` : ""}<h2 id="dlg-title">${title}</h2>${sub ? `<p class="dlg-sub">${sub}</p>` : ""}</div><button class="icon-btn" id="dlg-close" aria-label="关闭">${icon("x")}</button></div><div class="dlg-body">${body}</div>`;
    $("#dlg-close").onclick = () => modal.close();
    if (!modal.open) modal.showModal();
    return modal;
}
modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.close();
});
modal.addEventListener("close", () => { clearInterval(workPoll); workPoll = null; });
function formError(form, error) {
    const out = $(".form-error", form);
    if (out) out.textContent = error.message;
    const submit = $("[type=submit]", form);
    if (submit) submit.disabled = false;
}
function stepper(labels, current) {
    return `<ol class="stepper">${labels.map((l, i) => `<li class="${i < current ? "done" : i === current ? "now" : ""}"><b>${i < current ? icon("check") : i + 1}</b><span>${l}</span></li>`).join("")}</ol>`;
}

/* ------------------------------------------------------------------ shell */

const NAV = [
    ["start", "总览", "radar"],
    ["directory", "智能体", "agent"],
    ["chat", "直接对话", "agent"],
    ["assistant", "门户助手", "spark"],
    ["mine", "我的连接", "plug"],
    ["guide", "帮助", "book"],
    ["members", "成员", "users"],
];
function navKey(r) {
    return r === "agents" || r === "devices" ? "mine" : r;
}
function brand() {
    return `<a class="brand" href="#start" aria-label="Agent Space 总览"><span class="seal">派</span><span class="wordmark">Agent Space<small>智能体调度台</small></span></a>`;
}
function shell() {
    const items = NAV.filter(([id]) => id !== "members" || me.role === "admin");
    app.innerHTML = `<div class="frame">
    <header class="masthead"><div class="masthead-row">
      ${brand()}
      <nav class="nav" aria-label="主导航">${items.map(([id, name, i]) => `<a href="#${id === "mine" ? "agents" : id}" class="${navKey(route) === id ? "active" : ""}" ${navKey(route) === id ? 'aria-current="page"' : ""}>${icon(i)}<span>${name}</span></a>`).join("")}</nav>
      <div class="account">
        <button class="sync" id="sync" title="刷新连接状态">${icon("refresh")}<span id="sync-time">${syncedAt ? clock(syncedAt) : ""}</span></button>
        <details class="account-menu"><summary aria-label="账户"><span class="avatar">${initials(me.name)}</span><span class="account-name">${esc(me.name)}</span></summary>
          <div class="menu"><p class="menu-head"><strong>${esc(me.name)}</strong><span>@${esc(me.username)} · ${me.role === "admin" ? "管理员" : "成员"}</span></p>
          <a href="/download">${icon("arrow")}下载客户端</a><a href="#api-access">${icon("key")}API 访问</a><button id="change-password">${icon("key")}修改密码</button><a href="/architecture.html">${icon("spark")}原理 ↗</a><button id="logout">${icon("logout")}退出</button></div>
        </details>
      </div>
    </div></header>
    <main class="page" id="content"></main>
    <footer class="colophon"><span>AGENT SPACE</span><span>在你的智能体或门户中发起对话，保持连接</span><a href="/download">下载客户端</a><a href="/docs/anet-api-v1.txt">API 文档</a><a href="#guide">帮助</a></footer>
  </div>`;
    $("#logout").onclick = guard(async () => {
        await api("/api/auth/logout", "POST", {});
        stopRefresh();
        me = null;
        workspace = { devices: [], own: [], shared: [] };
        clearDrafts();
        directReset();
        history.replaceState(null, "", "/#login");
        authPage("login");
    });
    $("#change-password").onclick = () => {
        $(".account-menu").open = false;
        passwordDialog();
    };
    $("#sync").onclick = guard(async () => {
        $("#sync").classList.add("spinning");
        try {
            await refreshWorkspace();
            await load(false);
            toast("刷新好了", "ok");
        } finally {
            $("#sync")?.classList.remove("spinning");
        }
    });
}
function pageHead(kicker, title, lead, action = "") {
    return `<header class="page-head"><div><p class="kicker">${kicker}</p><h1 tabindex="-1">${title}</h1>${lead ? `<p class="lead">${lead}</p>` : ""}</div>${action ? `<div class="page-actions">${action}</div>` : ""}</header>`;
}

/* ------------------------------------------------------------------ network map */

/* The map: every member is a circle on the ring around the hub, their computers and agents orbit
   them. Layout is plain geometry (no force simulation) so it is stable between refreshes.
   One animation loop moves everything: anchors tween between layouts, nodes breathe a little and lean
   toward the pointer (toward it, never away, so whatever is under the pointer stays there and clicks
   land), and real calls between agents travel the links as sparks. The SVG carries structure, labels
   and hit areas; a canvas behind it and one in front of it carry the light. Positions are written as
   SVG attributes and one-shot flashes use the Web Animations API, which keeps to the CSP. */

let graphData = null;
const mapState = {
    focus: null,
    geo: null,
    raf: 0,
    intro: true,
    born: 0,
    last: 0,
    tw: null,
    cur: null,
    pos: null,
    hover: { key: null, n: null, hub: false },
    waved: { id: "", at: 0 },
    ptr: {
        x: 0,
        y: 0,
        sx: 0,
        sy: 0,
        inside: false,
        down: false,
        mouse: false,
        spot: 0,
    },
    lean: new Map(),
    links: [],
    seen: new Map(),
    primed: false,
    glow: new Map(),
    sparks: [],
    bursts: [],
    feed: [],
    dust: [],
    skew: 0,
    onScreen: true,
    io: null,
    poll: 0,
    fxDirty: false,
};
const REDUCED = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const LINK_RGB = {
    real: "255,138,92",
    live: "56,232,198",
    hover: "130,240,224",
    refused: "255,86,86",
    echo: "255,176,140",
};

function fallbackGraph() {
    return {
        members: [
            {
                key: "me",
                name: me.name,
                is_me: true,
                nodes: [...workspace.devices, ...workspace.own].map((a) => ({
                    ...a,
                    redacted: false,
                })),
            },
        ],
        links: [],
    };
}
function mapGeometry(members, W, H) {
    const cx = W / 2,
        cy = H / 2,
        M = members.length,
        narrow = W < 640;
    const items = members.map((m, i) => {
        const nodes = m.nodes.length
            ? m.nodes
            : m.is_me
              ? [
                    {
                        ghost: true,
                        kind: "device",
                        status: "pending",
                        name: "还没连电脑",
                    },
                ]
              : [];
        const theta = Math.PI + (i / M) * 2 * Math.PI;
        const rings = [];
        nodes.forEach((n, j) => {
            const k = j < 8 ? 0 : 1 + Math.floor((j - 8) / 12);
            (rings[k] = rings[k] || []).push(j);
        });
        const rel = [];
        let outer = 0;
        rings.forEach((list, k) => {
            const r = 64 + 34 * k,
                span =
                    list.length === 1
                        ? 0
                        : Math.min(k ? 340 : 300, 54 * (list.length - 1)) *
                          (Math.PI / 180);
            outer = Math.max(outer, r);
            list.forEach((j, jj) => {
                const t =
                    list.length === 1
                        ? theta
                        : theta + (jj / (list.length - 1) - 0.5) * span;
                rel[j] = { x: Math.cos(t) * r, y: Math.sin(t) * r };
            });
        });
        return { m, nodes, rel, theta, outer: outer || 30, i };
    });
    const rx = (narrow ? 0.3 : 0.36) * W,
        ry = (narrow ? 0.34 : 0.33) * H,
        chord = M > 1 ? 2 * ((rx + ry) / 2) * Math.sin(Math.PI / M) : 9999,
        maxOut = Math.max(...items.map((x) => x.outer)) + 8;
    const sAll = Math.min(1.15, Math.max(0.5, (chord / 2 - 14) / maxOut));
    // Keep every cluster, including its labels, on the canvas.
    const fit = (v, size, lo, hi) =>
            hi > lo ? Math.min(hi, Math.max(lo, v)) : size / 2,
        labels = W >= 760 && sAll >= 0.85;
    items.forEach((it) => {
        const mx = it.outer * sAll + (labels ? 120 : 24),
            my = it.outer * sAll + 38,
            ex = it.outer * 0.42 + 26,
            ey = it.outer * 0.42 + 40;
        it.all = {
            x: fit(cx + Math.cos(it.theta) * rx, W, mx, W - mx),
            y: fit(cy + Math.sin(it.theta) * ry, H, my, H - my),
            s: sAll,
        };
        it.edge = {
            x: fit(
                cx + Math.cos(it.theta) * (narrow ? 0.4 : 0.44) * W,
                W,
                ex,
                W - ex
            ),
            y: fit(
                cy + Math.sin(it.theta) * 0.4 * H,
                H,
                Math.max(ey, 128),
                H - ey
            ),
            s: 0.42,
        };
    });
    return {
        cx,
        cy,
        items,
        narrow,
        W,
        H,
        byKey: new Map(items.map((it) => [it.m.key, it])),
    };
}
function mapTargets(geo, focus) {
    const out = { hub: { s: focus ? 0.5 : 1, o: focus ? 0.3 : 1 } };
    geo.items.forEach((it) => {
        const key = it.m.key;
        if (!focus) out[key] = { ...it.all };
        else if (key === focus)
            out[key] = {
                x: geo.cx,
                y: geo.cy,
                s: Math.min(
                    1.9,
                    Math.max(
                        0.8,
                        (Math.min(geo.H * 0.34, geo.W * 0.3) - 8) / it.outer
                    )
                ),
            };
        else out[key] = { ...it.edge };
    });
    return out;
}
function nodeLabel(n) {
    return n.ghost
        ? n.name
        : n.redacted
          ? n.kind === "device"
              ? "电脑"
              : "私有智能体"
          : n.name;
}
function mapSvg(geo) {
    const hubWires = geo.items
        .map(
            (it) =>
                `<path class="wire hwire ${memberTone(it.m)}" data-k="${it.m.key}"/>`
        )
        .join("");
    const mems = geo.items
        .map((it) => {
            const spokes = it.nodes
                .map(
                    (n, j) =>
                        `<line class="wire spoke ${nodeTone(n)}" data-n="${j}" x1="0" y1="0"/>`
                )
                .join("");
            const nodes = it.nodes
                .map((n, j) => {
                    const tone = nodeTone(n),
                        label = nodeLabel(n),
                        sub = nodeSub(n);
                    const right = it.rel[j].x >= 0;
                    return `<g class="node ${tone} ${n.redacted ? "anon" : ""}" data-n="${j}" tabindex="0" role="button" aria-label="${esc(label)}，${esc(stateInfo(n).label)}"><circle class="hit" r="16"/><circle class="ring" r="9"/><circle class="halo" r="11"/><circle class="core" r="6"/><text x="${right ? 14 : -14}" y="-1" text-anchor="${right ? "start" : "end"}">${esc(clip(label, 12))}</text>${sub ? `<text class="sub" x="${right ? 14 : -14}" y="12" text-anchor="${right ? "start" : "end"}">${esc(sub)}</text>` : ""}</g>`;
                })
                .join("");
            return `<g class="mem ${it.m.is_me ? "me" : ""} ${memberTone(it.m)}" data-k="${it.m.key}"><circle class="aura"/>${spokes}<g class="mhead" tabindex="0" role="button" aria-label="${esc(it.m.name)}的连接"><circle class="hit"/><circle class="mring"/><circle class="mcore"/><text class="minit">${initials(it.m.name)}</text></g><text class="mname">${esc(clip(it.m.name, 10))}${it.m.is_me ? " · 我" : ""}</text>${nodes}</g>`;
        })
        .join("");
    return `<svg class="mapsvg" width="${geo.W}" height="${geo.H}" viewBox="0 0 ${geo.W} ${geo.H}" role="group" aria-label="每位成员的连接图">
      <defs><radialGradient id="hubGrad" cx="35%" cy="30%" r="75%"><stop offset="0" stop-color="#ffa27a"/><stop offset=".55" stop-color="#ff5b36"/><stop offset="1" stop-color="#b8341a"/></radialGradient>
      <radialGradient id="auraGrad"><stop offset="0" stop-color="#38e8c6" stop-opacity=".2"/><stop offset=".6" stop-color="#38e8c6" stop-opacity=".05"/><stop offset="1" stop-color="#38e8c6" stop-opacity="0"/></radialGradient></defs>
      <rect class="map-bg" width="${geo.W}" height="${geo.H}"/>
      ${hubWires}
      <g class="axons"></g>
      <g class="hub"><circle class="hub-hit" r="58"/><circle class="hub-ring slow" r="62"/><circle class="hub-ring" r="47"/><circle class="hub-core" r="31"/><text class="hub-text" y="9" text-anchor="middle">派</text></g>
      ${mems}
    </svg>`;
}
function memberTone(m) {
    const tones = m.nodes.map(nodeTone);
    return tones.includes("ok") || tones.includes("busy")
        ? "ok"
        : tones.includes("warn")
          ? "warn"
          : "off";
}
function nodeTone(n) {
    if (n.ghost) return "ghost";
    const tone = stateInfo(n).tone;
    // A ready agent that is executing a task gets its own colour so activity reads at a glance.
    return tone === "ok" && n.kind === "agent" && !n.redacted && workState(n) === "running"
        ? "busy"
        : tone;
}

/* ---- motion helpers */

const clamp01 = (v) => Math.min(1, Math.max(0, v));
const easeOut = (k) => 1 - Math.pow(1 - k, 3);
const easeBack = (k) => 1 + 2.4 * Math.pow(k - 1, 3) + 1.4 * Math.pow(k - 1, 2);
const easeInOut = (k) =>
    k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
function qpt(g, t) {
    const u = 1 - t;
    return {
        x: u * u * g.x0 + 2 * u * t * g.cx + t * t * g.x1,
        y: u * u * g.y0 + 2 * u * t * g.cy + t * t * g.y1,
    };
}
function qlen(g) {
    return (
        Math.hypot(g.cx - g.x0, g.cy - g.y0) +
        Math.hypot(g.x1 - g.cx, g.y1 - g.cy)
    );
}
function serverNow() {
    return Date.now() / 1000 - mapState.skew;
}
function agoSec(at) {
    const s = Math.max(0, serverNow() - at);
    return s < 60
        ? "刚刚"
        : s < 3600
          ? Math.round(s / 60) + " 分钟前"
          : Math.round(s / 3600) + " 小时前";
}

/* ---- one frame: anchors → positions → SVG → light */

function mapAnchors(t) {
    const tw = mapState.tw;
    if (!tw) return mapState.cur;
    const k = tw.ms && !REDUCED() ? clamp01((t - tw.t0) / tw.ms) : 1,
        e = easeOut(k),
        mix = (a, b) => a + (b - a) * e,
        p = {
            hub: {
                s: mix(tw.from.hub.s, tw.to.hub.s),
                o: mix(tw.from.hub.o, tw.to.hub.o),
            },
        };
    Object.keys(tw.to).forEach((key) => {
        if (key === "hub") return;
        const a = tw.from[key] || tw.to[key],
            b = tw.to[key];
        p[key] = { x: mix(a.x, b.x), y: mix(a.y, b.y), s: mix(a.s, b.s) };
    });
    if (k >= 1) mapState.tw = null;
    mapState.cur = p;
    return p;
}
function mapLean(id, bx, by, reach, pull, cap, dt) {
    const ptr = mapState.ptr;
    let o = mapState.lean.get(id);
    if (!o) mapState.lean.set(id, (o = { x: 0, y: 0 }));
    if (ptr.down) return o; // hold still under a pressed pointer
    let tx = 0,
        ty = 0;
    if (ptr.inside && ptr.mouse) {
        const dx = ptr.x - bx,
            dy = ptr.y - by,
            d = Math.hypot(dx, dy);
        if (d < reach) {
            const f = Math.pow(1 - d / reach, 2) * pull,
                len = Math.min(cap, d * f);
            tx = d ? (dx / d) * len : 0;
            ty = d ? (dy / d) * len : 0;
        }
    }
    const k = 1 - Math.exp(-dt / 110);
    o.x += (tx - o.x) * k;
    o.y += (ty - o.y) * k;
    return o;
}
function mapPlace(t, dt) {
    const geo = mapState.geo,
        p = mapState.cur,
        still = REDUCED(),
        sec = t / 1000,
        pos = { hub: { x: geo.cx, y: geo.cy, s: p.hub.s, o: p.hub.o } };
    let introDone = true;
    if (mapState.born && !still) {
        const k = clamp01((t - mapState.born) / 520);
        pos.hub.s *= 0.55 + 0.45 * easeBack(k);
        pos.hub.o *= k;
    }
    geo.items.forEach((it) => {
        const key = it.m.key,
            q = p[key];
        let wire = 1,
            bloom = 1;
        if (mapState.born && !still) {
            const start = t - mapState.born - 180 - it.i * 120;
            wire = easeOut(clamp01(start / 420));
            bloom = start < 300 ? 0 : easeBack(clamp01((start - 300) / 720));
            if (start < 1100) introDone = false;
        }
        const wob = still ? 0 : 1.4 * q.s,
            hx0 = q.x + Math.sin(sec * 0.55 + it.i * 1.7) * wob,
            hy0 = q.y + Math.cos(sec * 0.47 + it.i * 2.3) * wob,
            hl = still
                ? { x: 0, y: 0 }
                : mapLean(key, hx0, hy0, 150, 0.16, 7, dt),
            hx = hx0 + hl.x,
            hy = hy0 + hl.y;
        const nodes = it.rel.map((r, j) => {
            const amp = still ? 0 : 2.4 * Math.max(0.6, q.s),
                ph = it.i * 3.1 + j * 1.37,
                bx = hx + r.x * q.s * bloom + Math.sin(sec * 0.62 + ph) * amp,
                by =
                    hy +
                    r.y * q.s * bloom +
                    Math.cos(sec * 0.51 + ph * 1.3) * amp,
                l = still
                    ? { x: 0, y: 0 }
                    : mapLean(key + ":" + j, bx, by, 150, 0.3, 13, dt);
            return { x: bx + l.x, y: by + l.y };
        });
        const ex = geo.cx + (hx - geo.cx) * wire,
            ey = geo.cy + (hy - geo.cy) * wire,
            dx = ex - geo.cx,
            dy = ey - geo.cy;
        pos[key] = {
            x: hx,
            y: hy,
            s: q.s,
            bloom,
            wire,
            nodes,
            hw: {
                x0: geo.cx,
                y0: geo.cy,
                cx: geo.cx + dx / 2 - dy * 0.12,
                cy: geo.cy + dy / 2 + dx * 0.12,
                x1: ex,
                y1: ey,
            },
        };
    });
    if (introDone) mapState.born = 0;
    mapState.pos = pos;
    return pos;
}
function mapPaint(pos) {
    const geo = mapState.geo,
        svg = $(".mapsvg");
    if (!svg) return;
    const hub = $(".hub", svg);
    hub.setAttribute(
        "transform",
        `translate(${geo.cx} ${geo.cy}) scale(${pos.hub.s.toFixed(3)})`
    );
    hub.style.opacity = pos.hub.o.toFixed(2);
    geo.items.forEach((it) => {
        const k = it.m.key,
            q = pos[k],
            g = it.el || (it.el = $(`.mem[data-k="${k}"]`, svg)),
            rM = 16 + 10 * q.s;
        g.setAttribute(
            "transform",
            `translate(${q.x.toFixed(1)} ${q.y.toFixed(1)})`
        );
        // Only the intro sets an inline opacity; afterwards the stylesheet's hover dimming applies.
        if (mapState.born)
            g.style.opacity = Math.min(1, q.bloom * 1.6).toFixed(2);
        else if (g.style.opacity) g.style.opacity = "";
        if (it.lastS !== q.s.toFixed(3)) {
            it.lastS = q.s.toFixed(3);
            $$(".mhead circle", g).forEach((c, i) =>
                c.setAttribute(
                    "r",
                    (i === 0 ? rM + 8 : i === 1 ? rM + 4 : rM).toFixed(1)
                )
            );
            $(".aura", g).setAttribute("r", ((it.outer + 34) * q.s).toFixed(1));
            $(".minit", g).setAttribute("y", (4 + 3 * q.s).toFixed(1));
            $(".minit", g).setAttribute("font-size", (12 + 7 * q.s).toFixed(1));
            $(".mname", g).setAttribute("y", (rM + 18).toFixed(1));
            const labels =
                mapState.focus === k
                    ? geo.W >= 520
                    : !mapState.focus && q.s >= 0.85 && geo.W >= 760;
            g.classList.toggle("lab", labels);
            g.classList.toggle("far", q.s < 0.6);
        }
        const hw = it.wireEl || (it.wireEl = $(`.hwire[data-k="${k}"]`, svg));
        hw.setAttribute(
            "d",
            `M${q.hw.x0} ${q.hw.y0} Q${q.hw.cx.toFixed(1)} ${q.hw.cy.toFixed(1)} ${q.hw.x1.toFixed(1)} ${q.hw.y1.toFixed(1)}`
        );
        if (!it.nodeEls)
            it.nodeEls = it.rel.map((_, j) => [
                $(`.node[data-n="${j}"]`, g),
                $(`.spoke[data-n="${j}"]`, g),
            ]);
        q.nodes.forEach((n, j) => {
            const x = (n.x - q.x).toFixed(1),
                y = (n.y - q.y).toFixed(1),
                [ne, se] = it.nodeEls[j];
            ne.setAttribute("transform", `translate(${x} ${y})`);
            se.setAttribute("x2", x);
            se.setAttribute("y2", y);
        });
    });
    mapState.links.forEach((l) => {
        const a = mapEnd(l.from),
            b = mapEnd(l.to);
        l.g = a && b ? linkCurve(a, b, l) : null;
        if (!l.el) return;
        if (!l.g) return l.el.setAttribute("d", "");
        l.el.setAttribute(
            "d",
            `M${l.g.x0.toFixed(1)} ${l.g.y0.toFixed(1)} Q${l.g.cx.toFixed(1)} ${l.g.cy.toFixed(1)} ${l.g.x1.toFixed(1)} ${l.g.y1.toFixed(1)}`
        );
        const lit = (mapState.glow.get(l.key) || 0) > 0.22;
        if (lit !== l.lit) l.el.classList.toggle("lit", (l.lit = lit));
    });
}
function mapEnd(ref) {
    const pos = mapState.pos;
    if (!ref || !ref.length) return { x: pos.hub.x, y: pos.hub.y, hub: true };
    const m = pos[ref[0]];
    if (!m) return null;
    if (ref.length === 1) return { x: m.x, y: m.y, key: ref[0] };
    const n = m.nodes[ref[1]];
    return n ? { x: n.x, y: n.y, key: ref[0] } : null;
}
function linkCurve(a, b) {
    const geo = mapState.geo,
        mx = (a.x + b.x) / 2,
        my = (a.y + b.y) / 2,
        dx = b.x - a.x,
        dy = b.y - a.y,
        len = Math.hypot(dx, dy) || 1;
    let cx, cy;
    if (a.key && a.key === b.key) {
        // Inside one member's cluster: bow outward, away from the member's head.
        const h = mapState.pos[a.key],
            ox = mx - h.x,
            oy = my - h.y,
            ol = Math.hypot(ox, oy);
        const ux = ol > 2 ? ox / ol : -dy / len,
            uy = ol > 2 ? oy / ol : dx / len;
        cx = mx + ux * (len * 0.45 + 24);
        cy = my + uy * (len * 0.45 + 24);
    } else if (a.hub || b.hub) {
        cx = mx - dy * 0.18;
        cy = my + dx * 0.18;
    } else {
        // Between members: sweep in toward the core and curl past it, like a synapse around the cell body.
        cx = mx + (geo.cx - mx) * 0.55 - dy * 0.14;
        cy = my + (geo.cy - my) * 0.55 + dx * 0.14;
    }
    return { x0: a.x, y0: a.y, cx, cy, x1: b.x, y1: b.y };
}

/* ---- light: the field behind the map and the sparks in front of it */

function mapCanvas(cv, W, H) {
    const r = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.round(W * r);
    cv.height = Math.round(H * r);
    const ctx = cv.getContext("2d");
    ctx.setTransform(r, 0, 0, r, 0, 0);
    return ctx;
}
function seedDust(W, H) {
    let seed = 11;
    const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647,
        n = Math.round(Math.min(150, Math.max(46, (W * H) / 8500)));
    mapState.dust = Array.from({ length: n }, () => ({
        x: rnd() * W,
        y: rnd() * H,
        z: 0.25 + rnd() * 0.75,
        vx: (rnd() - 0.5) * 0.012,
        vy: (rnd() - 0.5) * 0.012,
        r: 0.5 + rnd() * 1.1,
        a: 0.12 + rnd() * 0.35,
        ph: rnd() * 6.28,
        warm: rnd() < 0.18,
    }));
}
function drawField(ctx, t, dt) {
    const geo = mapState.geo,
        { W, H } = geo,
        ptr = mapState.ptr,
        pos = mapState.pos,
        still = REDUCED(),
        sec = t / 1000;
    ctx.clearRect(0, 0, W, H);
    ctx.globalCompositeOperation = "lighter";
    // The hub breathes, and every few seconds a slow ring leaves it.
    const hubR = 150 * pos.hub.s,
        hg = ctx.createRadialGradient(geo.cx, geo.cy, 0, geo.cx, geo.cy, hubR);
    hg.addColorStop(
        0,
        `rgba(255,91,54,${(0.1 + (still ? 0 : 0.035 * Math.sin(sec * 1.6))) * pos.hub.o})`
    );
    hg.addColorStop(1, "rgba(255,91,54,0)");
    ctx.fillStyle = hg;
    ctx.fillRect(geo.cx - hubR, geo.cy - hubR, hubR * 2, hubR * 2);
    if (!still) {
        const k = (sec % 4.2) / 4.2;
        ctx.strokeStyle = `rgba(255,145,84,${0.16 * (1 - k) * pos.hub.o})`;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(geo.cx, geo.cy, (36 + k * 170) * pos.hub.s, 0, 6.2832);
        ctx.stroke();
    }
    // Dust drifts with a little parallax against the pointer.
    const px = ptr.spot ? (ptr.sx - W / 2) * -0.018 : 0,
        py = ptr.spot ? (ptr.sy - H / 2) * -0.018 : 0;
    mapState.dust.forEach((d) => {
        if (!still) {
            d.x = (d.x + d.vx * dt * d.z + W) % W;
            d.y = (d.y + d.vy * dt * d.z + H) % H;
        }
        const a = d.a * (still ? 1 : 0.65 + 0.35 * Math.sin(sec * 1.3 + d.ph));
        ctx.fillStyle = d.warm
            ? `rgba(255,170,130,${a})`
            : `rgba(170,215,235,${a})`;
        ctx.fillRect(d.x + px * d.z, d.y + py * d.z, d.r, d.r);
    });
    // Links that just carried a call keep an afterglow; open tasks breathe.
    mapState.links.forEach((l) => {
        if (!l.g) return;
        const g = mapState.glow.get(l.key) || 0,
            live = l.live
                ? 0.08 + (still ? 0 : 0.05 * Math.sin(sec * 2.2 + l.at))
                : 0;
        if (g < 0.01 && !live) return;
        const curve = () => {
            ctx.beginPath();
            ctx.moveTo(l.g.x0, l.g.y0);
            ctx.quadraticCurveTo(l.g.cx, l.g.cy, l.g.x1, l.g.y1);
        };
        if (live) {
            ctx.strokeStyle = `rgba(56,232,198,${live})`;
            ctx.lineWidth = 7;
            curve();
            ctx.stroke();
        }
        if (g >= 0.01) {
            const rgb = l.refused ? LINK_RGB.refused : LINK_RGB.real;
            ctx.strokeStyle = `rgba(${rgb},${0.2 * g})`;
            ctx.lineWidth = 10;
            curve();
            ctx.stroke();
            ctx.strokeStyle = `rgba(255,214,190,${0.5 * g})`;
            ctx.lineWidth = 1.6;
            curve();
            ctx.stroke();
            mapState.glow.set(l.key, still ? g : g * Math.exp(-dt / 1700));
        }
    });
    // Under the pointer: a soft light, a lattice it reveals, and threads to whatever is near.
    ptr.spot +=
        ((ptr.inside && ptr.mouse && !still ? 1 : 0) - ptr.spot) *
        (1 - Math.exp(-dt / 160));
    if (ptr.spot > 0.01) {
        const k = 1 - Math.exp(-dt / 70);
        ptr.sx += (ptr.x - ptr.sx) * k;
        ptr.sy += (ptr.y - ptr.sy) * k;
        const R = 230,
            sg = ctx.createRadialGradient(ptr.sx, ptr.sy, 0, ptr.sx, ptr.sy, R);
        sg.addColorStop(0, `rgba(56,232,198,${0.085 * ptr.spot})`);
        sg.addColorStop(0.5, `rgba(56,232,198,${0.025 * ptr.spot})`);
        sg.addColorStop(1, "rgba(56,232,198,0)");
        ctx.fillStyle = sg;
        ctx.fillRect(ptr.sx - R, ptr.sy - R, R * 2, R * 2);
        const L = 180,
            step = 22,
            rowH = step * 0.866;
        for (
            let row = Math.floor((ptr.sy - L) / rowH);
            row * rowH <= ptr.sy + L;
            row++
        ) {
            const y = row * rowH,
                shift = row & 1 ? step / 2 : 0;
            for (
                let x = Math.floor((ptr.sx - L) / step) * step + shift;
                x <= ptr.sx + L;
                x += step
            ) {
                const d = Math.hypot(x - ptr.sx, y - ptr.sy);
                if (d > L) continue;
                ctx.fillStyle = `rgba(150,225,235,${0.42 * Math.pow(1 - d / L, 2) * ptr.spot})`;
                ctx.fillRect(x - 0.6, y - 0.6, 1.2, 1.2);
            }
        }
        const reach = 190;
        ctx.lineWidth = 0.8;
        geo.items.forEach((it) => {
            const m = pos[it.m.key];
            [{ x: m.x, y: m.y, head: true }, ...m.nodes].forEach((n) => {
                const d = Math.hypot(n.x - ptr.sx, n.y - ptr.sy);
                if (d > reach || d < 4) return;
                const a =
                    0.5 * Math.pow(1 - d / reach, 1.5) * ptr.spot * m.bloom;
                ctx.strokeStyle = `rgba(56,232,198,${a})`;
                ctx.beginPath();
                ctx.moveTo(ptr.sx, ptr.sy);
                ctx.lineTo(n.x, n.y);
                ctx.stroke();
                ctx.fillStyle = `rgba(190,255,240,${a * 1.4})`;
                ctx.fillRect(n.x - 1.2, n.y - 1.2, 2.4, 2.4);
            });
        });
        ctx.fillStyle = `rgba(200,255,245,${0.8 * ptr.spot})`;
        ctx.beginPath();
        ctx.arc(ptr.sx, ptr.sy, 1.8, 0, 6.2832);
        ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";
}
function drawSpark(ctx, g, head, s) {
    const rgb = LINK_RGB[s.kind],
        tail = Math.max(0, head - s.tail),
        N = 18;
    if (s.wake) {
        // A real call lights the whole way it has come, like a fuse, until the afterglow takes over.
        ctx.beginPath();
        for (let i = 0; i <= 28; i++) {
            const pt = qpt(g, (head * i) / 28);
            i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y);
        }
        ctx.strokeStyle = `rgba(${rgb},${0.1 * s.alpha})`;
        ctx.lineWidth = 7;
        ctx.stroke();
        ctx.strokeStyle = `rgba(255,214,190,${0.42 * s.alpha})`;
        ctx.lineWidth = 1.3;
        ctx.stroke();
    }
    for (let pass = 0; pass < 2; pass++) {
        let prev = qpt(g, tail);
        for (let i = 1; i <= N; i++) {
            const u = tail + ((head - tail) * i) / N,
                pt = qpt(g, u),
                f = Math.pow(i / N, 2);
            ctx.strokeStyle = pass
                ? `rgba(255,240,230,${0.85 * f * s.alpha})`
                : `rgba(${rgb},${0.28 * f * s.alpha})`;
            ctx.lineWidth = pass
                ? s.width * (0.3 + 0.7 * f)
                : s.width * 4.5 * (0.4 + 0.6 * f);
            ctx.beginPath();
            ctx.moveTo(prev.x, prev.y);
            ctx.lineTo(pt.x, pt.y);
            ctx.stroke();
            prev = pt;
        }
    }
    const h = qpt(g, head),
        rg = ctx.createRadialGradient(h.x, h.y, 0, h.x, h.y, s.glow);
    rg.addColorStop(0, `rgba(255,255,255,${0.95 * s.alpha})`);
    rg.addColorStop(0.22, `rgba(${rgb},${0.6 * s.alpha})`);
    rg.addColorStop(1, `rgba(${rgb},0)`);
    ctx.fillStyle = rg;
    ctx.fillRect(h.x - s.glow, h.y - s.glow, s.glow * 2, s.glow * 2);
}
function drawFx(ctx, t) {
    const { W, H } = mapState.geo;
    if (!mapState.sparks.length && !mapState.bursts.length) {
        if (mapState.fxDirty) ctx.clearRect(0, 0, W, H);
        mapState.fxDirty = false;
        return;
    }
    mapState.fxDirty = true;
    ctx.clearRect(0, 0, W, H);
    ctx.globalCompositeOperation = "lighter";
    mapState.sparks = mapState.sparks.filter((s) => {
        const g = s.geom();
        if (!g) return false;
        const k = clamp01((t - s.t0) / s.dur);
        if (k <= 0) return true;
        const head = (s.ease || easeInOut)(k) * s.stop;
        drawSpark(ctx, g, head, s);
        if (k >= 1) {
            s.done && s.done(qpt(g, s.stop));
            return false;
        }
        return true;
    });
    mapState.bursts = mapState.bursts.filter((b) => {
        const k = (t - b.t0) / b.dur;
        if (k < 0) return true;
        if (k >= 1) return false;
        const at = b.at ? b.at() : b;
        if (!at) return false;
        const e = easeOut(k),
            r = b.r0 + (b.r1 - b.r0) * e;
        ctx.strokeStyle = `rgba(${b.rgb},${(1 - k) * b.alpha})`;
        ctx.lineWidth = b.width * (1 - k) + 0.3;
        ctx.beginPath();
        ctx.arc(at.x, at.y, r, 0, 6.2832);
        ctx.stroke();
        if (b.flash && k < 0.6) {
            const fr = b.r1 * 0.7,
                fg = ctx.createRadialGradient(at.x, at.y, 0, at.x, at.y, fr);
            fg.addColorStop(0, `rgba(255,236,220,${0.5 * (1 - k / 0.6)})`);
            fg.addColorStop(1, `rgba(${b.rgb},0)`);
            ctx.fillStyle = fg;
            ctx.fillRect(at.x - fr, at.y - fr, fr * 2, fr * 2);
        }
        return true;
    });
    ctx.globalCompositeOperation = "source-over";
}
function spark(geom, kind, opts = {}) {
    if (REDUCED() || !mapState.onScreen) return;
    const g = geom();
    if (!g) return;
    const speed = {
            real: 0.5,
            live: 0.42,
            hover: 0.95,
            refused: 0.5,
            echo: 0.3,
        }[kind],
        base = {
            real: { width: 2.6, glow: 20, tail: 0.34, alpha: 1, wake: true },
            live: { width: 1.6, glow: 11, tail: 0.26, alpha: 0.7 },
            hover: { width: 1.3, glow: 9, tail: 0.3, alpha: 0.55 },
            refused: {
                width: 2.2,
                glow: 14,
                tail: 0.3,
                alpha: 0.95,
                wake: true,
            },
            echo: { width: 1.4, glow: 9, tail: 0.4, alpha: 0.6 },
        }[kind];
    mapState.sparks.push({
        geom,
        kind,
        ...base,
        stop: kind === "refused" ? 0.8 : 1,
        t0: performance.now() + (opts.delay || 0),
        dur: Math.min(
            1700,
            Math.max(kind === "hover" ? 260 : 620, qlen(g) / speed)
        ),
        ...opts,
    });
    mapKick();
}
function burst(at, kind, big = false, delay = 0) {
    if (REDUCED()) return;
    const rgb = LINK_RGB[kind];
    mapState.bursts.push({
        at,
        rgb,
        t0: performance.now() + delay,
        dur: big ? 950 : 560,
        r0: 5,
        r1: big ? 48 : 20,
        width: big ? 2.6 : 1.4,
        alpha: big ? 0.95 : 0.6,
        flash: big,
    });
    if (big)
        mapState.bursts.push({
            at,
            rgb: LINK_RGB.live,
            t0: performance.now() + delay + 150,
            dur: 800,
            r0: 5,
            r1: 30,
            width: 1.4,
            alpha: 0.6,
        });
    mapKick();
}
function flashNode(ref, kind) {
    const svg = $(".mapsvg");
    if (!svg || REDUCED()) return;
    const g =
        ref && ref.length ? $(`.mem[data-k="${ref[0]}"]`, svg) : $(".hub", svg);
    if (!g) return;
    const color = kind === "refused" ? "#ff6a5c" : "#fff4ea";
    if (ref && ref.length === 2) {
        const core = $(`.node[data-n="${ref[1]}"] .core`, g);
        core?.animate(
            [
                { transform: "scale(2.4)", fill: color },
                { transform: "scale(1)" },
            ],
            { duration: 900, easing: "cubic-bezier(.2,.9,.25,1)" }
        );
    } else {
        const core = $(ref && ref.length ? ".mcore" : ".hub-core", g);
        core?.animate(
            [{ transform: "scale(1.18)" }, { transform: "scale(1)" }],
            {
                duration: 700,
                easing: "cubic-bezier(.2,.9,.25,1)",
            }
        );
    }
    if (ref && ref.length)
        $(".mring", g)?.animate(
            [
                {
                    stroke: kind === "refused" ? "#ff6a5c" : "#ffb08c",
                    strokeWidth: 2.5,
                    strokeDasharray: "none",
                },
                { stroke: "rgba(148,163,184,.25)", strokeWidth: 1 },
            ],
            { duration: 1200, easing: "ease-out" }
        );
}

/* ---- real calls between agents */

function linkKey(l) {
    return JSON.stringify([l.from, l.to, !!l.refused]);
}
function refName(ref) {
    const data = graphData || fallbackGraph();
    if (!ref || !ref.length) return "网络中的节点";
    const m = data.members.find((x) => x.key === ref[0]);
    if (!m) return "?";
    if (ref.length === 1) return m.is_me ? "我的助手" : esc(m.name) + " 的助手";
    const n = m.nodes[ref[1]];
    return n ? esc(clip(nodeLabel(n), 14)) : "?";
}
function fire(l, delay = 0, listed = true) {
    if (listed) feedPush(l, true);
    if (REDUCED() || !mapState.onScreen) {
        mapState.glow.set(l.key, 1);
        setTimeout(() => {
            mapState.glow.set(l.key, 0);
            mapKick();
        }, 4000);
        return mapKick();
    }
    const kind = l.refused ? "refused" : "real";
    setTimeout(() => {
        const from = mapEnd(l.from);
        if (from) burst(() => mapEnd(l.from), kind, false);
        flashNode(l.from, kind);
        spark(() => linkGeom(l.key), kind, {
            done: (end) => {
                mapState.glow.set(l.key, 1);
                if (l.refused) {
                    burst(end, "refused", true);
                    return;
                }
                burst(() => mapEnd(l.to), "real", true);
                flashNode(l.to, kind);
                // The excitation carries on into the receiving member: node → member, like a dendrite to its cell body.
                if (l.to.length === 2) {
                    const key = l.to[0],
                        j = l.to[1];
                    spark(() => spokeGeom(key, j, true), "echo", {
                        done: () => flashNode([key], "real"),
                    });
                }
            },
        });
    }, delay);
}
// Looked up by key on every frame: a poll that replaces the link objects must not cut a spark off.
function linkGeom(key) {
    return mapState.links.find((x) => x.key === key)?.g || null;
}
function spokeGeom(key, j, inward = false) {
    const m = mapState.pos?.[key],
        n = m?.nodes[j];
    if (!n) return null;
    const a = inward ? n : m,
        b = inward ? m : n;
    return {
        x0: a.x,
        y0: a.y,
        cx: (a.x + b.x) / 2,
        cy: (a.y + b.y) / 2,
        x1: b.x,
        y1: b.y,
    };
}
function setLinks(list, now) {
    if (now) mapState.skew = Date.now() / 1000 - now;
    const t = now || serverNow(),
        old = new Map(mapState.links.map((l) => [l.key, l])),
        fresh = [];
    const links = (list || []).map((raw) => {
        const l = { ...raw, key: linkKey(raw) },
            prev = mapState.seen.get(l.key);
        l.nextLive =
            old.get(l.key)?.nextLive ||
            performance.now() + 900 + Math.random() * 2400;
        // Every new message flashes the link once (up to four at a time); a link seen for the first time flashes once.
        const gained = prev
            ? Math.max(l.at > prev.at ? 1 : 0, l.msgs - prev.msgs)
            : t - l.at < (mapState.primed ? 120 : 25)
              ? 1
              : 0;
        if (gained) fresh.push([l, Math.min(4, gained)]);
        mapState.seen.set(l.key, { at: l.at, msgs: l.msgs });
        return l;
    });
    if (!mapState.primed) {
        mapState.feed = [];
        links
            .filter((l) => t - l.at < 900 && !fresh.some(([f]) => f === l))
            .slice(0, 3)
            .reverse()
            .forEach((l) => feedPush(l, false));
    }
    mapState.primed = true;
    mapState.links = links;
    // Geometry now, not on the next frame: a call fired right away needs a path to travel.
    if (mapState.pos)
        links.forEach((l) => {
            const a = mapEnd(l.from),
                b = mapEnd(l.to);
            l.g = a && b ? linkCurve(a, b) : null;
        });
    const layer = $(".mapsvg .axons");
    if (layer) {
        // Keyed by link, so an unchanged link keeps its element and its running dash animation.
        const els = new Map(
            $$(".axon", layer).map((el) => [el.dataset.key, el])
        );
        links.forEach((l) => {
            let el = els.get(l.key);
            if (!el)
                layer.append(
                    (el = document.createElementNS(
                        "http://www.w3.org/2000/svg",
                        "path"
                    ))
                );
            els.delete(l.key);
            el.dataset.key = l.key;
            el.setAttribute(
                "class",
                `axon${l.live ? " live" : ""}${l.refused ? " refused" : ""}`
            );
            l.el = el;
            l.lit = false;
        });
        els.forEach((el) => el.remove());
    }
    const sum = $("#map-live");
    if (sum) {
        const live = links.filter((l) => l.live).length,
            recent = links.filter((l) => t - l.at < 600).length;
        sum.textContent = live
            ? ` · ${live} 条交互进行中`
            : recent
              ? ` · ${recent} 条最近交互`
              : "";
    }
    feedDraw();
    let slot = 0;
    fresh
        .slice(0, 6)
        .reverse()
        .forEach(([l, n]) => {
            for (let i = 0; i < n; i++) fire(l, slot++ * 240, i === 0);
        });
    emphasize(mapState.hover.key, mapState.hover.n, mapState.hover.hub);
    mapKick();
}
function feedRow(l, fresh) {
    const when = new Date((l.at + mapState.skew) * 1000).toLocaleTimeString(
            "zh-CN",
            { hour: "2-digit", minute: "2-digit", second: "2-digit" }
        ),
        state = l.refused ? "被拒绝" : l.live ? "进行中" : "已连接";
    return `<li class="${fresh ? "fresh" : ""} ${l.refused ? "refused" : l.live ? "live" : ""}" data-key="${esc(l.key)}"><time>${when}</time><span>${refName(l.from)}</span><b aria-label="发给">→</b><span>${refName(l.to)}</span><em>${state}</em></li>`;
}
// New rows are inserted, not re-rendered, so each arrival animates exactly once.
function feedPush(l, fresh) {
    mapState.feed = [l, ...mapState.feed.filter((x) => x.key !== l.key)].slice(
        0,
        3
    );
    const list = $("#map-feed ol");
    if (!fresh || !list) return;
    $("#map-feed").classList.remove("empty");
    $$("li", list).forEach(
        (li) =>
            (li.dataset.key === l.key || li.classList.contains("idle")) &&
            li.remove()
    );
    list.insertAdjacentHTML("afterbegin", feedRow(l, true));
    $$("li", list)
        .slice(3)
        .forEach((li) => li.remove());
    $("#map-feed").dataset.sig = feedSig();
}
const feedSig = () => mapState.feed.map((l) => l.key + l.at + l.live).join("|");
// Redrawn only when the rows themselves changed, so a poll that learns nothing new changes nothing.
function feedDraw() {
    const box = $("#map-feed");
    if (!box) return;
    box.classList.toggle("empty", !mapState.feed.length);
    const sig = feedSig();
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    $("ol", box).innerHTML = mapState.feed
        .map((l) => feedRow(l, false))
        .join("");
}

/* ---- hover: the pointer wakes up whatever it touches */

function hoverWave(key, n, hub) {
    const id = hub ? "hub" : key + ":" + n,
        t = performance.now();
    if (mapState.waved.id === id && t - mapState.waved.at < 900) return;
    mapState.waved = { id, at: t };
    if (hub) {
        mapState.geo.items.forEach((it, i) =>
            spark(() => mapState.pos?.[it.m.key]?.hw, "hover", {
                delay: i * 40,
            })
        );
        return;
    }
    const it = mapState.geo.byKey.get(key);
    if (!it) return;
    if (n === null) {
        spark(() => mapState.pos?.[key]?.hw, "hover", {
            done: () => {
                it.rel.forEach((_, j) =>
                    spark(() => spokeGeom(key, j), "hover", { delay: j * 45 })
                );
                $$(".node .ring", it.el).forEach((r, j) =>
                    r.animate(
                        [
                            { transform: "scale(.6)", opacity: 0.9 },
                            { transform: "scale(2.6)", opacity: 0 },
                        ],
                        {
                            duration: 760,
                            delay: 140 + j * 45,
                            easing: "ease-out",
                        }
                    )
                );
            },
        });
        $(".aura", it.el)?.animate(
            [{ opacity: 0.35 }, { opacity: 1 }, { opacity: 0.85 }],
            { duration: 700, easing: "ease-out" }
        );
    } else {
        spark(() => spokeGeom(key, n), "hover");
        $(`.node[data-n="${n}"] .ring`, it.el)?.animate(
            [
                { transform: "scale(.6)", opacity: 1 },
                { transform: "scale(3)", opacity: 0 },
            ],
            { duration: 700, easing: "ease-out" }
        );
    }
}
function mapStats(m) {
    const agents = m.nodes.filter((n) => n.kind === "agent"),
        devices = m.nodes.filter((n) => n.kind === "device");
    return `${agents.length} 个智能体 · ${devices.length} 台电脑 · ${m.nodes.filter((n) => n.status === "ready").length} 个在线`;
}
function mapChrome() {
    const bar = $("#map-focus");
    if (!bar) return;
    const m = (graphData || fallbackGraph()).members.find(
        (x) => x.key === mapState.focus
    );
    bar.hidden = !m;
    if (m)
        $(".map-focus-copy", bar).innerHTML =
            `<strong>${esc(m.name)}${m.is_me ? " · 我" : ""}</strong><span>${mapStats(m)}</span>`;
    $("#map-hint").hidden = !!m;
}
function setMapFocus(key) {
    mapState.focus = key;
    mapChrome();
    emphasize(null, null);
    mapState.geo.items.forEach((it) => (it.lastS = null));
    mapState.tw = {
        from: mapState.cur,
        to: mapTargets(mapState.geo, key),
        t0: performance.now(),
        ms: 640,
    };
    mapKick();
}
function touches(l, key, n) {
    return [l.from, l.to].some(
        (r) =>
            r.length &&
            r[0] === key &&
            (n === null || r.length === 1 || r[1] === n)
    );
}
function emphasize(key, n, hub = false) {
    const svg = $(".mapsvg");
    if (!svg) return;
    svg.classList.toggle("hl", !!key || hub);
    svg.classList.toggle("hl-hub", hub);
    $$(".mem", svg).forEach((g) => {
        g.classList.toggle("hot", g.dataset.k === key);
        g.classList.toggle("hl-node", g.dataset.k === key && n !== null);
        $$(".node,.spoke", g).forEach((x) =>
            x.classList.toggle(
                "hot",
                g.dataset.k === key && n !== null && +x.dataset.n === n
            )
        );
    });
    $$(".hwire", svg).forEach((w) =>
        w.classList.toggle("hot", hub || w.dataset.k === key)
    );
    $$(".node.peer", svg).forEach((x) => x.classList.remove("peer"));
    mapState.links.forEach((l) => {
        const on = !!key && touches(l, key, n);
        l.el?.classList.toggle(
            "hot",
            on || (hub && (!l.from.length || !l.to.length))
        );
        if (on)
            [l.from, l.to].forEach((r) => {
                if (r.length === 2 && !(r[0] === key && r[1] === n))
                    $(
                        `.mem[data-k="${r[0]}"] .node[data-n="${r[1]}"]`,
                        svg
                    )?.classList.add("peer");
            });
    });
}
function mapTip(e, key, n, hub = false) {
    const tip = $("#map-tip"),
        stage = $(".netmap");
    if (!tip || !stage) return;
    if (!key && !hub) {
        tip.hidden = true;
        return;
    }
    const data = graphData || fallbackGraph(),
        m = data.members.find((x) => x.key === key),
        recent = mapState.links
            .filter((l) => (hub ? true : touches(l, key, n)))
            .sort((a, b) => b.at - a.at)[0],
        ix = recent
            ? `<span class="tip-ix ${recent.live ? "live" : ""}">${recent.live ? "交互进行中" : "最近交互"} · ${agoSec(recent.at)}</span>`
            : "";
    let html;
    if (hub)
        html = `<strong>Agent Space</strong><span>${data.members.length} 位成员 · 24 小时内 ${mapState.links.length} 条连线</span>${ix}`;
    else if (n === null)
        html = `<strong>${esc(m.name)}${m.is_me ? " · 我" : ""}</strong><span>${mapStats(m)}</span>${ix}<em>${mapState.focus === key ? "点一下回到全部" : "点一下只看 TA"}</em>`;
    else {
        const x = m.nodes[n] || { ghost: true },
            st = x.ghost ? null : stateInfo(x);
        html = x.ghost
            ? "<strong>还没连电脑</strong><span>点一下去连接</span>"
            : x.redacted
              ? `<strong>${esc(m.name)} 的${x.kind === "device" ? "一台电脑" : "一个私有智能体"}</strong><span>${st.label}</span><em>名字和说明只有所有者能看到</em>`
              : `<strong>${esc(x.name)}</strong><span>${esc(x.kind === "device" ? "电脑" : executorName(x))} · ${st.label}${x.long_task ? " · 长任务" : ""} · ${x.is_owner ? "我接入的" : esc(x.owner_name)}</span>${ix}<em>点一下看说明</em>`;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const r = stage.getBoundingClientRect();
    tip.style.setProperty(
        "--tx",
        Math.min(Math.max(8, e.clientX - r.left + 16), r.width - 250) + "px"
    );
    tip.style.setProperty(
        "--ty",
        Math.min(Math.max(8, e.clientY - r.top + 16), r.height - 96) + "px"
    );
}
function openMapNode(m, j) {
    const n = m.nodes[j];
    if (!n) return;
    if (n.ghost)
        return n.kind === "device" ? connectSender() : edit(null, "agent");
    if (!n.redacted) {
        const rec = findRecord(n.id);
        if (rec) return rec.aid ? details(rec) : pair(rec);
    }
    const st = stateInfo(n),
        what =
            n.kind === "device"
                ? "一台电脑"
                : n.redacted
                  ? "一个私有智能体"
                  : "一个智能体";
    openDialog({
        kicker: "连接图",
        title: esc(m.name) + " 的" + what,
        sub: "状态：" + esc(st.label),
        body: `<p class="detail-state"><strong>${n.redacted ? "这是" + esc(m.name) + "没有共享的" + (n.kind === "device" ? "电脑" : "智能体") + "，当前" + esc(st.label) + "。" : esc(st.meaning)}</strong></p><p class="hint">${n.redacted ? "名字、说明和运行环境只有所有者能看到，你不能直接用它。" : "这个智能体还没出现在你的目录里，刷新后再试。"}团队共享的智能体可以在「智能体」页找到并派任务。</p><div class="form-actions"><a class="btn primary" href="#directory" id="map-to-dir">去智能体页${icon("arrow")}</a></div>`,
    });
    const link = $("#map-to-dir");
    if (link) link.onclick = () => modal.close();
}

/* ---- the loop */

function mapKick() {
    if (!mapState.raf && mapState.geo)
        mapState.raf = requestAnimationFrame(mapFrame);
}
function mapFrame(t) {
    mapState.raf = 0;
    const slot = $("#map-slot");
    if (!slot || !mapState.geo || !$(".mapsvg", slot)) return;
    // Nothing in flight and nobody pointing: drifting dust needs no more than ~30 frames a second.
    const idle =
        !mapState.sparks.length &&
        !mapState.bursts.length &&
        !mapState.tw &&
        !mapState.born &&
        !mapState.ptr.inside &&
        mapState.ptr.spot < 0.01;
    if (idle && mapState.last && t - mapState.last < 30 && !REDUCED()) {
        if (mapState.onScreen && !document.hidden)
            mapState.raf = requestAnimationFrame(mapFrame);
        return;
    }
    const dt = Math.min(64, Math.max(0, t - (mapState.last || t)));
    mapState.last = t;
    mapAnchors(t);
    const pos = mapPlace(t, dt);
    mapPaint(pos);
    if (mapState.field) drawField(mapState.field, t, dt);
    if (mapState.fx) drawFx(mapState.fx, t);
    // Open tasks pulse on their own, every few seconds.
    mapState.links.forEach((l) => {
        if (!l.live || l.refused || t < l.nextLive) return;
        l.nextLive = t + 2600 + Math.random() * 1800;
        spark(() => linkGeom(l.key), "live");
    });
    if (!REDUCED() && mapState.onScreen && !document.hidden && !mapState.raf)
        mapState.raf = requestAnimationFrame(mapFrame);
}
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
        mapState.last = 0;
        mapKick();
    }
});

function mapFind(e) {
    const g = e.target.closest(".mem"),
        n = e.target.closest(".node");
    return g ? { key: g.dataset.k, n: n ? +n.dataset.n : null } : null;
}
function mapHoverTo(e, f, hub) {
    const key = f ? f.key : null,
        n = f ? f.n : null,
        h = mapState.hover;
    if (h.key !== key || h.n !== n || h.hub !== hub) {
        mapState.hover = { key, n, hub };
        emphasize(key, n, hub);
        if (key || hub) hoverWave(key, n, hub);
    }
    mapTip(e, key, n, hub);
}
// The slot outlives redraws of the map inside it, so its pointer tracking is bound once.
const mapSlots = new WeakSet();
function bindMapSlot(slot) {
    if (mapSlots.has(slot)) return;
    mapSlots.add(slot);
    const ptr = mapState.ptr;
    slot.addEventListener("pointermove", (e) => {
        const r = slot.getBoundingClientRect();
        ptr.x = e.clientX - r.left;
        ptr.y = e.clientY - r.top;
        if (!ptr.inside) {
            ptr.sx = ptr.x;
            ptr.sy = ptr.y;
        }
        ptr.inside = true;
        ptr.mouse = e.pointerType === "mouse" || e.pointerType === "pen";
        const f = mapFind(e);
        if (f || e.target.closest(".hub")) mapTip(e, f && f.key, f && f.n, !f);
        mapKick();
    });
    slot.addEventListener("pointerdown", () => (ptr.down = true));
    slot.addEventListener("pointerleave", (e) => {
        ptr.inside = false;
        ptr.down = false;
        mapHoverTo(e, null, false);
        mapKick();
    });
}
window.addEventListener("pointerup", () => (mapState.ptr.down = false));
window.addEventListener("pointercancel", () => (mapState.ptr.down = false));
function drawMap() {
    const slot = $("#map-slot");
    if (!slot) return;
    cancelAnimationFrame(mapState.raf);
    mapState.raf = 0;
    const data = graphData || fallbackGraph();
    if (mapState.focus && !data.members.some((m) => m.key === mapState.focus))
        mapState.focus = null;
    const W = Math.max(300, Math.round(slot.clientWidth)),
        H = Math.max(360, Math.round(slot.clientHeight));
    const geo = mapGeometry(data.members, W, H);
    mapState.geo = geo;
    const online = data.members.reduce(
        (t, m) =>
            t +
            m.nodes.filter((n) => n.kind === "agent" && n.status === "ready")
                .length,
        0
    );
    slot.innerHTML = `<canvas class="map-field" aria-hidden="true"></canvas>
      <span class="map-title"><i></i>LIVE · 连接图</span><span class="map-sum">${data.members.length} 位成员 · ${online} 个智能体在线<b id="map-live"></b></span>
      ${mapSvg(geo)}
      <canvas class="map-fx" aria-hidden="true"></canvas>
      <p class="map-hint" id="map-hint">移动鼠标唤醒连线 · 点成员只看 TA · 点节点看说明</p>
      <div class="map-focus" id="map-focus" hidden><button class="btn small" id="map-all">${icon("back")}全部成员</button><div class="map-focus-copy"></div></div>
      <div class="map-tip" id="map-tip" hidden></div>
      <div class="map-feed empty" id="map-feed" aria-live="polite"><p><i></i>A2A 实时</p><ol></ol></div>
      <div class="map-legend"><span class="ok"><i></i>在线</span><span class="busy"><i></i>执行中</span><span class="warn"><i></i>待处理</span><span><i></i>离线</span><span class="ax"><i></i>交互</span></div>
      <button class="map-more" id="map-more" aria-label="往下看详细信息">详细信息${icon("arrow")}</button>`;
    mapState.field = mapCanvas($(".map-field", slot), W, H);
    mapState.fx = mapCanvas($(".map-fx", slot), W, H);
    mapState.fxDirty = false;
    mapState.sparks = [];
    mapState.bursts = [];
    mapState.lean.clear();
    mapState.links.forEach((l) => (l.el = null));
    seedDust(W, H);
    $$(".mem", slot).forEach((g) =>
        $$(".node", g).forEach((n, j) => n.style.setProperty("--i", j))
    );
    const svg = $(".mapsvg", slot);
    bindMapSlot(slot);
    svg.addEventListener("pointerover", (e) =>
        mapHoverTo(e, mapFind(e), !!e.target.closest(".hub"))
    );
    svg.addEventListener("pointerout", (e) => {
        const to = e.relatedTarget;
        if (
            !to ||
            !svg.contains(to) ||
            (!to.closest(".mem") && !to.closest(".hub"))
        )
            mapHoverTo(e, null, false);
    });
    svg.addEventListener("focusin", (e) => {
        const f = mapFind(e);
        if (f) {
            mapState.hover = { key: f.key, n: f.n, hub: false };
            emphasize(f.key, f.n);
        }
    });
    svg.addEventListener("focusout", () => {
        mapState.hover = { key: null, n: null, hub: false };
        emphasize(null, null);
    });
    svg.addEventListener("click", (e) => {
        const f = mapFind(e);
        if (!f) return setMapFocus(null);
        const m = data.members.find((x) => x.key === f.key);
        if (f.n !== null) return guard(() => openMapNode(m, f.n))();
        if (e.target.closest(".mhead"))
            setMapFocus(mapState.focus === f.key ? null : f.key);
    });
    svg.addEventListener("keydown", (e) => {
        if (
            (e.key === "Enter" || e.key === " ") &&
            e.target.matches(".node,.mhead")
        ) {
            e.preventDefault();
            e.target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        }
        if (e.key === "Escape" && mapState.focus) setMapFocus(null);
    });
    $("#map-all").onclick = () => setMapFocus(null);
    $("#map-more").onclick = () =>
        $("#home-details")?.scrollIntoView({
            behavior: REDUCED() ? "auto" : "smooth",
            block: "start",
        });
    mapState.io?.disconnect();
    if ("IntersectionObserver" in window) {
        mapState.io = new IntersectionObserver(([en]) => {
            mapState.onScreen = en.isIntersecting;
            if (en.isIntersecting) {
                mapState.last = 0;
                mapKick();
            }
        });
        mapState.io.observe(slot);
    }
    mapChrome();
    const target = mapTargets(geo, mapState.focus);
    mapState.cur = target;
    mapState.tw = null;
    mapState.born = mapState.intro && !REDUCED() ? performance.now() : 0;
    mapState.intro = false;
    mapState.hover = { key: null, n: null, hub: false };
    mapState.last = 0;
    mapFrame(performance.now());
    setLinks(data.links, data.now);
    mapPollStart();
}
async function refreshGraph() {
    try {
        applyGraph(await api("/api/graph"));
    } catch {
        /* the next tick will try again */
    }
}
// Same members and nodes: keep the map as it is. Only a changed state is painted in place;
// a changed cast of members or nodes is the one case that rebuilds it.
const mapShape = (g) =>
    JSON.stringify(
        g &&
            g.members.map((m) => [
                m.key,
                m.name,
                m.nodes.map((n) => [
                    n.kind,
                    !!n.redacted,
                    n.redacted ? "" : n.name,
                ]),
            ])
    );
function applyGraph(next) {
    if (route !== "start") return;
    const prev = graphData;
    graphData = next;
    if (!$("#map-slot") || !mapState.geo) return;
    if (mapShape(next) !== mapShape(prev)) {
        if (!modal.open) drawMap();
        return;
    }
    if (stable(next.members) !== stable(prev.members)) patchMap();
    setLinks(next.links, next.now);
}
function nodeSub(n) {
    return n.ghost
        ? ""
        : n.redacted
          ? stateInfo(n).label
          : n.kind === "device"
            ? "电脑"
            : clip(executorName(n), 14) + " · " + WORK_LABELS[workState(n)];
}
const TONES = ["ok", "busy", "warn", "info", "off", "ghost"];
function setTone(el, tone) {
    if (
        el.classList.contains(tone) &&
        !TONES.some((t) => t !== tone && el.classList.contains(t))
    )
        return;
    el.classList.remove(...TONES);
    el.classList.add(tone);
}
function patchMap() {
    const svg = $(".mapsvg"),
        geo = mapState.geo;
    graphData.members.forEach((m) => {
        const it = geo.byKey.get(m.key);
        if (!it || !it.el) return;
        it.m = m;
        it.nodes = m.nodes.length ? m.nodes : it.nodes;
        setTone(it.el, memberTone(m));
        setTone($(`.hwire[data-k="${m.key}"]`, svg), memberTone(m));
        m.nodes.forEach((n, j) => {
            const ne = $(`.node[data-n="${j}"]`, it.el);
            if (!ne) return;
            setTone(ne, nodeTone(n));
            setTone($(`.spoke[data-n="${j}"]`, it.el), nodeTone(n));
            ne.setAttribute(
                "aria-label",
                `${nodeLabel(n)}，${stateInfo(n).label}`
            );
            const sub = $(".sub", ne);
            if (sub) sub.textContent = nodeSub(n);
        });
    });
    const online = graphData.members.reduce(
        (t, m) =>
            t +
            m.nodes.filter((n) => n.kind === "agent" && n.status === "ready")
                .length,
        0
    );
    const sum = $(".map-sum");
    if (sum)
        sum.firstChild.textContent = `${graphData.members.length} 位成员 · ${online} 个智能体在线`;
    mapChrome();
}
function mapPollStart() {
    if (mapState.poll) return;
    // Calls between agents are short events; the map asks for them more often than the rest of the page refreshes.
    mapState.poll = setInterval(() => {
        if (route !== "start" || !$("#map-slot")) {
            clearInterval(mapState.poll);
            mapState.poll = 0;
            return;
        }
        if (!document.hidden && !modal.open && mapState.onScreen)
            refreshGraph();
    }, 2500);
}
let mapResize = null;
window.addEventListener("resize", () => {
    clearTimeout(mapResize);
    mapResize = setTimeout(() => {
        if (route === "start" && $("#map-slot")) drawMap();
    }, 150);
});

/* ------------------------------------------------------------------ home */

function renderHome(detailsOnly = false) {
    if (!detailsOnly) shell();
    const senders = callers(),
        ready = readyAgents(),
        own = workspace.own;
    const attention = [...workspace.devices, ...own].filter(
        (a) => a.status !== "ready"
    );
    const onlineDevices = workspace.devices.filter(
        (a) => a.status === "ready"
    ).length;
    const longCount = ready.filter((a) => a.long_task).length;
    const step = !senders.length ? 0 : !ready.length ? 1 : 2;
    // count · label · out of (for the meter) · tone when non-zero · icon
    const gauges = [
        [
            onlineDevices,
            "台电脑在线",
            workspace.devices.length,
            "lit",
            "device",
        ],
        [ready.length, "个智能体可用", catalog().length, "lit", "agent"],
        [longCount, "个支持长任务", ready.length, "hot", "bolt"],
        [
            attention.length,
            "个要处理",
            workspace.devices.length + own.length,
            "warn",
            "wrench",
        ],
    ];
    const hour = new Date().getHours();
    const greet =
        hour < 6
            ? "夜深了"
            : hour < 12
              ? "早上好"
              : hour < 18
                ? "下午好"
                : "晚上好";
    const title =
        step === 2
            ? `<span class="glow">${ready.length}</span> 个智能体<br>可以接任务`
            : step === 1
              ? '还差一个<br><span class="glow">能干活的</span>智能体'
              : '先连上<br><span class="glow">你的电脑</span>';
    const next = [
        [
            "连接这台电脑",
            "让空间知道任务从哪台电脑发出，连一次就行。",
            `<button class="btn primary" data-act="connect-sender">${icon("device")}开始连接</button>`,
        ],
        [
            "接入一个智能体",
            "把装好的 Codex、Pi 这些接进来，或者等同事共享。",
            `<button class="btn primary" data-act="new-agent">${icon("plus")}接入智能体</button>`,
        ],
        [
            "派一个任务",
            "从下面挑一个，或者去智能体列表里找。",
            `<a class="btn primary" href="#directory">${icon("arrow")}去挑选</a>`,
        ],
    ][step];
    const details = `
    <section class="home-top">
      <div class="hero">
        <p class="kicker">${greet}，${esc(me.name)}</p>
        <h1 tabindex="-1">${title}</h1>
        <div class="next-card">
          <ol class="next-steps" aria-label="上手进度">${["连接电脑", "接入智能体", "派任务"].map((l, i) => `<li class="${i < step ? "done" : i === step ? "now" : ""}"><b>${i < step ? icon("check") : i + 1}</b>${l}</li>`).join("")}</ol>
          <div class="next-body"><span class="next-no">0${step + 1}</span><div><strong>${next[0]}</strong><p>${next[1]}</p></div>${next[2]}</div>
        </div>
      </div>
      <div class="telemetry">
        ${gauges.map(([n, label, of, tone, ic]) => `<div class="gauge ${n ? tone : ""}" data-v="${of ? Math.min(1, n / of) : 0}">${icon(ic)}<b>${n}</b><span>${label}${of ? `<em>/ ${of}</em>` : ""}</span><i class="meter"><s></s></i></div>`).join("")}
      </div>
    </section>
    <div class="home-grid">
      <section class="panel">
        <header class="panel-head"><h2>可用的智能体</h2><a class="text-link" href="#directory">全部 ${icon("arrow")}</a></header>
        ${
            ready.length
                ? `<div class="mini-list">${ready
                      .sort(byReadiness)
                      .slice(0, 5)
                      .map(
                          (a) => `
          <div class="mini-row"><span class="mono-avatar">${initials(a.name)}</span><div class="mini-copy"><strong>${esc(a.name)}</strong><span>${esc(executorName(a))} · ${a.is_owner ? "我接入的" : esc(a.owner_name)}${a.long_task ? ' · <b class="tag accent">长任务</b>' : ""}</span></div>
          <button class="btn small ${senders.length ? "primary" : ""}" data-act="dispatch" data-id="${a.id}">派任务</button></div>`
                      )
                      .join("")}</div>`
                : `<div class="quiet-empty"><p>现在没有在线的智能体。</p><button class="btn" data-act="new-agent">${icon("plus")}接入自己的</button></div>`
        }
      </section>
      <section class="panel">
        <header class="panel-head"><h2>要处理的</h2><a class="text-link" href="#agents">我的连接 ${icon("arrow")}</a></header>
        ${
            attention.length
                ? `<div class="mini-list">${attention
                      .slice(0, 5)
                      .map(
                          (a) => `
          <button class="mini-row as-button" data-act="open-record" data-id="${a.id}"><span class="mono-avatar ${a.kind === "device" ? "device" : ""}">${icon(a.kind === "device" ? "device" : "agent")}</span><div class="mini-copy"><strong>${esc(a.name)}</strong><span>${esc(stateInfo(a).next)}</span></div>${pill(a)}</button>`
                      )
                      .join("")}</div>`
                : `<div class="quiet-empty"><p>${workspace.devices.length + own.length ? "都正常，没有要处理的。" : "你还没连接任何电脑或智能体。"}</p></div>`
        }
      </section>
    </div>`;
    if (detailsOnly) $("#home-details").innerHTML = details;
    else {
        $("#content").innerHTML = `
    <section class="map-stage"><div class="netmap" id="map-slot" aria-label="连接图"></div></section>
    <div class="home-details" id="home-details">${details}</div>`;
        drawMap();
    }
    $$(".gauge").forEach((g) => g.style.setProperty("--v", g.dataset.v));
    on("[data-act=connect-sender]", "click", connectSender);
    on("[data-act=new-agent]", "click", () => edit(null, "agent"));
    on("[data-act=dispatch]", "click", (el) =>
        openTask(findRecord(el.dataset.id))
    );
    on("[data-act=open-record]", "click", (el) => {
        const a = findRecord(el.dataset.id);
        return a.aid ? details(a) : pair(a);
    });
}
function connectSender() {
    const d = workspace.devices[0];
    if (d) return d.aid ? details(d) : pair(d);
    return edit(null, "device");
}

/* ------------------------------------------------------------------ directory */

function capabilityTags(a) {
    if (!a.long_task) return "";
    const c = a.capabilities || {};
    return `<div class="tags"><span class="tag accent">${icon("bolt")}长任务</span>${c.gui ? '<span class="tag">声明界面能力</span>' : ""}${c.review ? '<span class="tag">有审查</span>' : ""}${c.max_hours ? `<span class="tag">时长参数上限 ${esc(c.max_hours)} 小时</span>` : ""}</div>`;
}
function agentCard(a) {
    const s = stateInfo(a),
        own = a.is_owner,
        setup = own && !a.aid;
    const main = setup
        ? ["setup", "继续连接"]
        : receiveReady(a)
          ? ["dispatch", "派任务"]
          : ["details", "看看状态"];
    return `<article class="agent-card tone-${s.tone}" ${a.aid ? `data-card="${a.id}" title="点一下看详情"` : ""}>
    <div class="card-top"><span class="mono-avatar large">${initials(a.name)}</span>${led(a)}</div>
    <h3>${esc(a.name)}</h3>
    <p class="card-meta">${esc(executorName(a))}<span>·</span>${own ? "我接入的" : esc(a.owner_name)}<span>·</span>${a.visibility === "private" ? `${icon("lock")}只有我` : `${icon("globe")}团队共享`}</p>
    ${capabilityTags(a)}
    ${workSummary(a)}
    <p class="card-desc">${esc(a.description || (own ? "还没写它擅长什么，可以在编辑里补上。" : "所有者没写说明，用之前可以先问问。"))}</p>
    <footer><span class="card-hint">${esc(a.status === "ready" ? ago(a.last_seen) : s.next)}</span><span class="card-actions">${own ? `<button class="icon-btn" data-act="edit" data-id="${a.id}" aria-label="编辑" title="编辑">${icon("wrench")}</button>` : ""}${main[0] !== "details" && a.aid ? `<button class="btn small ghost" data-act="details" data-id="${a.id}">详情</button>` : ""}<button class="btn small ${main[0] === "dispatch" ? "primary" : ""}" data-act="${main[0]}" data-id="${a.id}">${main[1]}</button></span></footer>
  </article>`;
}
function renderDirectory() {
    shell();
    const v = view.directory,
        all = catalog(),
        senders = callers();
    $("#content").innerHTML =
        pageHead("Directory", "智能体", "你接入的和同事共享的都在这里。") +
        (!senders.length
            ? `<div class="banner">${icon("device")}<div><strong>还没有能发任务的电脑</strong><p>先连上你平时用的电脑，再回来派任务。</p></div><button class="btn" data-act="connect-sender">去连接</button></div>`
            : "") +
        `<div class="toolbar"><div class="chips" role="tablist" aria-label="筛选">${[
            ["all", "全部"],
            ["ready", "可用"],
            ["working", "执行中"],
            ["attention", "待处理"],
            ["long", "长任务"],
            ["mine", "我的"],
        ]
            .map(
                ([id, n]) =>
                    `<button role="tab" class="chip ${v.filter === id ? "on" : ""}" aria-selected="${v.filter === id}" data-filter="${id}">${n}<b>${countFor(all, id)}</b></button>`
            )
            .join("")}</div>
    <label class="search">${icon("search")}<input id="search" placeholder="搜名字、软件或擅长的事" value="${esc(v.query)}" aria-label="搜索智能体"></label></div>
    <div class="card-grid" id="grid"></div>`;
    drawDirectory();
    on("[data-filter]", "click", (el) => {
        v.filter = el.dataset.filter;
        renderDirectory();
    });
    $("#search").oninput = (e) => {
        v.query = e.target.value;
        drawDirectory();
    };
    on("[data-act=connect-sender]", "click", connectSender);
}
function countFor(all, f) {
    return all.filter((a) => matchFilter(a, f)).length;
}
function matchFilter(a, f) {
    if (f === "working") return workState(a) === "running";
    if (f === "attention") return workState(a) !== "unknown" && ((a.work?.counts?.waiting || 0) + (a.work?.counts?.blocked || 0) > 0 || (a.work?.tasks || []).some(t=>t.attention));
    return (
        f === "all" ||
        (f === "ready"
            ? receiveReady(a)
            : f === "long"
              ? a.long_task
              : f === "mine"
                ? a.is_owner
                : true)
    );
}
function drawDirectory() {
    const v = view.directory,
        all = catalog(),
        q = v.query.trim().toLowerCase();
    const shown = all
        .filter(
            (a) =>
                matchFilter(a, v.filter) &&
                `${a.name} ${a.description} ${executorName(a)} ${a.owner_name}`
                    .toLowerCase()
                    .includes(q)
        )
        .sort(byReadiness);
    $("#grid").innerHTML =
        shown.map(agentCard).join("") ||
        `<div class="empty"><span class="seal big">空</span><h3>${q ? "没搜到" : all.length ? "这个筛选下没有" : "还没有智能体"}</h3><p>${q ? "换个词试试，或者清空搜索。" : all.length ? "换个筛选看看。" : "接入你自己的，或者等同事共享。"}</p>${!all.length ? `<button class="btn primary" data-act="new-agent">${icon("plus")}接入智能体</button>` : ""}</div>`;
    bindCards();
}
function bindCards() {
    on("[data-act=dispatch]", "click", (el) =>
        openTask(findRecord(el.dataset.id))
    );
    on("[data-act=details]", "click", (el) =>
        details(findRecord(el.dataset.id))
    );
    on("[data-act=setup]", "click", (el) => pair(findRecord(el.dataset.id)));
    on("[data-act=edit]", "click", (el) => edit(findRecord(el.dataset.id)));
    on("[data-act=new-agent]", "click", () => edit(null, "agent"));
    $$(".agent-card[data-card]").forEach((card) => {
        card.classList.add("clickable");
        card.addEventListener("click", (e) => {
            if (e.target.closest("button, a")) return;
            guard(() => details(findRecord(card.dataset.card)))();
        });
    });
    $$(".agent-card").forEach((card) =>
        card.addEventListener("pointermove", (e) => {
            const r = card.getBoundingClientRect();
            card.style.setProperty("--mx", e.clientX - r.left + "px");
            card.style.setProperty("--my", e.clientY - r.top + "px");
        })
    );
}

/* ------------------------------------------------------------------ my connections */

function checks(a) {
    if (a.kind === "device")
        return [
            ["身份", !!a.aid],
            ["在线", a.status === "ready"],
            ["回传", !!a.requester_ready],
        ];
    return [
        ["身份", !!a.aid],
        [
            "在线",
            !["pending", "bound", "offline", "disabled"].includes(a.status),
        ],
        ["门户独占策略", !!a.policy_ok],
        ["执行器", !!a.executor_ready],
    ];
}
function pipeline(a) {
    const c = checks(a);
    return `<div class="pipeline" aria-label="连接检查"><div class="bars">${c.map(([, ok]) => `<i class="${ok ? "ok" : ""}"></i>`).join("")}</div><div class="labels">${c.map(([n, ok]) => `<span class="${ok ? "ok" : ""}">${n}</span>`).join("")}</div></div>`;
}
function recordRow(a) {
    const s = stateInfo(a),
        device = a.kind === "device";
    const main = !a.aid
        ? ["setup", "继续连接", "primary"]
        : a.status === "ready"
          ? device
              ? ["to-directory", "去派任务", ""]
              : ["dispatch", "派任务", "primary"]
          : a.is_owner && sendReady(a)
            ? ["to-directory", "去派任务", ""]
            : ["details", "排查", "primary"];
    const next =
        a.status !== "ready"
            ? sendReady(a) ? "发起连接可用；接收执行尚未就绪，可以继续向已就绪的智能体派任务。" : s.next
            : device
              ? a.requester_ready
                ? "节点上报回传就绪；每个任务仍需确认原会话绑定和实际回传。"
                  : "自动回传尚未确认，具体原因未提供；请核对本机回传状态。"
              : ago(a.last_seen);
    return `<article class="record tone-${s.tone}">
    <span class="mono-avatar ${device ? "device" : ""}">${icon(device ? "device" : "agent")}</span>
    <div class="record-main">
      <div class="record-title"><h3>${esc(a.name)}</h3>${pill(a)}${a.long_task ? '<span class="tag accent">长任务</span>' : ""}</div>
      <p class="record-sub">${device ? "发任务用的电脑" : `${esc(executorName(a))} · ${a.visibility === "private" ? "只有我" : "团队共享"}`}${a.aid ? ` · <span class="mono">${shortAid(a.aid)}</span>` : ""}</p>
      <p class="record-next">${esc(next)}</p>
      ${device ? "" : workSummary(a)}
    </div>
    ${pipeline(a)}
    <div class="record-actions"><button class="icon-btn" data-act="edit" data-id="${a.id}" aria-label="编辑" title="编辑">${icon("wrench")}</button>${a.aid ? `<button class="btn small ghost" data-act="details" data-id="${a.id}">详情</button>` : ""}<button class="btn small ${main[2]}" data-act="${main[0]}" data-id="${a.id}">${main[1]}</button></div>
  </article>`;
}
function renderMine() {
    shell();
    const list = route === "devices" ? workspace.devices : [...workspace.devices, ...workspace.own];
    $("#content").innerHTML = pageHead("Connections", "我的连接", "每台电脑连接一次：先发任务，需要时再开启接收任务。", `<button class="btn primary" data-act="create">${icon("plus")}连接电脑</button>`) +
    `<p class="tab-intro">能接收任务的连接也可以发任务，不需要重复安装。已有连接可在详情查看安装诊断。</p><div class="records">${list.map(recordRow).join("") || '<div class="empty"><h3>连接你的第一台电脑</h3><p>把安装说明交给电脑上的助手，它会按进度继续。</p><button class="btn primary" data-act="create">连接电脑</button></div>'}</div>`;
    on("[data-act=create]", "click", () => connectComputer());
    on("[data-act=to-directory]", "click", () => go("directory"));
    bindCards();
}

/* ------------------------------------------------------------------ create / edit */

function edit(a = null, kind = "agent", returnTarget = null) {
    if (!a) return connectComputer(null, returnTarget, kind === "agent");
    const device = a ? a.kind === "device" : kind === "device",
        software = a?.executor || "";
    openDialog({
        kicker: a ? "编辑" : device ? "新电脑" : "新智能体",
        title: a ? esc(a.name) : device ? "连接一台电脑" : "接入一个智能体",
        sub: a
            ? ""
            : device
              ? "给它起个好认的名字，下一步到这台电脑上完成连接。"
              : "先填资料，下一步把连接说明交给那台电脑上的助手。",
        body: `${!a ? stepper(["填资料", "到电脑上连接", "检查"], 0) : ""}
    <form id="record-form" class="form">
      ${device ? "" : `<label>用什么软件<select name="executor" id="executor" required ${a?.aid ? "disabled" : ""}><option value="" disabled ${software ? "" : "selected"}>选那台电脑上装好的软件</option>${harnesses.map((h) => `<option value="${h.id}" ${software === h.id ? "selected" : ""}>${esc(h.id === "custom" ? "其他（自定义命令）" : h.name)}</option>`).join("")}</select></label><p class="hint" id="executor-note">${esc(executorNote(software))}${a?.aid ? " 已经连上的，要先断开才能换软件。" : ""}</p>`}
      <label>名字<input name="name" value="${esc(a?.name || "")}" placeholder="${device ? "比如：办公室电脑、我的笔记本" : "比如：资料整理、EPS 仿真机"}" required maxlength="60"></label>
      <label>${device ? "备注" : "它擅长什么"} <span class="optional">选填</span><textarea name="description" maxlength="400" placeholder="${device ? "比如：平时写代码的那台 Mac" : "比如：整理 PDF、分析表格、在 EPS 里跑仿真"}">${esc(a?.description || "")}</textarea></label>
      ${device ? "" : `<fieldset class="choice"><legend>谁能用</legend><label class="option"><input type="radio" name="visibility" value="private" ${a?.visibility === "public" ? "" : "checked"}><span><strong>${icon("lock")}只有我</strong><small>只有你名下连上的电脑能给它派任务。</small></span></label><label class="option"><input type="radio" name="visibility" value="public" ${a?.visibility === "public" ? "checked" : ""}><span><strong>${icon("globe")}团队共享</strong><small>空间里的成员都能看到，也都能派任务。</small></span></label></fieldset>`}
      <p class="form-error" role="alert"></p>
      <div class="form-actions"><button type="button" class="btn ghost" id="form-cancel">取消</button><button type="submit" class="btn primary">${a ? "保存" : "下一步"}${a ? "" : icon("arrow")}</button></div>
      ${a ? `<details class="danger-zone"><summary>${a.aid ? "断开连接" : "删除"}</summary><p>${a.aid ? "断开后这台电脑就不能再用这个连接了，资料会留着，之后可以重新连。" : "还没连上的记录会被删掉，它的连接说明也一起作废。"}</p><button type="button" class="btn danger" id="disconnect">${a.aid ? "确定断开" : "确定删除"}</button></details>` : ""}
    </form>`,
    });
    if ($("#executor"))
        $("#executor").onchange = (e) => {
            $("#executor-note").textContent = executorNote(e.target.value);
        };
    $("#form-cancel").onclick = () => {
        modal.close();
        if (returnTarget)
            openTask(returnTarget).catch((e) => toast(e.message, "bad"));
    };
    $("#record-form").onsubmit = async (e) => {
        e.preventDefault();
        const form = e.target;
        $("[type=submit]", form).disabled = true;
        try {
            const d = Object.fromEntries(new FormData(form)),
                endpoint = "/api/" + (device ? "devices" : "agents");
            let id = a?.id;
            if (a) await api(endpoint + "/" + id, "PATCH", d);
            else id = (await api(endpoint, "POST", d)).id;
            await refreshWorkspace();
            const saved = (device ? workspace.devices : workspace.own).find(
                (x) => x.id === id
            );
            modal.close();
            await load(false);
            if (a) toast("已保存", "ok");
            else await pair(saved, returnTarget);
        } catch (error) {
            formError(form, error);
        }
    };
    if (a)
        $("#disconnect").onclick = guard(async () => {
            await api(recordApi(a) + "/" + a.id + "/disconnect", "POST", {});
            if (!a.aid) await api(recordApi(a) + "/" + a.id, "DELETE", {});
            modal.close();
            await load();
            toast(a.aid ? "已断开，资料还在" : "已删除", "ok");
        });
}

function connectComputer(existing = null, returnTarget = null, receiving = false) {
    const upgrade = !!existing;
    openDialog({kicker: upgrade ? "同一台电脑" : "连接电脑", title: upgrade ? "开启接收任务" : "这台电脑只需连接一次", sub: "发任务和接收任务共用一个连接。需要接收任务时，再选择本地软件和权限。", body: `
    <form id="computer-form" class="form">
      <label>电脑名字<input name="name" maxlength="60" required value="${esc(existing?.name || "")}" placeholder="比如：办公室电脑" ${upgrade ? "disabled" : ""}></label>
      <label class="option"><input type="checkbox" id="receive-tasks" ${upgrade || receiving ? "checked" : ""} ${upgrade ? "disabled" : ""}><span><strong>也让这台电脑接收任务</strong><small>可在本机执行任务；接入时需要你确认工作目录和本地权限。</small></span></label>
      <div id="receive-options"><label>执行任务的软件<select name="executor" id="computer-executor"><option value="">选择已安装的软件</option>${harnesses.map(h=>`<option value="${h.id}">${esc(h.name)}</option>`).join("")}</select></label></div>
      <p class="hint" id="computer-note"></p>
      <p class="hint">安装阶段、版本、耗时和错误类别会脱敏回传，用于定位卡点；不收集对话、项目文件、路径或凭证。安装器支持 --no-report 关闭回传。</p>
      <p class="form-error" role="alert"></p><div class="form-actions"><button type="button" class="btn ghost" id="computer-cancel">取消</button><button type="submit" class="btn primary">${upgrade ? "确认开启并继续安装" : "获取安装说明"}</button></div>
    </form>`});
    const refreshChoice=()=>{const enabled=$("#receive-tasks").checked;$("#receive-options").hidden=!enabled;$("#computer-executor").required=enabled;$("#computer-note").textContent=enabled?"这条连接也能发任务，不需要再连接一次电脑。开启后仍需在本机完成授权和自检，才会接收任务。":"先连接并发任务，以后可以在这条连接上开启接收任务。";};
    $("#receive-tasks").onchange=refreshChoice;refreshChoice();
    $("#computer-cancel").onclick=()=>modal.close();
    $("#computer-form").onsubmit=async e=>{e.preventDefault();const form=e.target;$("[type=submit]",form).disabled=true;
        try {
            const receive=$("#receive-tasks").checked;const executor=$("#computer-executor").value;
            let id=existing?.id;
            if(upgrade)await api(`/api/connections/${id}/enable-executor`,"POST",{executor,confirm:true});
            else id=(await api(receive?"/api/agents":"/api/devices","POST",{name:form.elements.name.value,executor:receive?executor:"codex",visibility:"private"})).id;
            await refreshWorkspace();const saved=findRecord(id);modal.close();await load(false);await pair(saved,returnTarget);
        } catch(error){formError(form,error);}
    };
}
const installStageNames={setup:"安装总流程",pair:"身份绑定",resume:"恢复进度",configure:"接收权限", "configure-pi":"Pi 配置","configure-harness":"执行器配置","test-pi":"Pi 自检","test-harness":"执行器自检",install:"登录启动项",diagnose:"连接检查",report:"诊断回传","anet-up":"启动 ANet","anet-status":"检查 ANet","anet-peers":"检查入口权限","anet-hub-register":"连接服务器","anet-profile":"身份验证", "anet-other":"ANet 配置"};
const installErrorNames={network:"网络连接失败",timeout:"等待超时",certificate:"证书验证失败",acl:"凭证文件权限设置失败",pair_expired:"接入凭证过期或被替换",identity_mismatch:"身份或原数据目录不匹配",approval_required:"需要完成本机授权",missing_dependency:"缺少依赖或命令",command_failed:"本机命令失败",permission:"系统权限不足",configuration:"配置不完整",unknown:"需要查看本机非敏感错误",executor_pending:"等待执行器授权或自检",heartbeat_pending:"尚未确认连接心跳",native_launcher:"Windows 原生桥接校验或格式异常",dsh_profile:"DSH profile 参数重复或顺序异常",model_auth:"DeepSeek headless 模型授权未就绪"};
async function installLogs(a) {
    const data=await api(`/api/connections/${a.id}/diagnostics`);
    const latest=data.reports[0];
    openDialog({kicker:"安装诊断",title:esc(a.name),sub:"只记录阶段和检查结果；记录仅所有者可见，保留 30 天。",size:"wide",body:`<label class="hint">补传本机脱敏诊断 JSON<input type="file" id="install-import" accept=".json,application/json"></label>` + (latest ? `<p class="hint">诊断编号：${esc(latest.id)} · ${esc(latest.report.os)} · 连接程序 ${esc(latest.report.connector_version)}</p><div class="install-events">${latest.report.events.map(ev=>`<div class="check-box"><strong>${esc(installStageNames[ev.stage]||ev.stage)}</strong><span>${esc(({ok:"通过",failed:"失败",pending:"待完成",started:"开始",unverified:"未验证"})[ev.result])}${ev.error&&ev.error!=="none"?" · "+esc(installErrorNames[ev.error]||ev.error):""}${ev.elapsed_ms?" · "+(ev.elapsed_ms/1000).toFixed(1)+" 秒":""}${ev.checks?" · "+esc(Object.entries(ev.checks).map(([k,v])=>({binding:"绑定",hub:"服务器",policy:"接收权限",executor:"执行器",heartbeat:"心跳",mcp:"通信工具"})[k]+"："+({ok:"通过",pending:"待完成",unverified:"未验证",failed:"失败"})[v]).join("；")):""}</span></div>`).join("")}</div><div class="form-actions"><button class="btn ghost" id="install-download">下载脱敏诊断</button><button class="btn primary" id="install-resume">继续安装</button></div>` : '<p class="body-text">尚未收到安装诊断。用最新版安装说明运行 setup；已有连接可运行 diagnose，再运行 report 回传。</p>')});
    $("#install-import").onchange=guard(async e=>{const file=e.target.files[0];if(!file)return;if(file.size>32768)throw new Error("诊断文件超过大小限制");await api(`/api/connections/${a.id}/diagnostics`,"POST",{report:JSON.parse(await file.text())});await installLogs(a);});
    if(latest){$("#install-resume").onclick=guard(()=>pair(a));$("#install-download").onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download="atoa-install-"+latest.id+".json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};}
}
async function installSummary(a) {
    try {const data=await api(`/api/connections/${a.id}/diagnostics`);const box=$("#install-summary");if(!box||box.dataset.record!==a.id)return;
        const latest=data.reports[0];if(!latest){box.textContent="尚未收到安装诊断，最新版安装器会自动回传。";return;}
        const blocked=[...latest.report.events].reverse().find(x=>["failed","pending"].includes(x.result));
        box.textContent=blocked?"最近安装卡点："+(installStageNames[blocked.stage]||blocked.stage)+" · "+(installErrorNames[blocked.error]||"等待处理") : "最近安装诊断已收到；连接和任务能力以实际检查为准。";
    }catch(error){const box=$("#install-summary");if(box)box.textContent="暂时无法读取安装诊断。";}
}

/* ------------------------------------------------------------------ connecting */

async function pair(a, returnTarget = null) {
    if (!a) throw new Error("找不到这条记录，刷新一下再试。");
    try {
        const d = await api(recordApi(a) + "/" + a.id + "/pair", "POST", {});
        showSetup(a, d, returnTarget);
    } catch (error) {
        openDialog({
            kicker: "连接",
            title: "现在拿不到连接说明",
            sub: esc(error.message),
            body: `<p class="body-text">保留原连接和电脑上的安装进度，稍后在这条记录上继续；不要反复新建电脑。</p><div class="form-actions"><button class="btn ghost" id="x-state">看看状态</button><button class="btn primary" id="x-ok">知道了</button></div>`,
        });
        $("#x-state").onclick = guard(() => details(a));
        $("#x-ok").onclick = () => modal.close();
    }
}
function showSetup(a, d, returnTarget = null) {
    const device = a.kind === "device",
        software = executorName(a);
    const prompt =
        (d.recovery
            ? `请恢复原连接“${a.name}”，继续完成安装。必须复用原 AID ${a.aid} 和原 ANet 数据目录，先用最新版 connector.py resume 恢复进度；只有原凭证确实丢失或失效时才使用此恢复链接。不要新建身份或电脑记录。${device ? "这是调用设备，只检查 MCP 和心跳，不配置执行器。" : "保留原执行器、工作目录和权限。"}`
            : device
            ? "请帮我连接发起任务的设备。仅绑定调用身份、检查 ANet 通信工具并保持连接状态；保留已有身份，不配置或启用本机任务执行器。"
            : `请按我的授权接入 ${software} 智能体“${a.name}”。保留现有调用设备和其他智能体身份；为新执行器使用独立身份，并让我选择工作目录及本地权限。`) +
        `\n\n请读取接入链接中的专属说明，逐步检查并完成配置：\n${d.url}\n\n如果浏览工具无法读取脚本，请将 #connect= 后的凭证作为 JSON 字段 token，POST 到 ${location.origin}/api/pair/info 获取 instructions。凭证只用于本次连接，不要写入 URL 查询参数或公开日志。报告真实连接和自检状态，不要将配置完成当作远程任务成功。`;
    openDialog({
        kicker: d.recovery ? "恢复原连接" : device ? "新电脑" : "新智能体",
        size: "wide",
        title: d.recovery ? "在原电脑上继续安装" : "到那台电脑上完成连接",
        sub:
            esc(a.name) +
            (device
                ? " · 在你准备发任务的电脑上做"
                : " · 在装了 " + esc(software) + " 的电脑上做"),
        body: `${stepper(["填资料", "到电脑上连接", "检查"], 1)}
    <div class="handoff">
      <ol class="numbered"><li><strong>复制连接说明</strong><span>里面有这次连接专用的链接，1 小时内有效。</span></li><li><strong>粘贴给那台电脑上的助手</strong><span>${device ? "它会运行统一安装器，复用进度，并把脱敏安装诊断回传到这里。" : "它会检查软件、完成连接，目录和权限由你确认。"}</span></li><li><strong>回来点检查</strong><span>以实际连接为准，点按钮本身不算连上。</span></li></ol>
      <div class="copy-card"><button class="btn primary wide" id="copy-prompt">${icon("copy")}复制连接说明</button><p class="hint" id="copy-feedback" role="status">说明有效期一小时，重新生成会替换旧说明；已完成的绑定会保留。不要转发。</p><details class="reveal"><summary>看全文</summary><pre class="code" id="pair-prompt"></pre></details></div>
    </div>
    <div class="check-box" id="check-box" aria-live="polite">${pill(a)}<span>${esc(stateInfo(a).meaning)}</span></div>
    <div class="form-actions"><button class="btn ghost" id="later">先这样</button><button class="btn primary" id="check">${icon("refresh")}检查连接</button></div>`,
    });
    $("#pair-prompt").textContent = prompt;
    $("#copy-prompt").onclick = async () => {
        try {
            await copy(prompt);
            $("#copy-prompt").innerHTML = icon("check") + "已复制";
            $("#copy-feedback").textContent =
                "去那台电脑的助手里粘贴，弄完回来点“检查连接”。";
        } catch (error) {
            $("#copy-feedback").textContent = error.message;
        }
    };
    $("#later").onclick = () => {
        modal.close();
        toast("资料存好了，随时可以在“我的连接”里接着连");
    };
    $("#check").onclick = async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
            await refreshWorkspace();
            await load(false);
            const fresh = (device ? workspace.devices : workspace.own).find(
                (x) => x.id === a.id
            );
            if (!fresh) throw new Error("这条记录已经被删了，回列表看看。");
            if (fresh.status === "ready") {
                showSetupSuccess(fresh, returnTarget);
                return;
            }
            $("#check-box").innerHTML =
                `${pill(fresh)}<span>${fresh.status === "pending" ? "还没检测到连接。先在那台电脑的助手里把说明走完。" : esc(stateInfo(fresh).meaning + " " + stateInfo(fresh).next)}</span>`;
        } catch (error) {
            $("#check-box").textContent = error.message;
        } finally {
            btn.disabled = false;
        }
    };
}
function showSetupSuccess(a, returnTarget) {
    const device = a.kind === "device";
    openDialog({
        kicker: "连接",
        title: "连上了",
        sub: esc(a.name),
        body: `${stepper(["填资料", "到电脑上连接", "检查"], 3)}<div class="success-mark">${bigStatus(a)}</div><p class="body-text center">${device ? "去智能体列表挑一个，派个任务试试。" : "先派个小任务，看看结果对不对。"}</p><div class="form-actions"><button class="btn ghost" id="ok-close">关闭</button><button class="btn primary" id="ok-next">${device ? "去挑选" : "派个任务"}${icon("arrow")}</button></div>`,
    });
    $("#ok-close").onclick = () => modal.close();
    $("#ok-next").onclick = guard(async () => {
        modal.close();
        if (device) {
            if (returnTarget) await openTask(returnTarget);
            else go("directory");
        } else await openTask(a);
    });
}

/* ------------------------------------------------------------------ interactions */

const WORK_LABELS = {idle:"空闲",running:"执行中",queued:"排队中",waiting:"等待处理",blocked:"受阻",unknown:"状态未知"};
const TASK_LABELS = {waiting_slot:"等待名额",waiting_desktop:"等待桌面",waiting_workspace:"等待工作目录",queued:"排队中",running:"执行中",accepted:"已接收",planning:"制定计划",verifying:"检查结果",awaiting_approval:"等待确认",needs_user:"等待你回答",blocked:"受阻"};
const EVENT_LABELS = {accepted:"收到任务",planning:"开始制定计划",running:"开始执行阶段",verifying:"核验最终交付文件和要求",awaiting_approval:"等待确认",needs_user:"等待回答",blocked:"任务受阻",completed:"执行方结束任务",stopped:"任务停止",executor_started:"Agent 开始本轮工作",executor_output:"Agent 正在操作或生成回复",executor_finished:"Agent 本轮工作结束",checks_started:"程序正在核对文件或界面状态",checks:"程序核对结束（是否通过见结果）",checks_advanced:"有新的检查项通过",milestone_done:"已核验一个阶段内里程碑",phase_done:"本阶段要求已核验，推进下一阶段",plan:"计划已保存",step:"步骤报告已保存",review:"独立审查结束",replan:"重新规划",environment_wait:"等待执行环境恢复",interrupted:"上次执行被中断"};
const PURPOSE_LABELS = {native_goal:"Agent 正在原生 Goal 中持续执行",planning:"Agent 正在制定或修订计划",execution:"Agent 正在执行当前操作",report_repair:"Agent 正在整理上一步报告",review:"独立审查 Agent 正在核验证据"};
function evidenceFresh(at) {
    return typeof at === "number" && Number.isFinite(at) && Date.now()/1000-at >= -30 && Date.now()/1000-at <= 90;
}
function evidenceTime(at) {
    if (typeof at !== "number" || !Number.isFinite(at) || at <= 0) return "时间未提供";
    return new Date(at*1000).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC") + (evidenceFresh(at) ? " · 近期上报" : " · 已过期，当前状态未验证");
}
function evidenceFlag(value, yes, no) {
    return value === true || value === 1 ? yes : value === false || value === 0 ? no : "未上报";
}
function evidenceRows(rows) {
    return `<dl class="work-tech">${rows.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>`;
}
function admissionExplanation(a) {
    if (a.kind === "device") return [];
    const f=a.runtime?.admission;
    if (!f || f.schema!==1) return [["门户接收路径配置","未上报；不能从 policy_ok 推断既有信任是否可用"]];
    if (!evidenceFresh(a.last_seen) || !evidenceFresh(f.observed_at))
        return [["门户接收路径配置","配置观察已过期；当前未验证"]];
    const reasons={configuration_matches:"绑定身份、内部能力、门户地址及凭据文件引用匹配",configuration_unreadable:"配置无法核对",
        capability_missing_or_ambiguous:"内部能力缺失或重复",binding_mismatch:"节点与门户绑定身份不一致",
        live_capability_mismatch:"当前节点未报告对应准入能力",endpoint_mismatch:"实际能力地址或内部服务配置不匹配",
        token_reference_mismatch:"凭据文件引用未匹配；没有读取凭据内容"};
    const rows=[["门户接收路径配置",evidenceFlag(f.portal_admission_configured,"节点上报配置匹配；认证与实际接收尚未实测","节点上报配置未闭合")],
        ["配置观察依据",(reasons[f.reason] || "原因未提供")+" · "+evidenceTime(f.observed_at)]];
    if (Number.isInteger(f.existing_trust_count)) rows.push(["保留的既有信任",`${f.existing_trust_count} 个身份；须核对具体发起 AID，不能借给其它身份使用`]);
    if (Number.isInteger(f.existing_allow_count)) rows.push(["保留的既有 allow",`${f.existing_allow_count} 个身份；仅是配置计数`]);
    if (Number.isInteger(f.public_capabilities_count)) rows.push(["现有公开能力",`${f.public_capabilities_count} 项；不代表文本任务准入`]);
    return rows;
}
function executionReadiness(a) {
    const recent = evidenceFresh(a.last_seen), caps = a.reported_capabilities || a.capabilities || {}, native = caps.native;
    const current = text => recent ? text : "未获得近期上报；当前未验证";
    const reasons = {disabled_by_owner:"所有者未开启",native_probe_failed:"原生能力探测失败",adapter_has_no_verified_native_goals:"该适配器尚未验证原生 Goal",unavailable:"节点上报能力不可用"};
    let goal = "未提供能力声明或实际验收，不能从执行器名称推断";
    if (native && recent) {
        goal = native.available === true
            ? `节点声明原生能力可用；适配器：${native.harness || "未提供"}；持久会话：${evidenceFlag(native.persistent_session,"声明支持","声明不支持")}。仍需真实持续任务验收`
            : native.available === false ? `节点声明原生能力不可用；${reasons[native.reason] || "具体原因未提供"}` : "原生能力是否可用未上报";
    }
    const rows = [
        ["节点心跳", a.last_seen ? evidenceTime(a.last_seen) : "尚未收到心跳；原因未提供"],
        ["节点连通", current(evidenceFlag(a.node_connected,"节点上报已连接","节点上报未连接；具体原因未提供"))],
        ["发起连接", current(evidenceFlag(a.send_ready,"发起入口可用；目标仍须授权且接收就绪","发起入口尚未就绪；核对绑定、启用和连接状态"))],
        ["门户通用接收", a.kind === "device" ? "仅发起设备，未启用接收任务" : current(evidenceFlag(a.receive_ready,"接收就绪由节点上报；不等于任务已接受","尚未核验门户通用接收；既有授权路径须按发起身份另验"))],
        ["门户独占策略", a.kind === "device" ? "此连接仅发起任务" : current(evidenceFlag(a.policy_ok,"节点上报符合门户独占准入；指定任务接收仍需另验","未确认符合门户独占准入；不等于所有既有授权失效"))],
        ...admissionExplanation(a),
        ["执行器", a.kind === "device" ? "此连接未要求接收执行" : `${a.executor || "未提供"} · ${current(evidenceFlag(a.executor_ready,"节点上报就绪；不证明已执行任务","未确认就绪；具体原因未提供"))}`],
        ["taskd 运行声明", a.kind === "device" ? "不由此字段判断回传能力" : current(evidenceFlag(a.runtime?.taskd,"节点上报 taskd 运行；接收证据见具体任务","节点上报 taskd 未运行；具体原因未提供"))],
        ["原生持续 Goal", a.kind === "device" ? "由接收执行的节点提供" : goal],
        ["自动回传", current(evidenceFlag(a.requester_ready,"节点上报就绪；原会话绑定和完成回传仍逐任务核验","未确认就绪；具体原因未提供"))],
        ["验收与交付", "需原任务的检查、文件接收和会话回传证据；心跳和就绪字段不能代替"],
    ];
    return `<section class="cap-box execution-readiness"><h3>对端执行准备</h3>${evidenceRows(rows)}<p class="hint">来源：连接程序最近心跳和节点能力声明。各项单独核对；没有原因时明确记为未提供。</p></section>`;
}
function taskExecutionEvidence(t) {
    const events = Array.isArray(t.events) ? t.events : [];
    const latest = name => events.filter(e => e.event === name && typeof e.at === "number").sort((a,b) => b.at-a.at)[0];
    const accepted = latest("accepted"), started = latest("executor_started"), f = t.execution_facts;
    const content = t.content?.schema === 1 ? t.content : null, fields = content?.fields || {};
    const rawReason = content ? fields.diagnostic || fields.reason || fields.step_blocker : t.blocker?.detail || t.reason || t.last_step?.blocker;
    const reasonText = plainText(rawReason, 300);
    const reason = reasonText ? `${content?.older_than_status || content?.stale ? "历史正文，不代表当前原因 · " : ""}${reasonText}` : "具体原因未提供";
    const rows = [
        ["taskd 接收", accepted ? `已上报 accepted 事件 · ${evidenceTime(accepted.at)}` : t.state === "accepted" ? `状态上报 accepted · ${evidenceTime(t.last_activity_at)}` : "本次同步未提供接收事件；不从消息送达推断"],
        ["执行器启动", started ? `已上报 executor_started 事件 · ${evidenceTime(started.at)}` : "本次同步未提供启动事件；不从运行标签推断"],
        ["执行更新时间", f?.updated_at ? evidenceTime(f.updated_at) : "执行器事件时间未提供"],
        ["等待或阻塞原因", reason],
        ["结果验收 / 已交付", "未在此状态快照中验证；查看原任务检查报告和交付回执"],
    ];
    return `<details class="work-content" data-detail-key="execution-evidence"><summary>接收、执行与交付证据</summary>${evidenceRows(rows)}<p class="hint">事件来自执行端上报。历史事件不代表当前仍在执行；没有事件不证明从未执行。</p></details>`;
}
function workEvent(e) {
    const label = PURPOSE_LABELS[e.purpose];
    return label && e.event === "executor_started" ? label : label && e.event === "executor_finished" ? `本轮调用结束：${label.replace("正在", "")}` : EVENT_LABELS[e.event] || e.event;
}
function workState(a) {
    const w=a.work;
    if (!w || !w.observed_at || Date.now()/1000-w.observed_at>90 || ["offline","disabled","pending"].includes(a.status)) return "unknown";
    return WORK_LABELS[w.state] ? w.state : "unknown";
}
function workAge(at) {
    if (!at) return "暂无记录";
    const s=Math.max(0,Math.floor(Date.now()/1000-at));
    return s<60 ? `${s} 秒前` : s<3600 ? `${Math.floor(s/60)} 分钟前` : s<86400 ? `${Math.floor(s/3600)} 小时前` : `${Math.floor(s/86400)} 天前`;
}
function workSummary(a) {
    if (a.kind === "device") return "";
    const state=workState(a), w=a.work || {}, c=w.counts || {};
    return `<div class="work-summary" data-work-state="${state}"><span class="work-badge work-${state}"><i></i>${WORK_LABELS[state]}</span>${state==="unknown" ? '<span class="work-muted">暂无有效工作状态</span>' : ["running","queued","waiting","blocked"].filter(k=>c[k]).map(k=>`<span>${WORK_LABELS[k]} <b>${c[k]}</b></span>`).join("") || '<span class="work-muted">当前没有 ANet 任务</span>'}</div>`;
}
// Step reports can arrive as JSON or markdown-ish text; show them as plain sentences.
function plainText(v, max = 140) {
    let t = typeof v === "string" ? v.trim() : "";
    if (/^[\[{]/.test(t)) {
        try {
            const o = JSON.parse(t), pick = (x) => {
                if (typeof x === "string") return x;
                if (Array.isArray(x)) return x.map(pick).filter(Boolean).join("；");
                if (x && typeof x === "object") {
                    for (const k of ["summary", "message", "text", "result", "title", "description", "status"])
                        if (typeof x[k] === "string" && x[k]) return x[k];
                    return Object.values(x).filter((y) => typeof y === "string" || typeof y === "number").slice(0, 3).join("，");
                }
                return x == null ? "" : String(x);
            };
            t = pick(o);
        } catch {}
    }
    t = t.replace(/[`*#>]+/g, "").replace(/\s+/g, " ").trim();
    return t.length > max ? t.slice(0, max) + "…" : t;
}
function taskHeadline(t) {
    const f=t.execution_facts;
    if (f?.stop?.resource_retained || /native_stop_unconfirmed|native_outcome_unknown/.test(String(t.reason || ""))) return "执行结果尚未确认；先核实原任务，不能盲目重派";
    if (["awaiting_approval","needs_user","blocked"].includes(t.state)) return ({awaiting_approval:"等待用户确认",needs_user:"等待用户补充信息",blocked:"执行端上报任务受阻"})[t.state];
    if (f?.route === "native" && evidenceFresh(f.updated_at)) {
        if (f.goal_status === "paused") return "原生 Goal 已暂停；原因以原任务记录为准";
        if (f.goal_status === "blocked") return "原生 Goal 上报受阻；查看具体原因";
        if (["budget_limited","budgetLimited"].includes(f.goal_status)) return "原生 Goal 上报预算受限；查看原任务预算与执行记录";
        if (f.last_event === "native/needsApproval") return "原生执行器正在等待用户确认";
        if (f.retry?.reason === "model_at_capacity" && evidenceFresh(f.retry.at) && ["active",undefined].includes(f.goal_status)) return `模型容量不足，执行器上报重试中${Number.isInteger(f.retry.attempt) ? " · 第 "+f.retry.attempt+" 次" : ""}`;
        if (f.goal_status === "complete") return "原生 Goal 上报结束；业务验收与交付仍需核验";
    }
    if (t.state === "running" && f?.updated_at && !evidenceFresh(f.updated_at)) return "任务记录为运行中；执行器事件已过期，当前状态待确认";
    return ({running:"执行端上报运行中",planning:"执行端上报正在制定计划",verifying:"正在核验结果",accepted:"taskd 上报已接收，等待执行确认",queued:"正在排队",waiting_slot:"等待执行名额",waiting_desktop:"等待桌面空闲",waiting_workspace:"等待工作目录可用"})[t.state] || "当前执行状态待确认";
}
function readableTaskTitle(t) {
    // A deterministic excerpt, never a generated claim about what was accomplished.
    const raw = t.title || t.goal || (t.mode === "quick" ? "快速回复" : "任务");
    return plainText(raw.replace(/\bix_[a-zA-Z0-9_-]+\b/g, "").replace(/\s+/g, " "), 48);
}
function taskNextAction(t) {
    const reason = String(t.reason || "");
    if (/native_stop_unconfirmed|native_outcome_unknown/.test(reason) || t.execution_facts?.stop?.resource_retained)
        return "先核实原执行是否停止；不要重复派发或直接恢复。";
    if (["awaiting_approval", "needs_user"].includes(t.state)) return "查看下方说明，由原调用方处理确认或补充信息。";
    if (t.state === "blocked") return ({wait_for_connection:"等待远端恢复连接，再由原调用方核对原任务。",repair_configuration:"由原调用方修正配置并重新检查。",repair_program:"由原调用方修正程序后重新检查。",repair_failed_checks:"由原调用方核对未通过的验收项。",revise_plan:"由原调用方修订执行计划。"})[t.blocker?.action] || "由原调用方查看阻塞证据并处理；未确认前不要重复派发。";
    if (t.state?.startsWith("waiting_") || t.state === "queued") return "等待资源；无需重复提交任务。";
    return "等待执行结果；有新报告后核对交付与验收。";
}
function executionFacts(f) {
    if (!f || f.schema !== 1) return '<p class="hint">执行路由未上报（旧节点或尚未开始），不推断为原生 Goal。</p>';
    const route = {native:"原生 Goal",planned:"分阶段计划",direct:"单次执行并验收",quick:"短任务"}[f.route] || "未上报";
    const reasons = {explicit_planned:"请求方明确选择计划模式",native_capability_verified:"原生能力探测通过",disabled_by_owner:"节点未开启原生 Goal",adapter_has_no_verified_native_goals:"适配器未验证原生 Goal",native_probe_failed:"原生能力探测失败",unavailable:"原生能力不可用"};
    const stale = !f.updated_at || Date.now()/1000-f.updated_at>90;
    const retryStale = !f.retry?.at || Date.now()/1000-f.retry.at>90;
    const parts = [`执行路径：${route}${reasons[f.routing_reason] ? " · "+reasons[f.routing_reason] : ""}`];
    if (f.route === "native") {
        parts.push(`Goal：${({active:"进行中",paused:"已暂停",blocked:"受阻",complete:"执行器声称完成（不等于验收通过）",budget_limited:"预算受限",budgetLimited:"预算受限"})[f.goal_status] || "未上报"}${f.iteration ? " · 已观察 "+f.iteration+" 轮" : ""}`);
        if (f.turn_status) parts.push(`本轮：${({inProgress:"执行中",completed:"调用结束",interrupted:"已中断",failed:"失败"})[f.turn_status] || "未知"}`);
        if (f.retry) parts.push(`${retryStale ? "历史重试记录（是否仍在重试待确认）" : "最近重试记录"}：${f.retry.reason === "model_at_capacity" ? "模型容量不足" : "上游请求重试"}${Number.isInteger(f.retry.attempt) ? " · 第 "+f.retry.attempt+" 次重试" : ""}`);
        if (f.stop?.requested) parts.push(f.stop.confirmed === true ? "原生调用及托管终端停止已确认；独立服务需另验" : "已请求停止，尚未确认；不能视为资源已释放");
        if (f.stop?.resource_retained) parts.push("执行结果不确定，资源仍保留隔离");
        parts.push(`原生事件：${f.last_event || "未上报"} · ${workAge(f.updated_at)}${stale ? "（无近期事件，当前状态待确认）" : ""}`);
    }
    return `<p class="work-line">${parts.map(esc).join("<br>")}</p>`;
}
// These are the redacted, permission-filtered fields already synchronized by the node.
// Do not call this a full log: legacy nodes cap goal/phase/report lengths upstream.
function workContent(t) {
    const c = t.content?.schema === 1 ? t.content : null, f = c?.fields || {};
    const pick = (key, legacy) => c ? f[key] : legacy;
    const fields = [["任务目标", pick("goal", t.goal)], ["当前阶段", pick("phase", t.phase?.title)],
        ["当前里程碑", pick("milestone", t.phase?.milestone?.current)], ["最近步骤报告", pick("summary", t.last_step?.summary)],
        ["下一步", pick("next", t.last_step?.next)], ["阻塞原因", pick("reason", t.reason)],
        ["步骤阻塞说明", pick("step_blocker", t.last_step?.blocker)], ["阻塞诊断", pick("diagnostic", t.blocker?.detail)]];
    const note = c ? `正文 v1 · ${workAge(c.observed_at)}观察${c.older_than_status ? "（早于最新状态快照，以下为历史正文；当前状态以卡片概览为准）" : c.stale ? "（较旧，不代表当前执行内容）" : ""}。${c.truncated?.length ? "部分字段超过传输上限，已截断。" : "本次上报字段未截断。"}单字段最多 16384 字、合计 32768 字；不是完整执行日志。`
        : "以下保留节点已同步的原文。当前同步链路最多保留目标 500 字、阶段 160 字、步骤报告 1000 字；这不是完整执行日志。";
    const present = fields.filter(([,v]) => typeof v === "string" && v.trim());
    if (!present.length) return '<p class="hint">尚无已同步的任务正文或步骤报告；活动记录不包含执行输出。</p>';
    return `<details class="work-content" data-detail-key="content"><summary>任务原文与报告 · ${present.length} 项</summary><p class="hint">${c?.older_than_status || c?.stale ? "以下是较旧的同步内容，不代表当前执行状态。" : "节点同步的原文，非完整执行日志。"}</p>${present.map(([label,value]) => `<details class="work-source-field" data-detail-key="field-${esc(label)}"><summary>${c?.older_than_status ? "历史 · " : ""}${esc(label)}</summary><div class="work-text">${esc(value)}</div></details>`).join("")}<details class="work-sync-info" data-detail-key="sync"><summary>同步范围与时间</summary><p class="hint">${esc(note)}</p></details></details>`;
}
function workTask(t) {
    const state = t.state || "unknown";
    const tone = state === "blocked" ? "blocked" : ["needs_user","awaiting_approval"].includes(state) ? "waiting" : (state === "queued" || state.startsWith("waiting_")) ? "queued" : TASK_LABELS[state] ? "running" : "unknown";
    const summary = plainText(t.last_step?.summary, 160), next = plainText(t.last_step?.next, 160);
    const oldContent = t.content?.older_than_status || t.content?.stale;
    const tech = [
        `<dt>任务编号</dt><dd class="mono">${esc(t.id)}</dd>`,
        `<dt>执行方式</dt><dd>${executionFacts(t.execution_facts)}</dd>`,
        t.plan_revision ? `<dt>计划版本</dt><dd>${Number(t.plan_revision)}</dd>` : "",
        t.phase?.total ? `<dt>当前阶段</dt><dd>${Number(t.phase.index)} / ${Number(t.phase.total)} · ${esc(plainText(t.phase.title, 180))}</dd>` : "",
        `<dt>已核验进展</dt><dd>${t.last_progress_at ? `${esc(EVENT_LABELS[t.progress_event] || "阶段推进")} · ${esc(workAge(t.last_progress_at))}` : "尚未记录到检查通过或阶段推进"}</dd>`,
        t.reason ? `<dt>原始阻塞原因</dt><dd>${esc(t.reason)}</dd>` : "",
        t.scheduling?.blocked_by?.length ? `<dt>占用任务</dt><dd class="mono">${t.scheduling.blocked_by.map(esc).join("、")}</dd>` : "",
        t.events?.length ? `<dt>活动记录</dt><dd><p class="hint">状态事件，不含完整执行输出。</p><ol>${[...t.events].reverse().map(e=>`<li>${esc(workEvent(e))} <time>${esc(workAge(e.at))}</time></li>`).join("")}</ol></dd>` : "",
        t.timing ? `<dt>累计耗时</dt><dd>${Object.entries(t.timing).map(([k,v])=>`${esc(k)} ${Number(v.seconds).toFixed(1)} 秒 / ${Number(v.calls)} 次`).join("；")}</dd>` : "",
    ].join("");
    return `<article class="work-task" data-task-id="${esc(t.id)}"><div class="work-heading"><strong>${esc(readableTaskTitle(t))}</strong><span class="work-badge work-${tone}"><i></i>${TASK_LABELS[state] || "状态未知"}</span></div>
    <p class="work-now">${esc(taskHeadline(t))}</p>
    <p class="work-activity">最近活动：${esc(workAge(t.last_activity_at))}<span>活动不等于成果验收通过</span></p>
    ${summary ? `<p class="work-line"><b>${oldContent ? "历史报告" : "执行方报告"}</b>${esc(summary)}</p>` : '<p class="work-line work-muted">暂无步骤结果报告</p>'}
    <p class="work-next"><b>下一步</b>${esc(taskNextAction(t))}</p>
    ${next ? `<p class="work-line"><b>${oldContent ? "历史计划" : "执行方计划"}</b>${esc(next)}</p>` : ""}
    ${t.phase?.milestone?.total ? `<p class="work-line"><b>已核验</b>${Number(t.phase.milestone.done)} / ${Number(t.phase.milestone.total)} 项</p>` : ""}
    ${t.revision_pending ? '<p class="hint">计划调整待当前步骤结束后生效</p>' : ""}
    ${t.attention ? '<p class="work-alert">近期缺少进展，请核对执行记录。</p>' : ""}
    ${workContent(t)}
    ${taskExecutionEvidence(t)}
    <details class="work-timeline" data-detail-key="technical"><summary>执行记录与技术详情</summary><dl class="work-tech">${tech}</dl></details></article>`;
}
function workPanel(a) {
    const state=workState(a), w=a.work || {}, tasks=w.tasks || [];
    if (state==="unknown") {
        const why=w.reason==="offline" || a.status==="offline" ? "节点离线，无法确认当前工作。" : w.reason==="stale" || w.observed_at ? "工作状态已过期，等待新的执行端上报。" : "此节点尚未上报工作状态；具体原因未提供，当前执行未验证。";
        return `<section class="cap-box work-panel"><h3>当前工作</h3>${workSummary(a)}<p class="hint">${why}</p></section>`;
    }
    return `<section class="cap-box work-panel"><div class="work-heading"><h3>当前工作</h3><span class="hint">${esc(workAge(w.observed_at))}更新</span></div>${workSummary(a)}${!w.tasks_visible && w.total ? '<p class="hint">所有者未公开任务详情，只展示工作状态和数量。</p>' : ""}${tasks.map(workTask).join("")}${w.truncated ? '<p class="hint">任务较多，这里展示前 50 条；数量统计包含全部任务。</p>' : ""}${w.tasks_visible && !w.content_synced && w.total ? '<p class="hint">任务内容同步未开启，仅显示任务阶段和活动时间。</p>' : ""}<p class="hint">仅包含通过 ANet 接收的任务，由执行端上报；节点心跳不等于执行进展，Goal 完成不等于成果验收通过。</p></section>`;
}
function pollWork(a) {
    let pending=false;
    workPoll=setInterval(async()=>{
        const root=$("#work-live");
        if (!root || !modal.open) {clearInterval(workPoll);return;}
        const cachedReadiness=$("#execution-readiness-live");
        if (cachedReadiness) cachedReadiness.innerHTML=executionReadiness(findRecord(a.id) || a);
        if (pending || document.hidden) return;
        pending=true;
        try {
            await refreshWorkspace();
            const current=findRecord(a.id);
            if (current && root.isConnected) {
                const readiness=$("#execution-readiness-live");
                if (readiness) readiness.innerHTML=executionReadiness(current);
                const openDetails=new Set($$(".work-task",root).flatMap(t=> $$("details",t).filter(d=>d.open).map(d=>`${t.dataset.taskId}:${d.dataset.detailKey || d.className}`)));
                root.innerHTML=workPanel(current);
                $$(".work-task",root).forEach(t=> $$("details",t).forEach(d=>{d.open=openDetails.has(`${t.dataset.taskId}:${d.dataset.detailKey || d.className}`);}));
            }
        } catch (e) {if(root.isConnected) root.innerHTML='<p class="hint">工作状态暂时无法刷新，请关闭后重试。</p>';}
        finally {pending=false;}
    },5000);
}

const IX_STATES = {
    working: "进行中",
    submitted: "已提交",
    completed: "执行方结束",
    failed: "失败",
    canceled: "已取消",
    rejected: "被拒绝",
    "input-required": "等回复",
    "auth-required": "等授权",
};
function isoAgo(t) {
    const s = Math.max(0, Date.now() / 1000 - Date.parse(t) / 1000);
    if (s < 90) return "刚刚";
    if (s < 3600) return Math.round(s / 60) + " 分钟前";
    if (s < 86400) return Math.round(s / 3600) + " 小时前";
    return Math.round(s / 86400) + " 天前";
}
function peerLabel(i) {
    return i.peer_agent ? esc(i.peer_agent.name) : shortAid(i.peer);
}

function interactionEvidence(t) {
    const e = t.evidence || {};
    const values = {
        "anet.delivery": ["pending", "confirmed", "unconfirmed"],
        "anet.delivery_detail": ["not_sent", "sent", "queued_at_hub"],
        "anet.effect_status": ["OK", "UNVERIFIED", "FAILED", "UNAVAILABLE", "PAYMENT_REQUIRED"],
        "anet.receipt_verified": ["verified", "unverified", "unknown"]
    };
    const value = key => values[key].includes(e[key]) ? e[key] : "未上报";
    const row = (label, text) => `<dt>${label}</dt><dd>${esc(text)}</dd>`;
    return `<section class="cap-box"><h3>交付证据</h3><dl class="work-tech">${
        row("消息送达", value("anet.delivery")) +
        row("消息传输详情", value("anet.delivery_detail")) +
        row("效果状态", value("anet.effect_status")) +
        row("回执校验", value("anet.receipt_verified")) +
        row("执行器接收 / 实际执行", "当前对话同步未提供执行确认；查看原任务执行记录") +
        row("业务验收", "未验证；需要 report.json 和逐项验收证据") +
        row("文件实际交付 / 原会话回传", "未验证；附件名称和大小不是接收或回传证明")
    }</dl><p class="hint">以上枚举值来自节点同步的 ANet metadata。门户没有独立校验回执签名；confirmed、OK 或 verified 均不能替代业务验收。执行方结束后，交付失败只补交付，先查询原任务，不重跑业务。</p></section>`;
}

async function interactions(a) {
    let selected = null,
        timer = null,
        list = null;
    const owner = a.is_owner;
    const stop = () => {
        clearInterval(timer);
        timer = null;
    };
    openDialog({
        kicker: "智能体 · 交互",
        title: esc(a.name),
        size: "wide",
        sub: "它和别的智能体之间的对话。",
        body: '<div id="ix-root"><p class="hint">读取中…</p></div>',
    });
    modal.addEventListener("close", stop, { once: true });
    const draw = async () => {
        list = await api("/api/agents/" + a.id + "/interactions");
        const root = $("#ix-root");
        if (!root) return stop();
        const keep = [$(".ix-list"), $("#ix-thread")].map((x) =>
            x ? x.scrollTop : 0
        );
        const syncText = !list.sync_on
            ? owner
                ? "还没开启同步。在这台电脑的终端运行 connector.py enable-sync 开启。"
                : "所有者还没开启同步。"
            : list.sync_at
              ? "同步于 " + isoAgo(new Date(list.sync_at * 1000).toISOString())
              : "已开启，等待第一次同步。";
        root.innerHTML = `<div class="ix-bar"><span>${esc(syncText)}</span>${owner ? `<label><input type="checkbox" id="ix-share" ${list.share_content ? "checked" : ""}> 让其他成员看到内容</label>` : !list.content_visible ? "<span>所有者没有公开内容，只显示对象和状态。</span>" : ""}</div>
        <div class="ix-wrap"><div class="ix-list">${list.interactions.map((i) => `<button class="ix-item ${i.id === selected ? "on" : ""}" data-id="${i.id}"><strong>${i.role === "inbound" ? "← " : "→ "}${peerLabel(i)}</strong><span><em class="ix-state ${esc(i.state)}">${esc(IX_STATES[i.state] || i.state)}</em> · ${esc(isoAgo(i.updated_at))} · ${i.messages} 条</span>${i.goal ? `<span>${esc(clip(i.goal, 40))}</span>` : ""}</button>`).join("") || '<p class="ix-empty">还没有交互记录。</p>'}</div>
        <div class="ix-thread" id="ix-thread"><p class="ix-empty">${list.content_visible ? "选一条看对话。" : "所有者没有公开对话内容。"}</p></div></div>`;
        if ($("#ix-share"))
            $("#ix-share").onchange = guard(async (el) => {
                await api("/api/agents/" + a.id, "PATCH", {
                    share_content: el.target.checked,
                });
                toast(
                    el.target.checked
                        ? "其他成员现在能看到交互内容"
                        : "已改回只有你能看到内容"
                );
            });
        document.querySelectorAll(".ix-item").forEach(
            (b) =>
                (b.onclick = guard(() => {
                    selected = b.dataset.id;
                    return draw();
                }))
        );
        if ($(".ix-list")) $(".ix-list").scrollTop = keep[0];
        if (selected && list.content_visible) await drawThread(keep[1]);
    };
    const drawThread = async (top = 0) => {
        const box = $("#ix-thread");
        if (!box) return;
        const d = await api(
            "/api/agents/" + a.id + "/interactions/" + selected
        );
        const atBottom =
            box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        const who = (m) =>
            m.sender === "me" ? esc(a.name) : peerLabel(d.interaction);
        box.innerHTML = interactionEvidence(d.interaction) +
            `<div class="ix-msg"><header>任务目标 · ${d.interaction.role === "inbound" ? "对方发给它" : "它发给对方"}</header>${esc(d.interaction.goal)}</div>` +
            d.messages
                .map(
                    (m) =>
                        `<div class="ix-msg ${m.sender}"><header>${who(m)} · ${esc(isoAgo(m.created_at))}</header>${esc(m.body)}${m.attachments.map((f) => `<span class="ix-file">${esc(f.name)}${f.size != null ? " · " + f.size + " B" : ""} · 仅附件清单，需从原任务接收并核验哈希</span>`).join("")}</div>`
                )
                .join("");
        box.scrollTop = atBottom && !top ? box.scrollHeight : top;
    };
    await draw();
    timer = setInterval(() => draw().catch(stop), 5000);
}

function runtimeBox(a, owner) {
    if (a.kind === "device") return "";
    const r = a.runtime;
    if (!r) {
        const old =
            a.status === "ready" ||
            a.status === "executor_pending" ||
            a.status === "policy_pending";
        return `<section class="cap-box runtime-box"><h3>${icon("wrench")}运行环境</h3><p class="hint">${old ? "这台电脑上的连接程序还是旧版（低于 0.6.0），没有上报运行环境。更新 connector 就会显示。注意：升级 ANet 本身不会更新它，connector 是单独的。" : "连上以后这里会显示工作目录、权限和底层 harness。"}</p>${old && owner ? '<div class="form-actions"><button class="btn small" id="d-update">复制更新说明</button></div>' : ""}</section>`;
    }
    const modes = {
            direct: "直接运行（本机用户权限）",
            sandbox: "沙箱",
            taskd: "taskd 长任务守护进程",
        },
        rows = [
            [
                "底层 harness",
                [
                    executorName(a),
                    r.agent && r.agent !== a.executor
                        ? "经 " + r.agent + " 协议"
                        : "",
                    r.model,
                ]
                    .filter(Boolean)
                    .join(" · "),
            ],
            [
                "工作目录",
                r.work_dir
                    ? owner
                        ? r.work_dir
                        : "…/" + r.work_dir
                    : "未设置",
            ],
            ["执行方式", modes[r.mode] || r.mode || "未开启"],
            ["可读目录", owner ? (r.read_paths || []).join("、") : ""],
            ["操作界面", r.gui ? "节点声明界面能力；实际控制需单独验证" : ""],
            [
                "不受信任的节点",
                r.untrusted === "off" ? "不执行" : r.untrusted || "—",
            ],
            ["沙箱", r.sandbox || "—"],
            [
                "可联网主机",
                owner ? (r.egress_allow || []).join("、") || "无" : "",
            ],
            ["运行系统", [r.os, r.machine].filter(Boolean).join(" ")],
            [
                "自动更新",
                r.connector_version
                    ? (r.auto_update === false
                          ? "已关闭"
                          : "开，每 6 小时检查签名更新") +
                      (r.update_state
                          ? " · " +
                            ({ current: "已是最新", confirmed: "刚更新完" }[
                                r.update_state.split(":")[0]
                            ] || r.update_state)
                          : "")
                    : "",
            ],
            [
                "长任务 taskd",
                r.taskd
                    ? "已运行" +
                      (r.taskd_version ? " · " + r.taskd_version : "") +
                      (r.taskd_update
                          ? " · " +
                            ({ current: "已是最新", confirmed: "刚更新完" }[
                                r.taskd_update.split(":")[0]
                            ] || r.taskd_update)
                          : "")
                    : "",
            ],
            [
                "软件版本",
                [
                    r.anet_version && "ANet " + r.anet_version,
                    r.connector_version && "连接程序 " + r.connector_version,
                ]
                    .filter(Boolean)
                    .join(" · "),
            ],
        ].filter((x) => x[1]);
    return `<section class="cap-box runtime-box"><h3>${icon("wrench")}运行环境 <span class="hint">节点自己报的</span></h3><dl class="facts">${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>${owner && a.status === "ready" && !a.long_task ? '<p class="hint">没显示“支持长任务”：长任务由这台电脑上单独运行的 taskd 守护进程声明（要求连接程序 ≥ 0.5.0 并检测到 taskd 最近 2 分钟的状态）。只升级 ANet 不会出现。</p>' : ""}${r.mode === "direct" ? '<p class="hint">直接运行用的是那台电脑的用户权限，工作目录只是起点，不是隔离。</p>' : ""}${owner ? "" : '<p class="hint">完整路径只有所有者能看到。</p>'}</section>`;
}

async function workbuddyPanel(a) {
    const root = $("#workbuddy-auth");
    if (!root) return;
    const base = `/api/connections/${a.id}/workbuddy`;
    try {
        const status = await api(base);
        if (!root.isConnected) return;
        const text = !status.configured
            ? "服务器尚未配置 WorkBuddy 官方应用，请联系管理员完成配置后再授权。"
            : status.authorized && !status.local_assistant_scope
              ? "当前授权不包含本地助理权限。请重新授权 WorkBuddy 本地助理，再运行本机连接检查。"
            : status.authorized
              ? "已授权。连接程序会按官方返回的有效期自动续期；如果授权被撤销，需要重新授权。"
              : status.state === "reauthorize"
                ? "授权续期未完成。请重新授权；已发送任务请先在 WorkBuddy 核实，再决定是否重试。"
                : "请由 WorkBuddy 账号本人完成官方网页授权，然后继续本机配置和自检。";
        root.innerHTML = `<h3>WorkBuddy 本地助理授权</h3><p class="hint">${esc(text)}</p>${status.configured ? `<button class="btn ghost" id="wb-authorize">${status.authorized ? "重新授权" : "授权 WorkBuddy 本地助理"}</button>` : ""}${status.authorized || status.state === "reauthorize" ? '<button class="btn ghost" id="wb-revoke">解除平台授权</button>' : ""}<button class="btn ghost" id="wb-refresh">刷新授权状态</button>`;
        if ($("#wb-authorize")) $("#wb-authorize").onclick = guard(async () => {
            const result = await api(base + "/authorize", "POST", {});
            const url = new URL(result.url);
            if (url.origin !== "https://www.workbuddy.cn" || url.pathname !== "/openapi/v2/authorize") throw new Error("授权地址无效");
            location.assign(url.href);
        });
        if ($("#wb-revoke")) $("#wb-revoke").onclick = guard(async () => {
            if (!confirm("解除后，平台将删除保存的 WorkBuddy 授权。已送到 WorkBuddy 的任务仍可能继续运行。确定解除？")) return;
            await api(base + "/revoke", "POST", {});
            await workbuddyPanel(a);
        });
        $("#wb-refresh").onclick = guard(() => workbuddyPanel(a));
    } catch (error) {
        if (root.isConnected) root.innerHTML = `<h3>WorkBuddy 授权</h3><p class="hint">${esc(error.message)}</p>`;
    }
}

async function details(a) {
    await refreshWorkspace();
    await load(false);
    a = findRecord(a.id);
    if (!a) throw new Error("这个连接不在了，或者你没有权限看。");
    const device = a.kind === "device",
        s = stateInfo(a),
        c = a.capabilities || {};

    openDialog({
        kicker: device ? "电脑" : "智能体 · " + esc(executorName(a)),
        title: esc(a.name),
        size: "wide",
        sub: esc(
            device ? a.description || "发任务用的电脑" : a.description || ""
        ),
        body: `<div class="detail-top">${bigStatus(a)}<div><p class="detail-state"><strong>${esc(s.meaning)}</strong></p><p class="hint">${a.status === "ready" ? (device ? "可以从这里发任务。" : "干得怎么样，以助手里看到的结果为准。") : esc(a.is_owner ? "下一步：" + s.next : "找所有者恢复连接，或者先换一个。")}</p></div></div>
    ${pipeline(a)}
    <div id="execution-readiness-live" aria-live="polite">${executionReadiness(a)}</div>
    <dl class="facts"><div><dt>归属</dt><dd>${esc(a.owner_name)}</dd></div><div><dt>谁能用</dt><dd>${device ? "只有我" : a.visibility === "private" ? "只有我" : "团队共享"}</dd></div><div><dt>最近</dt><dd>${esc(ago(a.last_seen))}</dd></div><div><dt>创建</dt><dd>${dateOf(a.created_at)}</dd></div></dl>
    ${a.long_task ? `<section class="cap-box"><h3>${icon("bolt")}长任务 <span class="mono">taskd/1</span></h3><p>节点声明可接收长任务；实际路由、持续运行和成果验收以具体任务证据为准。${c.gui ? "节点声明界面能力；实际控制需单独验证。" : ""}${c.max_hours ? "节点上报任务参数上限 " + esc(c.max_hours) + " 小时" + (c.max_steps ? "、" + esc(c.max_steps) + " 步" : "") + "。" : ""}${c.review ? "程序判断不了的，交给另开的审查会话。" : ""}</p><p class="hint">支持的检查：${esc((c.checkers || []).join("、") || "—")}</p></section>` : ""}
    ${!device ? `<div id="work-live" aria-live="polite">${workPanel(a)}</div>` : ""}
    ${runtimeBox(a, a.is_owner)}
    ${a.is_owner && (a.executor === "workbuddy" || a.kind === "device") && a.aid ? '<section class="cap-box" id="workbuddy-auth"><h3>WorkBuddy 授权</h3><p class="hint">正在读取授权状态…</p></section>' : ""}
    ${a.is_owner ? `<section class="cap-box"><h3>安装进度与诊断</h3><p class="hint" id="install-summary" data-record="${a.id}">正在读取安装诊断…</p><button class="btn ghost" id="d-install-logs">查看安装日志</button>${device ? '<button class="btn ghost" id="d-enable-executor">也让这台电脑接收任务</button>' : '<p class="hint">这条连接也可发任务，不需要单独再连接电脑。</p>'}</section>` : ""}
    ${a.aid ? `<details class="reveal"><summary>技术信息</summary><p class="hint">节点编号（AID），用来认这台电脑，平时用不到。</p><pre class="code">${esc(a.aid)}</pre><p class="hint"><a href="/windows-repair.md" target="_blank" rel="noopener">Windows 连接修复说明</a></p></details>` : ""}
    <div class="form-actions">${a.is_owner ? '<button class="btn ghost" id="d-edit">编辑</button>' : ""}${!device && a.aid && (a.is_owner || a.visibility === "public") ? '<button class="btn ghost" id="d-ix">交互</button>' : ""}${a.is_owner && a.aid ? `<button class="btn ghost" id="d-repair">${icon("refresh")}继续安装 / 恢复连接</button>` : ""}${a.is_owner && !device && sendReady(a) && !receiveReady(a) ? '<button class="btn primary" id="d-send">继续发任务</button>' : ""}${a.is_owner && !a.aid ? '<button class="btn primary" id="d-setup">继续连接</button>' : (device ? sendReady(a) : receiveReady(a)) ? `<button class="btn primary" id="d-use">${device ? "去挑选" : "派给此执行端"}${icon("arrow")}</button>` : !a.is_owner ? '<button class="btn primary" id="d-others">换一个</button>' : ""}</div>`,
    });
    const bindIf = (id, fn) => {
        if ($(id)) $(id).onclick = guard(fn);
    };
    if (a.is_owner) installSummary(a);
    if (a.is_owner && (a.executor === "workbuddy" || a.kind === "device") && a.aid) workbuddyPanel(a);
    if (!device) pollWork(a);
    bindIf("#d-install-logs", () => installLogs(a));
    bindIf("#d-enable-executor", () => connectComputer(a));
    bindIf("#d-edit", () => edit(a));
    bindIf("#d-ix", () => interactions(a));
    bindIf("#d-repair", () => pair(a));
    bindIf("#d-update", () =>
        copy(
            `请更新 Agent Space 连接程序（只更新 connector，不改 ANet 身份和配置）。平台：${location.origin}。步骤：1) 用原来的 --data-dir（保留 portal/connection.json，不重新 pair）；2) 从 ${location.origin}/connector.py 下载最新版覆盖旧的 connector.py，先阅读代码；3) 停掉旧的连接程序进程或登录启动项，再运行 python3 connector.py install --data-dir <原数据目录>；4) 回到网页刷新“${a.name}”的详情，确认出现“运行环境”。不要修改工作目录、执行模式或权限。`
        )
    );
    bindIf("#d-setup", () => pair(a));
    bindIf("#d-send", () => {
        modal.close();
        go("directory");
    });
    bindIf("#d-use", () => {
        modal.close();
        if (device) go("directory");
        else return openTask(a);
    });
    bindIf("#d-others", () => {
        modal.close();
        go("directory");
    });
}

/* ------------------------------------------------------------------ dispatch */

function draftKey(id) {
    return `agent-space:draft:${me.id}:${id}`;
}
function readDraft(id) {
    try {
        return JSON.parse(sessionStorage.getItem(draftKey(id)) || "{}");
    } catch {
        return {};
    }
}
function saveDraft(id, value) {
    try {
        sessionStorage.setItem(draftKey(id), JSON.stringify(value));
    } catch {
        /* storage full or disabled */
    }
}
function clearDrafts() {
    try {
        Object.keys(sessionStorage)
            .filter((k) => k.startsWith("agent-space:draft:"))
            .forEach((k) => sessionStorage.removeItem(k));
    } catch {
        /* ignore */
    }
}
function lines(text) {
    return text
        .split("\n")
        .map((x) => x.trim())
        .filter(Boolean)
        .slice(0, 50);
}
function taskPrompt(target, caller, d) {
    if (!receiveReady(target) || caller?.is_owner !== true || !sendReady(caller))
        throw new Error("发起连接或接收执行状态已变化，请刷新后检查；未生成派工单。");
    const identity = `请从这台电脑的 ANet 身份发起任务。先读 ANet guide 并核验本机身份，查询目标 Agent Card；若仅返回 NONE/无网络名片，不要把它当作密钥失效，也不要开启公开技能或入站来补名片。先用本机已配对门户的只读 /api/node/info 核对凭据所属 AID 与本机一致，再用 /api/node/directory 核对目标名称与完整 AID（目录观测时间须新鲜，心跳时效单独报告），并注明这是门户绑定记录，不是对方自签名片。目标密钥不新鲜、名片验证失败、目录不可用或映射不一致时停止说明；目录匹配不代表允许执行，仍须遵守目标授权策略。确认本机 AID 为 ${caller.aid}（${caller.name}），目标为 ${target.name}，AID 为 ${target.aid}。身份不一致时停止说明，不切换或覆盖身份。按现有权限和用户授权执行。`;
    if (!target.long_task || d.mode !== "long")
        return `${identity}\n\n目标：\n${d.goal}\n\n验收条件：\n${d.acceptance || "根据目标核对实际交付结果。"}\n\n限制：\n${d.constraints || "遵守已有授权范围。"}\n\n用 ANet send_message 发送给目标，记录任务 ID，按 wait_task 返回的实际状态继续处理。报告 anet.effect_status、anet.receipt_verified 及交付证据；不确定是否生效时先查询原任务，不重复发送。`;
    const spec = {
        taskd: "submit",
        protocol: "taskd/1",
        goal: d.goal,
        acceptance: lines(d.acceptance),
        constraints: lines(d.constraints),
        budget: {
            max_steps: Number(d.steps),
            max_hours: Number(d.hours),
            step_timeout_min: 30,
        },
    };
    if (!spec.acceptance.length)
        spec.acceptance = [`交付满足以下目标的结果：${d.goal}`];
    return `${identity}\n\n执行端上报支持 taskd/1，检查器：${target.capabilities.checkers.join("、") || "请检查目标声明"}。\n\n发送前检查本机 taskd 发起端是否正在运行、当前助手是否为支持回传的 Codex，以及原会话是否能登记或定位。门户显示的回传状态仅来自最近心跳，仍需本机核实。若条件不满足，告知用户结果需通过原任务查询，不承诺自动回传；不要因此擅自安装软件或扩大权限。\n\n以下是用户确认的任务规格。可在保持目标、约束、预算不变的前提下，将文字验收条件细化为目标支持的程序检查器；无法明确转换的保留为文字验收。正文须为 taskd/1 JSON，用 ANet send_message 发往上述目标：\n\n${JSON.stringify(spec, null, 2)}\n\n发送后给用户任务 ID。若本机回传已确认可用，让 taskd 跟踪并在有结果或问题时回到原会话；否则使用 ANet wait_task 等待原任务并如实报告当前状态。不要为继续执行重复创建任务。收到结果时读取 report.json，分别报告已通过、失败、待确认项目，并说明 anet.effect_status 和 anet.receipt_verified 的实际值。`;
}
async function openTask(target) {
    if (!target) throw new Error("找不到这个智能体，刷新一下再试。");
    await refreshWorkspace();
    await load(false);
    target = catalog().find((a) => a.id === target.id);
    if (!target) throw new Error("这个智能体现在用不了，换一个吧。");
    const senders = callers(),
        ready = receiveReady(target),
        draft = readDraft(target.id),
        long = target.long_task,
        canQuick =
            !long || (target.capabilities.modes || ["long"]).includes("quick");
    let mode = long && (!canQuick || draft.mode === "long") ? "long" : "quick";
    const maxHours = target.capabilities?.max_hours || 72,
        maxSteps = target.capabilities?.max_steps || 200,
        existing = workspace.devices[0];
    openDialog({
        kicker: "派任务 · 1/2",
        size: "wide",
        title: "派给 " + esc(target.name),
        sub: "写清楚要做什么、怎样算做完。下一步生成派工单。",
        body: `<div class="route-card"><div class="route-end"><span class="route-label">从</span><select id="t-caller" ${senders.length ? "" : "disabled"} aria-label="从哪台电脑发">${senders.length ? senders.map((a) => `<option value="${a.id}" ${draft.caller === a.id ? "selected" : ""}>${esc(a.name)}${a.kind === "agent" ? "（智能体连接）" : ""}</option>`).join("") : "<option>还没有能发任务的电脑</option>"}</select></div><span class="route-arrow" aria-hidden="true"></span><div class="route-end"><span class="route-label">到</span><strong>${esc(target.name)}</strong><small>${esc(executorName(target))}${long ? " · 长任务" : ""}${long && target.capabilities.gui ? " · 声明界面能力" : ""}</small></div>${led(target)}</div>
    <p class="hint" id="return-hint"></p>
    ${!ready ? `<div class="banner">${icon("clock")}<div><strong>尚未核验此发起身份的接收路径</strong><p>${esc(stateInfo(target).meaning)}</p><p>保留既有授权；先核对具体发起身份，不自动修改策略。</p></div></div>` : ""}
    ${!senders.length ? `<div class="banner">${icon("device")}<div><strong>还没有能发任务的电脑</strong><p>先连上一台。写好的内容会留着。</p></div><button class="btn" id="t-connect">${existing ? "接着连" : "去连接"}</button></div>` : ""}
    <form id="task-form" class="form">
      ${long && canQuick ? `<fieldset class="mode-switch"><legend>怎么办这件事</legend><label class="${mode === "quick" ? "on" : ""}"><input type="radio" name="t-mode" value="quick" ${mode === "quick" ? "checked" : ""}><strong>快速问答</strong><small>一问一答，几十秒到几分钟，不用写验收条件。</small></label><label class="${mode === "long" ? "on" : ""}"><input type="radio" name="t-mode" value="long" ${mode === "long" ? "checked" : ""}><strong>长任务</strong><small>分步执行，有验收条件和证据，可以跑几小时。</small></label></fieldset>` : ""}
      <label>要做什么<textarea id="t-goal" required maxlength="4000" placeholder="比如：读这三份资料，写一份带出处的中文调研报告。">${esc(draft.goal || "")}</textarea></label>
      <div class="two"><label>怎样算做完 <span class="optional">一行一条</span><textarea id="t-acceptance" maxlength="4000" placeholder="交一份报告文件&#10;每个结论标出处&#10;列出还不确定的地方">${esc(draft.acceptance || "")}</textarea></label>
      <label>限制 <span class="optional">选填，一行一条</span><textarea id="t-constraints" maxlength="2000" placeholder="只读我给的资料&#10;不改原文件">${esc(draft.constraints || "")}</textarea></label></div>
      ${long ? `<div class="budget" id="t-budget" ${mode === "long" ? "" : "hidden"}><label>最多跑 <span class="optional">小时</span><input type="number" id="t-hours" min="1" step="1" max="${maxHours}" ${mode === "long" ? "required" : ""} value="${Math.min(Number(draft.hours) || 12, maxHours)}"></label><label>最多 <span class="optional">步</span><input type="number" id="t-steps" min="1" step="1" max="${maxSteps}" ${mode === "long" ? "required" : ""} value="${Math.min(Number(draft.steps) || 40, maxSteps)}"></label><p class="hint">在这个范围里直接执行，需要你拍板的事才会来问你。</p></div>` : ""}
      <p class="form-error" role="alert"></p>
      <div class="form-actions"><span class="hint">草稿自动保存</span><button type="button" class="btn ghost" id="t-cancel">先这样</button><button type="submit" class="btn primary" ${!ready || !senders.length ? "disabled" : ""}>生成派工单${icon("arrow")}</button></div>
    </form>`,
    });
    const values = () => ({
        caller: $("#t-caller").value,
        goal: $("#t-goal").value.trim(),
        acceptance: $("#t-acceptance").value.trim(),
        constraints: $("#t-constraints").value.trim(),
        hours: $("#t-hours")?.value,
        steps: $("#t-steps")?.value,
        mode,
    });
    const stash = () => saveDraft(target.id, values());
    const returnHint = () => {
        const c = senders.find((a) => a.id === $("#t-caller").value);
        $("#return-hint").textContent =
            long && mode === "long"
                ? c?.requester_ready
                    ? "节点上报回传就绪；发送前仍需核对原会话绑定，交付后核对实际回执。"
                    : "自动回传尚未确认，具体原因未提供；可以发起任务，并从原任务查询结果。"
                : "结果在发任务的助手里看。";
    };
    returnHint();
    $$("[name=t-mode]").forEach(
        (r) =>
            (r.onchange = () => {
                mode = r.value;
                $$(".mode-switch label").forEach((l) =>
                    l.classList.toggle("on", $("input", l).checked)
                );
                $("#t-budget").hidden = mode !== "long";
                $("#t-hours").required = $("#t-steps").required =
                    mode === "long";
                stash();
                returnHint();
            })
    );
    $("#task-form").oninput = stash;
    $("#t-caller").onchange = () => {
        stash();
        returnHint();
    };
    $("#t-cancel").onclick = () => {
        stash();
        modal.close();
    };
    if ($("#t-connect"))
        $("#t-connect").onclick = guard(() => {
            stash();
            if (existing)
                return existing.aid
                    ? details(existing)
                    : pair(existing, target);
            return edit(null, "device", target);
        });
    $("#task-form").onsubmit = async (e) => {
        e.preventDefault();
        const form = e.target,
            d = values();
        saveDraft(target.id, d);
        $("[type=submit]", form).disabled = true;
        try {
            await refreshWorkspace();
            const current = catalog().find((a) => a.id === target.id),
                caller = callers().find((a) => a.id === d.caller);
            if (!caller || !receiveReady(current))
                throw new Error("连接状态变了，检查一下电脑和智能体再试。");
            if (Boolean(current.long_task) !== Boolean(long))
                throw new Error(
                    "它的长任务能力变了，关掉重新打开吧。写的内容还在。"
                );
            if (
                long &&
                d.mode === "long" &&
                (!(Number(d.hours) > 0) ||
                    Number(d.hours) > (current.capabilities.max_hours || 72) ||
                    !(Number(d.steps) > 0) ||
                    Number(d.steps) > (current.capabilities.max_steps || 200))
            )
                throw new Error("预算超出了它支持的范围，调小一点。");
            showWorkOrder(current, caller, d);
        } catch (error) {
            formError(form, error);
        }
    };
}
function showWorkOrder(target, caller, d) {
    const prompt = taskPrompt(target, caller, d),
        longMode = target.long_task && d.mode === "long";
    const no =
        new Date().toISOString().slice(2, 10).replace(/-/g, "") +
        "-" +
        target.id.slice(0, 4).toUpperCase();
    openDialog({
        kicker: "派任务 · 2/2",
        size: "wide",
        title: "派工单",
        sub:
            "复制后到“" +
            esc(caller.name) +
            "”的助手里粘贴发出。网页不会替你发。",
        body: `<article class="work-order">
      <header><span class="wo-title">派工单<small>WORK ORDER</small></span><span class="mono">No.${no}</span></header>
      <div class="wo-route"><div><small>发出</small><strong>${esc(caller.name)}</strong></div><span class="route-arrow" aria-hidden="true"></span><div><small>执行</small><strong>${esc(target.name)}</strong></div></div>
      <dl><div><dt>目标</dt><dd id="wo-goal"></dd></div>${d.acceptance ? '<div><dt>验收</dt><dd id="wo-acc"></dd></div>' : ""}${d.constraints ? '<div><dt>限制</dt><dd id="wo-con"></dd></div>' : ""}${longMode ? `<div><dt>预算</dt><dd>最多 ${esc(d.hours)} 小时 · ${esc(d.steps)} 步 · taskd/1</dd></div>` : ""}</dl>
      <span class="wo-stamp" aria-hidden="true">待发送</span>
    </article>
    <div class="copy-card"><button class="btn primary wide" id="wo-copy">${icon("copy")}复制派工单</button><p class="hint" id="wo-feedback" role="status">发出去以后记下任务 ID${longMode && caller.requester_ready ? "，还需核对原会话绑定和回传回执" : ""}。</p><details class="reveal"><summary>看全文${longMode ? "和任务规格" : ""}</summary><pre class="code" id="wo-prompt"></pre></details></div>
    <div class="form-actions"><button class="btn ghost" id="wo-back">${icon("back")}返回修改</button><button class="btn" id="wo-done">完成</button></div>`,
    });
    $("#wo-goal").textContent = d.goal;
    if ($("#wo-acc")) $("#wo-acc").textContent = d.acceptance;
    if ($("#wo-con")) $("#wo-con").textContent = d.constraints;
    $("#wo-prompt").textContent = prompt;
    $("#wo-copy").onclick = async () => {
        try {
            await copy(prompt);
            $("#wo-copy").innerHTML = icon("check") + "已复制";
            $("#wo-feedback").textContent =
                "去“" + caller.name + "”的助手里粘贴发出，记下任务 ID。";
        } catch (error) {
            $("#wo-feedback").textContent = error.message;
        }
    };
    $("#wo-back").onclick = guard(() => openTask(target));
    $("#wo-done").onclick = () => modal.close();
}

/* ------------------------------------------------------------------ account */

function passwordDialog() {
    openDialog({
        kicker: "账户",
        title: "修改密码",
        sub: "改完要重新登录，其他地方的登录也会退出。",
        body: `<form id="pw-form" class="form"><label>当前密码<input type="password" name="current" autocomplete="current-password" required maxlength="200"></label><label>新密码<input type="password" name="password" autocomplete="new-password" required minlength="8" maxlength="200" placeholder="至少 8 位"></label><label>再输一次<input type="password" name="confirm" autocomplete="new-password" required minlength="8" maxlength="200"></label><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" class="btn ghost" id="pw-cancel">取消</button><button type="submit" class="btn primary">修改</button></div></form>`,
    });
    $("#pw-cancel").onclick = () => modal.close();
    $("#pw-form").onsubmit = async (e) => {
        e.preventDefault();
        const form = e.target,
            d = Object.fromEntries(new FormData(form));
        try {
            if (d.password !== d.confirm)
                throw new Error("两次输入的新密码不一样");
            $("[type=submit]", form).disabled = true;
            await api("/api/auth/password", "POST", {
                current: d.current,
                password: d.password,
            });
            modal.close();
            stopRefresh();
            me = null;
            history.replaceState(null, "", "/#login");
            await authPage("login");
            toast("密码改好了，用新密码登录吧", "ok");
        } catch (error) {
            formError(form, error);
        }
    };
}

/* ------------------------------------------------------------------ members (admin) */

async function renderMembers() {
    const admin = await api("/api/admin/overview");
    shell();
    const active = admin.invitations.filter(
        (i) => !i.used_at && i.expires > Date.now() / 1000
    );
    $("#content").innerHTML =
        pageHead(
            "Admin",
            "成员",
            "新同事可以直接注册。忘了密码的，在这里生成一次性的重置链接。",
            `<button class="btn" data-act="copy-register">${icon("link")}复制注册链接</button>`
        ) +
        `<section class="panel"><header class="panel-head"><h2>成员 <span class="count">${admin.users.length}</span></h2><span class="hint">每个人只管自己名下的智能体和电脑</span></header>
    <div class="table"><div class="tr th"><span>成员</span><span>角色</span><span>智能体 / 电脑</span><span>状态</span><span></span></div>${admin.users
        .map(
            (u) => `
      <div class="tr"><span class="who"><span class="avatar">${initials(u.name)}</span><span><strong>${esc(u.name)}${u.id === me.id ? ' <span class="tag">你</span>' : ""}</strong><small>@${esc(u.username)}</small></span></span><span>${u.role === "admin" ? "管理员" : "成员"}</span><span class="mono">${u.agents} / ${u.devices}</span><span><span class="pill ${u.enabled ? "ok" : "off"}"><i></i>${u.enabled ? "正常" : "已停用"}</span></span>
      <span class="row-actions"><button class="btn small ghost" data-act="reset" data-id="${u.id}">重置密码</button>${u.id !== me.id ? `<button class="btn small ${u.enabled ? "danger-ghost" : ""}" data-act="toggle" data-id="${u.id}">${u.enabled ? "停用" : "启用"}</button>` : ""}</span></div>`
        )
        .join("")}</div></section>
    <section class="panel"><header class="panel-head"><h2>重置链接 <span class="count">${active.length} 个没用</span></h2><span class="hint">一次有效，24 小时过期</span></header>
    <div class="table"><div class="tr th inv"><span>用途</span><span>备注</span><span>有效期到</span><span>状态</span><span></span></div>${
        admin.invitations.length
            ? admin.invitations
                  .map((i) => {
                      const live = !i.used_at && i.expires > Date.now() / 1000;
                      return `<div class="tr inv"><span>${i.kind === "reset" ? "重置密码" : "加入空间"}</span><span>${esc(i.label)}</span><span class="mono">${dateOf(i.expires)}</span><span><span class="pill ${live ? "info" : "off"}"><i></i>${i.used_at ? "已用" : live ? "可用" : "过期或撤销"}</span></span><span class="row-actions">${live ? `<button class="btn small ghost" data-act="revoke" data-id="${i.id}">撤销</button>` : ""}</span></div>`;
                  })
                  .join("")
            : '<p class="quiet-empty">还没有。</p>'
    }</div></section>`;
    await workbuddyApplicationSettings();
    on("[data-act=copy-register]", "click", () =>
        copy(location.origin + "/#register")
    );
    on("[data-act=reset]", "click", (el) =>
        resetLink(admin.users.find((u) => u.id === el.dataset.id))
    );
    on("[data-act=toggle]", "click", async (el) => {
        const u = admin.users.find((x) => x.id === el.dataset.id);
        if (
            u.enabled &&
            !confirm(`停用 ${u.name}？停用后无法登录，名下的节点也不能再调用。`)
        )
            return;
        await api("/api/admin/users/" + u.id, "PATCH", { enabled: !u.enabled });
        await renderMembers();
        toast("已更新", "ok");
    });
    on("[data-act=revoke]", "click", async (el) => {
        await api("/api/admin/invitations/" + el.dataset.id, "DELETE", {});
        await renderMembers();
        toast("已撤销", "ok");
    });
}
async function workbuddyApplicationSettings() {
    const content = $("#content");
    if (!content || me?.role !== "admin") return;
    const section = document.createElement("section");
    section.className = "panel";
    section.id = "workbuddy-app-settings";
    section.innerHTML = `<header class="panel-head"><h2>WorkBuddy 应用设置</h2><span class="hint" id="wb-app-state">正在读取配置状态…</span></header>
      <div class="panel-body"><p class="hint">先在 <a href="https://open.workbuddy.cn/" target="_blank" rel="noopener">WorkBuddy 开放平台</a>创建第三方应用，用途为调用本人电脑上的 WorkBuddy 本地助理执行任务；授权列表选择 <code>user.localassistant.readable</code> 和 <code>user.localassistant.invokable</code>。<a href="https://open.workbuddy.cn/en/docs/third-party-app" target="_blank" rel="noopener">查看官方创建与审核步骤 ↗</a></p>
      <form id="wb-app-form" class="form" autocomplete="off"><label>OAuth 回调地址<input value="https://124.222.173.36:8888/api/workbuddy/callback" readonly></label>
      <label>Client ID<input name="client_id" required maxlength="256" autocomplete="off" placeholder="填写应用的 Client ID"></label>
      <label>Client Secret<input name="client_secret" type="password" required maxlength="16384" autocomplete="new-password" placeholder="仅在这里填写，不要发到聊天"></label>
      <p class="hint">密钥保存后不回显。应用审核启用后，每台连接的账号所有者还需在连接详情中授权本地助理，并在电脑上完成 WorkBuddy 登录与连接检查。</p>
      <p class="form-error" role="alert"></p><div class="form-actions"><button type="submit" class="btn primary">保存应用配置</button></div></form></div>`;
    content.append(section);
    const show = status => {
        if (!section.isConnected) return;
        $("#wb-app-state").textContent = status.configured ? "应用配置已就绪" : "待配置：" + (status.missing.includes("client_id") ? "Client ID" : status.missing.includes("client_secret") ? "Client Secret" : "私有配置或回调地址");
    };
    try { show(await api("/api/admin/workbuddy-app")); }
    catch(error) { if(section.isConnected) $("#wb-app-state").textContent = error.message; }
    $("#wb-app-form").onsubmit = async event => {
        event.preventDefault();const form=event.target;const data=Object.fromEntries(new FormData(form));
        const button=$("[type=submit]",form);button.disabled=true;
        try { show(await api("/api/admin/workbuddy-app","POST",data));form.reset();toast("WorkBuddy 应用配置已保存","ok"); }
        catch(error) { formError(form,error); }
        finally { data.client_secret="";form.elements.client_secret.value="";button.disabled=false; }
    };
}
function resetLink(user) {
    openDialog({
        kicker: "成员",
        title: "给 " + esc(user.name) + " 生成重置链接",
        sub: "对方打开链接设新密码。一次有效，24 小时过期。",
        body: `<form id="reset-form" class="form"><label>备注<input name="label" maxlength="60" value="${esc(user.name + " 重置密码")}" required></label><p class="form-error" role="alert"></p><div class="form-actions"><button type="button" class="btn ghost" id="r-cancel">取消</button><button class="btn primary" type="submit">生成</button></div></form>`,
    });
    $("#r-cancel").onclick = () => modal.close();
    $("#reset-form").onsubmit = async (e) => {
        e.preventDefault();
        const form = e.target;
        $("[type=submit]", form).disabled = true;
        try {
            const d = await api("/api/admin/invitations", "POST", {
                kind: "reset",
                target: user.id,
                label: new FormData(form).get("label"),
            });
            openDialog({
                kicker: "成员",
                title: "链接好了",
                sub:
                    "发给 " +
                    esc(user.name) +
                    " 本人。对方设新密码之前，旧密码照常能用。",
                body: `<pre class="code" id="reset-url"></pre><div class="form-actions"><button class="btn ghost" id="r-close">关闭</button><button class="btn primary" id="r-copy">${icon("copy")}复制链接</button></div>`,
            });
            $("#reset-url").textContent = d.url;
            $("#r-close").onclick = () => modal.close();
            $("#r-copy").onclick = guard(() => copy(d.url));
            await renderMembers();
        } catch (error) {
            formError(form, error);
        }
    };
}

/* ------------------------------------------------------------------ help */

function guideHtml() {
    const states = [
        [
            "待连接",
            "warn",
            "资料存了，还没连上电脑。点“继续连接”，到那台电脑的助手里做完。",
        ],
        ["待上线", "info", "身份连上了，连接程序还没跑。到那台电脑上启动它。"],
        ["在线", "ok", "电脑在线，可以从它发任务。"],
        ["可用", "ok", "智能体连接正常，可以接任务。"],
        [
            "缺权限",
            "warn",
            "执行的电脑连上了，但还没开接任务的权限。复制排查说明给它的助手。",
        ],
        ["执行器待核验", "warn", "节点尚未确认执行器就绪。核对本机自检结果、配置及状态更新时间，不要仅凭此提示重装；已有任务的实际进度以任务详情为准。"],
        ["离线", "off", "最近没上报。看看电脑开着没、连接程序还在不在。"],
        ["已停用", "off", "用不了，找所有者或管理员。"],
    ];
    const faq = [
        [
            "电脑和智能体都要连吗？",
            "只用别人的智能体，连一台电脑就够了。你自己在线的智能体也能直接拿来发任务。想让自己的软件干活，才需要接入智能体。",
        ],
        [
            "为什么网页上没有聊天框？结果在哪看？",
            "任务从你电脑上的助手发出，网页只负责挑智能体、管连接、生成派工单。执行过程和结果都在发任务的那个助手里。",
        ],
        [
            "“长任务”是什么？",
            "执行的那台电脑装了 taskd。任务按节点能力使用原生 Goal 或计划执行；做没做完由独立检查验收。提问和最终结果能否回到原会话，还需核对本次任务的发起端绑定与送达记录。",
        ],
        [
            "Codex、Pi、WorkBuddy、DeepSeek 有什么区别？",
            "就是智能体实际用的运行软件，选执行电脑上装好的那个。模型登录和本机授权都在那台电脑上做，别把模型密钥填到网站上。",
        ],
        [
            "“只有我”和“团队共享”有什么区别？",
            "只有我：只有你名下连上的电脑能给它派任务。团队共享：空间里的成员都能看到、都能派任务。",
        ],
        [
            "连接说明过期了，或者半路关了页面？",
            "资料还在。还没开始连的，点“继续连接”重新拿一份，旧的就作废了。已经连上身份的，用“排查”，别反复重新绑定。",
        ],
        [
            "为什么还要在电脑上确认权限？",
            "注册不用审批，但让一台电脑替别人干活，得电脑主人点头：哪些目录能动、用什么权限。工作目录不等于文件隔离，按软件提示来。",
        ],
        [
            "任务超时了，能直接再发一遍吗？",
            "先去原来的助手里看看进度和文件。网络断了的时候，原任务可能还在跑；确认没做完再重发。",
        ],
        [
            "忘记密码怎么办？",
            "找管理员要一个重置链接。登录状态下也可以在右上角改密码。",
        ],
    ];
    return `<div class="guide">
    <nav class="guide-toc" aria-label="目录"><p class="kicker">本页</p>${[
        ["g-paths", "怎么开始"],
        ["g-roles", "三个角色"],
        ["g-states", "状态"],
        ["g-long", "长任务"],
        ["g-faq", "常见问题"],
    ]
        .map(([id, n]) => `<a href="#guide" data-jump="${id}">${n}</a>`)
        .join(
            ""
        )}<a class="toc-extra" href="/architecture.html">${icon("spark")}原理 ↗</a></nav>
    <div class="guide-body">
      <section id="g-paths"><h2>怎么开始</h2><div class="paths">
        <article><p class="kicker">让智能体帮你干活</p><ol class="numbered"><li><strong>连上你的电脑</strong><span>平时用助手的那台，连一次就行。</span></li><li><strong>挑一个智能体</strong><span>优先选“可用”的。</span></li><li><strong>写清楚要做什么</strong><span>做什么、怎样算做完、有什么限制。</span></li><li><strong>把派工单贴给助手发出</strong><span>结果回到那个助手里。</span></li></ol><a class="btn primary" href="#directory">去挑选${icon("arrow")}</a></article>
        <article><p class="kicker">把自己的智能体接进来</p><ol class="numbered"><li><strong>在“我的连接”里接入</strong><span>选执行电脑上装好的软件。</span></li><li><strong>把连接说明交给那台电脑上的助手</strong><span>它会检查软件、完成连接。</span></li><li><strong>确认目录和权限</strong><span>然后回网页点检查。</span></li><li><strong>派个小任务试试</strong><span>显示“可用”就能接任务了。</span></li></ol><a class="btn" href="#agents">我的连接${icon("arrow")}</a></article>
      </div></section>
      <section id="g-roles"><h2>三个角色</h2><div class="roles"><div><span class="mono-avatar device">${icon("device")}</span><strong>你的电脑</strong><p>发任务、收结果。</p></div><i>${icon("arrow")}</i><div><span class="seal">派</span><strong>Agent Space</strong><p>找智能体，管连接和权限，生成派工单。</p></div><i>${icon("arrow")}</i><div><span class="mono-avatar">${icon("agent")}</span><strong>智能体</strong><p>在另一台电脑上真正干活的 AI 软件。</p></div></div></section>
      <section id="g-states"><h2>状态</h2><div class="state-table">${states.map(([n, t, d]) => `<div><span class="pill ${t}"><i></i>${n}</span><p>${d}</p></div>`).join("")}</div><p class="hint">状态大约 30 秒更新一次，也可以点右上角刷新。状态正常不代表任务一定做得好。</p></section>
      <section id="g-long"><h2>长任务</h2><p class="body-text">带“长任务”标签的智能体装了 taskd。支持原生 Goal 的节点可在持续会话中推进长任务；也可显式选择计划模式，不支持原生 Goal 时会报告回退原因。实际执行路径、重试和停止确认以当前工作的执行端上报为准。Goal 自报完成不等于成果验收通过。</p><p class="body-text">计划默认直接执行。要超出你写的限制、要花钱、要改权限，或者预计超预算，才会先问你。</p></section>
      <section id="g-faq"><h2>常见问题</h2>${faq.map(([q, a]) => `<details class="faq"><summary>${q}</summary><p>${a}</p></details>`).join("")}</section>
    </div>
  </div>`;
}
function renderGuide(publicPage = false) {
    if (publicPage) {
        app.innerHTML = `<div class="frame"><header class="masthead"><div class="masthead-row">${brand()}<div class="account"><a class="btn ghost" href="#login">登录</a><a class="btn primary" href="#register">注册</a></div></div></header><main class="page">${pageHead("Help", "帮助", "从连上第一台电脑到派出第一个任务。")}${guideHtml()}</main></div>`;
    } else {
        shell();
        $("#content").innerHTML =
            pageHead("Help", "帮助", "从连上第一台电脑到派出第一个任务。") +
            guideHtml();
    }
    $$("[data-jump]").forEach((a) =>
        a.addEventListener("click", (e) => {
            e.preventDefault();
            document
                .getElementById(a.dataset.jump)
                ?.scrollIntoView({ behavior: "smooth", block: "start" });
        })
    );
}

/* ------------------------------------------------------------------ assistant */

let assistantTimer = null;
const TOOL_NOTE = {
    list_agents: "查了智能体目录",
    get_agent: "看了智能体详情",
    read_interactions: "读了交互记录",
    my_tasks: "查了发出的任务",
    check_task: "查了任务进展",
};
const ACTION_STATE = {
    pending: "等你确认",
    approved: "已确认，发送结果待核对",
    unknown: "发送结果未知，请核对原操作",
    sent: "本机已确认请求",
    rejected: "已取消",
    failed: "发送准备失败",
};

function unresolvedDispatches(rows) {
    if (!rows?.length) return "";
    return `<section class="cap-box"><h3>待核对的发送操作</h3><p class="hint">以下操作可能已被对端收到。保留原编号，先在原发起节点只读查询；清空聊天不会清除这些记录。不要重发业务任务。</p>${rows.map(a => `<article class="chat-card"><strong>${esc(a.target_name || "原目标")}</strong><p>${esc(ACTION_STATE[a.status] || "结果待核对")}</p><p>原操作：<code>${esc(a.id)}</code><br>开始时间：${esc(a.started_at ? evidenceTime(a.started_at) : a.decided_at ? evidenceTime(a.decided_at) : "旧记录未提供")}<br>原任务：<code>${esc(a.task_id || "尚未取得任务编号；不能据此断言未发送")}</code></p><details><summary>核对依据</summary><p>原发起节点：<code>${esc(a.source_aid || "未提供")}</code><br>原目标节点：<code>${esc(a.target_aid || "旧记录未提供")}</code><br>请求内容 SHA-256：<code>${esc(a.payload_sha256 || "旧记录未提供")}</code><br>记录阶段：${esc(a.phase || "未提供")}</p></details></article>`).join("")}</section>`;
}

function assistantLog(v) {
    const find = (id) =>
        [...workspace.own, ...workspace.shared].find((a) => a.id === id);
    const items = v.messages.map((m) => {
        if (m.role === "user")
            return `<div class="chat-msg me">${esc(m.content)}</div>`;
        if (m.role === "assistant")
            return `<div class="chat-msg">${esc(m.content)}</div>`;
        const d = m.data || {};
        if (d.kind === "tool")
            return `<p class="chat-note">${icon("wrench")}${esc(TOOL_NOTE[d.name] || "用了工具")}${
                d.args && Object.keys(d.args).length
                    ? " · " +
                      esc(
                          Object.values(d.args)
                              .map((x) => clip(x, 30))
                              .join(" / ")
                      )
                    : ""
            }</p>`;
        if (d.kind === "action") {
            const a = v.actions[d.action_id];
            if (!a) return "";
            const target = find(a.agent_id),
                text = a.kind === "delegate" ? a.args.goal : a.args.text;
            return `<div class="chat-card ${esc(a.status)}"><header>${a.kind === "delegate" ? "要发" + (a.args.mode === "long" ? "长任务" : "任务") + "给" : "要追加一条消息给"} <strong>${esc(target ? target.name : "智能体")}</strong><span class="ix-state">${esc(ACTION_STATE[a.status] || a.status)}</span></header><pre>${esc(text)}${a.args.mode === "long" ? "\n\n验收：\n" + a.args.acceptance.map((x) => "· " + x).join("\n") + "\n预算：最多 " + a.args.max_hours + " 小时 · " + a.args.max_steps + " 步" : ""}</pre>${a.status === "pending" ? `<div class="form-actions"><button class="btn ghost" data-decide="reject" data-id="${a.id}">取消</button><button class="btn primary" data-decide="approve" data-id="${a.id}">确认发送${icon("arrow")}</button></div><p class="hint">点确认才会真的发出去。</p>` : ""}</div>`;
        }
        return `<p class="chat-note ${d.kind === "error" ? "bad" : ""}">${esc(m.content)}</p>`;
    });
    if (v.busy) items.push('<p class="chat-note">助手在想…</p>');
    return unresolvedDispatches(v.unresolved) + (
        items.join("") ||
        `<div class="chat-empty"><p>可以问我哪些智能体在线、它们在哪个目录工作、底层是什么，也可以让我给它们发任务。</p><div class="chips">${["现在有哪些智能体可用？", "哪些智能体支持长任务？", "我的智能体最近在和谁交互？"].map((q) => `<button class="chip" data-ask="${q}">${q}</button>`).join("")}</div></div>`
    );
}

async function renderAssistant() {
    await refreshWorkspace();
    shell();
    $("#content").innerHTML =
        pageHead(
            "Assistant",
            "我的助手",
            "它用你的权限看目录和交互记录；要发给别的智能体的，都等你点确认。",
            '<button class="btn ghost" id="chat-reset">清空对话</button>'
        ) +
        `<section class="panel chat"><div class="chat-log" id="chat-log" aria-live="polite"></div><p class="form-error" id="chat-error" role="alert"></p><form class="chat-form" id="chat-form"><textarea id="chat-text" rows="2" maxlength="4000" placeholder="问点什么，或者让它去办件事（Enter 发送，Shift+Enter 换行）" aria-label="给助手的消息"></textarea><button class="btn primary" type="submit" id="chat-send">发送</button></form></section>`;
    let last = "",
        alive = true;
    const log = $("#chat-log");
    const draw = async (stick = false) => {
        const v = await api("/api/assistant");
        if (!alive || !$("#chat-log")) return null;
        const html =
            assistantLog(v) +
            (v.configured
                ? ""
                : '<p class="chat-note bad">管理员还没有配置模型密钥，助手暂时不能回答。</p>');
        if (html !== last) {
            const near =
                log.scrollHeight - log.scrollTop - log.clientHeight < 60;
            log.innerHTML = html;
            last = html;
            if (near || stick) log.scrollTop = log.scrollHeight;
            bind();
        }
        $("#chat-send").disabled = v.busy;
        return v;
    };
    const loop = async () => {
        let v = null;
        try {
            v = await draw();
        } catch (error) {
            if (error.status === 401) return showPageError(error);
        }
        if (alive && route === "assistant")
            assistantTimer = setTimeout(loop, v && v.busy ? 1200 : 5000);
    };
    const send = guard(async (text) => {
        $("#chat-error").textContent = "";
        try {
            await api("/api/assistant/messages", "POST", { text });
            $("#chat-text").value = "";
            await draw(true);
            clearTimeout(assistantTimer);
            assistantTimer = setTimeout(loop, 1200);
        } catch (error) {
            $("#chat-error").textContent = error.message;
        }
    });
    function bind() {
        $$("[data-ask]").forEach(
            (b) => (b.onclick = () => send(b.dataset.ask))
        );
        $$("[data-decide]").forEach(
            (b) =>
                (b.onclick = guard(async () => {
                    document
                        .querySelectorAll("[data-decide]")
                        .forEach((x) => (x.disabled = true));
                    try {
                        await api(
                            "/api/assistant/actions/" + b.dataset.id,
                            "POST",
                            { decision: b.dataset.decide }
                        );
                    } catch (error) {
                        toast(error.message, "bad");
                    }
                    await draw(true);
                    clearTimeout(assistantTimer);
                    assistantTimer = setTimeout(loop, 1200);
                }))
        );
    }
    $("#chat-form").onsubmit = (e) => {
        e.preventDefault();
        const text = $("#chat-text").value.trim();
        if (text) send(text);
    };
    $("#chat-text").onkeydown = (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            $("#chat-form").requestSubmit();
        }
    };
    $("#chat-reset").onclick = guard(async () => {
        if (!confirm("清空这段对话？没确认的申请会被取消。")) return;
        await api("/api/assistant/reset", "POST", {});
        await draw(true);
    });
    window.addEventListener("hashchange", () => (alive = false), {
        once: true,
    });
    await loop();
    $("#chat-text").focus();
}

/* ------------------------------------------------------------------ auth & connect pages */

function sky() {
    // A fixed constellation: the same picture on every visit.
    let seed = 7;
    const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    const W = 1400,
        H = 900,
        pts = [];
    for (let i = 0; i < 54; i++) pts.push([rnd() * W, rnd() * H, rnd()]);
    let lines = "",
        stars = "",
        packets = "",
        lit = 0;
    pts.forEach((p, i) => {
        pts.slice(i + 1).forEach((q, j) => {
            const dist = Math.hypot(p[0] - q[0], p[1] - q[1]);
            if (dist < 175) {
                const hot = lit < 9 && (i + j) % 5 === 0;
                const d = `M${p[0].toFixed(0)} ${p[1].toFixed(0)} L${q[0].toFixed(0)} ${q[1].toFixed(0)}`;
                lines += `<path class="link-line ${hot ? "lit" : ""}" d="${d}"/>`;
                if (hot) {
                    lit++;
                    packets += `<circle r="2.2" class="packet"><animateMotion dur="${(2.5 + lit * 0.4).toFixed(1)}s" begin="${(lit * 0.6).toFixed(1)}s" repeatCount="indefinite" path="${d}"/></circle>`;
                }
            }
        });
        const cls = p[2] > 0.93 ? "hot" : p[2] > 0.86 ? "cool" : "";
        stars += `<circle class="star ${cls} d${i % 4}" cx="${p[0].toFixed(0)}" cy="${p[1].toFixed(0)}" r="${(1 + p[2] * 1.8).toFixed(1)}"/>`;
    });
    return `<div class="sky" aria-hidden="true"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid slice">${lines}${packets}${stars}</svg></div>`;
}
async function authPage(kind = "login", t = "") {
    const register = kind === "register" || Boolean(t);
    let inv = null;
    if (t) {
        try {
            inv = await api("/api/invitations/check", "POST", { token: t });
        } catch (e) {
            app.innerHTML = `<div class="solo">${brand()}<div class="solo-card"><p class="kicker">链接</p><h1>这个链接用不了</h1><p class="lead">${esc(e.message)}</p><p class="solo-links"><a class="btn primary" href="#register">直接注册</a><a class="btn ghost" href="#login">去登录</a></p></div></div>`;
            return;
        }
    }
    const reset = inv?.kind === "reset",
        setup = inv?.kind === "setup",
        self = register && !t;
    const title = !register
        ? "登录"
        : reset
          ? "设置新密码"
          : setup
            ? "创建管理员"
            : "注册";
    app.innerHTML = `<div class="auth">${sky()}
    <section class="auth-art">
      ${brand()}
      <div class="auth-statement"><p class="kicker">Agent Space</p><h2>团队的智能体<br><span>都在这里</span></h2><p>找智能体、连电脑、派任务。长任务自己跑完，结果回到你发任务的那个对话。</p></div>
      <p class="auth-runtimes">${harnesses.map((h) => `<span>${esc(h.name.replace(" / 自定义 harness", ""))}</span>`).join("")}</p>
    </section>
    <section class="auth-area"><form class="auth-form form" novalidate>
      <p class="kicker">${register ? (self ? "Sign up" : "Account") : "Sign in"}</p><h1>${title}</h1>
      <p class="lead">${!register ? "用你的团队账号。" : reset ? "设好以后用新密码登录。" : setup ? "第一个账号是管理员。" : "填完就能用，不用等审批。"}</p>
      ${reset ? "" : `<label>用户名<input name="username" autocomplete="username" required maxlength="40" ${register ? 'pattern="[a-zA-Z0-9_.\\-]{3,40}" placeholder="3–40 位，字母数字或 _-."' : ""}></label>`}
      ${register && !reset ? '<label>显示名<input name="name" autocomplete="nickname" required maxlength="40" placeholder="大家看到的名字"></label>' : ""}
      <label>${reset ? "新密码" : "密码"}<span class="pw"><input id="pw" name="password" type="password" autocomplete="${register ? "new-password" : "current-password"}" required ${register ? 'minlength="8" placeholder="至少 8 位"' : ""} maxlength="200"><button type="button" class="icon-btn" id="pw-toggle" aria-label="显示密码">${icon("eye")}</button></span></label>
      ${register ? '<label>再输一次<input name="password_confirm" type="password" autocomplete="new-password" required minlength="8" maxlength="200"></label>' : ""}
      <p class="form-error" role="alert"></p>
      <button class="btn primary wide" type="submit">${!register ? "登录" : reset ? "保存" : "注册"}${icon("arrow")}</button>
      <p class="auth-switch">${!register ? '没有账号？<a href="#register">注册</a>' : reset ? '<a href="#login">去登录</a>' : '已有账号？<a href="#login">登录</a>'}</p>
      ${!register ? '<p class="hint center">忘记密码，找管理员要重置链接。</p>' : ""}
      <p class="auth-links"><a href="/download">下载客户端</a><a href="#guide">怎么用</a><a href="/architecture.html">原理 ↗</a></p>
    </form></section>
  </div>`;
    $("#pw-toggle").onclick = () => {
        const p = $("#pw"),
            show = p.type === "password";
        p.type = show ? "text" : "password";
        $("#pw-toggle").setAttribute(
            "aria-label",
            show ? "隐藏密码" : "显示密码"
        );
        $("#pw-toggle").classList.toggle("on", show);
    };
    $(".auth-form").onsubmit = async (e) => {
        e.preventDefault();
        const form = e.target;
        try {
            if (!form.checkValidity()) {
                const bad = $(":invalid", form);
                bad?.focus();
                throw new Error(bad?.validationMessage || "检查一下填的内容");
            }
            const d = Object.fromEntries(new FormData(form));
            if (register && d.password !== d.password_confirm)
                throw new Error("两次输入的密码不一样");
            $("[type=submit]", form).disabled = true;
            if (register) {
                const result = await api("/api/auth/register", "POST", {
                    ...d,
                    ...(t ? { token: t } : {}),
                });
                if (result.signed_in) {
                    history.replaceState(null, "", "/#start");
                    await start();
                    toast("注册好了", "ok");
                } else {
                    history.replaceState(null, "", "/#login");
                    toast(
                        reset ? "密码改好了，登录吧" : "账号建好了，登录吧",
                        "ok"
                    );
                    authPage("login");
                }
            } else {
                await api("/api/auth/login", "POST", d);
                history.replaceState(null, "", "/#start");
                await start();
            }
        } catch (err) {
            formError(form, err);
        }
    };
}
async function connectPage(t) {
    stopRefresh();
    try {
        const d = await api("/api/pair/info", "POST", { token: t }),
            device = d.kind === "device",
            software = executorName(d);
        app.innerHTML = `<div class="solo">${brand()}<div class="solo-card wide"><p class="kicker">${device ? "新电脑" : "新智能体 · " + esc(software)}</p><h1>${esc(d.name)}</h1><p class="lead">${device ? "在你准备发任务的电脑上打开助手，把下面的说明给它。" : "在装了 " + esc(software) + " 的电脑上打开助手，把下面的说明给它。"}</p>
      <ol class="numbered"><li><strong>复制连接说明</strong><span>这次连接专用。</span></li><li><strong>粘贴给那台电脑上的助手</strong><span>它会检查软件、完成连接，权限由电脑主人确认。</span></li><li><strong>回网站看结果</strong><span>在“我的连接”里看还差哪一步。</span></li></ol>
      <div class="copy-card"><button class="btn primary wide" id="copy-full">${icon("copy")}复制连接说明</button><p class="hint" id="connect-feedback" role="status">1 小时内有效，连上就作废。别转发。</p><details class="reveal"><summary>看全文</summary><pre class="code" id="connect-text"></pre></details></div>
      <p class="solo-links"><a class="btn ghost" href="#${device ? "devices" : "agents"}">回到我的连接</a><a class="text-link" href="#guide">帮助</a></p></div></div>`;
        $("#connect-text").textContent = d.instructions;
        $("#copy-full").onclick = async () => {
            try {
                await copy(d.instructions);
                $("#connect-feedback").textContent =
                    "已复制，去那台电脑的助手里粘贴。";
            } catch (error) {
                $("#connect-feedback").textContent = error.message;
            }
        };
    } catch (error) {
        app.innerHTML = `<div class="solo">${brand()}<div class="solo-card"><p class="kicker">连接</p><h1>这个连接链接用不了</h1><p class="lead">${esc(error.message)}</p><p class="body-text">已经开始连了的话，回到那个助手接着做；还没开始的，到“我的连接”重新拿一份。</p><p class="solo-links"><a class="btn primary" href="#start">回总览</a><a class="text-link" href="#guide">帮助</a></p></div></div>`;
    }
}

/* Direct conversations and scoped API credentials. This file is embedded into app.js. */
const directView = { selected: null, drafts: new Map(), agents: [], threads: [], timer: null, generation: 0, submitting: false, polling: null };
function directPendingKey(id) { return `anet-chat-pending:${me?.id || me?.username}:${id}`; }
function directPending(id) { try { return JSON.parse(sessionStorage.getItem(directPendingKey(id)) || "null"); } catch { return null; } }
function saveDirectPending(id, value) { if (value) sessionStorage.setItem(directPendingKey(id), JSON.stringify(value)); else sessionStorage.removeItem(directPendingKey(id)); }
function directState(state) { return ({sending:"正在发送",submitted:"等待智能体回复",queued:"排队中",working:"正在回复",running:"正在回复",completed:"已回复",failed:"未完成",rejected:"请求未被接受",canceled:"已取消",cancelled:"已取消",unknown:"结果待确认",unverified:"结果待确认",input_required:"等待补充",'input-required':"等待补充",ready:"可继续对话",idle:"等待消息",offline:"离线",error:"需要处理"})[state] || "等待更新"; }
function directBusy(thread) { return ["sending","submitted","working","running","unknown","unverified"].includes(thread.state); }
function directTerminal(thread) { return ["completed","failed","canceled","cancelled","rejected"].includes(thread.state); }
function directThreadState(thread) { return !directTerminal(thread)&&thread.progress?.label?thread.progress.label:thread.state==="completed"&&thread.response_available===false?"已结束，尚未收到回答":directState(thread.state); }
function directUpdatedAt(value) { const date=new Date(typeof value==="number"?value*1000:value);return value&&!Number.isNaN(date.valueOf())?date.toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",second:"2-digit"}):""; }
function directControlMessage(message) {
 // Compatibility for older stored responses. Only hide a complete protocol envelope
 // bound to this message's task; user text and answers discussing TASKD remain visible.
 if(message.role!=="assistant"||!message.task_id||typeof message.text!=="string")return false;
 const match=message.text.trim().match(/^(?:(?:正在处理。|Working on it\.|正在核对同一次调用的结果；不会重复执行。)\s*)?TASKD\s+(\{[\s\S]*\})$/);if(!match)return false;
 try{const value=JSON.parse(match[1]);return value.protocol==="taskd/1"&&value.task===message.task_id&&["queued","accepted","running","planning","verifying","blocked","needs_user","awaiting_approval"].includes(value.state);}catch{return false;}
}
function directSchedule(thread,generation,id,delay=2000) {
 clearTimeout(directView.timer);directView.timer=null;
 if(route==="chat"&&generation===directView.generation&&directView.selected===id&&(directBusy(thread)||directPending(id)||directView.submitting))directView.timer=setTimeout(()=>directPoll(generation,id),delay);
}
function directStop() { clearTimeout(directView.timer); directView.generation++; }
function directReset() { directStop(); directView.selected=null;directView.drafts.clear();directView.threads=[];directView.agents=[];directView.submitting=false; }
function directList() {
 const list=$("#direct-thread-list");if(!list)return;
 const markup=directView.threads.length?directView.threads.map(t=>`<button class="direct-thread ${t.id===directView.selected?"selected":""}" data-thread="${esc(t.id)}" ${t.id===directView.selected?'aria-current="true"':""}><strong>${esc(t.title||directView.agents.find(a=>a.id===t.agent_id)?.name||"对话")}</strong><span>${esc(directThreadState(t))}</span></button>`).join(""):`<p class="hint direct-list-empty">还没有对话。<br>选择一个在线智能体开始。</p>`;
 if(list._directMarkup===markup)return;list._directMarkup=markup;list.innerHTML=markup;
 on("[data-thread]","click",async el=>{directView.selected=el.dataset.thread;directList();await directOpen();},list);
}
async function renderDirectChat() {
 directStop();const generation=directView.generation;
 const [agents,threads]=await Promise.all([api("/api/chat/agents"),api("/api/chat/conversations")]);
 if(route!=="chat"||generation!==directView.generation)return;
 directView.agents=agents.agents||[];directView.threads=threads.conversations||[];
 shell();$("#content").classList.add("direct-page");$("#content").innerHTML=pageHead("Direct chat","与智能体聊天","选择你的智能体，消息会直接发到它所在的设备。")+`<section class="direct-layout"><aside class="direct-sidebar" aria-label="对话列表"><button class="btn primary" id="direct-new">${icon("plus")}新建对话</button><a class="direct-assistant" href="#assistant">${icon("spark")}门户助手<small>帮你找智能体、安排任务</small></a><div id="direct-thread-list"></div></aside><section class="direct-main" id="direct-main" aria-label="当前对话"><div class="direct-empty"><span class="direct-empty-icon">${icon("agent")}</span><h2>开始一次直接对话</h2><p>选择有权限的在线智能体，告诉它你需要什么。</p><button class="btn ghost" id="direct-empty-new">选择智能体</button></div></section></section>`;
 $("#direct-new").onclick=guard(directNew);$("#direct-empty-new").onclick=guard(directNew);directList();
 if(directView.selected&&directView.threads.some(t=>t.id===directView.selected))await directOpen();
}
async function directNew() {
 const result=await api("/api/chat/agents");directView.agents=result.agents||[];
 openDialog({title:"选择智能体",sub:"消息直接发给所选智能体。",body:`<div class="direct-agent-choices">${directView.agents.length?directView.agents.map(a=>`<button class="direct-agent-choice" data-chat-agent="${esc(a.id)}" ${a.can_chat===true?"":"disabled"}><span>${icon("agent")}<strong>${esc(a.name)}</strong></span><small>${esc(a.can_chat===true?"● 在线，可对话":a.reason||"当前不可对话")}</small></button>`).join(""):'<p class="hint">暂无有权限且在线的智能体。请先连接设备，或联系所有者开放权限。</p>'}</div><p class="form-error" role="alert" id="direct-new-error"></p>`});
 on("[data-chat-agent]","click",async el=>{if(el.disabled)return;$("#direct-new-error").textContent="";$$('[data-chat-agent]').forEach(b=>b.disabled=true);try{const thread=await api("/api/chat/conversations","POST",{agent_id:el.dataset.chatAgent});directView.threads.unshift(thread);directView.selected=thread.id;modal.close();directList();await directOpen(thread);}catch(error){$("#direct-new-error").textContent=error.message;directView.agents.forEach(a=>{const b=$(`[data-chat-agent="${CSS.escape(a.id)}"]`);if(b)b.disabled=a.can_chat!==true;});}},modal);
}
async function directOpen(initial) {
 directStop();const generation=directView.generation,id=directView.selected;if(!id)return;
 const thread=initial||await api("/api/chat/conversations/"+encodeURIComponent(id));if(generation!==directView.generation||route!=="chat")return;
 const agent=directView.agents.find(a=>a.id===thread.agent_id);const title=agent?.name||thread.title||"智能体";
 $(".direct-layout").classList.add("show-thread");$("#direct-main").innerHTML=`<header class="direct-heading"><button class="btn ghost direct-mobile-back" id="direct-back" aria-label="返回对话列表">${icon("back")}</button><div><p class="kicker">直接对话</p><h2>${esc(title)}</h2></div><button class="btn ghost" id="direct-refresh" aria-label="查询当前对话状态">${icon("refresh")}查询状态</button></header><div class="direct-messages" id="direct-messages" role="log" aria-live="polite" aria-relevant="additions text"></div><div class="direct-composer"><div class="direct-status" id="direct-status" role="status"></div><p class="form-error" id="direct-error" role="alert"></p><form id="direct-form"><label class="sr-only" for="direct-text">给${esc(title)}的消息</label><textarea id="direct-text" maxlength="12000" rows="3" placeholder="写下你的消息，Enter 发送，Shift+Enter 换行"></textarea><button class="btn primary" id="direct-send" type="submit">发送${icon("arrow")}</button></form><p class="hint">消息会在这个对话中继续。请在发送前确认所选智能体。</p></div>`;
 $("#direct-back").onclick=()=>$(".direct-layout").classList.remove("show-thread");$("#direct-text").value=directView.drafts.get(id)||"";
 $("#direct-text").oninput=e=>directView.drafts.set(id,e.target.value);
 $("#direct-text").onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!$("#direct-send").disabled)$("#direct-form").requestSubmit();}};
 $("#direct-form").onsubmit=e=>{e.preventDefault();directSend(id).catch(error=>{if($("#direct-error"))$("#direct-error").textContent=error.message;});};
 $("#direct-refresh").onclick=guard(()=>directPoll(generation,id));
 directDraw(thread);directSchedule(thread,generation,id);
}
function directDraw(thread) {
 if(thread.id!==directView.selected||!$("#direct-messages"))return;
 const pending=directPending(thread.id),messages=(thread.messages||[]).filter(m=>!directControlMessage(m));
 if(pending&&messages.some(m=>m.idempotency_key===pending.key))saveDirectPending(thread.id,null);
 const log=$("#direct-messages"),nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<70;
 const html=messages.length?messages.map(m=>`<article class="direct-message ${m.role==="user"?"from-user":"from-agent"}"><div class="direct-message-label">${m.role==="user"?"你":m.role==="assistant"?"智能体":"状态"}</div><div class="direct-message-text">${esc(m.text)}</div>${m.state?`<small>${esc(m.state==="completed"&&m.role!=="assistant"?(m.role==="user"?"已发送":"已结束"):directState(m.state))}</small>`:""}</article>`).join(""):`<div class="direct-empty"><h3>向智能体打个招呼</h3><p>发出的消息和回复都会保留在这里。</p></div>`;
 if(log._directHtml!==html){log._directHtml=html;log.innerHTML=html;if(nearBottom)log.scrollTop=log.scrollHeight;}
 const unresolved=directPending(thread.id),busy=directBusy(thread)||!!unresolved||directView.submitting;
 $("#direct-send").disabled=busy;
 const updated=directUpdatedAt(thread.progress?.updated_at||thread.updated_at);
 $("#direct-status").textContent=unresolved?"正在确认发送结果，会自动更新，请勿重复发送。":directThreadState(thread)+(busy?" · 自动更新中":"")+(updated?` · 最后更新 ${updated}`:"");
 const row=directView.threads.find(t=>t.id===thread.id);if(row){row.state=thread.state;row.progress=thread.progress;row.response_available=thread.response_available;directList();}
 // No cancel action is rendered until the server exposes a validated endpoint.
}
async function directPoll(generation,id) {
 clearTimeout(directView.timer);if(route!=="chat"||generation!==directView.generation||directView.selected!==id)return;
 const polling=`${generation}:${id}`;if(directView.polling===polling)return;directView.polling=polling;
 const button=$("#direct-refresh");if(button)button.disabled=true;
 let thread,delay=2000;
 try{thread=await api("/api/chat/conversations/"+encodeURIComponent(id));if(generation!==directView.generation||route!=="chat")return;directDraw(thread);const error=$("#direct-error");if(error?._directPollingError){error.textContent="";error._directPollingError=false;}}
 catch(error){if(generation!==directView.generation||route!=="chat")return;const field=$("#direct-error");if(field){field.textContent="暂时无法更新，连接恢复后会自动继续。请勿重复发送。";field._directPollingError=true;}thread={state:"unknown"};delay=5000;}
 finally{if(directView.polling===polling)directView.polling=null;if(generation===directView.generation&&route==="chat"&&directView.selected===id){if(button)button.disabled=false;if(thread)directSchedule(thread,generation,id,delay);}}
}
async function directSend(id) {
 const box=$("#direct-text");if(!box||directView.submitting||directPending(id)||$("#direct-send").disabled)return;
 const text=box.value.trim();if(!text)return;const key=crypto.randomUUID();
 saveDirectPending(id,{key});directView.submitting=true;$("#direct-send").disabled=true;$("#direct-status").textContent="正在发送…";$("#direct-error").textContent="";
 try{await api("/api/chat/conversations/"+encodeURIComponent(id)+"/messages","POST",{text,idempotency_key:key});directView.drafts.set(id,"");if(directView.selected===id&&$("#direct-text"))$("#direct-text").value="";}
 catch(error){if([400,401,403,404,413,422].includes(error.status)){saveDirectPending(id,null);if($("#direct-error"))$("#direct-error").textContent=error.message;}else if($("#direct-error"))$("#direct-error").textContent="发送结果尚未确认。请查询原请求状态，不要重复发送。";}
 finally{directView.submitting=false;if(directView.selected===id&&route==="chat")await directPoll(directView.generation,id);}
}

async function renderAccessTokens() {
 directStop();const generation=directView.generation;const [result,agents]=await Promise.all([api("/api/access-tokens"),api("/api/chat/agents")]);if(route!=="api-access"||generation!==directView.generation)return;
 shell();$("#content").innerHTML=pageHead("API access","API 访问","为你的程序创建受限令牌，调用获授权的智能体。",'<button class="btn primary" id="token-new">创建令牌</button>')+`<section class="panel token-panel"><h2>访问令牌</h2><p class="hint">只开放所需智能体和权限。令牌到期或撤销后无法继续使用。</p><div class="token-list">${(result.tokens||[]).length?(result.tokens||[]).map(t=>`<article class="token-row"><div><h3>${esc(t.name)}</h3><code>${esc(t.prefix||"令牌")}${t.prefix?"…":""}</code><p>${esc((t.scopes||[]).map(s=>s==="chat:write"?"发送消息":"读取对话").join(" · "))}</p></div><div><span>${t.revoked?"已撤销":t.expired?"已过期":"有效"}</span><p>到期：${esc(tokenDate(t.expires_at))}</p><small>最近使用：${esc(t.last_used_at?tokenDate(t.last_used_at):"尚未使用")}</small></div><button class="btn ghost" data-revoke-token="${esc(t.id)}" ${t.revoked?"disabled":""}>撤销</button></article>`).join(""):'<div class="direct-empty"><h3>尚无令牌</h3><p>创建一个令牌，让你的程序接入 ANet。</p></div>'}</div></section><section class="panel token-panel"><h2>开始调用</h2><p class="hint">使用 HTTPS，通过 Authorization: Bearer 请求头传递令牌。不要把令牌放在网址里。</p><pre class="token-example">GET /api/v1/agents\nGET /api/v1/conversations\nPOST /api/v1/conversations\nGET /api/v1/conversations/{id}\nPOST /api/v1/conversations/{id}/messages</pre><p class="hint">继续对话时使用同一个会话编号。每条新消息使用唯一的 idempotency_key，查询状态时无需重发消息。</p></section>`;
 $("#token-new").onclick=()=>tokenCreate(agents.agents||[]);
 on("[data-revoke-token]","click",el=>{const id=el.dataset.revokeToken;openDialog({title:"撤销这个令牌？",sub:"使用该令牌的程序将无法继续访问 ANet。已有对话记录会保留。",body:'<p class="form-error" id="token-revoke-error" role="alert"></p><div class="form-actions"><button class="btn ghost" id="token-revoke-back">返回</button><button class="btn primary" id="token-revoke-confirm">确认撤销</button></div>'});$("#token-revoke-back").onclick=()=>modal.close();$("#token-revoke-confirm").onclick=async()=>{const b=$("#token-revoke-confirm");b.disabled=true;try{await api("/api/access-tokens/"+encodeURIComponent(id),"DELETE");modal.close();await renderAccessTokens();}catch(error){$("#token-revoke-error").textContent=error.message;b.disabled=false;}};});
}
function tokenDate(value) { if(!value)return "未提供";const d=new Date(typeof value==="number"?value*1000:value);return Number.isNaN(d.valueOf())?"未提供":d.toLocaleDateString("zh-CN"); }
function tokenCreate(agents) {
 openDialog({title:"创建访问令牌",sub:"仅选择这个程序需要的权限。",body:`<form id="token-create-form"><label>名称<input name="name" maxlength="60" required placeholder="例如：我的自动化脚本"></label><fieldset><legend>允许访问的智能体</legend>${agents.map(a=>`<label class="token-choice"><input type="checkbox" name="agents" value="${esc(a.id)}">${esc(a.name)}</label>`).join("")||'<p class="hint">没有可授权的智能体。</p>'}</fieldset><fieldset><legend>权限</legend><label class="token-choice"><input type="checkbox" name="scopes" value="chat:read">读取智能体与对话</label><label class="token-choice"><input type="checkbox" name="scopes" value="chat:write">创建对话和发送消息</label></fieldset><label>有效期<select name="expires"><option value="7">7 天</option><option value="30" selected>30 天</option><option value="90">90 天</option></select></label><p class="form-error" role="alert"></p><div class="form-actions"><button class="btn primary" type="submit">创建令牌</button></div></form>`});
 const form=$("#token-create-form");form.onsubmit=async e=>{e.preventDefault();const button=$('[type="submit"]',form);if(button.disabled)return;const agent_ids=$$('input[name="agents"]:checked',form).map(el=>el.value),scopes=$$('input[name="scopes"]:checked',form).map(el=>el.value);if(!agent_ids.length||!scopes.length){formError(form,new Error("请选择至少一个智能体和一项权限。"));return;}button.disabled=true;try{const result=await api("/api/access-tokens","POST",{name:form.elements.name.value.trim(),agent_ids,scopes,expires_days:Number(form.elements.expires.value)});showCreatedToken(result.token);await renderAccessTokens();}catch(error){if(modal.open&&$("#token-create-form")){formError(form,new Error(error.status&&error.status<500?error.message:"创建结果尚未确认。请先关闭窗口并刷新令牌列表，避免重复创建。"));if(!error.status||error.status>=500)button.disabled=true;}}};
}
function showCreatedToken(token) {
 if(typeof token!=="string"||!token)throw new Error("未收到可展示的令牌，请刷新令牌列表核对。");
 openDialog({title:"令牌已创建",sub:"仅显示这一次。请保存到你的程序的安全配置中。",body:'<textarea id="token-secret" class="token-secret" rows="4" readonly aria-label="新建的访问令牌"></textarea><div class="form-actions"><button class="btn ghost" id="token-copy">复制令牌</button><button class="btn primary" id="token-done">已保存，关闭</button></div><p class="hint">不要把令牌写进公开代码、聊天记录或网址。</p>'});
 $("#token-secret").value=token;$("#token-copy").onclick=guard(()=>copy($("#token-secret").value));$("#token-done").onclick=()=>modal.close();modal.addEventListener("close",()=>{const field=$("#token-secret");if(field)field.value="";modal.innerHTML="";},{once:true});token="";
}


/* ------------------------------------------------------------------ routing */

const ROUTES = [
    "start",
    "directory",
    "assistant",
    "chat",
    "api-access",
    "agents",
    "devices",
    "guide",
    "members",
];
// A fresh tab session opens on the overview even if the URL was saved on a connections page.
let entered = false;
function resolveRoute() {
    let r = location.hash.slice(1);
    if (!entered) {
        entered = true;
        if (!sessionStorage.getItem("portal-entered") && ["agents", "devices", "mine"].includes(r)) {
            r = "start";
            history.replaceState(null, "", "/#start");
        }
        sessionStorage.setItem("portal-entered", "1");
    }
    if (r === "mine") return "agents";
    return ROUTES.includes(r) && !(r === "members" && me?.role !== "admin")
        ? r
        : "start";
}
function go(next) {
    if (location.hash === "#" + next) return load().catch(showPageError);
    location.hash = next;
}
async function refreshWorkspace() {
    const [devices, own, shared] = await Promise.all([
        api("/api/devices"),
        api("/api/agents"),
        api("/api/directory"),
    ]);
    workspace = {
        devices: devices.devices,
        own: own.agents,
        shared: shared.agents,
    };
    syncedAt = new Date();
    const s = $("#sync-time");
    if (s) s.textContent = clock(syncedAt);
}
function showPageError(error) {
    if (error.status === 401) {
        me = null;
        stopRefresh();
        authPage("login");
        return;
    }
    if ($("#content")) {
        $("#content").innerHTML =
            pageHead("Error", "没加载出来", esc(error.message)) +
            `<div class="empty"><span class="seal big">!</span><p>再试一次，或者先看看帮助。</p><button class="btn primary" id="retry">重新加载</button></div>`;
        $("#retry").onclick = () => load().catch(showPageError);
    } else toast(error.message, "bad");
}
async function load(fetchFirst = true) {
    const epoch = ++pageEpoch,
        current = route;
    clearTimeout(assistantTimer);
    directStop();
    if (current === "chat") { await renderDirectChat(); return; }
    if (current === "api-access") { await renderAccessTokens(); return; }
    if (current === "guide") {
        renderGuide();
        return;
    }
    if (current === "assistant") {
        await renderAssistant();
        return;
    }
    if (current === "members") {
        await renderMembers();
        return;
    }
    if (fetchFirst) await refreshWorkspace();
    // The map plays its intro when someone navigates to it, not when a refresh redraws it.
    if (fetchFirst) mapState.intro = true;
    // A background refresh is silent: no entrance animation, same scroll position, and on the
    // home page the map stays untouched while only the cards below it are repainted.
    app.classList.toggle("quiet", !fetchFirst);
    const keep = window.scrollY,
        inPlace = !fetchFirst && current === "start" && !!$("#map-slot");
    if (current === "start") {
        if (inPlace) await refreshGraph();
        else graphData = await api("/api/graph").catch(() => graphData);
    }
    if (epoch !== pageEpoch || current !== route) return;
    if (current === "start") renderHome(inPlace);
    else if (current === "directory") renderDirectory();
    else renderMine();
    if (!fetchFirst) window.scrollTo({ top: keep, behavior: "instant" });
    const h1 = $("#content h1");
    if (h1 && document.activeElement === document.body)
        h1.focus({ preventScroll: true });
}
function stopRefresh() {
    clearInterval(refreshTimer);
    refreshTimer = null;
}
function beginRefresh() {
    stopRefresh();
    refreshTimer = setInterval(async () => {
        if (
            modal.open ||
            document.hidden ||
            ["INPUT", "TEXTAREA", "SELECT"].includes(
                document.activeElement?.tagName
            ) ||
            ["guide", "members", "assistant", "chat", "api-access"].includes(route)
        )
            return;
        const before = stable(workspace);
        try {
            if (route === "start") await refreshGraph();
            await refreshWorkspace();
            if (before !== stable(workspace)) await load(false);
        } catch (error) {
            if (error.status === 401) showPageError(error);
        }
    }, 10000);
}
async function start() {
    stopRefresh();
    try {
        me = await api("/api/me");
        route = resolveRoute();
        await load();
        beginRefresh();
    } catch (error) {
        if (error.status === 401) {
            me = null;
            authPage("login");
            return;
        }
        app.innerHTML = `<div class="solo">${brand()}<div class="solo-card"><h1>打不开</h1><p class="lead">${esc(error.message)}</p><button id="retry-start" class="btn primary">重新加载</button></div></div>`;
        $("#retry-start").onclick = () => start();
    }
}
async function navigate() {
    const h = location.hash.slice(1);
    if (modal.open) modal.close();
    try {
        if (!harnesses.length) harnesses = (await api("/api/config")).harnesses;
        if (h === "register" || h === "login") {
            stopRefresh();
            await authPage(h);
            return;
        }
        if (h.startsWith("setup=") || h.startsWith("invite=")) {
            await authPage(h.split("=")[0], h.slice(h.indexOf("=") + 1));
            return;
        }
        if (h.startsWith("connect=")) {
            await connectPage(h.slice(8));
            return;
        }
        if (h === "guide" && !me) {
            try {
                me = await api("/api/me");
            } catch (error) {
                if (error.status !== 401) throw error;
            }
            route = "guide";
            renderGuide(!me);
            if (me) beginRefresh();
            return;
        }
        if (!me) {
            await start();
            return;
        }
        route = resolveRoute();
        await load();
        beginRefresh();
        window.scrollTo({ top: 0 });
    } catch (error) {
        showPageError(error);
    }
}
window.addEventListener("hashchange", navigate);
navigate();
