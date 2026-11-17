/* 수신 모니터의 그래프 옵션과 그리기. 서버 호출은 monitor-api.js가 한다. */
if (window.ChartZoom) Chart.register(window.ChartZoom);

const colors = { X: "#1f77b4", Y: "#2ca02c", Z: "#d62728", PIEZO: "#1377a6", ULTRASONIC: "#c05b13", H: "#c05b13" };
const selectedMacs = new Set();
let profiles = { ADXL: [], PIEZO: [] };
let graphDevice = "";
let ready = false;

/** 필터 한 줄의 체크와 값 입력을 만든다. */
function filterMarkup(items) {
  return items.map(([name, freq, extra]) => `
    <div class="row" data-filter="${name}">
      <strong>${name}</strong>
      <label class="check"><input class="raw" type="checkbox"><span>RAW</span></label>
      <label class="check"><input class="fft" type="checkbox"><span>FFT</span></label>
      <label>값<input class="freq" value="${freq}" style="width:80px"></label>
      <label>차수/폭<input class="extra" value="${extra}" style="width:70px"></label>
    </div>`).join("");
}
document.querySelector("#piezoFilters").innerHTML = filterMarkup([["HPF", 10, 4], ["LPF", 3000, 4], ["Notch", 975, 20]]);
document.querySelector("#adxlFilters").innerHTML = filterMarkup([["HPF", 10, 4], ["LPF", 1000, 4], ["Notch", 975, 20]]);

/** RAW 또는 FFT 차트 하나를 만들고 제목을 붙인다. */
function chart(id, title) {
  const plot = GraphView.chart(id);
  plot.options.plugins.title = { display: true, text: title, color: "#17324d", font: { size: 13, weight: "bold" } };
  return plot;
}
const rawChart = chart("raw", "파형");
const fftChart = chart("fft", "FFT");
GraphView.axes(rawChart, "Time (s)", "값");
GraphView.axes(fftChart, "Frequency (Hz)", "크기");
GraphView.bindAxisBar({ raw: rawChart, fft: fftChart });

/** 숫자로 바꿀 수 없으면 기본값을 쓴다. */
function num(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

/** 필터 표의 체크와 주파수를 서버 옵션 객체로 읽는다. */
function readFilters(rootId) {
  const filters = {};
  document.querySelectorAll(`${rootId} [data-filter]`).forEach((row) => {
    filters[row.dataset.filter] = {
      raw: row.querySelector(".raw").checked,
      fft: row.querySelector(".fft").checked,
      freq: num(row.querySelector(".freq").value, 0),
      extra: num(row.querySelector(".extra").value, 1),
    };
  });
  return filters;
}

/** 그래프 설정 칸을 서버 /api/monitor/view 본문으로 만든다. */
function viewOptions() {
  return {
    fft_log_x: fftLogX.checked,
    fft_log_y: fftLogY.checked,
    piezo: {
      unit: piezoUnit.value,
      vmax: num(piezoVmax.value, 3.89),
      amax: num(piezoAmax.value, 4096),
      sensitivity: num(piezoSens.value, 100),
      window: piezoWindow.value,
      window_n: piezoN.value,
      filters: readFilters("#piezoFilters"),
    },
    adxl: {
      axes: { X: true, Y: true, Z: true, gN: gnOn.checked, SRSS: srssOn.checked },
      raw_unit: adxlRaw.value,
      fft_unit: adxlFft.value,
      g_per_lsb: num(adxlG.value, 0.00390625),
      window: adxlWindow.value,
      window_n: adxlN.value,
      beta: num(adxlBeta.value, 8.6),
      filters: readFilters("#adxlFilters"),
      correction: {
        frequency_enabled: freqOn.checked,
        sensitivity_enabled: sensOn.checked,
      },
    },
  };
}

const fftUnitHint = {
  "자동 (g RMS)": "FFT g RMS = Count Peak × (g/LSB) / √2. RAW는 가속도 단위입니다.",
  "Count Peak": "FFT Count Peak = 2|rFFT(창)| / Σ창. RAW는 가속도 단위입니다.",
  "g Peak": "FFT g Peak = Count Peak × (g/LSB). RAW는 가속도 단위입니다.",
  "g RMS": "FFT g RMS = FFT g Peak / √2. RAW는 가속도 단위입니다.",
  "mm/s RMS": "속도 RMS[mm/s] = FFT g RMS × 9806.65 / (2πf), f>0. RAW는 가속도 그대로입니다.",
};

/** FFT 단위 선택에 맞는 설명 문장을 보여 준다. */
function showFftUnitHint() {
  const hint = document.querySelector("#adxlFftHint");
  if (hint) hint.textContent = fftUnitHint[adxlFft.value] || fftUnitHint["자동 (g RMS)"];
}
adxlFft.addEventListener("change", showFftUnitHint);
showFftUnitHint();

/** 서버 배열을 Chart.js {x,y} 점으로 바꾼다. 로그 축이면 0 이하는 뺀다. */
function pairs(xs, ys, logX, logY) {
  const data = [];
  for (let i = 0; i < (xs || []).length; i += 1) {
    const x = xs[i];
    const y = ys[i];
    if (logX && !(x > 0)) continue;
    if (logY && !(y > 0)) continue;
    data.push({ x, y });
  }
  return data;
}

/** 확대 상태를 풀고 전체 범위로 되돌린다. */
function loosen(target) {
  if (typeof target.resetZoom === "function") {
    try { target.resetZoom(); } catch (_error) { /* zoom plugin not active yet */ }
  }
}

/** 선택한 센서 종류에 맞는 Fs+N 목록을 채운다. */
function fillProfiles() {
  const current = document.querySelector("#sensor").value;
  const box = document.querySelector("#profile");
  const previous = box.value;
  const items = profiles[current] || [];
  box.innerHTML = items.map((item) => `<option>${item}</option>`).join("");
  if (items.includes(previous)) box.value = previous;
  else if (items.length) box.value = items[items.length - 1];
}

const sensorTypeText = {
  0: "0: collection 종료 / normal 복귀",
  1: "1: PIEZO normal",
  2: "2: MEMS normal",
  3: "3: PIEZO collection",
  4: "4: MEMS collection",
};

/** 센서 종류와 센서 타입 번호를 맞추고, 게인·G Range 입력을 켜거나 끈다. */
function syncSensorType(fromKind) {
  const typeBox = document.querySelector("#sensorType");
  if (!typeBox) return;
  const desc = document.querySelector("#sensorTypeDesc");
  const gain = document.querySelector("#piezoGain");
  const range = document.querySelector("#gRange");
  if (fromKind) {
    const collection = typeBox.value === "3" || typeBox.value === "4";
    typeBox.value = sensor.value === "PIEZO" ? (collection ? "3" : "1") : (collection ? "4" : "2");
  } else if (typeBox.value === "1" || typeBox.value === "3") {
    if (sensor.value !== "PIEZO") {
      sensor.value = "PIEZO";
      fillProfiles();
    }
  } else if (typeBox.value === "2" || typeBox.value === "4") {
    if (sensor.value !== "ADXL") {
      sensor.value = "ADXL";
      fillProfiles();
    }
  }
  if (desc) desc.textContent = sensorTypeText[typeBox.value] || "";
  const piezo = typeBox.value === "1" || typeBox.value === "3";
  const adxl = typeBox.value === "2" || typeBox.value === "4";
  if (gain) gain.disabled = !piezo;
  if (range) range.disabled = !adxl;
}

/** 체크된 MAC 목록을 배열로 돌려준다. */
function selectedList() {
  return [...selectedMacs];
}

/** 명령 대상. 체크한 MAC과, 목록에 없는 직접 입력 MAC을 합친다. */
function commandTargets() {
  const checked = [...selectedMacs];
  const typed = manualMac.value.trim();
  if (typed && !checked.includes(typed)) checked.push(typed);
  return checked;
}

/** 수신 MAC 칩을 다시 그린다. 체크 상태는 selectedMacs를 유지한다. */
/* MAC별 표시 이름. renderMacs가 상태 응답마다 바꾸고, 이름 선택 상자(monitor-db.js)도 읽는다. */
let macNames = {};

/** 화면에 넣을 글자의 &, <, >, ", ' 를 바꾼다. 표시 이름은 사용자가 입력한 값이다. */
function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

/**
 * 수신 MAC 목록을 칩으로 그린다.
 * 칩 한 개 = 체크박스 · MAC · 표시 이름(있을 때) · 마지막 수신 시각 · 받은 패킷 수.
 * names는 서버 상태 응답의 {MAC: 이름}이다. 이름이 길면 말줄임표로 줄이고 전체는 마우스를 올리면 보인다.
 */
function renderMacs(devices, arrivals, names) {
  macNames = names || {};
  const box = document.querySelector("#macs");
  box.innerHTML = (devices || []).map((mac) => {
    const stat = (arrivals || {})[mac] || {};
    const label = macNames[mac] || "";
    const name = label ? `<span class="mac-name" title="${escapeHtml(label)}">${escapeHtml(label)}</span>` : "";
    const when = stat.last ? `<span class="mac-time">${stat.last}</span>` : "";
    const count = stat.count ? `<span class="mac-count">${stat.count}</span>` : "";
    return `<label class="mac-chip${label ? " named" : ""}"><input type="checkbox" value="${escapeHtml(mac)}" ${selectedMacs.has(mac) ? "checked" : ""}><span class="mac-addr">${escapeHtml(mac)}</span>${name}${when}${count}</label>`;
  }).join("");
  document.querySelector("#macCount").textContent = `${selectedMacs.size}개 선택`;
}

/** SRSS 가로선 데이터셋. 체크가 꺼져 있으면 null. */
function srssDataset(srss, logX, logY) {
  if (!srss || !srss.ok || !srssOn.checked) return null;
  return {
    label: "SRSS",
    borderColor: "#9467BD",
    borderDash: [5, 3],
    borderWidth: 1.7,
    data: pairs(srss.f, srss.y, logX, logY),
  };
}

/** 서버가 준 장치 곡선을 RAW와 FFT 차트, 수신 정보 칸에 그린다. */
function draw(device) {
  rawChart.data.datasets = [];
  fftChart.data.datasets = [];
  const notes = [];
  let logX = fftLogX.checked;
  let logY = fftLogY.checked;
  let timeLabel = "Time (s)";
  let rawY = "값";
  let fftLabel = "크기";
  let fftX = "Frequency (Hz)";
  const srss = device && device._srss;
  // 서버는 축을 도착 순서로 준다. 그래프 범례와 수신 정보가 늘 같은 순서가 되게 X, Y, Z, PIEZO, H 순으로 그린다.
  const axisOrder = ["X", "Y", "Z", "PIEZO", "H"];
  const rank = (axis) => (axisOrder.includes(axis) ? axisOrder.indexOf(axis) : axisOrder.length);
  const axes = Object.keys(device || {})
    .filter((axis) => axis.charAt(0) !== "_")
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  // 축별 값(AC RMS, N, Fs)을 먼저 X·Y·Z 순으로 모으고, 계산식처럼 축과 무관한 줄은 뒤에 붙인다.
  const formulas = [];
  for (const axis of axes) {
    const item = device[axis];
    const color = colors[axis] || "#444";
    if (item.kind === "spectrum") {
      rawChart.data.datasets.push({
        label: axis, borderColor: color,
        data: pairs(item.x, item.y, false, false),
      });
      notes.push(`${axis} spectrum ${item.y_label || ""}`);
      timeLabel = item.x_label || "Frequency (kHz)";
      rawY = item.y_label || rawY;
    } else {
      timeLabel = item.x_label || timeLabel;
      rawY = item.unit || rawY;
      fftLabel = item.fft_unit || fftLabel;
      fftX = item.fft_x_label || fftX;
      rawChart.data.datasets.push({
        label: `${axis} (${item.unit || ""})`, borderColor: color,
        data: pairs(item.t, item.y, false, false),
      });
      fftChart.data.datasets.push({
        label: axis, borderColor: color,
        data: pairs(item.fft_f, item.fft_y, logX, logY),
      });
      notes.push(`${axis} ${item.unit || ""} AC RMS=${item.rms_value} N=${item.n} Window=${item.window} Fs=${item.fs}`);
      if (item.formula) formulas.push(item.formula);
    }
  }
  const gn = device && device._gn;
  if (gn && gn.ok && gnOn.checked) {
    fftChart.data.datasets.push({
      label: "gN", borderColor: "#17a2b8", borderWidth: 1.4,
      data: pairs(gn.f, gn.y, logX, logY),
    });
  }
  const srssLine = srssDataset(srss, logX, logY);
  if (srssLine) fftChart.data.datasets.push(srssLine);
  const limit = device && device._srss_limit;
  if (limit && limit.ok) {
    fftChart.data.datasets.push({
      label: limit.text || "SRSS 한계", borderColor: "#b45309", borderDash: [5, 3], borderWidth: 1.5,
      data: pairs(limit.f, limit.y, logX, logY),
    });
    if (limit.value != null && document.activeElement !== srssLimit) srssLimit.value = limit.value;
  }
  notes.push(...formulas);
  if (srss && srss.text) notes.push(srss.text);
  if (!lockX.checked) {
    delete rawChart.options.scales.x.min;
    delete rawChart.options.scales.x.max;
    delete fftChart.options.scales.x.min;
    delete fftChart.options.scales.x.max;
  }
  if (!lockY.checked) {
    delete rawChart.options.scales.y.min;
    delete rawChart.options.scales.y.max;
    delete fftChart.options.scales.y.min;
    delete fftChart.options.scales.y.max;
  }
  const info = document.querySelector("#info");
  const lines = notes.filter((line, index, all) => all.indexOf(line) === index);
  info.innerHTML = lines.length
    ? lines.map((line) => `<span class="info-chip">${line.replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch]))}</span>`).join("")
    : `<span class="info-chip">수신 대기</span>`;
  fftChart.options.plugins.title = {
    display: true,
    text: (srss && srss.text) || fftLabel,
    color: "#17324d",
    font: { size: 13, weight: "bold" },
  };
  fftChart.options.scales.x.type = logX ? "logarithmic" : "linear";
  fftChart.options.scales.y.type = logY ? "logarithmic" : "linear";
  GraphView.axes(rawChart, timeLabel, rawY);
  GraphView.axes(fftChart, fftX, fftLabel);
  rawChart.update();
  fftChart.update();
}
