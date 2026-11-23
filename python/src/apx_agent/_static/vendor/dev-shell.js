// Dev shell: tab switcher, topology overlay, and workspace sidebar (#1052).
// The page sets data-default-tab to the shell's first tab.
const defaultSlug = document.body.getAttribute("data-default-tab");
    (function () {
      const frame = document.getElementById("dash-frame");
      const tabs = document.querySelectorAll(".tab");
      function selectTab(slug) {
        const btn = document.querySelector('.tab[data-tab="' + slug + '"]');
        if (!btn) return;
        tabs.forEach((t) => t.classList.remove("active"));
        btn.classList.add("active");
        if (frame.src.replace(location.origin, "") !== btn.dataset.src) {
          frame.src = btn.dataset.src;
        }
        history.replaceState(null, "", "#" + slug);
      }
      tabs.forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));
      window.addEventListener("hashchange", () => {
        const slug = (location.hash || "#" + defaultSlug).slice(1);
        selectTab(slug);
      });
      const initial = (location.hash || "#" + defaultSlug).slice(1);
      selectTab(initial);
      window._selectTab = selectTab;
      window._dashFrame = frame;
      // Sidebar toggle
      document.getElementById("btn-sidebar-toggle").addEventListener("click", () => {
        document.body.classList.toggle("sidebar-off");
      });
    })();
    (function () {
      const overlay = document.getElementById("topo-overlay");
      const openBtn = document.getElementById("topo-open");
      const closeBtn = document.getElementById("topo-close");
      const tframe = document.getElementById("topo-frame");
      let loaded = false;
      function open() {
        if (!loaded) { tframe.src = "/_apx/topology"; loaded = true; }
        overlay.classList.add("open");
      }
      function close() { overlay.classList.remove("open"); }
      openBtn.addEventListener("click", open);
      closeBtn.addEventListener("click", close);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && overlay.classList.contains("open")) close();
      });
    })();
    // ── Sidebar data ──
    function selectTable(fqn) {
      // Stay on Eval if it's active (it handles apx:table-selected directly).
      // For all other tabs, navigate to Chat first.
      const activeSlug = (location.hash || "#" + defaultSlug).slice(1);
      if (activeSlug !== "eval" && window._selectTab) window._selectTab("chat");
      const frame = window._dashFrame || document.getElementById("dash-frame");
      setTimeout(() => {
        frame.contentWindow.postMessage({type: "apx:table-selected", fqn}, "*");
      }, 150);
    }
    async function loadSidebar() {
      try {
        const d = await fetch("/_apx/workspace-context").then(r => r.json());
        const id = document.getElementById("sb-identity");
        if (id) {
          id.innerHTML = `
            <a class="sb-host" href="${d.host||'#'}" target="_blank">${(d.host||'').replace('https://','')}</a>
            <div class="sb-user">${d.user||''}</div>`;
        }
        loadCatalogTree(d.used_catalogs||[], d.used_schemas||[]);
        renderSidebarLinks(d);
      } catch(e) {
        const id = document.getElementById("sb-identity");
        if (id) id.innerHTML = '<div class="sb-user">Unavailable</div>';
      }
    }
    // Rows are built with textContent: declared names are never parsed as markup.
    function sbSection(id, title, rows) {
      const box = document.getElementById(id);
      if (!box || !rows.length) return;
      const head = document.createElement("div");
      head.className = "sb-section";
      head.textContent = title;
      box.replaceChildren(head, ...rows);
    }
    function sbRow(icon, label, href) {
      const row = document.createElement(href ? "a" : "div");
      row.className = "sb-item";
      if (href) { row.href = href; row.target = "_blank"; row.rel = "noopener"; }
      const ico = document.createElement("span");
      ico.className = "sb-icon";
      ico.textContent = icon;
      const text = document.createElement("span");
      text.className = "sb-label";
      text.textContent = label;
      text.title = label;
      row.append(ico, text);
      return row;
    }
    function renderSidebarLinks(d) {
      const rows = [];
      if (d.app_url) rows.push(sbRow("▣", "Databricks App ↗", d.app_url));
      if (d.experiment_url) rows.push(sbRow("◷", "MLflow experiment ↗", d.experiment_url));
      sbSection("sb-links", "Workspace", rows);
    }
    loadSidebar();
    function loadCatalogTree(usedCats, usedSchemas) {
      const tree = document.getElementById("cat-tree");
      if (!tree) return;
      if (!usedCats.length) {
        tree.innerHTML = '<div class="tree-loading">No UC resources declared</div>';
        return;
      }
      // Group schemas by catalog
      const bycat = {};
      for (const cs of usedSchemas) {
        const [cat, sch] = cs.split(".");
        if (!bycat[cat]) bycat[cat] = [];
        bycat[cat].push(sch);
      }
      tree.innerHTML = usedCats.map(c => {
        const schemas = (bycat[c] || []).map(s => `<div>
          <div class="tree-row tree-sch" onclick="toggleSchema(this,'${c}','${s}')">
            <span class="tree-chevron">›</span>
            <span class="tree-icon" style="font-size:10px">◫</span>
            <span class="tree-name">${s}</span>
          </div>
          <div class="tree-tables" style="display:none"></div>
        </div>`).join("");
        return `<div>
          <div class="tree-row tree-cat open" onclick="this.classList.toggle('open');this.nextElementSibling.style.display=this.classList.contains('open')?'block':'none'">
            <span class="tree-chevron">›</span>
            <span class="tree-icon">🗄</span>
            <span class="tree-name">${c}</span>
          </div>
          <div class="tree-schemas">${schemas}</div>
        </div>`;
      }).join("");
    }
    async function toggleSchema(rowEl, catalog, schema) {
      rowEl.classList.toggle("open");
      const tablesEl = rowEl.nextElementSibling;
      if (tablesEl.style.display === "none") {
        tablesEl.style.display = "block";
        if (!tablesEl.dataset.loaded) {
          tablesEl.dataset.loaded = "1";
          tablesEl.innerHTML = '<div class="tree-loading">Loading…</div>';
          try {
            const tables = await fetch(`/_apx/setup/tables?catalog=${catalog}&schema=${schema}`).then(r => r.json());
            tablesEl.innerHTML = tables.length
              ? tables.map(t => `<div class="tree-tbl" onclick="selectTable('${catalog}.${schema}.${t}')" title="Ask about ${t}"><span class="tree-icon" style="font-size:9px;color:var(--apx-border)">▦</span><span class="tree-name">${t}</span></div>`).join("")
              : '<div class="tree-loading">no tables</div>';
          } catch(e) { tablesEl.innerHTML = '<div class="tree-loading">Error</div>'; }
        }
      } else {
        tablesEl.style.display = "none";
      }
    }
