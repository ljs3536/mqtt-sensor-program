/*
 * 센서 현황(대문 /)과 평면도 배치(설정 /settings#plan)가 같이 쓰는 스크립트.
 * 계산은 서버(site_status.py)가 하고 여기서는 그리기와 배치 편집만 한다.
 *
 * 모드: <body data-site-mode="view|edit">
 * - view(대문): 보기 전용. 상태 타일, 평면도 탭, 아이콘, 센서 목록, 정보 카드. 15초마다 상태 갱신.
 * - edit(설정 '평면도 배치' 탭): 평면도 추가·교체·이름·순서·삭제, 끌어 놓기·이동·빼기, 저장/되돌리기.
 *   일반 사용자가 대문에서 실수로 배치를 바꾸지 않게 편집은 설정 화면으로 옮겼다(2026-11-18).
 *
 * 데이터
 * - GET /api/site/plans → {plans:[{id,name,width,height,image}], placements:[{plan_id,mac,x,y}]}
 *   평면도 한 장에 센서 여러 개, 센서 하나는 평면도 한 장에만(서버가 지킨다). x·y는 0~1 비율.
 * - GET /api/site/status → {receiver, sensors:[{state, state_text, link, alarm, ...}], counts, rules}
 *   state = 하나의 상태(경보 alarm > 끊김 lost > 주의 warn > 정상 ok > 대기 idle). 아이콘 색은 이것 하나다.
 * - 고른 평면도 탭은 이 브라우저에만 기억한다(localStorage 'site-plan').
 */
(() => {
  const MODE = document.body.dataset.siteMode === "edit" ? "edit" : "view";
  const EDIT = MODE === "edit";
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
  let activeId = null;        // 지금 탭의 평면도 id
  let placements = [];        // 서버에 저장된 위치 [{plan_id, mac, x, y}]
  let draft = null;           // 편집 중 위치(edit 모드에서만)
  let dirty = false;          // draft가 서버 값과 다른지
  let status = null;          // 마지막 상태 응답
  let focus = "";             // view: 상태 타일로 고른 상태(그 상태만 강조·목록 거르기)
  let openMac = "";           // 정보 카드가 열린 MAC
  let timer = null;

  const STATES = [
    ["alarm", "경보"],
    ["lost", "끊김"],
    ["warn", "주의"],
    ["ok", "정상"],
    ["idle", "대기"],
  ];
  const KIND_LABEL = { ADXL: "MEMS 진동", PIEZO: "PIEZO", ULTRASONIC: "초음파", "": "-" };
  // 센서 종류 기호(아이콘 안). 선 그림이라 글자색(currentColor)을 따른다.
  const KIND_ICON = {
    ADXL: '<path d="M4 12h3l2-6 3 12 3-9 2 3h3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    PIEZO: '<circle cx="12" cy="12" r="6" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="2" fill="currentColor"/>',
    ULTRASONIC: '<path d="M8 8a6 6 0 0 0 0 8M12 6a9 9 0 0 0 0 12M16 4a12 12 0 0 0 0 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    "": '<circle cx="12" cy="12" r="3" fill="currentColor"/>',
  };

  const esc = (text) =>
    String(text ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const on = (id, event, handler) => {
    const node = $(id);
    if (node) node.addEventListener(event, handler);
  };

  /** 경과 초 → "방금", "3분 전", "9일 전". */
  function ago(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "-";
    if (seconds < 60) return "방금";
    if (seconds < 3600) return `${Math.round(seconds / 60)}분 전`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}시간 전`;
    return `${Math.round(seconds / 86400)}일 전`;
  }
  /** 간격 초 → "30초", "5분", "1.0시간". */
  function span(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "-";
    if (seconds < 90) return `${Math.round(seconds)}초`;
    if (seconds < 5400) return `${Math.round(seconds / 60)}분`;
    return `${(seconds / 3600).toFixed(1)}시간`;
  }
  /** 목록·카드의 한 줄 글. 수신 정보만 쓴다(2026-11-18 결정).
   *  진동 수치는 대문에 글로 내지 않는다. 문제가 있으면 색(경보 빨강·주의 주황)으로 알리고,
   *  정확한 값은 통계 화면에서 본다. */
  function reason(s) {
    switch (s.link) {
      case "lost": return `${ago(s.age_s)}부터 수신 없음`;
      case "late": return `수신 지연 · 마지막 ${ago(s.age_s)}`;
      case "stopped": return "측정 중지됨";
      case "none": return "받은 데이터 없음";
      case "offline": return s.age_s == null ? "수신 꺼짐" : `수신 꺼짐 · 마지막 ${ago(s.age_s)}`;
      default: return s.age_s == null ? "-" : `${ago(s.age_s)} 수신`;
    }
  }
  /** 지금 상태가 이어진 시간 "3시간째". since(site_watch 기록)가 없으면 "". */
  function lasting(s) {
    if (!s.since) return "";
    const seconds = (Date.now() - new Date(s.since).getTime()) / 1000;
    if (!Number.isFinite(seconds) || seconds < 60) return "방금 바뀜";
    if (seconds < 3600) return `${Math.round(seconds / 60)}분째`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}시간째`;
    return `${Math.round(seconds / 86400)}일째`;
  }
  /** 배터리 표시(0~10, 3 이하 빨강). 값이 없으면 "". */
  function batteryHtml(s, withMax) {
    if (s.battery_level == null) return "";
    const low = s.battery_state === "low";
    return `<span class="battery${low ? " low" : ""}" title="배터리 ${s.battery_level} / 10${low ? " (부족)" : ""}">` +
      `<i style="width:${Math.max(0, Math.min(10, s.battery_level)) * 10}%"></i></span>` +
      `<span class="battery-num${low ? " low" : ""}">${esc(s.battery_level)}${withMax ? " / 10" : ""}</span>`;
  }
  const STATE_TEXT = Object.fromEntries(STATES);
  // 이름이 없으면 MAC 전체(뒤 몇 자리만 쓰면 312270처럼 겹치는 MAC이 많다).
  const label = (s) => s.name || s.mac;
  const sensors = () => (status && status.sensors) || [];
  const sensorBy = (mac) => sensors().find((s) => s.mac === mac);
  const current = () => draft || placements;
  const plan = () => plans.find((p) => p.id === activeId) || null;
  const planName = (id) => (plans.find((p) => p.id === id) || {}).name || "?";
  /** MAC → 놓인 평면도 id (edit면 draft 기준). */
  const placedOn = () => new Map(current().map((p) => [p.mac, p.plan_id]));
  // 고른 탭 기억. 사생활 보호 창 등에서 localStorage가 막혀도 화면은 동작한다.
  const remember = {
    get: () => { try { return Number(localStorage.getItem("site-plan")) || null; } catch (_) { return null; } },
    set: (id) => { try { localStorage.setItem("site-plan", String(id)); } catch (_) { /* 무시 */ } },
  };

  // ---------- 서버 읽기 ----------
  async function loadPlans(want) {
    const response = await fetch("/api/site/plans");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    applyPlans(data, want ?? remember.get(), true);
  }

  /** 평면도 응답 반영. want = 열고 싶은 탭 id. resetDraft면 편집 중 위치를 서버 값으로 다시 시작. */
  function applyPlans(data, want, resetDraft) {
    plans = data.plans || [];
    placements = data.placements || [];
    const ids = plans.map((p) => p.id);
    activeId = [want, activeId].find((id) => id != null && ids.includes(id)) ?? (ids[0] ?? null);
    if (EDIT) {
      if (resetDraft || !draft) {
        draft = placements.map((p) => ({ ...p }));
        setDirty(false);
      } else {
        draft = draft.filter((p) => ids.includes(p.plan_id)); // 지운 평면도 위의 draft는 버린다.
      }
    }
    if (activeId != null) remember.set(activeId);
    paintPlan();
  }

  async function loadStatus() {
    try {
      const response = await fetch("/api/site/status");
      const data = await readJson(response);
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      status = data;
      if ($("refreshText")) $("refreshText").textContent = `${new Date().toLocaleTimeString()} 갱신`;
    } catch (error) {
      if ($("refreshText")) $("refreshText").textContent = "상태를 읽지 못했습니다";
      return;
    }
    paintSummary();
    paintTabs();
    paintMarkers();
    paintList();
    paintEvents();
    if (openMac) showCard(openMac);
  }

  /** 15초마다 상태를 읽는다. 브라우저 탭이 안 보이면 멈추고, 다시 보이면 바로 한 번 읽는다. */
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
  function selectPlan(id) {
    if (id === activeId) return;
    activeId = id;
    remember.set(id);
    hideCard();
    resetView();
    paintPlan();
  }

  // ---------- 평면도 확대·이동 ----------
  // 평면도 상자(planStage)에 CSS transform(이동 + 배율)을 건다. 아이콘은 상자 안 %위치라 같이 움직이고,
  // 크기는 --zoom으로 거꾸로 줄여(site.css) 확대해도 같은 크기로 보인다.
  // ratioAt(끌어 놓기 위치 계산)은 getBoundingClientRect를 쓰므로 확대 중에도 그대로 맞다.
  // - 휠: 마우스 위치를 중심으로 확대·축소. 빈 곳을 끌기: 이동. 버튼: + − 맞춤.
  const ZOOM_MIN = 1;
  const ZOOM_MAX = 6;
  const view = { zoom: 1, x: 0, y: 0 };
  function applyView() {
    // 평면도가 자기 자리(배율 1의 상자)를 벗어나 빈 곳이 보이지 않게 이동을 자른다.
    const width = planStage.offsetWidth;
    const height = planStage.offsetHeight;
    view.x = Math.min(0, Math.max(width * (1 - view.zoom), view.x));
    view.y = Math.min(0, Math.max(height * (1 - view.zoom), view.y));
    planStage.style.transform = view.zoom === 1 ? "" : `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`;
    planStage.style.setProperty("--zoom", String(view.zoom));
    if ($("zoomText")) $("zoomText").textContent = `${Math.round(view.zoom * 100)}%`;
    if (openMac) showCard(openMac);
  }
  function resetView() {
    view.zoom = 1;
    view.x = 0;
    view.y = 0;
    applyView();
  }
  /** 화면 좌표(clientX, clientY)를 고정점으로 배율을 바꾼다. */
  function zoomAt(next, clientX, clientY) {
    const zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next));
    const box = planStage.parentElement.getBoundingClientRect();
    const origin = planStage.offsetLeft;
    const top = planStage.offsetTop;
    // 상자 원래 자리 기준 마우스 위치 → 그 점이 확대 뒤에도 같은 화면 위치에 남게 이동을 다시 잡는다.
    const px = clientX - box.left - origin;
    const py = clientY - box.top - top;
    view.x = px - ((px - view.x) / view.zoom) * zoom;
    view.y = py - ((py - view.y) / view.zoom) * zoom;
    view.zoom = zoom;
    applyView();
  }
  function zoomCenter(factor) {
    const box = planStage.getBoundingClientRect();
    zoomAt(view.zoom * factor, box.left + box.width / 2, box.top + box.height / 2);
  }
  planWrap.addEventListener("wheel", (event) => {
    if (!plan()) return;
    event.preventDefault();
    zoomAt(view.zoom * (event.deltaY < 0 ? 1.2 : 1 / 1.2), event.clientX, event.clientY);
  }, { passive: false });
  let pan = null; // {x, y, startX, startY, moved}
  planWrap.addEventListener("pointerdown", (event) => {
    // 아이콘·확대 버튼 위에서는 이동하지 않는다(아이콘 누르기·끌기가 먼저).
    if (event.button !== 0 || view.zoom === 1 || event.target.closest(".sensor-marker, .zoom-tools")) return;
    pan = { x: view.x, y: view.y, startX: event.clientX, startY: event.clientY };
    planWrap.setPointerCapture(event.pointerId);
    planWrap.classList.add("panning");
  });
  planWrap.addEventListener("pointermove", (event) => {
    if (!pan) return;
    view.x = pan.x + (event.clientX - pan.startX);
    view.y = pan.y + (event.clientY - pan.startY);
    applyView();
  });
  const endPan = () => {
    pan = null;
    planWrap.classList.remove("panning");
  };
  planWrap.addEventListener("pointerup", endPan);
  planWrap.addEventListener("pointercancel", endPan);
  on("zoomIn", "click", () => zoomCenter(1.5));
  on("zoomOut", "click", () => zoomCenter(1 / 1.5));
  on("zoomFit", "click", resetView);
  window.addEventListener("resize", applyView);

  function paintPlan() {
    const active = plan();
    planEmpty.hidden = !!active;
    planWrap.hidden = !active;
    if ($("planTools")) $("planTools").hidden = !active;
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

  /** view: 위쪽 상태 타일(상태별 개수, 누르면 그 상태만 강조)과 수신 꺼짐 안내. */
  function paintSummary() {
    if (EDIT || !status) return;
    const receiver = status.receiver || {};
    const banner = $("receiverBanner");
    banner.hidden = !!receiver.connected;
    banner.innerHTML = receiver.active
      ? "수신 서버에 연결하는 중입니다. 연결되기 전까지 센서 연결 상태는 '대기'로 보입니다."
      : '센서 데이터 수신이 꺼져 있어 연결 상태를 판단할 수 없습니다. <a href="/settings">설정</a>에서 수신 MQTT를 켜 주세요.';
    $("liveDot").className = "live-dot" + (receiver.connected ? " on" : "");
    $("liveDot").title = receiver.connected ? `수신 중 (${receiver.host}:${receiver.port})` : "수신 꺼짐";
    const counts = (status.counts && status.counts.state) || {};
    $("statusTiles").innerHTML = STATES
      .filter(([key]) => key !== "idle" || counts.idle) // 대기는 있을 때만
      .map(([key, text]) => {
        const count = counts[key] || 0;
        const cls = `status-tile state-${key}${focus === key ? " active" : ""}${count ? "" : " zero"}`;
        return `<button type="button" class="${cls}" data-state="${key}" aria-pressed="${focus === key}">
          <span class="tile-count">${count}</span><span class="tile-text">${text}</span></button>`;
      })
      .join("");
    const rules = status.rules || {};
    $("legendInfo").title =
      `경보: 진동(SRSS 전체 평균) ≥ 한계\n주의: 진동 ≥ 한계의 ${Math.round((rules.warn_ratio || 0.8) * 100)}%, 또는 수신이 늦음\n` +
      `끊김: 원래 수신 간격×${rules.late_factor}+${rules.grace_s}초보다 오래 수신 없음\n` +
      `대기: 수신 꺼짐·측정 중지·데이터 없음 등 판단할 수 없음\n` +
      "진동 값은 매일 자정 뒤 자동으로 계산되는 통계입니다.";
  }

  /** 평면도 탭. 탭마다 그 평면도의 문제 수(경보·끊김·주의) 배지. 보기 화면에서 한 장뿐이면 탭을 숨긴다. */
  function paintTabs() {
    const tabs = $("planTabs");
    tabs.hidden = plans.length === 0 || (!EDIT && plans.length === 1);
    tabs.innerHTML = plans
      .map((p) => {
        const here = current().filter((x) => x.plan_id === p.id).map((x) => sensorBy(x.mac)).filter(Boolean);
        const badges = ["alarm", "lost", "warn"]
          .map((key) => {
            const count = here.filter((s) => s.state === key).length;
            return count ? `<span class="tab-badge state-${key}" title="${STATES.find((x) => x[0] === key)[1]}">${count}</span>` : "";
          })
          .join("");
        const selected = p.id === activeId;
        return `<button type="button" role="tab" class="site-tab${selected ? " active" : ""}" aria-selected="${selected}" data-plan="${p.id}">` +
          `<span class="tab-name">${esc(p.name)}</span>${EDIT ? `<small>${here.length}</small>` : ""}${badges}</button>`;
      })
      .join("");
  }
  on("planTabs", "click", (event) => {
    const tab = event.target.closest(".site-tab");
    if (tab) selectPlan(Number(tab.dataset.plan));
  });

  const blank = (mac) => ({ mac, name: "", kind: "", state: "idle", state_text: "대기", link: "none", alarm: "na", age_s: null });

  function markerHtml(s, x, y) {
    const dim = focus && s.state !== focus ? " dim" : "";
    return `<div class="sensor-marker state-${s.state}${dim}${EDIT ? " editing" : ""}"
        data-mac="${esc(s.mac)}" style="left:${x * 100}%;top:${y * 100}%" title="${esc(label(s))} · ${esc(s.state_text)}">
      <span class="marker-dot"><svg viewBox="0 0 24 24" aria-hidden="true">${KIND_ICON[s.kind] || KIND_ICON[""]}</svg></span>
      ${s.battery_state === "low" ? '<span class="marker-battery" title="배터리 부족">!</span>' : ""}
      <span class="marker-label">${esc(label(s))}</span>
      ${EDIT ? `<button type="button" class="marker-remove" data-remove="${esc(s.mac)}" title="평면도에서 빼기">×</button>` : ""}
    </div>`;
  }

  function paintMarkers() {
    if (!plan()) {
      markerLayer.innerHTML = "";
      return;
    }
    markerLayer.innerHTML = current()
      .filter((p) => p.plan_id === activeId)
      .map((p) => markerHtml(sensorBy(p.mac) || blank(p.mac), p.x, p.y))
      .join("");
  }

  /** 오른쪽 센서 목록. view: 나쁜 상태부터(서버 순서), 타일로 거름. edit: 끌어 놓기 출발점. */
  function paintList() {
    const all = sensors();
    const placed = placedOn();
    let shown = all;
    if (EDIT) {
      const filter = $("listFilter") ? $("listFilter").value : "all";
      shown = all.filter((s) =>
        filter === "unplaced" ? !placed.has(s.mac) : filter === "here" ? placed.get(s.mac) === activeId : true,
      );
    } else if (focus) {
      shown = all.filter((s) => s.state === focus);
    }
    // 검색: 이름·MAC에 글자가 들어 있으면 남긴다(대소문자·공백 무시). 상태 타일·편집 거르기와 함께 쓴다.
    const query = ($("listSearch") ? $("listSearch").value : "").replace(/\s+/g, "").toLowerCase();
    if (query) shown = shown.filter((s) => `${s.name}${s.mac}`.replace(/\s+/g, "").toLowerCase().includes(query));
    if ($("listCount")) $("listCount").textContent = shown.length === all.length ? String(all.length) : `${shown.length}/${all.length}`;
    sensorList.innerHTML = shown.length
      ? shown
          .map((s) => {
            const where = placed.get(s.mac);
            const tag = where == null ? '<span class="list-where">평면도 없음</span>'
              : where === activeId ? "" : `<span class="list-where other">${esc(planName(where))}</span>`;
            const draggable = EDIT && plan() && where !== activeId ? " draggable" : "";
            const sub = EDIT ? `${esc(s.mac)} · ${KIND_LABEL[s.kind] || "-"}` : esc(reason(s));
            return `<li class="list-item state-${s.state}${draggable}" data-mac="${esc(s.mac)}">
              <span class="list-dot" title="${esc(s.state_text)}"></span>
              <span class="list-main"><strong>${esc(label(s))}</strong><small>${sub}</small></span>
              ${tag}${EDIT ? "" : `<span class="list-battery">${batteryHtml(s, false)}</span>`}
            </li>`;
          })
          .join("")
      : `<li class="list-empty">${emptyText(all.length)}</li>`;
  }
  /** 목록이 비었을 때 문구. 편집에서 '평면도 없음'이 비면 모두 놓였다는 뜻이다. */
  function emptyText(total) {
    if (!total) return "센서가 없습니다.";
    if ($("listSearch") && $("listSearch").value.trim()) return "검색과 맞는 센서가 없습니다.";
    if (focus) return "이 상태의 센서가 없습니다.";
    const filter = EDIT && $("listFilter") ? $("listFilter").value : "all";
    if (filter === "unplaced") return "모든 센서가 평면도에 놓여 있습니다. 위 선택을 '전체'로 바꾸면 옮길 수 있습니다.";
    if (filter === "here") return "이 평면도에 놓인 센서가 없습니다.";
    return "센서가 없습니다.";
  }

  // ---------- 최근 알림(상태 변화 기록, site_watch가 1분마다 남김) ----------
  const events = () => (status && status.events) || [];
  // 알림 '확인'은 서버에 남긴다(POST /api/site/events/ack). 한 사람이 확인하면 모든 화면에서 사라진다.
  // 기록은 지우지 않는다. '확인한 것도 보기'를 켜면 다시 보인다. 탭 배지 = 확인 안 한 경보·끊김·주의 수(서버 계산).
  let listTab = "sensors";
  let showAcked = false;
  const stamp = (text) => String(text || "").replace("T", " ").slice(5, 16);
  function eventHtml(e, withName) {
    const s = sensorBy(e.mac);
    const name = s ? label(s) : e.mac;
    // 목록(withName)에서만 '확인' 버튼. 이미 확인한 것은 흐리게.
    const ack = withName && !e.acked_at ? `<button type="button" class="event-ack" data-ack="${e.id}" title="확인(목록에서 숨김)">확인</button>` : "";
    return `<li class="event-item${e.acked_at ? " acked" : ""}" data-mac="${esc(e.mac)}">${ack}
      <span class="event-time">${esc(stamp(e.changed_at))}</span>
      ${withName ? `<strong class="event-name">${esc(name)}</strong>` : ""}
      <span class="event-change"><span class="state-${e.prev_state}"><i></i>${STATE_TEXT[e.prev_state] || e.prev_state}</span>
        → <span class="state-${e.state}"><i></i>${STATE_TEXT[e.state] || e.state}</span></span>
    </li>`;
  }
  /** 카드 아래 '최근 변화' 3개(이 MAC). 기록이 없으면 "". */
  function historyHtml(mac) {
    const mine = events().filter((e) => e.mac === mac).slice(0, 3);
    return mine.length ? `<div class="card-history"><small>최근 변화</small><ul>${mine.map((e) => eventHtml(e, false)).join("")}</ul></div>` : "";
  }
  function paintEvents() {
    const box = $("eventList");
    if (!box) return;
    const all = events();
    const list = showAcked ? all : all.filter((e) => !e.acked_at);
    const fresh = (status && status.unacked) || 0;
    const badge = $("eventBadge");
    badge.hidden = !fresh;
    badge.textContent = String(fresh);
    if ($("eventTools")) $("eventTools").hidden = listTab !== "events";
    if ($("ackAll")) $("ackAll").disabled = !all.some((e) => !e.acked_at);
    box.innerHTML = list.length
      ? list.map((e) => eventHtml(e, true)).join("")
      : `<li class="list-empty">${all.length ? "확인하지 않은 알림이 없습니다." : "아직 상태 변화 기록이 없습니다. 수신 중일 때 1분마다 확인해 바뀐 것만 남깁니다."}</li>`;
  }
  /** 알림 확인을 서버에 보내고 상태를 다시 읽는다. body = {ids:[...]} 또는 {all:true}. */
  async function ackEvents(body) {
    const { response, data } = await postJson("/api/site/events/ack", body);
    if (!response.ok) {
      alert(data.detail || "확인 처리 실패");
      return;
    }
    loadStatus();
  }
  on("ackAll", "click", () => {
    if (confirm("확인하지 않은 알림을 모두 확인 처리할까요? 목록에서 사라지고 기록은 남습니다.")) ackEvents({ all: true });
  });
  on("showAcked", "change", (event) => {
    showAcked = event.target.checked;
    paintEvents();
  });
  function showListTab(name) {
    listTab = name;
    document.querySelectorAll("[data-list-tab]").forEach((b) => b.classList.toggle("active", b.dataset.listTab === name));
    sensorList.hidden = name !== "sensors";
    if ($("listSearch")) $("listSearch").hidden = name !== "sensors"; // 검색은 센서 목록에만 쓴다
    if ($("eventList")) $("eventList").hidden = name !== "events";
    paintEvents();
  }
  document.querySelectorAll("[data-list-tab]").forEach((b) => b.addEventListener("click", () => showListTab(b.dataset.listTab)));
  on("eventList", "click", (event) => {
    const ackButton = event.target.closest("[data-ack]");
    if (ackButton) {
      ackEvents({ ids: [Number(ackButton.dataset.ack)] });
      return;
    }
    const item = event.target.closest(".event-item");
    if (!item) return;
    const where = placedOn().get(item.dataset.mac);
    if (where != null && where !== activeId) selectPlan(where);
    showCard(item.dataset.mac, where == null ? item : null);
  });

  // ---------- 정보 카드(view) ----------
  function showCard(mac, anchor) {
    const s = sensorBy(mac);
    if (!s || !card) {
      hideCard();
      return;
    }
    openMac = mac;
    // 진동 알람(경보·주의)이면 수치 대신 통계로 가는 안내만 둔다.
    // 통계 화면에서 이 MAC 줄을 강조해 연다. 주소에 MAC을 남기지 않으려고 링크에는 data-focus만 두고
    // 누를 때 sessionStorage('dashboard-focus')에 넣는다(아래 card click). 통계 화면이 한 번 읽고 지운다.
    const dashLink = "/dashboard";
    const vibration = s.alarm === "over" ? "진동이 한계를 넘었습니다"
      : s.alarm === "warn" ? "진동이 한계에 가깝습니다" : "";
    const interval = s.interval_s == null ? "모름" : `${span(s.interval_s)} (${esc(s.interval_source)})`;
    const config = s.config_type == null ? "-" : `SENSORTYPE ${esc(s.config_type)}${s.config_period != null ? ` · 주기 ${esc(s.config_period)}초` : ""}`;
    card.innerHTML = `
      <div class="card-head">
        <strong>${esc(label(s))}</strong>
        <button type="button" class="card-close" aria-label="닫기">×</button>
      </div>
      <div class="card-state state-${s.state}"><span class="state-chip">${esc(s.state_text)}${lasting(s) ? ` · ${lasting(s)}` : ""}</span>${esc(reason(s))}</div>
      ${vibration ? `<a class="card-alert state-${s.state}" href="${dashLink}" data-focus="${esc(s.mac)}">${vibration} · 통계에서 확인 →</a>` : ""}
      <dl>
        <dt>마지막 수신</dt><dd>${s.last_at ? `${ago(s.age_s)} <small>${esc(s.last_at.replace("T", " ").slice(0, 19))}</small>` : "-"}</dd>
        <dt>배터리</dt><dd>${batteryHtml(s, true) || "-"}${s.battery_state === "low" ? ' <b class="battery-warn">부족</b>' : ""}</dd>
      </dl>
      <details class="card-more">
        <summary>자세히</summary>
        <dl>
          <dt>MAC</dt><dd>${esc(s.mac)} · ${KIND_LABEL[s.kind] || "-"}</dd>
          <dt>연결</dt><dd>${esc(s.link_text)}</dd>
          <dt>원래 간격</dt><dd>${interval}</dd>
          <dt>설정</dt><dd>${config}</dd>
        </dl>
      </details>
      ${historyHtml(s.mac)}
      <div class="card-links"><a href="${dashLink}" data-focus="${esc(s.mac)}">통계 보기</a><a href="/studio">데이터 분석</a></div>`;
    card.hidden = false;
    // 아이콘 옆에 띄운다. 화면 밖으로 나가면 안쪽으로 당긴다.
    const box = (anchor || document.querySelector(`.sensor-marker[data-mac="${CSS.escape(mac)}"]`) || sensorList).getBoundingClientRect();
    const width = card.offsetWidth;
    const height = card.offsetHeight;
    let left = box.right + 10;
    if (left + width > window.innerWidth - 8) left = Math.max(8, box.left - width - 10);
    const top = Math.min(window.innerHeight - height - 8, Math.max(8, box.top - 10));
    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
  }
  function hideCard() {
    openMac = "";
    if (card) card.hidden = true;
  }
  if (card) {
    card.addEventListener("click", (event) => {
      if (event.target.closest(".card-close")) hideCard();
      const link = event.target.closest("[data-focus]");
      if (link) {
        try {
          sessionStorage.setItem("dashboard-focus", link.dataset.focus);
        } catch (_) {
          /* 저장이 막혀도 통계 화면은 열린다(강조만 빠짐) */
        }
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") hideCard();
    });
    window.addEventListener("scroll", () => openMac && showCard(openMac), { passive: true });
  }

  // ---------- view: 타일·목록 누르기 ----------
  on("statusTiles", "click", (event) => {
    const tile = event.target.closest(".status-tile");
    if (!tile) return;
    focus = focus === tile.dataset.state ? "" : tile.dataset.state;
    paintSummary();
    paintMarkers();
    paintList();
  });
  on("refreshNow", "click", loadStatus);
  sensorList.addEventListener("click", (event) => {
    const item = event.target.closest(".list-item");
    if (!item || EDIT) return;
    // 다른 평면도에 놓인 센서면 그 탭으로 옮긴 뒤 아이콘 옆에 카드를 띄운다.
    const where = placedOn().get(item.dataset.mac);
    if (where != null && where !== activeId) {
      selectPlan(where);
      showCard(item.dataset.mac);
      return;
    }
    showCard(item.dataset.mac, where === activeId ? null : item);
  });

  // ---------- 아이콘: view(누르면 카드) / edit(끌어 옮기기, 빼기) ----------
  /** 화면 좌표 → 평면도 비율(0~1). 평면도 밖이면 null. */
  function ratioAt(clientX, clientY) {
    const box = planStage.getBoundingClientRect();
    const x = (clientX - box.left) / box.width;
    const y = (clientY - box.top) / box.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return { x, y };
  }

  let drag = null; // {mac, fromList, node?, ghost?, last?}
  markerLayer.addEventListener("pointerdown", (event) => {
    const marker = event.target.closest(".sensor-marker");
    if (!marker) return;
    if (!EDIT) {
      showCard(marker.dataset.mac, marker);
      return;
    }
    if (event.target.closest(".marker-remove")) return;
    event.preventDefault();
    drag = { mac: marker.dataset.mac, fromList: false, node: marker };
    marker.setPointerCapture(event.pointerId);
    marker.classList.add("dragging");
  });
  markerLayer.addEventListener("click", (event) => {
    const remove = event.target.closest(".marker-remove");
    if (!remove || !EDIT) return;
    draft = draft.filter((p) => p.mac !== remove.dataset.remove);
    changed();
  });
  sensorList.addEventListener("pointerdown", (event) => {
    const item = event.target.closest(".list-item.draggable");
    if (!item || !EDIT || !plan()) return;
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
      changed();
    } else if (!drag.fromList) {
      paintMarkers();
    }
    drag = null;
  });

  // ---------- edit: 저장 / 되돌리기 ----------
  function setDirty(value) {
    dirty = value;
    if ($("dirtyText")) $("dirtyText").textContent = value ? "저장하지 않은 변경이 있습니다" : "";
    if ($("editSave")) $("editSave").disabled = !value;
    if ($("editCancel")) $("editCancel").disabled = !value;
  }
  function changed() {
    setDirty(true);
    paintTabs();
    paintMarkers();
    paintList();
  }
  on("listFilter", "change", paintList);
  on("listSearch", "input", paintList);
  on("editCancel", "click", () => {
    if (dirty && !confirm("저장하지 않은 배치 변경을 버릴까요?")) return;
    draft = placements.map((p) => ({ ...p }));
    setDirty(false);
    paintPlan();
  });
  on("editSave", "click", async () => {
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
    draft = placements.map((p) => ({ ...p }));
    setDirty(false);
    paintPlan();
  });
  if (EDIT) {
    window.addEventListener("beforeunload", (event) => {
      if (dirty) event.preventDefault();
    });
  }

  // ---------- edit: 평면도 추가·교체·이름·순서·삭제(바로 서버 반영) ----------
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
  on("planAdd", "click", () => {
    fileMode = "add";
    $("planFile").click();
  });
  on("planReplace", "click", () => {
    if (!plan()) return;
    fileMode = "replace";
    $("planFile").click();
  });
  on("planFile", "change", async (event) => {
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
    // 이미지 교체: 위치는 비율이라 새 이미지에서도 같은 자리. 추가: 새 탭을 연다. 편집 중 위치는 그대로.
    applyPlans(data, data.created ?? activeId, false);
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
    applyPlans(data, activeId, false);
  }
  on("planRename", "click", () => {
    const active = plan();
    if (!active) return;
    const name = prompt("평면도 이름", active.name);
    if (name != null && name.trim() && name.trim() !== active.name) patchPlan({ name });
  });
  on("planLeft", "click", () => patchPlan({ move: -1 }));
  on("planRight", "click", () => patchPlan({ move: 1 }));
  on("planDelete", "click", async () => {
    const active = plan();
    if (!active) return;
    const count = current().filter((p) => p.plan_id === active.id).length;
    if (!confirm(`'${active.name}' 평면도를 지울까요? 이 평면도의 센서 ${count}개는 '평면도 없음'으로 돌아갑니다.`)) return;
    const response = await fetch(`/api/site/plans/${active.id}`, { method: "DELETE" });
    const data = await readJson(response).catch((error) => ({ detail: error.message }));
    if (!response.ok) {
      alert(data.detail || "삭제 실패");
      return;
    }
    applyPlans(data, null, false);
  });

  // ---------- 시작 ----------
  loadPlans().catch((error) => alert("평면도를 읽지 못했습니다: " + error.message));
  loadStatus();
  schedule();
})();
