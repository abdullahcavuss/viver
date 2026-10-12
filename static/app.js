/* VIVER general page behavior */
(function () {
  "use strict";
  const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { } },
  };

  document.addEventListener("DOMContentLoaded", () => {
    // ---- author name (remembered in the browser)
    const author = store.get("viver.author", "");
    const ai = document.getElementById("author-name");
    if (ai) {
      ai.value = author;
      ai.addEventListener("input", () => { store.set("viver.author", ai.value); document.querySelectorAll("input[name=author]").forEach(i => i.value = ai.value); });
    }
    document.querySelectorAll("input[name=author]").forEach(i => { if (!i.value) i.value = author; });
    document.querySelectorAll("form[data-needs-author]").forEach(f => f.addEventListener("submit", ev => {
      const a = f.querySelector("input[name=author]");
      if (a && !a.value.trim()) {
        const v = prompt("Your name as it should appear in commits:", "");
        if (!v) { ev.preventDefault(); return; }
        a.value = v; store.set("viver.author", v); if (ai) ai.value = v;
      }
    }));

    // ---- copy buttons
    document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", ev => {
      ev.preventDefault();
      navigator.clipboard && navigator.clipboard.writeText(b.dataset.copy);
      const t = b.textContent; b.textContent = "✓ copied"; setTimeout(() => b.textContent = t, 1200);
    }));

    // ---- forms that ask for confirmation
    document.querySelectorAll("form[data-confirm]").forEach(f => f.addEventListener("submit", ev => { if (!confirm(f.dataset.confirm)) ev.preventDefault(); }));

    // ---- VI diff summaries (changes / commit / compare pages)
    const LEVEL_TXT = { functional: "Content changed", layout: "Visual/layout", saved: "Re-saved only" };
    const queue = [...document.querySelectorAll("[data-vi-summary]")];
    const tally = { functional: 0, layout: 0, saved: 0, error: 0 };
    let running = 0;
    function savedGroup(row) {
      const card = row.closest(".card");
      let g = card.querySelector(".saved-group");
      if (!g) {
        g = document.createElement("details");
        g.className = "saved-group";
        g.innerHTML = '<summary><span class="sg-title"></span><span class="faint small"> — content unchanged (revision / compiled code / internal numbers). Still included in the commit.</span></summary><div class="sg-body"></div>';
        card.appendChild(g);
      }
      return g;
    }
    function updateTally() {
      const t = document.querySelector("[data-vi-tally]");
      if (t) {
        const parts = [];
        if (tally.functional) parts.push(`<span class="lv functional">${tally.functional} VI content</span>`);
        if (tally.layout) parts.push(`<span class="lv layout">${tally.layout} VI visual/layout</span>`);
        if (tally.saved) parts.push(`<span class="lv saved">${tally.saved} VI re-saved only</span>`);
        if (tally.error) parts.push(`<span class="lv functional">${tally.error} VI unreadable</span>`);
        t.innerHTML = parts.join(" ");
      }
      document.querySelectorAll(".saved-group .sg-title").forEach(el => { const n = el.closest(".saved-group").querySelectorAll("[data-vi-row]").length; el.textContent = `${n} VI${n === 1 ? "" : "s"} re-saved only`; });
    }
    function render(elx, s) {
      const row = elx.closest("[data-vi-row]");
      if (s.error || (s.errors && s.errors.length && s.level !== "functional")) {
        const msg = s.error || s.errors.join(" | ");
        elx.innerHTML = `<span class="lv functional" title="${esc(msg)}">⚠ unreadable</span><span class="small faint" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(msg)}</span>`;
        tally.error++; updateTally(); return;
      }
      const lv = s.level || "functional";
      tally[lv]++;
      if (row) row.dataset.level = lv;
      if (lv === "saved") {
        if (row) row.dataset.cats = "saved";
        const ver = (s.parts || []).find(p => p.key === "version");
        elx.innerHTML = `<span class="lv saved">re-saved only</span>` + (ver ? `<span class="small faint">LabVIEW ${esc(ver.detail)}</span>` : "");
        if (row && row.dataset.viRow !== "keep") savedGroup(row).querySelector(".sg-body").appendChild(row);
        updateTally();
        applyFilter();
        return;
      }
      if (row) { row._parts = s.parts || []; row.dataset.cats = (s.cats || []).join(" "); }
      paintRow(elx, row, s.parts || [], lv);
      updateTally();
      applyFilter();
    }
    function paintRow(elx, row, parts, lv) {
      const on = p => p.key === "version" || isOn(p.cat || "other");
      let h = "";
      for (const p of parts) {
        if (!on(p)) continue;
        if (p.key === "version") { h += `<span class="part saved" title="LabVIEW version">LV ${esc(p.detail)}</span>`; continue; }
        h += `<span class="part ${p.level}" title="${esc(p.detail || p.label)}">${esc(p.label)}${p.detail && p.key === "bd" ? ` <b>${esc(p.detail.split(" · ")[0])}</b>` : ""}</span>`;
      }
      elx.innerHTML = h || `<span class="lv ${lv}">${LEVEL_TXT[lv]}</span>`;
      const det = parts.filter(p => p.detail && p.key !== "version" && on(p)).map(p => `<b>${esc(p.label)}:</b> ${esc(p.detail)}`).join(" · ");
      if (row) {
        let d = row.querySelector(".vi-detail");
        if (!d && det) { d = document.createElement("div"); d.className = "vi-detail"; row.appendChild(d); }
        if (d) { d.innerHTML = det; d.hidden = !det; }
      }
    }

    // ---- category filter
    const CATS = [["logic", "Block diagram logic"], ["bd_layout", "Block diagram layout"], ["fp", "Front panel"],
                  ["auto", "Timed Loop / XNode internal settings"],
                  ["other", "Other (icon, description, settings)"], ["saved", "Re-saved only"]];
    let FILTER = {};
    try { FILTER = JSON.parse(store.get("viver.filter", "{}")) || {}; } catch (e) { FILTER = {}; }
    // default: everything on, only "auto" (settings LabVIEW regenerates by itself) off
    const isOn = k => (k in FILTER) ? FILTER[k] !== false : k !== "auto";
    function applyFilter() {
      let hidden = 0;
      document.querySelectorAll("[data-vi-row]").forEach(row => {
        if (row.dataset.cats === undefined) return;
        const cats = row.dataset.cats.split(" ").filter(Boolean);
        const vis = !cats.length || cats.some(c => isOn(c));
        row.hidden = !vis;
        if (!vis) hidden++;
        const elx = row.querySelector("[data-vi-summary]");
        if (elx && row._parts && row.dataset.level !== "saved") paintRow(elx, row, row._parts, row.dataset.level);
      });
      document.querySelectorAll(".saved-group").forEach(g => {
        g.hidden = !isOn("saved");
        if (g.hidden) hidden += g.querySelectorAll("[data-vi-row]").length;
      });
      const info = document.querySelector("[data-filter-info]");
      if (info) {
        info.hidden = !hidden;
        const n = info.querySelector("b");
        if (n) n.textContent = hidden;
      }
    }
    document.querySelectorAll("[data-vi-filter]").forEach(box => {
      box.innerHTML = '<span class="small muted">Show:</span>' + CATS.map(([k, t]) =>
        `<label class="chip ${isOn(k) ? "on" : ""}"><input type="checkbox" data-cat="${k}" ${isOn(k) ? "checked" : ""} hidden>${t}</label>`).join("") +
        (box.dataset.viFilter === "commit" ? ' <span class="small muted" data-filter-info hidden>· <b>0</b> file(s) hidden <button type="button" class="btn sm" data-uncheck-hidden>Exclude hidden files from the commit</button></span>'
                                            : ' <span class="small muted" data-filter-info hidden>· <b>0</b> VI(s) hidden</span>');
      box.querySelectorAll("input[data-cat]").forEach(cb => cb.addEventListener("change", () => {
        FILTER[cb.dataset.cat] = cb.checked;
        cb.parentElement.classList.toggle("on", cb.checked);
        store.set("viver.filter", JSON.stringify(FILTER));
        applyFilter();
      }));
      const un = box.querySelector("[data-uncheck-hidden]");
      if (un) un.addEventListener("click", () => {
        document.querySelectorAll("[data-vi-row]").forEach(row => {
          const hiddenRow = row.hidden || (row.closest(".saved-group") && row.closest(".saved-group").hidden);
          if (hiddenRow) row.querySelectorAll("input[name=paths]").forEach(c => { c.checked = false; c.dispatchEvent(new Event("change")); });
        });
      });
    });

    function next() {
      while (running < 3 && queue.length) {
        const elx = queue.shift();
        running++;
        const p = new URLSearchParams({ old: elx.dataset.old || "", new: elx.dataset.new || "", name: elx.dataset.name || "x.vi" });
        fetch("/api/visummary?" + p).then(r => r.json()).then(s => render(elx, s))
          .catch(e => render(elx, { error: "server error" }))
          .finally(() => { running--; next(); });
      }
    }
    next();

    // ---- text diff toggle
    document.querySelectorAll("[data-textdiff]").forEach(btn => btn.addEventListener("click", async () => {
      const box = document.getElementById(btn.dataset.target);
      if (box.dataset.loaded) { box.hidden = !box.hidden; return; }
      box.hidden = false;
      box.innerHTML = '<div style="padding:12px"><span class="spinner"></span></div>';
      const r = await fetch(btn.dataset.textdiff).then(r => r.json());
      let h = '<table class="tdiff">';
      for (const row of r.rows) {
        if (row.t === "hunk") h += `<tr class="hunk"><td colspan="3">${esc(row.text)}</td></tr>`;
        else h += `<tr class="${row.t}"><td class="ln">${row.o || ""}</td><td class="ln">${row.n || ""}</td><td>${row.t === "add" ? "+" : row.t === "del" ? "−" : " "} ${esc(row.text)}</td></tr>`;
      }
      if (!r.rows.length) h += '<tr><td style="padding:10px" class="muted">No text differences.</td></tr>';
      box.innerHTML = h + "</table>";
      box.dataset.loaded = "1";
    }));

    // ---- working folder status: select all
    const all = document.getElementById("check-all");
    if (all) {
      const boxes = () => [...document.querySelectorAll("input[name=paths]")];
      const upd = () => {
        const n = boxes().filter(b => b.checked).length;
        const btn = document.getElementById("commit-btn");
        if (btn) { btn.disabled = n === 0; btn.querySelector("span").textContent = n === boxes().length ? "all changes" : n + (n === 1 ? " file" : " files"); }
        all.checked = n === boxes().length; all.indeterminate = n > 0 && n < boxes().length;
      };
      all.addEventListener("change", () => { boxes().forEach(b => b.checked = all.checked); upd(); });
      boxes().forEach(b => b.addEventListener("change", upd));
      upd();
    }

    // ---- upload page
    const up = document.getElementById("upload-form");
    if (up) initUpload(up);
  });

  function initUpload(form) {
    const drop = form.querySelector(".drop");
    const input = form.querySelector("input[type=file][webkitdirectory]");
    const zipIn = form.querySelector("input[name=zipfile]");
    const info = form.querySelector(".pick-info");
    let picked = [];
    const show = () => {
      if (!picked.length) { info.textContent = ""; return; }
      const total = picked.reduce((a, f) => a + f.file.size, 0);
      const vis = picked.filter(f => /\.(vi|ctl|vit|vim|ctt)$/i.test(f.path)).length;
      info.innerHTML = `<b>${picked.length}</b> file(s) (${vis} VI/control) · ${(total / 1048576).toFixed(1)} MB · root: <code>${esc(picked[0].path.split("/")[0])}</code>`;
    };
    input.addEventListener("change", () => { picked = [...input.files].map(f => ({ file: f, path: f.webkitRelativePath || f.name })); show(); });
    drop.addEventListener("click", () => input.click());
    drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", async e => {
      e.preventDefault(); drop.classList.remove("over");
      const items = [...e.dataTransfer.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
      picked = [];
      async function walk(entry, prefix) {
        if (entry.isFile) {
          await new Promise(res => entry.file(f => { picked.push({ file: f, path: prefix + f.name }); res(); }, res));
        } else if (entry.isDirectory) {
          const reader = entry.createReader();
          let batch;
          do {
            batch = await new Promise(res => reader.readEntries(res, () => res([])));
            for (const ch of batch) await walk(ch, prefix + entry.name + "/");
          } while (batch.length);
        }
      }
      for (const it of items) await walk(it, "");
      show();
    });
    form.addEventListener("submit", ev => {
      ev.preventDefault();
      const mode = form.querySelector("input[name=mode]:checked").value;
      const fd = new FormData();
      for (const n of ["message", "description", "author", "branch"]) { const i = form.querySelector(`[name=${n}]`); if (i) fd.append(n, i.value); }
      if (!fd.get("author")) { const v = prompt("Your name as it should appear in commits:"); if (!v) return; fd.set("author", v); store.set("viver.author", v); }
      fd.append("mode", mode);
      if (form.querySelector("[name=merge]").checked) fd.append("merge", "1");
      if (mode === "zip") {
        if (!zipIn.files.length) { alert("Select a ZIP file."); return; }
        fd.append("zipfile", zipIn.files[0]);
      } else {
        if (!picked.length) { alert("Select or drag the project folder."); return; }
        fd.append("strip_root", "1");
        for (const p of picked) { fd.append("files", p.file, p.file.name); fd.append("relpath", p.path); }
      }
      const bar = form.querySelector(".progress > div");
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      const xhr = new XMLHttpRequest();
      xhr.open("POST", form.action);
      xhr.setRequestHeader("X-Requested-With", "fetch");
      xhr.upload.onprogress = e => { if (e.lengthComputable) bar.style.width = (100 * e.loaded / e.total).toFixed(1) + "%"; };
      xhr.onload = () => {
        btn.disabled = false;
        let r = {};
        try { r = JSON.parse(xhr.responseText); } catch (e) { }
        if (xhr.status === 200 && r.url) location.href = r.url;
        else alert("Error: " + (r.error || xhr.statusText));
      };
      xhr.onerror = () => { btn.disabled = false; alert("Upload failed."); };
      xhr.send(fd);
    });
    form.querySelectorAll("input[name=mode]").forEach(r => r.addEventListener("change", () => {
      const z = form.querySelector("input[name=mode]:checked").value === "zip";
      form.querySelector(".mode-folder").hidden = z;
      form.querySelector(".mode-zip").hidden = !z;
    }));
  }
})();
