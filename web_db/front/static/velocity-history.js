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
  let plot = null;
  let busy = false;

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
    plot.options.plugins.tooltip.callbacks = {
      // 점 위에 올리면 세트 번호, packet_no, 원래 수신 시각을 보여 준다.
      title: (items) => {
        const row = items.length && items[0].raw && items[0].raw.row;
        return row ? `세트 ${row.set} | packet_no=${row.packet_no} | ${row.received_at}` : "";
      },
      label: (item) => `${item.dataset.label}: ${fixed(item.parsed.y)} mm/s`,
    };
    if (window.SensorTheme) window.SensorTheme.watch(plot);
    return plot;
  }

  /** 서버 결과를 차트, 표, 제외 목록에 넣는다. */
  function render(data) {
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
    GraphView.axes(chart, "X 축 수신 시각 (received_at)", "속도 RMS (mm/s)");
    chart.options.plugins.title = {
      display: true,
      text:
        `PSD 구간 N=${data.settings.segment} | Overlap=${data.settings.overlap}% | 목표 ${data.settings.target}구간` +
        ` | 장치 ${data.device || "-"}`,
    };
    if (typeof chart.resetZoom === "function") chart.resetZoom();
    chart.update();

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
