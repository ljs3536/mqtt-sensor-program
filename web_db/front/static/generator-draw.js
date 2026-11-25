/* 분석 화면의 그래프 옵션과 그리기. 서버 호출은 generator-api.js가 한다. */
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
    <input class="enabled" type="checkbox" hidden ${name === "PIEZO" ? "checked" : ""}>
    <input class="topic" type="hidden" value="${topics[name]}">
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

// 필터 줄 HTML·읽기·계산식 문구는 filters.js(수신 모니터와 공용).
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
refreshFilterNotes();

/** 캔버스 id로 RAW 또는 FFT 차트를 만든다. 범례는 카드 제목 줄에(그래프 높이를 아낀다). */
function chart(id) {
  return GraphView.chart(id, { cardLegend: true });
}

/** 파일 입력 옆에 고른 파일 이름을 보여 준다. */
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

/** 숫자로 바꿀 수 없으면 기본값을 쓴다. */
function num(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

/** 요소가 없으면 기본값을 쓴다. 분석 화면마다 칸이 다르다. */
function optionalValue(id, fallback) {
  const node = document.getElementById(id);
  return node ? node.value : fallback;
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

/** 그래프 설정 칸을 미리보기 요청 본문으로 만든다. */
function viewOptions() {
  return {
    fft_log_x: fftLogX.checked,
    fft_log_y: fftLogY.checked,
    fft_avg: fftAvg.checked,
    fft_avg_count: fftAvgCount.value,
    // PSD(Welch). 칸이 없는 화면이면 꺼진 상태로 보낸다. 서버 graph_psd.psd_settings가 값을 검사한다.
    psd: {
      on: !!(document.getElementById("psdOn") || {}).checked,
      segment: optionalValue("psdSegment", "전체"),
      overlap: num(optionalValue("psdOverlap", "50"), 50),
      target: optionalValue("psdTarget", "30").trim(),
    },
    piezo: {
      // RAW·FFT 단위 따로(2026-11-25). 서버 graph_options.piezo_units.
      raw_unit: piezoRaw.value,
      fft_unit: piezoFft.value,
      vmax: num(piezoVmax.value, 3.89),
      amax: num(piezoAmax.value, 4096),
      sensitivity: num(piezoSens.value, 100),
      window: piezoWindow.value,
      // Window 보정 모드. 칸이 없는 옛 화면이면 기본 '진폭 보정'(예전과 같은 값).
      window_correction: optionalValue("piezoWindowCorrection", "진폭 보정"),
      window_n: piezoN.value,
      filters: readFilters("#piezoFilters"),
    },
    adxl: {
      axes: { X: axisX.checked, Y: axisY.checked, Z: axisZ.checked, gN: gnOn.checked, SRSS: srssOn.checked },
      raw_unit: adxlRaw.value,
      fft_unit: adxlFft.value,
      g_per_lsb: num(adxlG.value, 0.00390625),
      window: adxlWindow.value,
      window_correction: optionalValue("adxlWindowCorrection", "진폭 보정"),
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

/** CSV 분석 화면은 PIEZO와 MEMS 설정을 같이 보여 준다. 핀 화면은 studio.js가 덮어쓴다. */
function showPanels() {
  piezoBox.hidden = false;
  adxlBox.hidden = false;
}

/** 서버 배열을 Chart.js {x,y} 점으로 바꾼다. 로그 축이면 0 이하는 뺀다. */
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

/**
 * 새 패킷을 그리기 전에 확대 상태를 풀고 전체 범위로 되돌린다. 단 **고정한 축은 그대로 둔다**.
 * 예전에는 resetZoom()이 '축 적용/고정'·'X/Y축 고정'으로 넣은 min/max까지 지워서, 체크는 남아 있는데
 * 다음 패킷에서 범위가 풀렸다(2026-11-23 수정). 고정 축의 min/max를 먼저 받아 두고 resetZoom 뒤 다시 넣는다.
 */
function loosen(target) {
  const kept = {};
  ["x", "y"].forEach((axis) => {
    const locked = axis === "x" ? target.$lockX : target.$lockY;
    const scale = target.options.scales[axis] || {};
    if (locked && Number.isFinite(scale.min) && Number.isFinite(scale.max)) kept[axis] = [scale.min, scale.max];
  });
  if (typeof target.resetZoom === "function") {
    try {
      target.resetZoom("none");
    } catch (_error) {
      /* zoom plugin not active yet */
    }
  }
  Object.entries(kept).forEach(([axis, [min, max]]) => {
    target.options.scales[axis].min = min;
    target.options.scales[axis].max = max;
  });
}

/**
 * 서버가 계산한 Window 보정계수 문구를 그 센서 칸 옆에 넣는다.
 * 예: "C=N/Σw = ×2.3812 (N=8192)". 초음파처럼 FFT가 없으면 바꾸지 않는다.
 */
function showWindowFactor(preview) {
  if (!preview || !preview.window_factor) return;
  const id = preview.sensor === "PIEZO" ? "piezoWindowFactor" : preview.sensor === "ADXL" ? "adxlWindowFactor" : "";
  const box = id && document.getElementById(id);
  if (box) box.textContent = preview.window_factor;
}

/**
 * PSD 결과 칸(합성 SRSS, 한계값, 판정, 연산 단계 ①~⑥)을 채운다.
 * preview.psd는 PSD 모드(PIEZO·ADXL)에서만 온다. 없으면 칸을 숨긴다.
 * 판정 색: over=초과(빨강), ok=정상(초록), none=비교 안 함(회색).
 */
function showPsd(preview) {
  const box = document.getElementById("psdResult");
  if (!box) return;
  const psd = preview && preview.psd;
  box.hidden = !psd;
  if (!psd) return;
  document.getElementById("psdSrss").textContent = psd.srss || "";
  document.getElementById("psdLimit").textContent = psd.limit || "";
  const judge = document.getElementById("psdJudge");
  judge.textContent = psd.judgement || "";
  judge.dataset.state = psd.state || "none";
  document.getElementById("psdSteps").textContent = psd.error ? "PSD 오류: " + psd.error : psd.steps || "";
}

/** 서버 미리보기를 RAW와 FFT 차트에 그린다. 초음파 스펙트럼은 RAW 칸에만 둔다. */
function draw(preview) {
  const infoBox = document.querySelector("#packetInfo") || document.querySelector("#position");
  const bits = [];
  if (preview.sensor) bits.push(preview.sensor);
  if (preview.index && preview.count) bits.push(`${preview.index} / ${preview.count}`);
  // 그 패킷(ADXL은 세트 시작)의 수신 시각. DB·CSV에 시각이 없으면 빠진다(2026-11-23).
  if (preview.time) bits.push(preview.time);
  String(preview.info || "")
    .split("|")
    .map((part) => part.trim())
    .filter(Boolean)
    .forEach((part) => bits.push(part));
  if (preview.srss && preview.srss.text) bits.push(preview.srss.text);
  if (infoBox) {
    infoBox.innerHTML = bits
      .map((text) => `<span class="info-chip">${text.replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch]))}</span>`)
      .join("");
  }
  document.querySelector("#index").value = preview.index;
  const formulaBox = document.getElementById("formula");
  if (formulaBox) {
    formulaBox.textContent = [preview.formula, preview.avg_status]
      .filter(Boolean)
      .join("\n");
  }
  showWindowFactor(preview);
  showPsd(preview);
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
        // PSD 모드에서는 gN 자리에 X+Y+Z 합성 PSD가 온다(서버가 text로 이름을 준다).
        label: (preview.psd && preview.gn.text) || "gN",
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
  // 그래프 안 제목은 끈다(2026-11-18). RAW 제목('ADXL g' 등)은 Y축 이름과 같고,
  // FFT 제목('SRSS = …')은 위 정보 칩에 이미 있다. 그만큼 그래프가 커진다.
  rawChart.options.plugins.title = { display: false };
  fftChart.options.plugins.title = { display: false };
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
