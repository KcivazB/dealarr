// dealarr — interactions légères (aucune donnée injectée via innerHTML).
"use strict";

const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

// Confirmation des actions destructives.
document.addEventListener("submit", (event) => {
  const message = event.target.dataset?.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

// Un seul menu "Modifier" ouvert à la fois.
document.addEventListener("toggle", (event) => {
  const el = event.target;
  if (el.matches?.("details.menu") && el.open) {
    document.querySelectorAll("details.menu[open]").forEach((other) => { if (other !== el) other.open = false; });
  }
}, true);

// Survol des graphiques : réticule + infobulle.
document.querySelectorAll(".chart").forEach((chart) => {
  const svg = chart.querySelector("svg");
  const tip = chart.querySelector(".tooltip");
  const xhair = svg.querySelector(".xhair");
  const xdot = svg.querySelector(".xdot");
  const show = (hit) => {
    const x = hit.dataset.x, y = hit.dataset.y;
    xhair.setAttribute("x1", x); xhair.setAttribute("x2", x);
    xdot.setAttribute("cx", x); xdot.setAttribute("cy", y);
    const vb = svg.viewBox.baseVal, box = svg.getBoundingClientRect();
    tip.textContent = hit.dataset.tip;
    tip.style.left = `${(x / vb.width) * box.width}px`;
    tip.style.top = `${(y / vb.height) * box.height}px`;
    tip.hidden = false;
    chart.classList.add("hovering");
  };
  svg.querySelectorAll(".hit").forEach((hit) => {
    hit.addEventListener("mouseenter", () => show(hit));
    hit.addEventListener("touchstart", () => show(hit), { passive: true });
  });
  svg.addEventListener("mouseleave", () => { tip.hidden = true; chart.classList.remove("hovering"); });
});

// Aperçu des prix avant ajout d'un article.
const probeBtn = document.getElementById("probe-btn");
if (probeBtn) {
  const status = document.getElementById("probe-status");
  const box = document.getElementById("probe-results");
  const tbody = box.querySelector("tbody");
  const nameInput = document.getElementById("name");

  const cell = (row, text, cls) => {
    const td = document.createElement("td");
    td.textContent = text ?? "";
    if (cls) td.className = cls;
    row.appendChild(td);
    return td;
  };

  probeBtn.addEventListener("click", async () => {
    const urls = document.getElementById("urls").value.split("\n").map((u) => u.trim()).filter(Boolean);
    if (!urls.length) { status.textContent = "Ajoute au moins une URL."; return; }
    probeBtn.disabled = true;
    status.textContent = `Lecture de ${urls.length} page(s)…`;
    try {
      const resp = await fetch("/api/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ urls }),
        credentials: "same-origin",
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      tbody.replaceChildren();
      let okCount = 0;
      for (const r of data.results) {
        const tr = document.createElement("tr");
        cell(tr, r.store || r.url);
        cell(tr, r.ok ? r.title : "");
        cell(tr, r.ok ? r.price : "—", "num");
        cell(tr, r.ok ? `${r.shipping} (${r.shipping_source})` : "—", "num");
        cell(tr, r.ok ? r.total : "—", "num strong");
        cell(tr, r.ok ? r.in_stock : "");
        const notes = [];
        if (r.error) notes.push(`⚠ ${r.error}`);
        if (r.already) notes.push(`déjà suivi (${r.already.join(", ")})`);
        cell(tr, notes.join(" · "), r.error ? "err-text" : "muted");
        tbody.appendChild(tr);
        if (r.ok) {
          okCount += 1;
          if (!nameInput.value && r.title) nameInput.value = r.title;
        }
      }
      box.hidden = false;
      status.textContent = `${okCount}/${data.results.length} page(s) lue(s).`;
    } catch (err) {
      status.textContent = `Erreur : ${err.message}`;
    } finally {
      probeBtn.disabled = false;
    }
  });
}

// Recherche : indique que ça prend quelques secondes.
const searchForm = document.getElementById("search-form");
if (searchForm) {
  searchForm.addEventListener("submit", () => {
    const btn = document.getElementById("search-btn");
    btn.disabled = true;
    document.getElementById("search-status").textContent = "Recherche dans les boutiques… (10 à 20 s)";
  });
}
