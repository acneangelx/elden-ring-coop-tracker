/* Live Seamless Co-op (.co2) Sync Engine */
(function () {
  const PREFS_KEY = "er-coop-sync-prefs-v1";
  let prefs = { file: "", slot: 0, enabled: true };
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "null");
    if (saved && typeof saved === "object") prefs = { ...prefs, ...saved };
  } catch (e) {}

  function saveCoopPrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) {}
  }

  let autoFound = {};          // marker id -> true (graces + bosses from .co2)
  let autoQuests = {};         // questId -> { npc_status, direct:{}, inferred:{}, current_step }
  let latestState = null;
  let prevGraces = null;
  let prevBosses = null;
  let prevQuestSteps = null;
  let sse = null;
  let playerMarker = null;

  const markerById = {};
  if (typeof MARKERS !== "undefined") {
    MARKERS.forEach(m => { markerById[m.id] = m; });
  }

  window.COOP_AUTO = {
    isMarkerFound: id => !!autoFound[id],
    isStepDone: (qid, n) => {
      const q = autoQuests[qid];
      if (!q) return false;
      const k = String(n);
      return !!(q.direct?.[k] || q.inferred?.[k]);
    },
    getStepReason: (qid, n) => {
      const q = autoQuests[qid];
      if (!q) return null;
      const k = String(n);
      if (q.direct?.[k]) return { kind: "direct", text: q.direct[k] };
      if (q.inferred?.[k]) return { kind: "inferred", text: q.inferred[k] };
      return null;
    },
    getNpcStatus: qid => autoQuests[qid]?.npc_status || "unknown",
  };

  function showToast(html, onClick) {
    const wrap = document.getElementById("coopToasts");
    if (!wrap) return;
    const el = document.createElement("div");
    el.className = "coop-toast";
    el.innerHTML = html;
    if (onClick) el.onclick = () => { onClick(); el.remove(); };
    wrap.appendChild(el);
    setTimeout(() => { if (el.parentNode) el.remove(); }, 6500);
  }

  function updatePlayerMarker() {
    if (typeof map === "undefined") return;
    if (playerMarker) {
      map.removeLayer(playerMarker);
      playerMarker = null;
    }
    const p = latestState?.player_pos;
    if (!p || p.in_dungeon || p.px == null || p.py == null) return;
    if (p.master !== activeBase) return;
    playerMarker = L.marker(xy(p.px, p.py), {
      icon: L.divIcon({
        html: `<div class="player-beacon" title="Вы здесь">★</div>`,
        className: "",
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      }),
      zIndexOffset: 9500,
    }).addTo(map);
    const ch = latestState.character;
    playerMarker.bindPopup(
      `<div class="pop-cat">Позиция в .co2</div>` +
      `<div class="pop-t">${ch?.name || "Персонаж"} (Ур. ${ch?.level ?? "?"})</div>` +
      `<div class="pop-d">Карта: ${p.map_code} · px:${p.px}, py:${p.py}</div>`
    );
  }

  function flyToPlayer() {
    const p = latestState?.player_pos;
    if (!p) return;
    if (p.in_dungeon || p.px == null) {
      showToast(`<span>🧭 Персонаж сейчас в подземелье (<code>${p.map_code}</code>) — точные координаты карты доступны на открытой местности.</span>`);
      return;
    }
    if (p.master !== activeBase) switchBase(p.master);
    updatePlayerMarker();
    map.flyTo(xy(p.px, p.py), Math.max(map.getZoom(), NZ - 1), { duration: 0.6 });
    if (playerMarker) setTimeout(() => playerMarker.openPopup(), 420);
  }

  function flyToMarker(id) {
    const m = markerById[id];
    if (!m) return;
    if (m.master !== activeBase) switchBase(m.master);
    if (!catState[m.cat]) {
      catState[m.cat] = true;
      const row = document.querySelector(`.cat[data-cat="${m.cat}"]`);
      if (row) row.classList.remove("off");
      rebuildCluster();
    }
    const ref = markerRefs.find(r => r.data.id === id);
    if (ref) {
      if (typeof UNCLUSTERED_CATS !== "undefined" && UNCLUSTERED_CATS[ref.cat.id]) {
        map.flyTo(ref.marker.getLatLng(), Math.max(map.getZoom(), NZ - 1), { duration: 0.45 });
        setTimeout(() => ref.marker.openPopup(), 320);
      } else {
        cluster.zoomToShowLayer(ref.marker, () => ref.marker.openPopup());
      }
    }
  }

  function applyState(st) {
    latestState = st;
    const dot = document.getElementById("coopDot");
    const title = document.getElementById("coopStatusText");
    const meta = document.getElementById("coopMeta");
    const worldEl = document.getElementById("coopWorld");

    if (!st || !st.ok) {
      if (dot) dot.className = "coop-dot err";
      if (title) title.textContent = st?.error || "Ошибка чтения .co2";
      return;
    }

    if (dot) dot.className = "coop-dot live";
    const ch = st.character || {};
    const nowStr = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (title) title.textContent = `Live: ${ch.name || "Герой"} (Ур. ${ch.level ?? "?"})`;
    if (meta) {
      meta.innerHTML =
        `<span>🔥 <b>${st.counts.graces}</b>/${st.counts.graces_total} · 💀 <b>${st.counts.bosses}</b>/${st.counts.bosses_total}</span>` +
        `<span>⏱ ${nowStr}</span>`;
    }

    if (worldEl && st.world_state) {
      const ws = st.world_state;
      const tags = [
        { on: ws.radahn_defeated, label: "☄ Радан повержен" },
        { on: ws.study_hall_inverted, label: "⌛ Башня перевёрнута" },
        { on: ws.erdtree_burning, label: "🔥 Древо горит" },
        { on: ws.frenzied_flame, label: "👁 Яростное пламя" },
      ].filter(t => t.on);
      worldEl.innerHTML = tags.map(t => `<span class="coop-tag on">${t.label}</span>`).join("");
    }

    // Populate character slots
    const slotSel = document.getElementById("coopSlotSel");
    if (slotSel && Array.isArray(st.slots)) {
      const sig = st.slots.map(s => `${s.slot}:${s.name}:${s.level}`).join("|");
      if (slotSel.dataset.sig !== sig) {
        slotSel.dataset.sig = sig;
        slotSel.innerHTML = st.slots.map(s =>
          `<option value="${s.slot}" ${s.slot === st.slot ? "selected" : ""}>Слот ${s.slot + 1}: ${s.name} (Ур. ${s.level})</option>`
        ).join("");
      } else {
        slotSel.value = String(st.slot);
      }
    }

    // Detect diffs for toast notifications (skip very first load)
    const nextGraces = new Set(st.graces || []);
    const nextBosses = new Set(st.bosses || []);

    if (prevGraces !== null) {
      for (const gid of nextGraces) {
        if (!prevGraces.has(gid)) {
          const m = markerById[gid];
          showToast(
            `<span>🔥 Открыта благодать: <b>${m ? m.name : gid}</b></span><small>Показать</small>`,
            () => flyToMarker(gid)
          );
        }
      }
    }
    if (prevBosses !== null) {
      for (const bid of nextBosses) {
        if (!prevBosses.has(bid)) {
          const m = markerById[bid];
          showToast(
            `<span>💀 Босс повержен: <b>${m ? m.name : bid}</b></span><small>Показать</small>`,
            () => flyToMarker(bid)
          );
        }
      }
    }
    if (prevQuestSteps !== null && st.quests) {
      for (const [qid, qval] of Object.entries(st.quests)) {
        const oldSet = prevQuestSteps[qid] || new Set();
        const newDone = Object.keys(qval.direct || {});
        for (const sn of newDone) {
          if (!oldSet.has(sn)) {
            const qObj = (typeof QL !== "undefined") ? QL.find(x => x.id === qid) : null;
            const qName = qObj ? qObj.name : qid;
            showToast(
              `<span>📜 Квест <b>${qName}</b>: шаг ${sn}</span><small>Открыть</small>`,
              () => { if (qObj && typeof openQuestPanel === "function") openQuestPanel(qObj); }
            );
          }
        }
      }
    }

    prevGraces = nextGraces;
    prevBosses = nextBosses;
    prevQuestSteps = {};
    if (st.quests) {
      for (const [qid, qval] of Object.entries(st.quests)) {
        prevQuestSteps[qid] = new Set(Object.keys(qval.direct || {}));
      }
    }

    // Apply autoFound & autoQuests to map UI
    const nextAuto = {};
    for (const gid of nextGraces) nextAuto[gid] = true;
    for (const bid of nextBosses) nextAuto[bid] = true;
    autoFound = nextAuto;
    autoQuests = st.quests || {};

    if (typeof markerRefs !== "undefined") {
      markerRefs.forEach(r => {
        if (!r.isRegion) {
          const on = !!(found[r.data.id] || autoFound[r.data.id]);
          r.marker.setIcon(divIcon(r.cat, on));
        }
      });
    }
    if (typeof refreshCounts === "function") refreshCounts();
    if (typeof rebuildCluster === "function") rebuildCluster();
    if (typeof QL !== "undefined") {
      QL.forEach(q => {
        if (typeof updateQuestRow === "function") updateQuestRow(q);
      });
      if (typeof renderActiveQuests === "function") renderActiveQuests();
      if (typeof panelQuest !== "undefined" && panelQuest && typeof fillPanel === "function") {
        fillPanel(panelQuest);
      }
    }
    updatePlayerMarker();
  }

  async function loadSavesList() {
    try {
      const res = await fetch("/api/saves");
      const data = await res.json();
      const sel = document.getElementById("coopFileSel");
      if (!sel || !Array.isArray(data.saves)) return;
      if (!data.saves.length) {
        sel.innerHTML = `<option value="">Сохранения .co2 не найдены</option>`;
        return;
      }
      if (!prefs.file || !data.saves.some(s => s.path === prefs.file)) {
        prefs.file = data.saves[0].path;
        saveCoopPrefs();
      }
      sel.innerHTML = data.saves.map(s => {
        const tag = s.is_coop ? ".co2" : ".sl2";
        const bkp = s.is_backup ? " [Backup]" : "";
        return `<option value="${s.path}" ${s.path === prefs.file ? "selected" : ""}>${s.name} (${tag}${bkp})</option>`;
      }).join("");
    } catch (e) {}
  }

  function connectSSE() {
    if (sse) {
      sse.close();
      sse = null;
    }
    if (!prefs.enabled) {
      const dot = document.getElementById("coopDot");
      const txt = document.getElementById("coopStatusText");
      if (dot) dot.className = "coop-dot";
      if (txt) txt.textContent = "Автообновление на паузе";
      return;
    }
    const q = new URLSearchParams({
      file: prefs.file || "",
      slot: String(prefs.slot || 0),
    });
    sse = new EventSource(`/api/events?${q.toString()}`);
    sse.addEventListener("state", ev => {
      try {
        const st = JSON.parse(ev.data);
        applyState(st);
      } catch (e) {}
    });
    sse.onerror = () => {
      const dot = document.getElementById("coopDot");
      if (dot) dot.className = "coop-dot err";
    };
  }

  async function refreshNow() {
    const q = new URLSearchParams({
      file: prefs.file || "",
      slot: String(prefs.slot || 0),
    });
    try {
      const res = await fetch(`/api/state?${q.toString()}`);
      const st = await res.json();
      applyState(st);
    } catch (e) {}
  }

  function initUI() {
    const box = document.getElementById("coopBox");
    if (!box) return;

    const toggleBtn = document.getElementById("coopToggleBtn");
    const fileSel = document.getElementById("coopFileSel");
    const slotSel = document.getElementById("coopSlotSel");
    const syncNowBtn = document.getElementById("coopSyncNow");
    const locateBtn = document.getElementById("coopLocate");
    const gracesOnlyBtn = document.getElementById("coopGracesOnly");

    function renderToggle() {
      toggleBtn.classList.toggle("on", prefs.enabled);
      toggleBtn.textContent = prefs.enabled ? "Авто: ВКЛ" : "Авто: ВЫКЛ";
    }
    renderToggle();

    toggleBtn.onclick = () => {
      prefs.enabled = !prefs.enabled;
      saveCoopPrefs();
      renderToggle();
      connectSSE();
    };
    fileSel.onchange = () => {
      prefs.file = fileSel.value;
      prefs.slot = 0;
      prevGraces = null; prevBosses = null; prevQuestSteps = null;
      saveCoopPrefs();
      connectSSE();
    };
    slotSel.onchange = () => {
      prefs.slot = parseInt(slotSel.value, 10) || 0;
      prevGraces = null; prevBosses = null; prevQuestSteps = null;
      saveCoopPrefs();
      connectSSE();
    };
    syncNowBtn.onclick = () => refreshNow();
    locateBtn.onclick = () => flyToPlayer();
    gracesOnlyBtn.onclick = () => {
      const onlyGraces = gracesOnlyBtn.classList.toggle("active");
      CATEGORIES.forEach(c => {
        const keep = onlyGraces ? (c.id === "grace" || c.id === "region") : true;
        catState[c.id] = keep;
        const row = document.querySelector(`.cat[data-cat="${c.id}"]`);
        if (row) row.classList.toggle("off", !keep);
      });
      rebuildCluster();
    };

    // Re-draw player marker whenever base map changes
    window._onBaseSwitch = () => updatePlayerMarker();

    loadSavesList().then(() => connectSSE());
  }

  window.addEventListener("DOMContentLoaded", initUI);
})();
