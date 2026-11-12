if (window.ChartZoom) Chart.register(window.ChartZoom);

const colors = { PIEZO: "#d62728", X: "#1f77b4", Y: "#2ca02c", Z: "#d62728", 초음파: "#1377a6" };
const sensors = ["PIEZO", "ADXL", "초음파"];
const periods = { PIEZO: 320, ADXL: 2560, 초음파: 10000 };
const topics = { PIEZO: "V/{mac}", ADXL: "V/{mac}", 초음파: "D/{mac}" };
let ready = false;

const sensorBox = document.querySelector("#sensors");
sensorBox.innerHTML = sensors.map((name) => `
  <div class="sensor-line" data-sensor="${name}">
    <label class="check"><input class="enabled" type="checkbox" ${name === "PIEZO" ? "checked" : ""}><span>${name}</span></label>
    <label>파일<span class="file-pick"><input class="file" type="file"><span class="file-btn">파일 선택</span><span class="file-name">선택된 파일 없음</span></span></label>
    <label>Topic<input class="topic" value="${topics[name]}"></label>
    <label>주기(ms)<input class="period" value="${periods[name]}" style="width:80px"></label>
    <label class="check"><input class="auto" type="checkbox" ${name !== "초음파" ? "checked" : ""}><span>자동주기</span></label>
    <label>±Jitter<input class="jitter" value="0" style="width:60px"></label>
    <label>Loss%<input class="loss" value="0" style="width:60px"></label>
  </div>`).join("");

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

function chart(id) {
  return GraphView.chart(id);
}
function rememberFile(input) {
  const pick = input.closest(".file-pick");
  const name = pick && pick.querySelector(".file-name");
  const show = () => {
    const chosen = input.files && input.files[0];
    if (name) name.textContent = chosen ? chosen.name : "선택된 파일 없음";
    if (pick) pick.classList.toggle("chosen", !!chosen);
  };
  input.addEventListener("change", show);
}
document.querySelectorAll("input.file, #adxlX, #adxlY, #adxlZ").forEach(rememberFile);
const rawChart = chart("raw");
const fftChart = chart("fft");
GraphView.axes(rawChart, "Time (ms)", "값");
GraphView.axes(fftChart, "Frequency (kHz)", "크기");
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
    fft_avg: fftAvg.checked,
    fft_avg_count: fftAvgCount.value,
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
      axes: { X: axisX.checked, Y: axisY.checked, Z: axisZ.checked },
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
        frequency_divisor: num(freqDiv.value, 1.014),
        low_hz: num(lowHz.value, 60),
        high_hz: num(highHz.value, 360),
        low_sensitivity: num(lowS.value, 0.99),
        mid_intercept: num(midA.value, 1.07),
        mid_slope: num(midB.value, 0.0005),
        high_sensitivity: num(highS.value, 0.89),
      },
      rpm: num(rpm.value, 1800),
      rot_axis: rotAxis.value,
      rot_type: rotType.value,
    },
  };
}

function showPanels() {
  piezoBox.hidden = false;
  adxlBox.hidden = false;
}

function pairs(xs, ys, logX, logY) {
  const data = [];
  for (let i = 0; i < xs.length; i += 1) {
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

function draw(preview) {
  document.querySelector("#position").textContent =
    `${preview.sensor} ${preview.index} / ${preview.count} | ${preview.info || ""}`;
  document.querySelector("#index").value = preview.index;
  formula.textContent = [preview.formula, preview.avg_status].filter(Boolean).join("\n");
  rotation.textContent = preview.rotation || "";
  const logX = !!preview.fft_log_x;
  const logY = !!preview.fft_log_y;
  if (preview.kind === "spectrum") {
    rawChart.data.datasets = [{
      label: "초음파",
      borderColor: colors["초음파"],
      data: pairs(preview.x || [], preview.y || [], false, false),
    }];
    fftChart.data.datasets = [];
  } else {
    rawChart.data.datasets = Object.entries(preview.series || {}).map(([axis, series]) => ({
      label: axis,
      borderColor: colors[axis],
      data: pairs(series.t, series.y, false, false),
    }));
    fftChart.data.datasets = Object.entries(preview.fft || {}).map(([axis, series]) => ({
      label: axis,
      borderColor: colors[axis],
      data: pairs(series.f, series.y, logX, logY),
    }));
  }
  rawChart.options.plugins.title = { display: true, text: preview.y_label || "RAW" };
  fftChart.options.plugins.title = { display: true, text: preview.fft_label || "FFT" };
  rawChart.options.scales.x.type = "linear";
  rawChart.options.scales.y.type = "linear";
  fftChart.options.scales.x.type = logX ? "logarithmic" : "linear";
  fftChart.options.scales.y.type = logY ? "logarithmic" : "linear";
  GraphView.axes(rawChart, preview.x_label || "Time (ms)", preview.y_label || "값");
  GraphView.axes(fftChart, preview.fft_x_label || "Frequency (kHz)", preview.fft_label || "크기");
  loosen(rawChart);
  loosen(fftChart);
  rawChart.update();
  fftChart.update();
}

function openGraph() {
  const panel = document.querySelector("#graphWindow");
  panel.hidden = false;
  requestAnimationFrame(() => {
    rawChart.resize();
    fftChart.resize();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

document.querySelector("#openGraph").onclick = openGraph;

function enableSensor(name) {
  const box = document.querySelector(`[data-sensor="${name}"] .enabled`);
  if (box) box.checked = true;
}

async function uploadAdxlCsv() {
  const files = { X: adxlX.files[0], Y: adxlY.files[0], Z: adxlZ.files[0] };
  if (!files.X || !files.Y || !files.Z) {
    alert("ADXL X, Y, Z CSV를 모두 선택하세요.");
    return;
  }
  const body = new FormData();
  body.append("x", files.X);
  body.append("y", files.Y);
  body.append("z", files.Z);
  body.append("recover_partial", recoverPartial.checked ? "true" : "false");
  const response = await fetch("/api/generator/upload-adxl-csv", { method: "POST", body });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "XYZ CSV 로드 실패");
  document.querySelector("#sensor").value = "ADXL";
  enableSensor("ADXL");
  showPanels();
  openGraph();
  await move(1);
}

document.querySelector("#loadAdxlCsv").onclick = () => uploadAdxlCsv().catch((error) => alert(error.message));
for (const id of ["adxlX", "adxlY", "adxlZ"]) {
  document.querySelector("#" + id).addEventListener("change", () => {
    if (adxlX.files[0] && adxlY.files[0] && adxlZ.files[0]) {
      uploadAdxlCsv().catch((error) => alert(error.message));
    }
  });
}

async function upload(sensor, file) {
  const body = new FormData();
  body.append("sensor", sensor);
  body.append("file", file);
  const response = await fetch("/api/generator/upload", { method: "POST", body });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "업로드 실패");
  document.querySelector("#sensor").value = sensor;
  enableSensor(sensor);
  showPanels();
  openGraph();
  await move(1);
}

sensorBox.addEventListener("change", (event) => {
  const input = event.target.closest(".file");
  if (!input || !input.files[0]) return;
  const sensor = input.closest("[data-sensor]").dataset.sensor;
  upload(sensor, input.files[0]).catch((error) => alert(error.message));
});

async function move(index) {
  const sensor = document.querySelector("#sensor").value;
  const response = await fetch("/api/generator/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sensor, index, options: viewOptions() }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "미리보기 실패");
  draw(data);
}
document.querySelector("#prev").onclick = () => move(Number(document.querySelector("#index").value) - 1).catch((e) => alert(e.message));
document.querySelector("#next").onclick = () => move(Number(document.querySelector("#index").value) + 1).catch((e) => alert(e.message));
document.querySelector("#go").onclick = () => move(Number(document.querySelector("#index").value)).catch((e) => alert(e.message));
document.querySelector("#sensor").onchange = () => {
  showPanels();
  move(Number(document.querySelector("#index").value) || 1).catch(() => {});
};

function config() {
  const rows = [...document.querySelectorAll("[data-sensor]")];
  const pick = (cls) => Object.fromEntries(rows.map((row) => [row.dataset.sensor, row.querySelector(cls).value]));
  const checked = (cls) => Object.fromEntries(rows.map((row) => [row.dataset.sensor, row.querySelector(cls).checked]));
  return {
    host: host.value, port: num(port.value, 1883), qos: num(qos.value, 1),
    user: user.value, password: password.value, device: device.value, mac: mac.value,
    rewrite_mac: rewrite.checked, seq_auto: seq.checked, jitter_on: jitterOn.checked, loss_on: lossOn.checked,
    topics: pick(".topic"), period: pick(".period"), auto_period: checked(".auto"),
    jitter: pick(".jitter"), loss: pick(".loss"),
    xyz_gap: num(xyzGap.value, 5), tick_step: num(tickStep.value, 0), enabled: checked(".enabled"),
  };
}

document.querySelector("#start").onclick = async () => {
  const response = await fetch("/api/generator/start", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config()),
  });
  const data = await response.json();
  if (!response.ok) alert(data.detail || "시작 실패");
};
document.querySelector("#stop").onclick = () => fetch("/api/generator/stop", { method: "POST" });
document.querySelector("#testBroker").onclick = async () => {
  const response = await fetch("/api/generator/test", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config()),
  });
  const data = await response.json();
  alert(response.ok ? "MQTT 연결 성공" : (data.detail || "연결 실패"));
};
document.querySelector("#resetZoom").onclick = () => {
  loosen(rawChart);
  loosen(fftChart);
  rawChart.update();
  fftChart.update();
};

async function loadDefaults() {
  const data = await (await fetch("/api/analysis/defaults")).json();
  const adxl = data.adxl || {};
  const correction = adxl.correction || {};
  if (adxl.g_per_lsb != null) adxlG.value = adxl.g_per_lsb;
  freqOn.checked = !!correction.frequency_enabled;
  sensOn.checked = !!correction.sensitivity_enabled;
  if (correction.frequency_divisor != null) freqDiv.value = correction.frequency_divisor;
  if (correction.low_hz != null) lowHz.value = correction.low_hz;
  if (correction.high_hz != null) highHz.value = correction.high_hz;
  if (correction.low_sensitivity != null) lowS.value = correction.low_sensitivity;
  if (correction.mid_intercept != null) midA.value = correction.mid_intercept;
  if (correction.mid_slope != null) midB.value = correction.mid_slope;
  if (correction.high_sensitivity != null) highS.value = correction.high_sensitivity;
}

document.querySelector("#analysis").addEventListener("change", () => {
  if (!ready) return;
  if (document.querySelector("#position").textContent.includes("선택하세요")) return;
  move(Number(document.querySelector("#index").value)).catch((error) => alert(error.message));
});

async function saveJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "저장 실패");
  if (!document.querySelector("#position").textContent.includes("선택하세요")) {
    await move(Number(document.querySelector("#index").value));
  }
}

document.querySelector("#saveG").onclick = () => saveJson("/api/analysis/mems-scale", {
  g_per_lsb: num(adxlG.value, 0.00390625),
}).catch((error) => alert(error.message));
document.querySelector("#saveCorrection").onclick = () => saveJson("/api/analysis/mems-correction", {
  correction: viewOptions().adxl.correction,
}).catch((error) => alert(error.message));

showPanels();
loadDefaults().then(() => { ready = true; }).catch(() => { ready = true; });

setInterval(async () => {
  const data = await (await fetch("/api/generator/status")).json();
  stats.textContent = `Publish ${data.sent} / PUBACK ${data.acked || 0} / Drop ${data.dropped} | ` +
    Object.entries(data.counter).map(([name, count]) => `${name} ${count}`).join(", ") +
    (data.running ? " | 전송 중" : " | 대기");
  log.textContent = data.logs.join("\n");
}, 1000);
