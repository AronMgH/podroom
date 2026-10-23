// Podroom's sidebar, the same on every page: New podcast, the three places (Studio, Casts, Settings), your episodes,
// and until OpenRouter is connected, a line at the bottom offering its free AI models.
// A page puts a <header class="page-head"> first in its body and loads this script; the sidebar goes in front of it.
// The Studio sets window.podroomBusy while an episode plays, so leaving asks first instead of cutting the show off.
(() => {
  const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const ICONS = {
    studio: svg('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>'),
    casts: svg('<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><circle cx="17" cy="9" r="2.4"/><path d="M15.5 14.2A4.5 4.5 0 0 1 21 18.5"/>'),
    settings: svg('<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>'),
    episode: svg('<path d="M4 12h2M8 8v8M12 5v14M16 9v6M20 11v2"/>'),
    plus: svg('<path d="M12 5v14M5 12h14"/>'),
    menu: svg('<path d="M4 7h16M4 12h16M4 17h16"/>'),
    arrow: svg('<path d="M9 6l6 6-6 6"/>'),
  };
  const LOGO = `<span class="logo"><svg width="24" height="24" viewBox="6 6 52 52" aria-hidden="true" style="color:var(--accent-ink)">
    <path d="M12 54V30a20 20 0 0 1 40 0v24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" opacity=".28"/>
    <rect x="25" y="14" width="14" height="24" rx="7" fill="currentColor"/>
    <path d="M19 31a13 13 0 0 0 26 0M32 44v7M25 52h14" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round"/></svg></span>`;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const here = location.pathname === "/podcast" ? "/" : location.pathname;
  let active = new URLSearchParams(location.search).get("episode") || "";

  const item = (href, label, icon) =>
    `<a class="nav-item${here === href ? " on" : ""}" href="${href}"${here === href ? ' aria-current="page"' : ""}><span class="ic">${icon}</span>${label}</a>`;
  const nav = document.createElement("aside");
  nav.className = "side-nav";
  nav.setAttribute("aria-label", "Podroom");
  nav.innerHTML = `
    <a class="brand" href="/">${LOGO}Podroom</a>
    <div class="nav-new-wrap"><a class="nav-new" href="/?new=1">${ICONS.plus}New podcast</a></div>
    <nav class="nav-sec">
      <div class="nav-label">Main menu</div>
      ${item("/", "Studio", ICONS.studio)}${item("/casts", "Casts", ICONS.casts)}${item("/settings", "Settings", ICONS.settings)}
    </nav>
    <div class="nav-sec eps">
      <div class="nav-label">Episodes</div>
      <div class="ep-list" id="epList"><div class="ep-empty">Loading…</div></div>
    </div>
    <button type="button" class="nav-connect" id="navPromo" data-connect hidden>
      <span class="t"><b>Connect OpenRouter</b><small>Use free AI models</small></span>${ICONS.arrow}
    </button>
    <div class="nav-foot">Voices run on this computer</div>`;
  const scrim = document.createElement("div");
  scrim.className = "nav-scrim";
  document.body.prepend(nav, scrim);
  document.body.classList.add("shell");

  // Narrow windows: a menu button in the page's title bar opens the sidebar.
  const toggle = document.createElement("button");
  toggle.className = "nav-toggle"; toggle.type = "button"; toggle.title = "Menu"; toggle.setAttribute("aria-label", "Menu");
  toggle.innerHTML = ICONS.menu;
  toggle.onclick = () => document.body.classList.toggle("nav-open");
  scrim.onclick = () => document.body.classList.remove("nav-open");
  document.querySelector(".page-head")?.prepend(toggle);

  // Leaving the Studio stops a playing episode, so ask first.
  nav.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (a && window.podroomBusy?.() && !confirm("An episode is playing. Leaving the Studio stops it. Leave anyway?")) e.preventDefault();
  });

  const when = (at) => {
    const d = new Date((at || "").replace(" ", "T"));
    if (isNaN(d)) return "";
    const today = new Date(), yest = new Date(Date.now() - 864e5), hm = d.toTimeString().slice(0, 5);
    if (d.toDateString() === today.toDateString()) return `Today ${hm}`;
    if (d.toDateString() === yest.toDateString()) return `Yesterday ${hm}`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
  async function refresh() {
    const list = document.getElementById("epList");
    const eps = await fetch("/api/podcast/episodes").then((r) => r.json()).catch(() => null);
    if (!eps) { list.innerHTML = '<div class="ep-empty">Could not load your episodes.</div>'; return; }
    if (!eps.length) { list.innerHTML = '<div class="ep-empty">Your episodes appear here, ready to pick up again.</div>'; return; }
    list.innerHTML = eps.map((e) => `
      <a class="ep-item${e.id === active ? " on" : ""}" href="/?episode=${encodeURIComponent(e.id)}" title="${esc(e.topic || e.show)}">
        ${ICONS.episode}<span class="t"><b>${esc(e.topic || e.show)}</b><small>${esc(e.show)} · ${esc(when(e.updatedAt))}</small></span>
      </a>`).join("");
  }

  // Connect OpenRouter: your own browser opens openrouter.ai (a localhost link, so the desktop app hands it over),
  // and the server saves the key when you approve. This watches for that for ten minutes, then sends the page a
  // "podroom:connected" event with the new state, or with null when it gave up. Anything marked data-connect starts it.
  let connectedAt = 0, watch = null;
  function renderPromo(status) {
    connectedAt = status.openrouterConnectedAt;
    const or = status.providers.find((p) => p.id === "openrouter");
    document.getElementById("navPromo").hidden = status.fake || !or || or.keyConfigured;
  }
  function connect() {
    window.open(`http://localhost:${location.port || 80}/openrouter/connect`, "_blank");
    const since = connectedAt, until = Date.now() + 600000, btn = nav.querySelector(".nav-connect small");
    btn.textContent = "Finish in your browser…";
    clearInterval(watch);
    watch = setInterval(async () => {
      const st = await fetch("/api/podcast/state").then((r) => r.json()).catch(() => null);
      const done = !!st && st.status.openrouterConnectedAt !== since;
      if (!done && Date.now() < until) return;
      clearInterval(watch);
      btn.textContent = "Use free AI models";
      if (st) renderPromo(st.status);
      window.dispatchEvent(new CustomEvent("podroom:connected", { detail: done ? st : null }));
    }, 2000);
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest("[data-connect]")) return;
    e.preventDefault();
    connect();
  });
  fetch("/api/podcast/state").then((r) => r.json()).then((st) => renderPromo(st.status)).catch(() => {});

  // The Studio calls these when an episode starts, is saved or is opened.
  window.podroomShell = { refresh, setActive(id) { active = id || ""; refresh(); }, connect };
  refresh();
})();
