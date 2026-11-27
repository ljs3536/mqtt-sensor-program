/*
 * 센서 현황 정보 카드의 '진동 경고 · … · 통계에서 확인 →'을 누르면 뜨는 간단 통계 창 (2026-11-27 요청).
 * - 통계 페이지로 가지 않고 그 MAC 한 개의 진동 통계만 평면도 위에 띄운다.
 *   카드 아래쪽 '통계 보기'는 지금처럼 통계 페이지로 가서 그 센서를 강조한다(역할 나눔).
 * - 값은 통계 페이지 자세히 보기와 같은 API: GET /api/db/dashboard?period=&mac=&bucket=hour (서버 계산 없음, 저장된 시간 줄만 읽음).
 *   센서 한 개·시간 줄만이라 LTE 데이터가 적다(24시간 ≈ 24줄).
 * - 보여 주는 것: 최근 한 시간 값(센서 현황 경보와 같은 기준) · 기간 평균 · 기간 최대,
 *   구간별 시간 수(양호·주의·경고·위험·기록 없음), 시간별 추이 그래프(구간 경계 점선), 통계 페이지 링크.
 * - 기간: 24시간 / 7일 / 30일. 점은 늘 한 시간(구간별 '몇 시간'을 정확히 세려고). 고른 기간은 localStorage('site-stat-period').
 * - Chart.js는 이 창을 처음 열 때만 불러온다(센서 현황 화면을 여는 것만으로는 받지 않음, LTE 절약).
 * - 닫기: ×, 바깥 누르기, ESC. ESC는 이 창만 닫고 뒤의 정보 카드는 그대로 둔다.
 * site.js가 window.SiteStat.open({ mac, kind, title })으로 연다. 이 파일이 없거나 실패하면 링크는 예전처럼 통계 페이지로 간다.
 */
(function () {
  const PERIOD_TEXT = { "24h": "24시간", "7d": "7일", "30d": "30일" };
  const PERIOD_DAYS = { "24h": 1, "7d": 7, "30d": 30 };
  const ZONE_NAME = { good: "양호", caution: "주의", warn: "경고", over: "위험" };
  const ZONE_VAR = { good: "--st-good", caution: "--st-caution", warn: "--st-warn", over: "--st-alarm", none: "--st-idle" };
  const KIND_NAME = { ADXL: "MEMS (Type2)", PIEZO: "PIEZO (Type1)" };

  let box = null; // 창(바깥 어두운 판 포함). 처음 열 때 만든다.
  let chart = null;
  let current = null; // { mac, kind, title, period }
  let seq = 0; // 늦게 온 응답이 새 요청을 덮지 않게 요청 번호를 센다.
  let chartLoading = null;
  let period = "24h";
  try {
    const saved = localStorage.getItem("site-stat-period");
    if (PERIOD_TEXT[saved]) period = saved;
  } catch (_) { /* 저장소를 못 쓰면 기본 24시간 */ }

  /* ---------- 작은 도우미 ---------- */
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  function num(value, digits) {
    const number = Number(value);
    return value != null && value !== "" && Number.isFinite(number) ? number.toFixed(digits) : "-";
  }
  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function zoneColor(level) {
    return css(ZONE_VAR[level || "none"]);
  }
  /* 진동 구간(서버 limits.zone_of·통계 페이지와 같은 경계 규칙: 경계값은 위 구간). */
  function zoneOf(value, zones) {
    if (value == null || !Number.isFinite(value)) return "";
    return value >= zones.limit ? "over" : value >= zones.caution ? "warn" : value >= zones.good ? "caution" : "good";
  }
  function zonesOf(item) {
    const limit = Number(item.limit) || 4.5;
    return item.zones || { good: limit * 1.4 / 4.5, caution: limit * 2.8 / 4.5, limit };
  }
  const toNumber = (value) => (value == null || value === "" ? null : Number(value));
  /* '2026-11-27T14:00:00' → '11-27 14시' */
  function pointLabel(t) {
    const text = String(t || "");
    return text.endsWith(":00:00") ? `${text.slice(5, 10)} ${text.slice(11, 13)}시` : text.slice(5, 16).replace("T", " ");
  }
  const shortTime = (t) => String(t || "").slice(5, 16).replace("T", " ");
  /* 세트 수로 가중한 평균(통계 페이지와 같은 방식). */
  function weightedMean(series, key) {
    let sum = 0, weight = 0;
    series.forEach((point) => {
      const value = Number(point[key]);
      const sets = Number(point.set_count) || 0;
      if (point[key] != null && Number.isFinite(value) && sets > 0) { sum += value * sets; weight += sets; }
    });
    return weight ? sum / weight : null;
  }
  /* 기간 안의 시간 수(서버와 같은 시작: 24시간은 지금 정시-23시간, 7일·30일은 오늘 0시에서 (일수-1)일 앞). */
  function expectedHours(key) {
    const now = new Date();
    const start = new Date(now);
    start.setMinutes(0, 0, 0);
    if (key === "24h") start.setHours(start.getHours() - 23);
    else { start.setHours(0); start.setDate(start.getDate() - (PERIOD_DAYS[key] - 1)); }
    return Math.floor((now - start) / 3600000) + 1;
  }

  /* Chart.js를 처음 한 번만 불러온다. 이미 있으면(다른 화면 스크립트) 그대로 쓴다. */
  function loadChart() {
    if (window.Chart) return Promise.resolve();
    if (!chartLoading) {
      chartLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "/static/chart.umd.min.js";
        script.onload = resolve;
        script.onerror = () => { chartLoading = null; reject(new Error("그래프 도구를 불러오지 못했습니다")); };
        document.head.appendChild(script);
      });
    }
    return chartLoading;
  }

  /* ---------- 창 만들기 ---------- */
  function build() {
    box = document.createElement("div");
    box.className = "stat-pop";
    box.hidden = true;
    box.innerHTML = `
      <div class="stat-box" role="dialog" aria-modal="true" aria-labelledby="statPopTitle">
        <div class="stat-head">
          <div class="stat-title"><strong id="statPopTitle"></strong><small class="stat-sub"></small></div>
          <button type="button" class="stat-close" aria-label="닫기">×</button>
        </div>
        <div class="stat-period" role="group" aria-label="기간">
          ${Object.entries(PERIOD_TEXT).map(([key, text]) => `<button type="button" data-period="${key}">${text}</button>`).join("")}
        </div>
        <div class="stat-body"></div>
        <div class="stat-foot">
          <small>통계는 매시 정각 뒤 자동 계산된 값입니다.</small>
          <a class="stat-more" href="/dashboard">통계 페이지에서 자세히 →</a>
        </div>
      </div>`;
    document.body.appendChild(box);
    box.addEventListener("click", (event) => {
      if (event.target === box || event.target.closest(".stat-close")) { close(); return; }
      const button = event.target.closest("[data-period]");
      if (button && current && button.dataset.period !== current.period) {
        period = current.period = button.dataset.period;
        try { localStorage.setItem("site-stat-period", period); } catch (_) { /* 기억만 못 할 뿐 */ }
        load();
        return;
      }
      // 통계 페이지로 갈 때는 '통계 보기'와 똑같이 그 센서를 강조하게 MAC을 넘긴다(통계 페이지가 한 번 읽고 지움).
      if (event.target.closest(".stat-more") && current) {
        try { sessionStorage.setItem("dashboard-focus", current.mac); } catch (_) { /* 강조만 빠짐 */ }
      }
    });
    // ESC: 이 창만 닫는다. 창(capture)에서 먼저 받아 멈춰야 site.js의 ESC(정보 카드 닫기)까지 가지 않는다.
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && box && !box.hidden) {
        event.stopPropagation();
        close();
      }
    }, true);
    // 헤더에서 테마를 바꾸면(theme.js) 색 변수가 바뀌므로 다시 그린다.
    window.addEventListener("sensor-theme", () => { if (current && current.item !== undefined) paint(); });
  }

  function open(info) {
    if (!box) build();
    current = { mac: info.mac, kind: info.kind, title: info.title || info.mac, period, item: undefined };
    box.querySelector("#statPopTitle").textContent = current.title;
    box.querySelector(".stat-sub").textContent = [KIND_NAME[info.kind] || "진동", info.mac].join(" · ");
    box.hidden = false;
    document.body.classList.add("stat-pop-open");
    load();
    box.querySelector(".stat-close").focus({ preventScroll: true });
  }

  function close() {
    if (!box) return;
    box.hidden = true;
    document.body.classList.remove("stat-pop-open");
    if (chart) { chart.destroy(); chart = null; }
    current = null;
    seq += 1; // 닫은 뒤 도착한 응답은 버린다
  }

  async function load() {
    const my = ++seq;
    const body = box.querySelector(".stat-body");
    box.querySelectorAll("[data-period]").forEach((button) => button.classList.toggle("active", button.dataset.period === current.period));
    body.innerHTML = '<p class="stat-msg">불러오는 중…</p>';
    try {
      const url = `/api/db/dashboard?period=${encodeURIComponent(current.period)}&mac=${encodeURIComponent(current.mac)}&bucket=hour`;
      const [response] = await Promise.all([fetch(url), loadChart()]);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "통계를 읽지 못했습니다");
      if (my !== seq || !current) return;
      const items = data.items || [];
      // 같은 MAC이 PIEZO·MEMS 두 줄일 수 있다(종류 변경 이력). 카드의 종류와 같은 줄, 없으면 첫 줄.
      current.item = items.find((item) => item.sensor_type === current.kind) || items[0] || null;
      paint();
    } catch (error) {
      if (my !== seq) return;
      body.innerHTML = `<p class="stat-msg">${esc(error.message || "통계를 읽지 못했습니다")}</p>`;
    }
  }

  /* ---------- 그리기 ---------- */
  function paint() {
    const body = box.querySelector(".stat-body");
    if (chart) { chart.destroy(); chart = null; }
    const item = current.item;
    if (!item) {
      body.innerHTML = '<p class="stat-msg">이 센서는 통계 화면에 나오지 않습니다.<br><small>설정 &gt; 장치에서 이름·표시를 확인하세요.</small></p>';
      return;
    }
    const zones = zonesOf(item);
    const piezo = item.sensor_type === "PIEZO";
    const series = item.series || [];
    const unitName = piezo ? "Vrms" : "SRSS";
    const latest = toNumber(item.latest_srss_mms);
    const latestLevel = zoneOf(latest, zones);
    const mean = weightedMean(series, "srss_mms");
    const meanLevel = zoneOf(mean, zones);
    // 기간 최대: 시간 안 최대 세트 값(srss_mms_max)이 있으면 그것, 없으면(PIEZO·옛 줄) 시간 평균의 최대.
    let peakValue = null, peakAt = "";
    series.forEach((point) => {
      const value = toNumber(point.srss_mms_max) ?? toNumber(point.srss_mms);
      if (value != null && Number.isFinite(value) && (peakValue == null || value > peakValue)) { peakValue = value; peakAt = point.t; }
    });
    const peakLevel = zoneOf(peakValue, zones);

    // 구간별 시간 수(시간 평균 기준). 기록 없는 시간 = 기간 시간 수 - 줄 수(수신 없음·계산 전).
    const counts = { good: 0, caution: 0, warn: 0, over: 0 };
    series.forEach((point) => {
      const level = zoneOf(toNumber(point.srss_mms), zones);
      if (level) counts[level] += 1;
    });
    const missing = Math.max(0, expectedHours(current.period) - series.length);
    const total = counts.good + counts.caution + counts.warn + counts.over + missing || 1;
    const seg = (level, count, name) => count
      ? `<i class="seg" style="width:${(count / total) * 100}%;background:${zoneColor(level)}" title="${name} ${count}시간"></i>` : "";
    const legend = (level, count, name) =>
      `<span class="${count ? "" : "zero"}"><i style="background:${zoneColor(level)}"></i>${name} <b>${count}</b></span>`;
    const tile = (title, value, level, sub) => `
      <div class="stat-tile">
        <span>${title}</span>
        <div><b style="color:${level ? zoneColor(level) : "inherit"}">${num(value, 2)}</b> <small>mm/s</small>${level ? ` <em class="zone-chip" style="--c:${zoneColor(level)}">${ZONE_NAME[level]}</em>` : ""}</div>
        <small>${sub}</small>
      </div>`;

    body.innerHTML = `
      <div class="stat-tiles">
        ${tile("최근 한 시간 (경보 기준)", latest, latestLevel, latest == null ? "값 없음" : esc(shortTime(item.latest_at)))}
        ${tile(`${PERIOD_TEXT[current.period]} 평균 ${unitName}`, mean, meanLevel, `${Number(item.set_count) || 0}세트`)}
        ${tile(`${PERIOD_TEXT[current.period]} 최대`, peakValue, peakLevel, peakAt ? esc(pointLabel(peakAt)) : "값 없음")}
      </div>
      <div class="stat-limits">기준(mm/s): 양호 &lt; ${num(zones.good, 2)} ≤ 주의 &lt; ${num(zones.caution, 2)} ≤ 경고 &lt; ${num(zones.limit, 2)} ≤ 위험</div>
      <div class="stat-zones">
        <div class="stat-zone-title">구간별 시간 <small>(한 시간 평균 기준)</small></div>
        <div class="stat-zonebar">${seg("good", counts.good, "양호")}${seg("caution", counts.caution, "주의")}${seg("warn", counts.warn, "경고")}${seg("over", counts.over, "위험")}${seg("none", missing, "기록 없음")}</div>
        <div class="stat-legend">${legend("good", counts.good, "양호")}${legend("caution", counts.caution, "주의")}${legend("warn", counts.warn, "경고")}${legend("over", counts.over, "위험")}${legend("none", missing, "기록 없음")}</div>
      </div>
      ${series.length
        ? `<div class="stat-chart"><canvas></canvas></div><p class="stat-note">점 = 한 시간 평균 ${unitName}${piezo ? "" : " · 옅은 점선 = 그 시간 안 최대"} · 색 점선 = 구간 경계</p>`
        : emptyHtml(item)}`;
    if (series.length) drawChart(body.querySelector("canvas"), item, zones, piezo, unitName);
  }

  /* 고른 기간에 시간 줄이 없을 때. 미수신 센서는 24시간이 비어 보이므로 마지막 통계 시각과 더 긴 기간 버튼을 준다. */
  function emptyHtml(item) {
    const text = `${PERIOD_TEXT[current.period]} 동안 계산된 통계가 없습니다.`;
    if (!item.has_stats) return `<p class="stat-msg">${text}<br><small>아직 통계 전입니다.</small></p>`;
    const longer = { "24h": "7d", "7d": "30d" }[current.period];
    return `<p class="stat-msg">${text}${item.latest_at ? `<br><small>마지막 통계 ${esc(shortTime(item.latest_at))}</small>` : ""}
      ${longer ? `<br><button type="button" class="stat-longer" data-period="${longer}">${PERIOD_TEXT[longer]} 보기</button>` : ""}</p>`;
  }

  function drawChart(canvas, item, zones, piezo, unitName) {
    const series = item.series;
    const values = series.map((point) => toNumber(point.srss_mms));
    const flat = (value) => series.map(() => value);
    const many = series.length > 48;
    const tick = css("--site-sub");
    const grid = css("--site-line");
    const main = css("--site-accent");
    const datasets = [{
      label: `${unitName} (시간 평균)`,
      data: values,
      borderColor: main,
      backgroundColor: "transparent",
      borderWidth: 2,
      tension: 0.25,
      // 점 색 = 그 시간의 구간 색. 점이 많으면(7일·30일) 점은 숨기고 선만.
      pointRadius: many ? 0 : 3,
      pointHoverRadius: 4,
      pointBackgroundColor: values.map((value) => zoneColor(zoneOf(value, zones))),
      pointBorderWidth: 0,
      spanGaps: false,
    }];
    if (!piezo) {
      datasets.push({ label: "시간 안 최대", data: series.map((point) => toNumber(point.srss_mms_max)), borderColor: main + "66",
        borderDash: [2, 3], borderWidth: 1, pointRadius: 0, fill: false, tension: 0.25 });
    }
    // 구간 경계. 낮은 센서는 위험 기준선까지 그리면 곡선이 납작해지므로 값의 1.6배를 넘는 경계는 빼고 그린다(위험 기준은 위 숫자로 보인다).
    const top = Math.max(0, ...values.filter((v) => v != null), ...series.map((p) => toNumber(p.srss_mms_max) || 0));
    [["양호 상한", zones.good, "caution"], ["주의 상한", zones.caution, "warn"], ["위험 기준", zones.limit, "over"]].forEach(([name, value, level]) => {
      if (value <= top * 1.6 || level === "caution") {
        datasets.push({ label: `${name} ${num(value, 2)}`, data: flat(value), borderColor: zoneColor(level), borderDash: [6, 4], borderWidth: 1.2, pointRadius: 0, fill: false });
      }
    });
    chart = new Chart(canvas, {
      type: "line",
      data: { labels: series.map((point) => pointLabel(point.t)), datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${num(ctx.raw, 3)} mm/s` } },
        },
        scales: {
          x: { ticks: { maxTicksLimit: 6, color: tick, font: { size: 10 }, maxRotation: 0 }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { maxTicksLimit: 5, color: tick, font: { size: 10 } }, grid: { color: grid },
            title: { display: true, text: "mm/s", color: tick, font: { size: 10 } } },
        },
      },
    });
  }

  window.SiteStat = { open, close };
})();
