// MQTT 파형 발생기. 파일 업로드와 송신은 서버가 한다.
// RAW/FFT는 이 페이지에 항상 둔다. 차트는 그릴 때마다 datasets를 교체하고, 점은 서버에서 상한을 둔다.
// PIEZO와 MEMS 설정은 같이 둔다.
// 아래 1초 status 요청은 브로커 접속이 아니라 Publish/PUBACK/Drop 숫자 갱신이다.
if (window.ChartZoom) Chart.register(window.ChartZoom);

const colors = {
  PIEZO: "#d62728",
  X: "#1f77b4",
  Y: "#2ca02c",
  Z: "#d62728",
  초음파: "#1377a6",
};
const sensors = ["PIEZO", "ADXL", "초음파"];
const periods = { PIEZO: 320, ADXL: 2560, 초음파: 10000 };
const topics = { PIEZO: "V/{mac}", ADXL: "V/{mac}", 초음파: "D/{mac}" };
let ready = false;

const sensorBox = document.querySelector("#sensors");
const studioLayout = document.body.classList.contains("layout-pin");
sensorBox.innerHTML = sensors
  .map((name) => {
    const enabled = `<label class="check"><input class="enabled" type="checkbox" ${name === "PIEZO" ? "checked" : ""}><span>${name}</span></label>`;
    const topic = `<label>Topic<input class="topic" value="${topics[name]}"></label>`;
    const auto = `<label class="check"><input class="auto" type="checkbox" ${name !== "초음파" ? "checked" : ""}><span>자동주기</span></label>`;
    if (!studioLayout) {
      return `
  <div class="sensor-line" data-sensor="${name}">
    ${enabled}
    <label>파일<span class="file-pick"><input class="file" type="file"><span class="file-btn">파일 선택</span><span class="file-name">선택된 파일 없음</span></span></label>
    ${topic}
    <label>주기(ms)<input class="period" value="${periods[name]}" style="width:80px"></label>
    ${auto}
    <label>±Jitter<input class="jitter" value="0" style="width:60px"></label>
    <label>Loss%<input class="loss" value="0" style="width:60px"></label>
  </div>`;
    }
    return `
  <div class="sensor-line" data-sensor="${name}">
    ${enabled}
    ${topic}
    <div class="pair">
      <label>주기(ms)<input class="period" value="${periods[name]}"></label>
      <label>±Jitter<input class="jitter" value="0"></label>
    </div>
    <div class="pair">
      <label>Loss%<input class="loss" value="0"></label>
      ${auto}
    </div>
  </div>`;
  })
  .join("");

function filterMarkup(items) {
  return items
    .map(
      ([name, freq, extra]) => `
    <div class="row" data-filter="${name}">
      <strong>${name}</strong>
      <label class="check"><input class="raw" type="checkbox"><span>RAW</span></label>
      <label class="check"><input class="fft" type="checkbox"><span>FFT</span></label>
      <label>값<input class="freq" value="${freq}" style="width:80px"></label>
      <label>차수/폭<input class="extra" value="${extra}" style="width:70px"></label>
    </div>`,
    )
    .join("");
}
document.querySelector("#piezoFilters").innerHTML = filterMarkup([
  ["HPF", 10, 4],
  ["LPF", 3000, 4],
  ["Notch", 975, 20],
]);
document.querySelector("#adxlFilters").innerHTML = filterMarkup([
  ["HPF", 10, 4],
  ["LPF", 1000, 4],
  ["Notch", 975, 20],
]);

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
document
  .querySelectorAll("input.file, #adxlX, #adxlY, #adxlZ")
  .forEach(rememberFile);
const rawChart = chart("raw");
const fftChart = chart("fft");
GraphView.axes(rawChart, "Time (ms)", "값");
GraphView.axes(fftChart, "Frequency (kHz)", "크기");
GraphView.bindAxisBar({ raw: rawChart, fft: fftChart });

function num(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function optionalValue(id, fallback) {
  const node = document.getElementById(id);
  return node ? node.value : fallback;
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
      axes: { X: axisX.checked, Y: axisY.checked, Z: axisZ.checked, gN: gnOn.checked, SRSS: srssOn.checked },
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
      rpm: num(optionalValue("rpm", 1800), 1800),
      rot_axis: optionalValue("rotAxis", "X"),
      rot_type: optionalValue("rotType", "미지정"),
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
    try {
      target.resetZoom();
    } catch (_error) {
      /* zoom plugin not active yet */
    }
  }
}

function draw(preview) {
  document.querySelector("#position").textContent =
    `${preview.sensor} ${preview.index} / ${preview.count} | ${preview.info || ""}`;
  document.querySelector("#index").value = preview.index;
  formula.textContent = [preview.formula, preview.avg_status, preview.srss && preview.srss.text]
    .filter(Boolean)
    .join("\n");
  const rotationBox = document.getElementById("rotation");
  if (rotationBox) rotationBox.textContent = preview.rotation || "";
  const logX = !!preview.fft_log_x;
  const logY = !!preview.fft_log_y;
  if (preview.kind === "spectrum") {
    rawChart.data.datasets = [
      {
        label: "초음파",
        borderColor: colors["초음파"],
        data: pairs(preview.x || [], preview.y || [], false, false),
      },
    ];
    fftChart.data.datasets = [];
  } else {
    rawChart.data.datasets = Object.entries(preview.series || {}).map(
      ([axis, series]) => ({
        label: axis,
        borderColor: colors[axis],
        data: pairs(series.t, series.y, false, false),
      }),
    );
    fftChart.data.datasets = Object.entries(preview.fft || {}).map(
      ([axis, series]) => ({
        label: axis,
        borderColor: colors[axis],
        data: pairs(series.f, series.y, logX, logY),
      }),
    );
    if (preview.gn && preview.gn.ok && gnOn.checked) {
      fftChart.data.datasets.push({
        label: "gN",
        borderColor: "#17a2b8",
        borderWidth: 1.4,
        data: pairs(preview.gn.f || [], preview.gn.y || [], logX, logY),
      });
    }
    if (preview.srss && preview.srss.ok && srssOn.checked) {
      fftChart.data.datasets.push({
        label: "SRSS",
        borderColor: "#9467BD",
        borderDash: [5, 3],
        borderWidth: 1.7,
        data: pairs(preview.srss.f || [], preview.srss.y || [], logX, logY),
      });
    }
    if (preview.srss_limit && preview.srss_limit.ok) {
      fftChart.data.datasets.push({
        label: preview.srss_limit.text || "SRSS 한계",
        borderColor: "#b45309",
        borderDash: [5, 3],
        borderWidth: 1.5,
        data: pairs(preview.srss_limit.f || [], preview.srss_limit.y || [], logX, logY),
      });
    }
    if (preview.srss_limit && preview.srss_limit.device && preview.srss_limit.device !== window._srssDevice) {
      window._srssDevice = preview.srss_limit.device;
      if (preview.srss_limit.value != null && document.activeElement !== srssLimit) {
        srssLimit.value = preview.srss_limit.value;
      }
    }
  }
  rawChart.options.plugins.title = {
    display: true,
    text: preview.y_label || "RAW",
  };
  fftChart.options.plugins.title = {
    display: true,
    text: (preview.srss && preview.srss.text) || preview.fft_label || "FFT",
  };
  rawChart.options.scales.x.type = "linear";
  rawChart.options.scales.y.type = "linear";
  fftChart.options.scales.x.type = logX ? "logarithmic" : "linear";
  fftChart.options.scales.y.type = logY ? "logarithmic" : "linear";
  GraphView.axes(
    rawChart,
    preview.x_label || "Time (ms)",
    preview.y_label || "값",
  );
  GraphView.axes(
    fftChart,
    preview.fft_x_label || "Frequency (kHz)",
    preview.fft_label || "크기",
  );
  loosen(rawChart);
  loosen(fftChart);
  rawChart.update();
  fftChart.update();
}

document.querySelector("#fftCheck").onclick = async () => {
  const sensor = document.querySelector("#sensor").value;
  const response = await fetch("/api/generator/fft-check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sensor }),
  });
  const data = await readBody(response);
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

function enableSensor(name) {
  const box = document.querySelector(`[data-sensor="${name}"] .enabled`);
  if (box) box.checked = true;
}

async function readBody(response) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch (_error) {
    throw new Error(text.slice(0, 180) || "서버 응답을 해석하지 못했습니다");
  }
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
  const response = await fetch("/api/generator/upload-adxl-csv", {
    method: "POST",
    body,
  });
  const data = await readBody(response);
  if (!response.ok) throw new Error(data.detail || "XYZ CSV 로드 실패");
  document.querySelector("#sensor").value = "ADXL";
  enableSensor("ADXL");
  showPanels();
  await move(1);
}

document.querySelector("#loadAdxlCsv").onclick = () =>
  uploadAdxlCsv().catch((error) => alert(error.message));
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
  const response = await fetch("/api/generator/upload", {
    method: "POST",
    body,
  });
  const data = await readBody(response);
  if (!response.ok) throw new Error(data.detail || "업로드 실패");
  document.querySelector("#sensor").value = sensor;
  enableSensor(sensor);
  showPanels();
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
  const data = await readBody(response);
  if (!response.ok) throw new Error(data.detail || "미리보기 실패");
  draw(data);
}
document.querySelector("#prev").onclick = () =>
  move(Number(document.querySelector("#index").value) - 1).catch((e) =>
    alert(e.message),
  );
document.querySelector("#next").onclick = () =>
  move(Number(document.querySelector("#index").value) + 1).catch((e) =>
    alert(e.message),
  );
document.querySelector("#go").onclick = () =>
  move(Number(document.querySelector("#index").value)).catch((e) =>
    alert(e.message),
  );
document.querySelector("#sensor").onchange = () => {
  showPanels();
  move(Number(document.querySelector("#index").value) || 1).catch(() => {});
};

function config() {
  const rows = [...document.querySelectorAll("[data-sensor]")];
  const pick = (cls) =>
    Object.fromEntries(
      rows.map((row) => [row.dataset.sensor, row.querySelector(cls).value]),
    );
  const checked = (cls) =>
    Object.fromEntries(
      rows.map((row) => [row.dataset.sensor, row.querySelector(cls).checked]),
    );
  return {
    host: host.value,
    port: num(port.value, 1883),
    qos: num(qos.value, 1),
    device: device.value,
    mac: mac.value,
    rewrite_mac: rewrite.checked,
    seq_auto: seq.checked,
    jitter_on: jitterOn.checked,
    loss_on: lossOn.checked,
    topics: pick(".topic"),
    period: pick(".period"),
    auto_period: checked(".auto"),
    jitter: pick(".jitter"),
    loss: pick(".loss"),
    xyz_gap: num(xyzGap.value, 5),
    tick_step: num(tickStep.value, 0),
    enabled: checked(".enabled"),
  };
}

document.querySelector("#start").onclick = async () => {
  const response = await fetch("/api/generator/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config()),
  });
  const data = await readBody(response);
  if (!response.ok) alert(data.detail || "시작 실패");
};
document.querySelector("#stop").onclick = () =>
  fetch("/api/generator/stop", { method: "POST" });
document.querySelector("#testBroker").onclick = async () => {
  const response = await fetch("/api/generator/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config()),
  });
  const data = await readBody(response);
  alert(response.ok ? "MQTT 연결 성공" : data.detail || "연결 실패");
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
  if (correction.frequency_divisor != null)
    freqDiv.value = correction.frequency_divisor;
  if (correction.low_hz != null) lowHz.value = correction.low_hz;
  if (correction.high_hz != null) highHz.value = correction.high_hz;
  if (correction.low_sensitivity != null)
    lowS.value = correction.low_sensitivity;
  if (correction.mid_intercept != null) midA.value = correction.mid_intercept;
  if (correction.mid_slope != null) midB.value = correction.mid_slope;
  if (correction.high_sensitivity != null)
    highS.value = correction.high_sensitivity;
}

document.querySelector("#analysis").addEventListener("change", () => {
  if (!ready) return;
  if (document.querySelector("#position").textContent.includes("선택하세요"))
    return;
  move(Number(document.querySelector("#index").value)).catch((error) =>
    alert(error.message),
  );
});

async function saveJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await readBody(response);
  if (!response.ok) throw new Error(data.detail || "저장 실패");
  if (!document.querySelector("#position").textContent.includes("선택하세요")) {
    await move(Number(document.querySelector("#index").value));
  }
}

document.querySelector("#applySrssLimit").onclick = async () => {
  const device = window._srssDevice || document.querySelector("#mac").value.trim();
  if (!device) {
    alert("MEMS 장치 MAC이 없습니다. ADXL 파일을 먼저 불러오세요.");
    return;
  }
  const response = await fetch("/api/analysis/srss-limit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device, value: num(srssLimit.value, 4.5) }),
  });
  const data = await readBody(response);
  if (!response.ok) {
    alert(data.detail || "SRSS 한계값 저장 실패");
    return;
  }
  srssLimit.value = data.value;
  if (!document.querySelector("#position").textContent.includes("선택하세요")) {
    await move(Number(document.querySelector("#index").value));
  }
};
document.querySelector("#saveG").onclick = () =>
  saveJson("/api/analysis/mems-scale", {
    g_per_lsb: num(adxlG.value, 0.00390625),
  }).catch((error) => alert(error.message));
document.querySelector("#saveCorrection").onclick = () =>
  saveJson("/api/analysis/mems-correction", {
    correction: viewOptions().adxl.correction,
  }).catch((error) => alert(error.message));

showPanels();
loadDefaults()
  .then(() => {
    ready = true;
  })
  .catch(() => {
    ready = true;
  });

setInterval(async () => {
  const data = await (await fetch("/api/generator/status")).json();
  stats.textContent =
    `Publish ${data.sent} / PUBACK ${data.acked || 0} / Drop ${data.dropped} | ` +
    Object.entries(data.counter)
      .map(([name, count]) => `${name} ${count}`)
      .join(", ") +
    (data.running ? " | 전송 중" : " | 대기");
  log.textContent = data.logs.join("\n");
}, 1000);
