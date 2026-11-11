const colors = { PIEZO: "#d62728", X: "#1f77b4", Y: "#2ca02c", Z: "#d62728", 초음파: "#1377a6" };
const sensors = ["PIEZO", "ADXL", "초음파"];
const periods = { PIEZO: 320, ADXL: 2560, 초음파: 10000 };
const topics = { PIEZO: "V/{mac}", ADXL: "V/{mac}", 초음파: "D/{mac}" };

const sensorBox = document.querySelector("#sensors");
sensorBox.innerHTML = sensors.map((name) => `
  <div class="row" data-sensor="${name}">
    <label><span>${name}</span><input class="enabled" type="checkbox" ${name === "PIEZO" ? "checked" : ""}></label>
    <label>파일<input class="file" type="file"></label>
    <label>Topic<input class="topic" value="${topics[name]}"></label>
    <label>주기(ms)<input class="period" value="${periods[name]}" style="width:80px"></label>
    <label><span>자동주기</span><input class="auto" type="checkbox" ${name !== "초음파" ? "checked" : ""}></label>
    <label>±Jitter<input class="jitter" value="0" style="width:60px"></label>
    <label>Loss%<input class="loss" value="0" style="width:60px"></label>
  </div>`).join("");

function chart(id) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      parsing: false,
      plugins: { legend: { position: "top" } },
      scales: {
        x: { type: "linear", title: { display: true, text: "" } },
        y: { type: "linear" },
      },
      elements: { point: { radius: 0 }, line: { borderWidth: 1 } },
    },
  });
}
const rawChart = chart("raw");
const fftChart = chart("fft");
const trendChart = chart("trend");
attachGraphZoom(rawChart);
attachGraphZoom(fftChart);
attachGraphZoom(trendChart);
trendChart.options.plugins.title = { display: true, text: "저장 데이터 g RMS" };
let savedFileItems = [];

function chosenSavedFiles() {
  return [...document.querySelectorAll("#savedFiles input:checked")].map((input) => input.value);
}

function savedColor(sensor) {
  const axis = String(sensor).includes("-") ? sensor.split("-").pop() : sensor;
  return colors[axis] || colors[sensor] || "#444";
}

async function drawSaved() {
  const session = savedSession.value;
  const files = chosenSavedFiles();
  if (!session || !files.length) {
    trendChart.data.datasets = [];
    trendChart.update();
    return;
  }
  const query = `session=${encodeURIComponent(session)}&files=${encodeURIComponent(files.join(","))}&window=${encodeURIComponent(rmsWindow.value)}`;
  const trend = await (await fetch(`/api/archive/trend?${query}`)).json();
  trendChart.data.datasets = (trend.series || []).map((item) => ({
    label: `${item.device} ${item.sensor} (${item.unit})`,
    borderColor: savedColor(item.sensor),
    data: item.packet.map((x, i) => ({ x, y: item.rms[i] })),
  }));
  trendChart.options.scales.x.title = { display: true, text: "Packet" };
  trendChart.update();
}

function draw(preview) {
  document.querySelector("#position").textContent =
    `${preview.sensor} ${preview.index} / ${preview.count} | ${preview.info}`;
  document.querySelector("#index").value = preview.index;
  if (preview.kind === "spectrum") {
    rawChart.data.datasets = [{
      label: "초음파",
      borderColor: colors["초음파"],
      data: preview.x.map((x, i) => ({ x, y: preview.y[i] })),
    }];
    fftChart.data.datasets = [];
  } else {
    rawChart.data.datasets = Object.entries(preview.series).map(([axis, series]) => ({
      label: axis,
      borderColor: colors[axis],
      data: series.t.map((x, i) => ({ x, y: series.y[i] })),
    }));
    fftChart.data.datasets = Object.entries(preview.fft).map(([axis, series]) => ({
      label: axis,
      borderColor: colors[axis],
      data: series.f.map((x, i) => ({ x, y: series.y[i] })),
    }));
  }
  rawChart.options.plugins.title = { display: true, text: preview.y_label || "RAW" };
  fftChart.options.plugins.title = { display: true, text: preview.fft_label || "FFT" };
  rawChart.options.scales.x.title = { display: true, text: preview.x_label || "Time (ms)" };
  fftChart.options.scales.x.title = { display: true, text: preview.fft_x_label || preview.x_label || "Frequency (kHz)" };
  rawChart.update();
  fftChart.update();
}

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
  draw(data.preview);
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
  draw(data.preview);
}

sensorBox.addEventListener("change", (event) => {
  const input = event.target.closest(".file");
  if (!input || !input.files[0]) return;
  const sensor = input.closest("[data-sensor]").dataset.sensor;
  upload(sensor, input.files[0]).catch((error) => alert(error.message));
});

async function move(index) {
  const sensor = document.querySelector("#sensor").value;
  const response = await fetch(`/api/generator/preview?sensor=${encodeURIComponent(sensor)}&index=${index}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "미리보기 실패");
  draw(data);
}
document.querySelector("#prev").onclick = () => move(Number(document.querySelector("#index").value) - 1).catch((e) => alert(e.message));
document.querySelector("#next").onclick = () => move(Number(document.querySelector("#index").value) + 1).catch((e) => alert(e.message));
document.querySelector("#go").onclick = () => move(Number(document.querySelector("#index").value)).catch((e) => alert(e.message));
document.querySelector("#sensor").onchange = () => move(1).catch(() => {});

function config() {
  const rows = [...document.querySelectorAll("[data-sensor]")];
  const pick = (cls) => Object.fromEntries(rows.map((row) => [row.dataset.sensor, row.querySelector(cls).value]));
  const checked = (cls) => Object.fromEntries(rows.map((row) => [row.dataset.sensor, row.querySelector(cls).checked]));
  return {
    host: host.value, port: Number(port.value), qos: Number(qos.value),
    user: user.value, password: password.value, device: device.value, mac: mac.value,
    rewrite_mac: rewrite.checked, seq_auto: seq.checked, jitter_on: jitterOn.checked, loss_on: lossOn.checked,
    topics: pick(".topic"), period: pick(".period"), auto_period: checked(".auto"),
    jitter: pick(".jitter"), loss: pick(".loss"), xyz_gap: 5, tick_step: 0, enabled: checked(".enabled"),
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

async function loadSavedSessions() {
  const data = await (await fetch("/api/archive/sessions")).json();
  savedSession.innerHTML = data.sessions.map((item) => `<option value="${item.name}">${item.name}</option>`).join("");
  await loadSavedDevices();
}

async function loadSavedDevices() {
  if (!savedSession.value) return;
  const data = await (await fetch(`/api/archive/files?session=${encodeURIComponent(savedSession.value)}`)).json();
  const previous = savedDevice.value;
  savedDevice.innerHTML = (data.devices || []).map((item) => `<option>${item}</option>`).join("");
  if (previous && (data.devices || []).includes(previous)) savedDevice.value = previous;
  savedFileItems = (data.files || []).filter((item) => item.role === "decoded");
  const checked = new Set(chosenSavedFiles());
  const device = savedDevice.value;
  const useDefault = !savedFileItems.some((item) => checked.has(item.name));
  document.querySelector("#savedFiles").innerHTML = savedFileItems.map((item) => `
    <label><input type="checkbox" value="${item.name}" ${(useDefault && item.device === device) || checked.has(item.name) ? "checked" : ""}>
      ${item.device} / ${item.sensor}</label>
  `).join("");
  await drawSaved();
}

document.querySelector("#savedSession").onchange = loadSavedDevices;
document.querySelector("#savedDevice").onchange = () => {
  document.querySelectorAll("#savedFiles input").forEach((input) => {
    const item = savedFileItems.find((file) => file.name === input.value);
    input.checked = item && item.device === savedDevice.value;
  });
  drawSaved();
};
document.querySelector("#savedFiles").onchange = drawSaved;
document.querySelector("#rmsWindow").onchange = drawSaved;
document.querySelector("#reloadSaved").onclick = loadSavedSessions;
document.querySelector("#loadSaved").onclick = async () => {
  const response = await fetch("/api/generator/load-saved", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session: savedSession.value,
      device: savedDevice.value,
      sensor: savedSensor.value,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.detail || "CSV 불러오기 실패");
    return;
  }
  document.querySelector("#sensor").value = savedSensor.value;
  enableSensor(savedSensor.value);
  draw(data.preview);
  drawSaved();
};
loadSavedSessions();

setInterval(async () => {
  const data = await (await fetch("/api/generator/status")).json();
  stats.textContent = `Publish ${data.sent} / Drop ${data.dropped} | ` +
    Object.entries(data.counter).map(([name, count]) => `${name} ${count}`).join(", ") +
    (data.running ? " | 전송 중" : " | 대기");
  log.textContent = data.logs.join("\n");
}, 1000);
