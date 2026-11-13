// 센서 수신 모니터. MQTT 연결은 서버가 유지하고, 이 스크립트는 1초마다 최신 곡선을 그린다.
// 그래프 장치 값 "" 는 최근 수신 MAC이다. id="status"는 window.status와 겹치므로 쓰지 않는다.
// 명령 대상은 체크한 MAC과 직접 입력 MAC이다. 보내기 전에 주소 확인을 받는다.
if (window.ChartZoom) Chart.register(window.ChartZoom);

const colors = { X: "#1f77b4", Y: "#2ca02c", Z: "#d62728", PIEZO: "#1377a6", ULTRASONIC: "#c05b13", H: "#c05b13" };
const selectedMacs = new Set();
let profiles = { ADXL: [], PIEZO: [] };
let graphDevice = "";
let ready = false;

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

function num(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

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
function showFftUnitHint() {
  const hint = document.querySelector("#adxlFftHint");
  if (hint) hint.textContent = fftUnitHint[adxlFft.value] || fftUnitHint["자동 (g RMS)"];
}
adxlFft.addEventListener("change", showFftUnitHint);
showFftUnitHint();

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

function loosen(target) {
  if (typeof target.resetZoom === "function") {
    try { target.resetZoom(); } catch (_error) { /* zoom plugin not active yet */ }
  }
}

function fillProfiles() {
  const current = document.querySelector("#sensor").value;
  const box = document.querySelector("#profile");
  const previous = box.value;
  const items = profiles[current] || [];
  box.innerHTML = items.map((item) => `<option>${item}</option>`).join("");
  if (items.includes(previous)) box.value = previous;
  else if (items.length) box.value = items[items.length - 1];
}

function selectedList() {
  return [...selectedMacs];
}

function commandTargets() {
  const checked = [...selectedMacs];
  const typed = manualMac.value.trim();
  if (typed && !checked.includes(typed)) checked.push(typed);
  return checked;
}

function renderMacs(devices, arrivals) {
  const box = document.querySelector("#macs");
  box.innerHTML = (devices || []).map((mac) => {
    const stat = (arrivals || {})[mac];
    const stamp = stat && stat.count ? ` ${stat.last} (${stat.count})` : "";
    return `<label><input type="checkbox" value="${mac}" ${selectedMacs.has(mac) ? "checked" : ""}> ${mac}<span class="stats">${stamp}</span></label>`;
  }).join("");
  document.querySelector("#macCount").textContent = `${selectedMacs.size}개 선택`;
}

function points(xs, ys, logX, logY) {
  const data = [];
  for (let i = 0; i < xs.length; i += 1) {
    const x = xs[i];
    const y = ys[i];
    if (logX && x <= 0) continue;
    if (logY && y <= 0) continue;
    data.push({ x, y });
  }
  return data;
}

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
  for (const axis of Object.keys(device || {})) {
    if (axis.charAt(0) === "_") continue;
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
      if (item.formula) notes.push(item.formula);
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
  if (srss && srss.text) notes.push(srss.text);
  document.querySelector("#info").textContent = notes.filter((line, index, all) => all.indexOf(line) === index).join("\n") || "수신 대기";
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

async function refresh() {
  const query = graphDevice ? `?device=${encodeURIComponent(graphDevice)}` : "";
  const data = await (await fetch(`/api/monitor/state${query}`)).json();
  profiles = data.profiles || profiles;
  if (!document.querySelector("#profile").options.length) fillProfiles();
  const linkStatus = document.querySelector("#linkStatus");
  linkStatus.className = data.connected ? "pill on" : "pill off";
  linkStatus.textContent = data.connected
    ? `CONNECTED ${data.messages} packets | CSV ${data.csv_saved} / 오류 ${data.csv_errors}`
    : "DISCONNECTED";
  csvInfo.textContent = data.csv_dir
    ? `저장 폴더: ${data.csv_dir}`
    : "CSV 체크 시 연결 중 수신 데이터를 바로 저장합니다. HEX와 decoded CSV를 저장 폴더에 나눕니다.";
  log.textContent = (data.logs || []).join("\n");
  window._monitorDevice = graphDevice || data.latest_device || "";
  renderMacs(data.devices || [], data.arrivals || {});
  const box = document.querySelector("#device");
  const devices = data.graph_devices || Object.keys(data.latest || {});
  const previous = box.value;
  box.innerHTML = `<option value="">최근 수신</option>` + devices.map((id) => `<option>${id}</option>`).join("");
  if (previous && devices.includes(previous)) box.value = previous;
  else box.value = "";
  graphDevice = box.value;
  const shown = box.value || data.latest_device || "";
  draw((data.latest || {})[shown] || {});
  const mac = selectedList()[0];
  if (mac && data.configs && data.configs[mac]) {
    preview.textContent = JSON.stringify(data.configs[mac], null, 2);
  }
}

async function pushView() {
  await fetch("/api/monitor/view", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ options: viewOptions(), window: "전체", device: graphDevice }),
  });
  await refresh();
}

async function command(action) {
  const macs = commandTargets();
  if (!macs.length) {
    alert("체크된 MAC이 없고, 직접 입력한 MAC도 없습니다.");
    return;
  }
  const names = {
    apply: "설정 적용",
    start: "START",
    stop: "STOP",
    query: "현재 설정 조회",
  };
  const label = names[action] || action;
  if (!confirm(`${macs.join(", ")} 주소로 ${label}하겠습니까?`)) return;
  const response = await fetch("/api/monitor/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action,
      macs,
      sensor: sensor.value,
      period: Number(period.value),
      profile: profile.value,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.detail || "명령 실패");
    return;
  }
  preview.textContent = data.payload || "";
}

document.querySelector("#sensor").onchange = fillProfiles;
document.querySelector("#macs").onchange = (event) => {
  const input = event.target;
  if (!input.value) return;
  if (input.checked) selectedMacs.add(input.value);
  else selectedMacs.delete(input.value);
  document.querySelector("#macCount").textContent = `${selectedMacs.size}개 선택`;
};
document.querySelector("#selectAll").onclick = () => {
  document.querySelectorAll("#macs input").forEach((input) => {
    input.checked = true;
    selectedMacs.add(input.value);
  });
  document.querySelector("#macCount").textContent = `${selectedMacs.size}개 선택`;
};
document.querySelector("#clearMacs").onclick = () => {
  selectedMacs.clear();
  document.querySelectorAll("#macs input").forEach((input) => { input.checked = false; });
  document.querySelector("#macCount").textContent = "0개 선택";
};
document.querySelector("#connect").onclick = async () => {
  const response = await fetch("/api/monitor/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host: host.value, port: Number(port.value), topic: topic.value,
      csv_enabled: csvEnabled.checked,
    }),
  });
  const data = await response.json();
  if (!response.ok) alert(data.detail || "연결 실패");
  refresh();
};
document.querySelector("#disconnect").onclick = async () => {
  await fetch("/api/monitor/disconnect", { method: "POST" });
  refresh();
};
document.querySelector("#analysis").addEventListener("change", () => {
  if (ready) pushView().catch((error) => alert(error.message));
});
document.querySelector("#resetZoom").onclick = () => {
  loosen(rawChart);
  loosen(fftChart);
  rawChart.update();
  fftChart.update();
};
document.querySelector("#applySrssLimit").onclick = async () => {
  const device = graphDevice || window._monitorDevice || document.querySelector("#manualMac").value.trim();
  if (!device) {
    alert("한계값을 저장할 장치 MAC이 없습니다.");
    return;
  }
  const response = await fetch("/api/analysis/srss-limit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device, value: num(srssLimit.value, 4.5) }),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.detail || "SRSS 한계값 저장 실패");
    return;
  }
  srssLimit.value = data.value;
  pushView();
};
document.querySelector("#start").onclick = () => command("start");
document.querySelector("#stop").onclick = () => command("stop");
document.querySelector("#apply").onclick = () => command("apply");
document.querySelector("#query").onclick = () => command("query");
document.querySelector("#device").onchange = () => { graphDevice = device.value; refresh(); };
document.querySelector("#fftCheck").onclick = async () => {
  const response = await fetch("/api/monitor/fft-check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device: graphDevice }),
  });
  const data = await response.json();
  if (!response.ok) {
    GraphView.showFftCheck(document.querySelector("#fftCheckBox"), {
      ok: false,
      message: data.detail || "검증 실패",
      note: "① 입력 → ② 양측 FFT → ③ 단측 FFT | ② 복소수 FFT → ④ IFFT 복원 · 평균 제거/SRSS/물리단위 보정은 검증 제외",
    });
    return;
  }
  GraphView.showFftCheck(document.querySelector("#fftCheckBox"), data);
};

async function loadDefaults() {
  const data = await (await fetch("/api/analysis/defaults")).json();
  const adxl = data.adxl || {};
  const correction = adxl.correction || {};
  if (adxl.g_per_lsb != null) adxlG.value = adxl.g_per_lsb;
  freqOn.checked = !!correction.frequency_enabled;
  sensOn.checked = !!correction.sensitivity_enabled;
}

loadDefaults()
  .catch(() => {})
  .then(() => pushView())
  .catch(() => {})
  .then(() => { ready = true; });
setInterval(refresh, 1000);
