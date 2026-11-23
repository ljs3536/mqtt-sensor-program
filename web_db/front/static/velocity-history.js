/*
 * ADXL X/Y/Z 속도 RMS 시간 이력 창(발생기 v070 기능). /generator와 /studio가 같이 쓴다.
 *
 * 흐름
 * 1. 서랍의 'X/Y/Z 속도 RMS 시간 이력' 버튼 → 창을 열고 시작 세트를 지금 고른 세트로 맞춘 뒤 계산한다.
 * 2. POST /api/generator/velocity-history {start, count, options: viewOptions()}
 *    서버(graph_psd.velocity_history)가 세트마다 PSD로 X/Y/Z 속도 RMS와 SRSS를 구해 돌려준다.
 * 3. 그래프: x = 그 세트 X 축의 원래 수신 시각(received_at), y = mm/s RMS.
 *    X, Y, Z, SRSS 네 선과 한계값 점선. 한계를 넘은 SRSS 점은 빨간 점으로 그린다.
 * 4. 표: 세트별 값. 줄을 누르면 메인 그래프가 그 세트로 이동한다(move).
 * 5. 제외 세트: 축이 빠졌거나 received_at이 없거나 PSD 구간이 부족한 세트와 사유.
 * 6. (발생기 v073) 축별 허용치: X/Y/Z 한계값 + '표시' 체크. 체크한 것만 점선으로 그린다. 화면 전용이고
 *    장치 SRSS 한계(설정 화면)와 따로다. 값은 브라우저(localStorage 'velocity-axis-limits')에 기억.
 * 7. (발생기 v073) 분석 데이터 저장: POST /api/generator/velocity-export → ZIP(CSV 4개 + JSON) 내려받기.
 * 8. 툴팁: 점 근처에 세트·packet_no·수신 시각과 그 세트의 X/Y/Z·SRSS·SRSS 한계·축별 한계를 한 번에.
 *
 * 필요한 전역: postJson(api.js), GraphView(graph.js), viewOptions·num(generator-draw.js),
 * move(generator-api.js). 테마 색은 theme.js의 SensorTheme.watch로 입힌다.
 */
(() => {
  const modal = document.getElementById("historyModal");
  const openButton = document.getElementById("velocityHistory");
  if (!modal || !openButton) return;
  const startBox = document.getElementById("historyStart");
  const countBox = document.getElementById("historyCount");
  const note = document.getElementById("historyNote");
  const tableBody = modal.querySelector("#historyTable tbody");
  const skipList = document.getElementById("historySkipped");
  const skipCount = document.getElementById("historySkipCount");
  const lineColors = { X: "#1f77b4", Y: "#2ca02c", Z: "#d62728", SRSS: "#9467BD", limit: "#b45309" };
  // 축별 한계선 색(v073과 같음: 축 색보다 옅게)
  const limitColors = { X: "#5a98c4", Y: "#5eb56a", Z: "#e77a7a" };
  let plot = null;
  let busy = false;
  let lastData = null;

  // ---------- 축별 허용치(화면 전용) ----------
  const LIMIT_KEY = "velocity-axis-limits";
  const limitBoxes = {};
  ["X", "Y", "Z"].forEach((axis) => {
    limitBoxes[axis] = {
      value: document.getElementById(`historyLimit${axis}`),
      show: document.getElementById(`historyShow${axis}`),
    };
  });
  /** 저장해 둔 값을 입력칸에 채운다. 저장소를 못 쓰면(사생활 보호 창 등) 빈칸으로 둔다. */
  function loadLimits() {
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(LIMIT_KEY) || "{}") || {};
    } catch (_error) {
      saved = {};
    }
    Object.entries(limitBoxes).forEach(([axis, box]) => {
      if (!box.value || !box.show) return;
      box.value.value = (saved[axis] && saved[axis].value) || "";
      box.show.checked = !!(saved[axis] && saved[axis].show);
    });
  }
  function saveLimits() {
    const data = {};
    Object.entries(limitBoxes).forEach(([axis, box]) => {
      if (box.value && box.show) data[axis] = { value: box.value.value.trim(), show: box.show.checked };
    });
    try {
      localStorage.setItem(LIMIT_KEY, JSON.stringify(data));
    } catch (_error) {
      /* 저장 못 해도 화면은 그대로 동작 */
    }
  }
  /** 표시할 축별 한계 {X: 값, …}. 체크했고 0보다 큰 숫자인 것만. */
  function enabledLimits() {
    const out = {};
    Object.entries(limitBoxes).forEach(([axis, box]) => {
      if (!box.value || !box.show || !box.show.checked) return;
      const value = Number(box.value.value.trim());
      if (Number.isFinite(value) && value > 0) out[axis] = value;
    });
    return out;
  }
  /** 체크를 켰는데 값이 비었거나 잘못되면 체크를 끄고 알린다(v073 경고와 같음). */
  function checkLimit(axis, warn) {
    const box = limitBoxes[axis];
    const text = box.value.value.trim();
    const value = Number(text);
    if (box.show.checked && !(text && Number.isFinite(value) && value > 0)) {
      box.show.checked = false;
      if (warn) alert(text ? `${axis}축 한계값은 0보다 큰 숫자로 입력하세요.` : `${axis}축 한계값을 입력하세요.`);
    }
    saveLimits();
    paintLimits();
  }
  /** 축별 한계 점선만 다시 그린다(다시 계산하지 않음, 확대 상태 유지). */
  function paintLimits() {
    if (!plot || !lastData) return;
    const rows = lastData.rows || [];
    plot.data.datasets = plot.data.datasets.filter((set) => !set.$axisLimit);
    if (rows.length) {
      const first = rows[0].t;
      const last = rows[rows.length - 1].t;
      Object.entries(enabledLimits()).forEach(([axis, value]) => {
        plot.data.datasets.push({
          label: `${axis} 한계값 ${value}`,
          $axisLimit: axis,
          borderColor: limitColors[axis],
          borderDash: [7, 4],
          borderWidth: 2,
          pointRadius: 0,
          data: [{ x: first, y: value }, { x: last, y: value }],
        });
      });
    }
    plot.update("none");
  }
  loadLimits();
  Object.entries(limitBoxes).forEach(([axis, box]) => {
    if (!box.value || !box.show) return;
    box.show.addEventListener("change", () => checkLimit(axis, true));
    box.value.addEventListener("change", () => checkLimit(axis, false));
    box.value.addEventListener("keydown", (event) => {
      if (event.key === "Enter") checkLimit(axis, true);
    });
  });

  const pad = (value) => String(value).padStart(2, "0");
  /** 차트 눈금용 시각. 하루를 넘는 범위면 월-일도 붙인다. */
  function clock(ms, withDate) {
    const d = new Date(ms);
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    return withDate ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}` : time;
  }
  const fixed = (value) => (Number.isFinite(value) ? value.toFixed(6) : "-");
  const escapeText = (text) =>
    String(text).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));

  /** 처음 열 때 한 번 차트를 만든다. 기존 RAW/FFT와 같은 휠 확대·드래그 이동을 쓴다. */
  function ensurePlot() {
    if (plot) return plot;
    plot = GraphView.chart("historyChart");
    plot.options.elements.point.radius = 2.5;
    // 툴팁(v073 커서 옆 값 상자): 가장 가까운 점 하나를 기준으로 그 세트의 값을 한 번에 보여 준다.
    plot.options.plugins.tooltip.mode = "nearest";
    plot.options.plugins.tooltip.intersect = false;
    plot.options.plugins.tooltip.filter = (item, index) => index === 0;
    plot.options.plugins.tooltip.callbacks = {
      title: (items) => {
        const row = items.length && items[0].raw && items[0].raw.row;
        if (row) return `세트 ${row.set} · packet_no=${row.packet_no}`;
        return items.length ? items[0].dataset.label : "";
      },
      label: (item) => `현재: ${item.dataset.label} ${fixed(item.parsed.y)} mm/s RMS`,
      afterBody: (items) => {
        const row = items.length && items[0].raw && items[0].raw.row;
        if (!row) return [];
        const limits = enabledLimits();
        const axisText = Object.keys(limits).length
          ? Object.entries(limits).map(([axis, value]) => `${axis}=${value}`).join(", ")
          : "표시 안 함";
        return [
          `수신: ${row.received_at}`,
          `X ${fixed(row.x)}  |  Y ${fixed(row.y)} mm/s RMS`,
          `Z ${fixed(row.z)}  |  SRSS ${fixed(row.srss)} mm/s RMS`,
          `SRSS 한계값: ${lastData ? lastData.limit : "-"} mm/s RMS${row.over ? " (초과)" : ""}`,
          `축별 한계값: ${axisText}`,
        ];
      },
    };
    if (window.SensorTheme) window.SensorTheme.watch(plot);
    return plot;
  }

  /** 서버 결과를 차트, 표, 제외 목록에 넣는다. */
  function render(data) {
    lastData = data;
    const rows = data.rows || [];
    const chart = ensurePlot();
    const first = rows.length ? rows[0].t : 0;
    const last = rows.length ? rows[rows.length - 1].t : 0;
    const withDate = last - first > 24 * 3600 * 1000;
    const series = (key) => rows.map((row) => ({ x: row.t, y: row[key], row }));
    chart.data.datasets = [
      { label: "X", borderColor: lineColors.X, backgroundColor: lineColors.X, data: series("x") },
      { label: "Y", borderColor: lineColors.Y, backgroundColor: lineColors.Y, data: series("y") },
      { label: "Z", borderColor: lineColors.Z, backgroundColor: lineColors.Z, data: series("z") },
      {
        label: "SRSS",
        borderColor: lineColors.SRSS,
        borderWidth: 2,
        data: series("srss"),
        // 한계를 넘은 세트만 빨간 큰 점.
        pointBackgroundColor: rows.map((row) => (row.over ? "#dc2626" : lineColors.SRSS)),
        pointRadius: rows.map((row) => (row.over ? 4 : 2.5)),
      },
      {
        label: `한계값 ${data.limit} mm/s`,
        borderColor: lineColors.limit,
        borderDash: [6, 4],
        pointRadius: 0,
        data: rows.length ? [{ x: first, y: data.limit }, { x: last, y: data.limit }] : [],
      },
    ];
    chart.options.scales.x.type = "linear";
    chart.options.scales.y.type = "linear";
    chart.options.scales.x.ticks.callback = (value) => clock(value, withDate);
    // 고정 커서 값 상자도 X를 이 시각 글자로 보인다(graph.js pinCursor).
    chart.$pinTickText = true;
    GraphView.axes(chart, "수신 시각 (X축 received_at)", "속도 RMS (mm/s RMS)");
    chart.options.plugins.title = {
      display: true,
      text:
        `채널별 속도 RMS 시간 이력 | PSD 구간 N=${data.settings.segment} | Overlap=${data.settings.overlap}% | 목표 ${data.settings.target}구간` +
        ` | 장치 ${data.device || "-"}`,
    };
    if (typeof chart.resetZoom === "function") chart.resetZoom();
    paintLimits();

    tableBody.innerHTML = rows
      .map(
        (row) =>
          `<tr data-set="${row.set}" class="${row.over ? "over" : ""}">` +
          `<td>${row.set}</td><td>${escapeText(row.packet_no)}</td><td>${escapeText(row.received_at)}</td>` +
          `<td>${fixed(row.x)}</td><td>${fixed(row.y)}</td><td>${fixed(row.z)}</td><td>${fixed(row.srss)}</td>` +
          `<td>${row.over ? "초과" : "정상"}</td></tr>`,
      )
      .join("");
    const skipped = data.skipped || [];
    skipCount.textContent = skipped.length;
    skipList.innerHTML = skipped.map((text) => `<li>${escapeText(text)}</li>`).join("");
    const overCount = rows.filter((row) => row.over).length;
    note.textContent =
      `${data.start}번부터 ${data.count}세트 / 전체 ${data.total}세트 → 성공 ${rows.length}, 제외 ${skipped.length}` +
      (overCount ? `, 한계 초과 ${overCount}` : "");
    startBox.value = data.start;
  }

  /** 지금 입력값과 서랍 설정으로 서버에 다시 계산을 요청한다. 계산 중에는 겹쳐 보내지 않는다. */
  async function run() {
    if (busy) return;
    busy = true;
    note.textContent = "계산 중…";
    try {
      const count = countBox.value.trim();
      const { response, data } = await postJson("/api/generator/velocity-history", {
        start: num(startBox.value, 1),
        count: count ? num(count, null) : null,
        options: viewOptions(),
      });
      if (!response.ok) throw new Error(data.detail || "시간 이력 계산 실패");
      render(data);
    } catch (error) {
      note.textContent = "오류: " + error.message;
    } finally {
      busy = false;
    }
  }

  function close() {
    modal.hidden = true;
  }

  openButton.onclick = () => {
    // 시작 세트 기본값 = 메인 화면에서 지금 보고 있는 패킷 번호(v070과 같음).
    const current = document.querySelector("#index");
    startBox.value = (current && current.value) || "1";
    modal.hidden = false;
    run();
  };
  document.getElementById("historyRun").onclick = run;
  document.getElementById("historyReset").onclick = () => {
    if (plot && typeof plot.resetZoom === "function") plot.resetZoom();
  };
  document.getElementById("historyClose").onclick = close;

  /**
   * 분석 데이터 저장(발생기 v073). 지금 시작 세트·세트 수와 서랍 설정으로 서버가 ZIP을 만든다.
   * 데스크톱은 폴더를 골라 파일 5개를 쓰지만, 웹은 ZIP 하나를 내려받는다(안의 파일 이름·열은 같다).
   */
  const exportButton = document.getElementById("historyExport");
  if (exportButton) {
    exportButton.onclick = async () => {
      if (busy) return;
      busy = true;
      exportButton.disabled = true;
      note.textContent = "분석 데이터 계산·압축 중… (세트가 많으면 몇 초 걸립니다)";
      try {
        const count = countBox.value.trim();
        const response = await fetch("/api/generator/velocity-export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            start: num(startBox.value, 1),
            count: count ? num(count, null) : null,
            options: viewOptions(),
          }),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.detail || "분석 데이터 저장 실패");
        }
        const blob = await response.blob();
        const match = /filename="?([^";]+)"?/.exec(response.headers.get("Content-Disposition") || "");
        const name = match ? match[1] : "ADXL_analysis.zip";
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = name;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 10000);
        let summary = {};
        try {
          summary = JSON.parse(decodeURIComponent(response.headers.get("X-Export-Summary") || "{}"));
        } catch (_error) {
          summary = {};
        }
        const good = summary.good == null ? "-" : summary.good;
        const skipped = summary.skipped == null ? "-" : summary.skipped;
        note.textContent =
          `${name} 저장: 정상 ${good}세트 / 제외 ${skipped}세트 ` +
          "(가속도 시간 이력, 가속도·속도 PSD, 속도 RMS 이력 CSV + 설정 JSON)";
      } catch (error) {
        note.textContent = "오류: " + error.message;
      } finally {
        busy = false;
        exportButton.disabled = false;
      }
    };
  }
  // 바깥 어두운 영역을 누르거나 Esc를 누르면 닫는다.
  modal.addEventListener("click", (event) => {
    if (event.target === modal) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) close();
  });
  [startBox, countBox].forEach((box) =>
    box.addEventListener("keydown", (event) => {
      if (event.key === "Enter") run();
    }),
  );
  // 표의 줄을 누르면 메인 그래프를 그 세트로 옮긴다. ADXL 화면일 때만 의미가 있다.
  tableBody.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-set]");
    if (!row || typeof move !== "function") return;
    const sensor = document.querySelector("#sensor");
    if (sensor && sensor.value !== "ADXL") return;
    move(Number(row.dataset.set)).catch((error) => alert(error.message));
  });
})();
