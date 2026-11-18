/*
 * 대문(센서 현황) 화면. 계산은 서버(site_status.py)가 하고, 여기서는 그리기와 배치 편집만 한다.
 *
 * 데이터
 * - plans: GET /api/site/plans → {plans:[{id,name,width,height,image}], placements:[{plan_id,mac,x,y}]}
 *   평면도는 여러 장(탭). 평면도 한 장에 센서 여러 개, 센서 하나는 평면도 한 장에만(서버가 지킨다). x·y는 0~1 비율.
 * - status: GET /api/site/status → {receiver, sensors:[…], counts, rules}  (15초마다)
 * - 고른 탭은 이 브라우저에만 기억한다(localStorage 'site-plan'). 없거나 지워졌으면 첫 탭.
 *
 * 아이콘 모양
 * - 몸통 색 = 연결 상태(link): ok 초록, late 노랑, lost 진회색, stopped·none·unknown·offline 연회색
 * - 바깥 고리 = 알람(alarm): over 빨강(깜빡임), warn 주황. 통계 화면과 같은 기준(서버 판정).
 * - 글자 = 표시 이름(없으면 MAC 전체), 안쪽 기호 = 센서 종류(MEMS / PIEZO / 초음파)
 *
 * 편집
 * - '배치 편집'을 누르면 모든 평면도의 placements를 복사(draft)해서 고친다. '저장'에서만 서버에 보낸다. '취소'는 버린다.
 * - 목록 → 지금 탭의 평면도로 끌어 놓기(다른 평면도에 있던 센서면 이쪽으로 옮김), 아이콘 끌어 옮기기, ×로 빼기.
 * - 평면도 추가·이미지 교체·이름·순서·삭제는 바로 서버에 반영한다. draft(아직 저장 안 한 위치)는 그대로 둔다
 *   (지운 평면도 위의 draft만 같이 버린다).
 */
(() => {
  const REFRESH_MS = 15000;
  const $ = (id) => document.getElementById(id);
  const planEmpty = $("planEmpty");
  const planWrap = $("planWrap");
  const planStage = $("planStage");
  const planImage = $("planImage");
  const markerLayer = $("markerLayer");
  const sensorList = $("sensorList");
  const card = $("sensorCard");

  let plans = [];             // 서버 평면도 목록(탭 순서)
  let activeId = null;        // 지금 탭의 평면도 id(평면도가 없으면 null)
  let placements = [];        // 서버에 저장된 위치 [{plan_id, mac, x, y}]
  let draft = null;           // 편집 중 위치(편집 모드가 아니면 null)
  let status = null;          // 마지막 상태 응답
  let highlight = "";         // 요약 칩으로 고른 강조 상태
  let openMac = "";           // 정보 카드가 열린 MAC
  let timer = null;

  const LINK_ORDER = ["ok", "late", "lost", "stopped", "unknown", "none", "offline"];
  const KIND_LABEL = { ADXL: "MEMS", PIEZO: "PIEZO", ULTRASONIC: "초음파", "": "-" };
  // 센서 종류 기호(아이콘 안). 단순한 선 그림이라 테마 색(currentColor)을 따른다.
  const KIND_ICON = {
    ADXL: '<path d="M4 12h3l2-6 3 12 3-9 2 3h3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    PIEZO: '<circle cx="12" cy="12" r="6" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="2" fill="currentColor"/>',
    ULTRASONIC: '<path d="M8 8a6 6 0 0 0 0 8M12 6a9 9 0 0 0 0 12M16 4a12 12 0 0 0 0 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    "": '<circle cx="12" cy="12" r="3" fill="currentColor"/>',
  };

  const esc = (text) =>
    String(text ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  /** 경과 초 → "방금", "n분 전" 같은 글. */
  function ago(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "-";
    if (seconds < 10) return "방금";
    if (seconds < 60) return `${Math.round(seconds)}초 전`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}분 전`;
    if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}시간 전`;
    return `${Math.round(seconds / 86400)}일 전`;
  }
  /** 간격 초 → "30초", "5분". */
  function span(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "-";
    if (seconds < 90) return `${Math.round(seconds)}초`;
    if (seconds < 5400) return `${Math.round(seconds / 60)}분`;
    return `${(seconds / 3600).toFixed(1)}시간`;
  }
  // 이름이 없으면 MAC 전체(뒤 몇 자리만 쓰면 312270처럼 겹치는 MAC이 많다).
  const label = (s) => s.name || s.mac;
  const sensorBy = (mac) => ((status && status.sensors) || []).find((s) => s.mac === mac);
  const current = () => draft || placements;
  const plan = () => plans.find((p) => p.id === activeId) || null;
  const planName = (id) => (plans.find((p) => p.id === id) || {}).name || "?";
  /** MAC → 놓인 평면도 id (편집 중이면 draft 기준). */
  const placedOn = () => new Map(current().map((p) => [p.mac, p.plan_id]));
  // 고른 탭 기억. 사생활 보호 창 등에서 localStorage가 막혀도 화면은 동작한다.
  const remember = {
    get: () => { try { return Number(localStorage.getItem("site-plan")) || null; } catch (_) { return null; } },
    set: (id) => { try { localStorage.setItem("site-plan", String(id)); } catch (_) { /* 무시 */ } },
  };
  const isProblem = (s) => ["late", "lost", "offline"].includes(s.link) || ["over", "warn"].includes(s.alarm);

  // ---------- 서버 읽기 ----------
  async function loadPlans() {
    const response = await fetch("/api/site/plans");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    applyPlans(data, remember.get());
  }

  /** 평면도 응답 반영. want = 열고 싶은 탭 id(없거나 지워졌으면 지금 탭 → 첫 탭). */
  function applyPlans(data, want) {
    plans = data.plans || [];
    placements = data.placements || [];
    const ids = plans.map((p) => p.id);
    activeId = [want, activeId].find((id) => id != null && ids.includes(id)) ?? (ids[0] ?? null);
    if (draft) draft = draft.filter((p) => ids.includes(p.plan_id)); // 지운 평면도 위의 draft는 버린다.
    if (activeId != null) remember.set(activeId);
    paintPlan();
  }

  function selectPlan(id) {
    if (id === activeId) return;
    activeId = id;
    remember.set(id);
    hideCard();
    paintPlan();
  }

  async function loadStatus() {
    try {
      const response = await fetch("/api/site/status");
      const data = await readJson(response);
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      status = data;
      $("refreshText").textContent = `갱신 ${new Date().toLocaleTimeString()}`;
    } catch (error) {
      $("refreshText").textContent = "상태를 읽지 못했습니다: " + error.message;
      return;
    }
    paintSummary();
    paintTabs();
    paintMarkers();
    paintList();
    if (openMac) showCard(openMac);
  }

  /** 15초마다 상태를 읽는다. 탭이 안 보이면 멈추고, 다시 보이면 바로 한 번 읽는다. */
  function schedule() {
    clearInterval(timer);
    timer = null;
    if (document.hidden) return;
    timer = setInterval(loadStatus, REFRESH_MS);
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadStatus();
    schedule();
  });

  // ---------- 그리기 ----------
  function paintPlan() {
    const active = plan();
    planEmpty.hidden = !!active;
    planWrap.hidden = !active;
    $("planTools").hidden = !active;
    if (active) {
      if (planImage.getAttribute("src") !== active.image) planImage.src = active.image;
      // 이미지가 오기 전에도 자리 비율을 잡아 아이콘 위치가 튀지 않게 한다.
      planStage.style.aspectRatio = `${active.width} / ${active.height}`;
      // site.css가 이 비율로 '화면 높이 안에 들어오는 최대 폭'을 계산한다.
      planStage.style.setProperty("--plan-ratio", String(active.width / active.height));
    }
    paintTabs();
    paintMarkers();
    paintList();
  }

  /** 평면도 탭. 탭마다 그 평면도 위 센서 수와 문제 수(알람 초과·주의, 연결 지연·끊김)를 붙인다. */
  function paintTabs() {
    const tabs = $("planTabs");
    tabs.hidden = plans.length === 0;
    tabs.innerHTML = plans
      .map((p) => {
        const here = current().filter((x) => x.plan_id === p.id).map((x) => sensorBy(x.mac)).filter(Boolean);
        const link = here.filter((s) => ["late", "lost"].includes(s.link)).length;
        const over = here.filter((s) => s.alarm === "over").length;
        const warn = here.filter((s) => s.alarm === "warn").length;
        const badges =
          (over ? `<span class="tab-badge alarm-over" title="알람 초과">${over}</span>` : "") +
          (warn ? `<span class="tab-badge alarm-warn" title="주의">${warn}</span>` : "") +
          (link ? `<span class="tab-badge link-lost" title="지연·끊김">${link}</span>` : "");
        const selected = p.id === activeId;
        return `<button type="button" role="tab" class="site-tab${selected ? " active" : ""}" aria-selected="${selected}" data-plan="${p.id}">` +
          `<span class="tab-name">${esc(p.name)}</span><small>${here.length}</small>${badges}</button>`;
      })
      .join("");
  }
  $("planTabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".site-tab");
    if (tab) selectPlan(Number(tab.dataset.plan));
  });

  function paintSummary() {
    const receiver = status.receiver || {};
    const pill = $("receiverPill");
    pill.className = "pill " + (receiver.connected ? "on" : "off");
    pill.textContent = receiver.connected ? "수신 연결됨" : receiver.active ? "수신 연결 시도 중" : "수신 꺼짐";
    $("receiverText").innerHTML = receiver.connected || receiver.active
      ? `${esc(receiver.host)}:${esc(receiver.port)}${receiver.origin === "auto" ? " · 자동 연결" : ""}`
      : '센서 상태를 판정할 수 없습니다. <a href="/settings">설정</a>에서 자동 연결을 켜거나 <a href="/receiver">수신 모니터</a>에서 연결하세요.';
    const counts = status.counts || { link: {}, alarm: {} };
    const chips = [
      ["", `전체 ${counts.total || 0}`],
      ["link:ok", `정상 ${counts.link.ok || 0}`],
      ["link:late", `지연 ${counts.link.late || 0}`],
      ["link:lost", `끊김 ${counts.link.lost || 0}`],
      ["link:stopped", `중지 ${counts.link.stopped || 0}`],
      ["alarm:over", `알람 초과 ${counts.alarm.over || 0}`],
      ["alarm:warn", `주의 ${counts.alarm.warn || 0}`],
    ];
    $("countChips").innerHTML = chips
      .map(([key, text]) => {
        const [kind, value] = key.split(":");
        const tone = key ? `${kind}-${value}` : "all";
        return `<button type="button" class="count-chip tone-${tone}${highlight === key ? " active" : ""}" data-key="${key}">${text}</button>`;
      })
      .join("");
    const rules = status.rules || {};
    $("ruleText").textContent =
      `판정 기준(임시): 정상 = 마지막 수신 후 경과 ≤ 원래 간격×${rules.ok_factor}+${rules.grace_s}초, ` +
      `지연 ≤ ×${rules.late_factor}+${rules.grace_s}초, 그 밖은 끊김. 원래 간격 = 실제 수신 간격 중앙값(없으면 설정 주기). ` +
      `알람 = 쌓인 통계의 SRSS 전체 평균 ≥ 한계(초과), ≥ ${Math.round((rules.warn_ratio || 0.8) * 100)}%(주의). 통계는 매일 자정 뒤 자동으로 계산됩니다.`;
  }

  function markerHtml(s, x, y) {
    const editing = !!draft;
    const dim = highlight && !matches(s, highlight) ? " dim" : "";
    return `<div class="sensor-marker link-${s.link} alarm-${s.alarm}${dim}${editing ? " editing" : ""}"
        data-mac="${esc(s.mac)}" style="left:${x * 100}%;top:${y * 100}%" title="${esc(label(s))} · ${esc(s.link_text)} · ${esc(s.alarm_text)}">
      <span class="marker-dot"><svg viewBox="0 0 24 24" aria-hidden="true">${KIND_ICON[s.kind] || KIND_ICON[""]}</svg></span>
      <span class="marker-label">${esc(label(s))}</span>
      ${editing ? `<button type="button" class="marker-remove" data-remove="${esc(s.mac)}" title="평면도에서 빼기">×</button>` : ""}
    </div>`;
  }

  function matches(s, key) {
    if (!key) return true;
    const [kind, value] = key.split(":");
    return s[kind] === value;
  }

  function paintMarkers() {
    if (!plan()) {
      markerLayer.innerHTML = "";
      return;
    }
    markerLayer.innerHTML = current()
      .filter((p) => p.plan_id === activeId)
      .map((p) => {
        // 상태에 아직 없는 MAC(이름만 있다가 지워진 경우 등)도 자리는 보이게 빈 상태로 그린다.
        const s = sensorBy(p.mac) || { mac: p.mac, name: "", kind: "", link: "none", alarm: "na", link_text: "기록 없음", alarm_text: "" };
        return markerHtml(s, p.x, p.y);
      })
      .join("");
  }

  function paintList() {
    const sensors = (status && status.sensors) || [];
    const placed = placedOn();
    const filter = $("listFilter").value;
    const shown = sensors.filter((s) =>
      filter === "unplaced" ? !placed.has(s.mac)
        : filter === "here" ? placed.get(s.mac) === activeId
        : filter === "problem" ? isProblem(s) : true,
    );
    $("listCount").textContent = `${shown.length}/${sensors.length}`;
    sensorList.innerHTML = shown.length
      ? shown
          .map((s) => {
            // 어디에 놓였는지: 지금 평면도면 표시 없음, 다른 평면도면 그 이름, 없으면 '배치 안 됨'.
            const on = placed.get(s.mac);
            const where = on == null ? '<span class="list-unplaced">배치 안 됨</span>'
              : on === activeId ? "" : `<span class="list-unplaced list-other">${esc(planName(on))}</span>`;
            const srss = s.srss_mms == null ? "" : ` · SRSS ${s.srss_mms.toFixed(2)}/${s.limit}`;
            return `<li class="list-item link-${s.link} alarm-${s.alarm}${draft && plan() && on !== activeId ? " draggable" : ""}" data-mac="${esc(s.mac)}">
              <span class="list-dot"></span>
              <span class="list-main"><strong>${esc(label(s))}</strong> <small>${esc(s.mac)} · ${KIND_LABEL[s.kind] || "-"}</small>
                <small class="list-sub">${esc(s.link_text)} · ${ago(s.age_s)} · ${esc(s.alarm_text)}${srss}</small></span>
              ${where}
            </li>`;
          })
          .join("")
      : '<li class="hint">표시할 센서가 없습니다.</li>';
  }

  // ---------- 정보 카드 ----------
  function showCard(mac, anchor) {
    const s = sensorBy(mac);
    if (!s) {
      hideCard();
      return;
    }
    openMac = mac;
    const interval = s.interval_s == null ? "모름" : `${span(s.interval_s)} (${esc(s.interval_source)})`;
    const config = s.config_type == null ? "-" : `SENSORTYPE ${esc(s.config_type)}${s.config_period != null ? ` · 주기 ${esc(s.config_period)}초` : ""}`;
    const alarm = s.srss_mms == null
      ? (s.alarm === "na" ? "알람 대상 아님(SRSS 한계는 MEMS만)" : "통계 없음(매일 자정 뒤 자동 계산)")
      : `SRSS 평균 ${s.srss_mms.toFixed(3)} / 한계 ${s.limit} mm/s · ${s.set_count}세트`;
    card.innerHTML = `
      <div class="card-head link-${s.link} alarm-${s.alarm}">
        <span class="list-dot"></span><strong>${esc(label(s))}</strong>
        <button type="button" class="card-close" aria-label="닫기">×</button>
      </div>
      <dl>
        <dt>MAC</dt><dd>${esc(s.mac)} · ${KIND_LABEL[s.kind] || "-"}</dd>
        <dt>연결</dt><dd><b class="tag link-${s.link}">${esc(s.link_text)}</b> 마지막 수신 ${ago(s.age_s)}</dd>
        <dt>수신 시각</dt><dd>${esc(s.last_at || "-")}</dd>
        <dt>원래 간격</dt><dd>${interval}</dd>
        <dt>설정</dt><dd>${config}</dd>
        <dt>배터리</dt><dd>${esc(s.battery || "-")}</dd>
        <dt>알람</dt><dd><b class="tag alarm-${s.alarm}">${esc(s.alarm_text)}</b> ${alarm}</dd>
        <dt>마지막 통계</dt><dd>${esc(s.stat_last_at || "-")}</dd>
      </dl>
      <div class="card-links"><a href="/studio">센서 데이터 분석</a><a href="/dashboard">통계</a><a href="/receiver">수신 모니터</a></div>`;
    card.hidden = false;
    // 아이콘 옆에 띄운다. 화면 밖으로 나가면 안쪽으로 당긴다.
    const box = (anchor || document.querySelector(`.sensor-marker[data-mac="${CSS.escape(mac)}"]`) || sensorList).getBoundingClientRect();
    const width = card.offsetWidth;
    const height = card.offsetHeight;
    let left = box.right + 10;
    if (left + width > window.innerWidth - 8) left = Math.max(8, box.left - width - 10);
    let top = Math.min(window.innerHeight - height - 8, Math.max(8, box.top - 10));
    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
  }
  function hideCard() {
    openMac = "";
    card.hidden = true;
  }
  card.addEventListener("click", (event) => {
    if (event.target.closest(".card-close")) hideCard();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideCard();
  });
  window.addEventListener("scroll", () => openMac && showCard(openMac), { passive: true });

  // ---------- 요약 칩·목록 ----------
  $("countChips").addEventListener("click", (event) => {
    const chip = event.target.closest(".count-chip");
    if (!chip) return;
    highlight = highlight === chip.dataset.key ? "" : chip.dataset.key;
    paintSummary();
    paintMarkers();
  });
  $("listFilter").addEventListener("change", paintList);
  sensorList.addEventListener("click", (event) => {
    const item = event.target.closest(".list-item");
    if (!item || draft) return;
    // 다른 평면도에 놓인 센서면 그 탭으로 옮긴 뒤 아이콘 옆에 카드를 띄운다.
    const on = placedOn().get(item.dataset.mac);
    if (on != null && on !== activeId) {
      selectPlan(on);
      showCard(item.dataset.mac);
      return;
    }
    showCard(item.dataset.mac, item);
  });
  $("refreshNow").onclick = loadStatus;

  // ---------- 아이콘: 보기(누르면 카드) / 편집(끌어 옮기기, 빼기) ----------
  /** 화면 좌표 → 평면도 비율(0~1). 평면도 밖이면 null. */
  function ratioAt(clientX, clientY) {
    const box = planStage.getBoundingClientRect();
    const x = (clientX - box.left) / box.width;
    const y = (clientY - box.top) / box.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return { x, y };
  }

  let drag = null; // {mac, ghost?, fromList}
  markerLayer.addEventListener("pointerdown", (event) => {
    const remove = event.target.closest(".marker-remove");
    const marker = event.target.closest(".sensor-marker");
    if (!marker) return;
    if (!draft) {
      showCard(marker.dataset.mac, marker);
      return;
    }
    if (remove) return;
    event.preventDefault();
    drag = { mac: marker.dataset.mac, fromList: false, node: marker };
    marker.setPointerCapture(event.pointerId);
    marker.classList.add("dragging");
  });
  markerLayer.addEventListener("click", (event) => {
    const remove = event.target.closest(".marker-remove");
    if (!remove || !draft) return;
    draft = draft.filter((p) => p.mac !== remove.dataset.remove);
    markDirty();
  });
  sensorList.addEventListener("pointerdown", (event) => {
    const item = event.target.closest(".list-item.draggable");
    if (!item || !draft || !plan()) return;
    event.preventDefault();
    const ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    ghost.textContent = item.querySelector("strong").textContent;
    document.body.appendChild(ghost);
    drag = { mac: item.dataset.mac, fromList: true, ghost };
    moveGhost(event);
  });
  function moveGhost(event) {
    if (drag && drag.ghost) {
      drag.ghost.style.left = `${event.clientX}px`;
      drag.ghost.style.top = `${event.clientY}px`;
    }
  }
  document.addEventListener("pointermove", (event) => {
    if (!drag) return;
    if (drag.fromList) {
      moveGhost(event);
      return;
    }
    const at = ratioAt(event.clientX, event.clientY);
    if (at) {
      drag.node.style.left = `${at.x * 100}%`;
      drag.node.style.top = `${at.y * 100}%`;
      drag.last = at;
    }
  });
  document.addEventListener("pointerup", (event) => {
    if (!drag) return;
    const at = ratioAt(event.clientX, event.clientY) || (drag.fromList ? null : drag.last);
    if (drag.ghost) drag.ghost.remove();
    if (at) {
      // 센서 하나는 평면도 한 장에만: 다른 평면도에 있던 자리는 지우고 지금 평면도에 놓는다.
      draft = draft.filter((p) => p.mac !== drag.mac).concat([{ plan_id: activeId, mac: drag.mac, x: at.x, y: at.y }]);
      markDirty();
    } else if (!drag.fromList) {
      paintMarkers();
    }
    drag = null;
  });

  // ---------- 편집 모드 ----------
  let dirty = false;
  function markDirty() {
    dirty = true;
    $("dirtyText").textContent = "저장하지 않은 변경 있음";
    paintTabs();
    paintMarkers();
    paintList();
  }
  function setEditing(on) {
    draft = on ? current().map((p) => ({ ...p })) : null;
    dirty = false;
    $("dirtyText").textContent = "";
    $("editBar").hidden = !on;
    $("editToggle").textContent = on ? "편집 중" : "배치 편집";
    $("editToggle").disabled = on;
    document.body.classList.toggle("site-editing", on);
    if (on) {
      hideCard();
      $("listFilter").value = "unplaced";
    }
    paintTabs();
    paintMarkers();
    paintList();
  }
  $("editToggle").onclick = () => setEditing(true);
  $("editCancel").onclick = () => {
    if (dirty && !confirm("저장하지 않은 배치 변경을 버릴까요?")) return;
    setEditing(false);
  };
  $("editSave").onclick = async () => {
    if (!plans.length) {
      alert("평면도를 먼저 추가해 주세요.");
      return;
    }
    const { response, data } = await postJson("/api/site/placements", { items: draft });
    if (!response.ok) {
      alert(data.detail || "저장 실패");
      return;
    }
    placements = data.placements || [];
    setEditing(false);
  };
  window.addEventListener("beforeunload", (event) => {
    if (draft && dirty) event.preventDefault();
  });

  /** 이미지 원본 크기. SVG처럼 크기가 없으면 1600×1000으로 본다. */
  function imageSize(file) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        resolve({ width: img.naturalWidth || 1600, height: img.naturalHeight || 1000 });
        URL.revokeObjectURL(url);
      };
      img.onerror = () => {
        resolve({ width: 0, height: 0 });
        URL.revokeObjectURL(url);
      };
      img.src = url;
    });
  }
  // 파일 선택창 하나를 '평면도 추가'와 '이미지 교체'가 같이 쓴다.
  let fileMode = "add";
  $("planAdd").onclick = () => {
    fileMode = "add";
    $("planFile").click();
  };
  $("planReplace").onclick = () => {
    if (!plan()) return;
    fileMode = "replace";
    $("planFile").click();
  };
  $("planFile").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file) return;
    const size = await imageSize(file);
    if (!size.width) {
      alert("이미지를 읽지 못했습니다.");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    form.append("width", String(size.width));
    form.append("height", String(size.height));
    let url = `/api/site/plans/${activeId}/image`;
    if (fileMode === "add") {
      // 탭 이름. 기본은 파일 이름(확장자 뺌). 취소하면 올리지 않는다.
      const guess = file.name.replace(/\.[^.]+$/, "") || `평면도 ${plans.length + 1}`;
      const name = prompt("평면도 이름(탭에 보이는 글자)", guess);
      if (name == null || !name.trim()) return;
      form.append("name", name);
      url = "/api/site/plans";
    }
    const response = await fetch(url, { method: "POST", body: form });
    const data = await readJson(response).catch((error) => ({ detail: error.message }));
    if (!response.ok) {
      alert(data.detail || "평면도 올리기 실패");
      return;
    }
    // 이미지 교체: 위치는 비율이라 새 이미지에서도 같은 자리. 추가: 새 탭을 연다.
    applyPlans(data, data.created ?? activeId);
  });

  /** 이름 바꾸기·순서 옮기기(PATCH). 응답은 평면도 전체라 탭을 다시 그린다. */
  async function patchPlan(body) {
    if (!plan()) return;
    const response = await fetch(`/api/site/plans/${activeId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await readJson(response).catch((error) => ({ detail: error.message }));
    if (!response.ok) {
      alert(data.detail || "평면도 바꾸기 실패");
      return;
    }
    applyPlans(data, activeId);
  }
  $("planRename").onclick = () => {
    const active = plan();
    if (!active) return;
    const name = prompt("평면도 이름", active.name);
    if (name != null && name.trim() && name.trim() !== active.name) patchPlan({ name });
  };
  $("planLeft").onclick = () => patchPlan({ move: -1 });
  $("planRight").onclick = () => patchPlan({ move: 1 });
  $("planDelete").onclick = async () => {
    const active = plan();
    if (!active) return;
    const count = current().filter((p) => p.plan_id === active.id).length;
    if (!confirm(`'${active.name}' 평면도를 지울까요? 이 평면도의 센서 ${count}개는 '배치 안 됨'으로 돌아갑니다.`)) return;
    const response = await fetch(`/api/site/plans/${active.id}`, { method: "DELETE" });
    const data = await readJson(response).catch((error) => ({ detail: error.message }));
    if (!response.ok) {
      alert(data.detail || "삭제 실패");
      return;
    }
    applyPlans(data, null);
  };

  // ---------- 시작 ----------
  loadPlans().catch((error) => alert("평면도를 읽지 못했습니다: " + error.message));
  loadStatus();
  schedule();
})();
