/*
 * HPF / LPF / Notch 필터 칸. 수신 모니터(/)와 분석 화면(/generator, /studio)이 같이 쓴다.
 * (예전에는 monitor-draw.js와 generator-draw.js에 같은 코드가 두 벌 있었다.)
 *
 * 줄마다: RAW 체크, FFT 체크, 값(차단·중심 주파수 Hz), 차수/폭, 그리고 그 아래 실제 계산식.
 * 계산식 문구는 발생기 v070·수신기 V057 필터 칸과 같다. 서버 signal.spectral_filter가 쓰는 식이다.
 *   HPF   |H(f)| = 1/√(1+(fc/f)^(2n)), f>0   (0 Hz는 0)
 *   LPF   |H(f)| = 1/√(1+(f/fc)^(2n))
 *         '자동 Fs/2'(기본 켜짐)면 차단 주파수가 Nyquist라서 LPF를 실제로 걸지 않는다(전 대역 통과).
 *   Notch H(f) = 1 − exp(−(f−f0)²/(2σ²)), σ = 폭/(2√(2ln2))   (폭 = 반값 전폭 FWHM)
 * 이 파일은 그리기 스크립트(monitor-draw.js, generator-draw.js)보다 먼저 불러야 한다.
 */

/** 필터 줄 HTML. items = [[이름, 값, 차수/폭], …]. LPF 줄에는 '자동 Fs/2' 체크가 붙는다. */
function filterMarkup(items) {
  return items
    .map(([name, freq, extra]) => {
      const auto =
        name === "LPF"
          ? `<label class="check" title="켜면 차단 주파수 = Fs/2(Nyquist) → LPF를 걸지 않고 전 대역 통과"><input class="auto" type="checkbox" checked><span>자동 Fs/2</span></label>`
          : "";
      return `
    <div class="row filter-row" data-filter="${name}">
      <strong>${name}</strong>
      <label class="check"><input class="raw" type="checkbox"><span>RAW</span></label>
      <label class="check"><input class="fft" type="checkbox"><span>FFT</span></label>
      ${auto}
      <label>${name === "Notch" ? "중심 Hz" : "차단 Hz"}<input class="freq" value="${freq}" style="width:80px"></label>
      <label>${name === "Notch" ? "폭 Hz" : "차수"}<input class="extra" value="${extra}" style="width:70px"></label>
      <p class="filter-note field-note"></p>
    </div>`;
    })
    .join("");
}

/** 숫자로 바꿀 수 없으면 기본값. 이 파일만으로도 동작하도록 따로 둔다. */
function filterNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

/** 필터 표의 체크와 값을 서버 옵션 객체로 읽는다. auto는 LPF 줄에만 있다. */
function readFilters(rootId) {
  const filters = {};
  document.querySelectorAll(`${rootId} [data-filter]`).forEach((row) => {
    const auto = row.querySelector(".auto");
    filters[row.dataset.filter] = {
      raw: row.querySelector(".raw").checked,
      fft: row.querySelector(".fft").checked,
      freq: filterNumber(row.querySelector(".freq").value, 0),
      extra: filterNumber(row.querySelector(".extra").value, 1),
      ...(auto ? { auto: auto.checked } : {}),
    };
  });
  return filters;
}

/** 한 줄의 계산식 문구를 지금 값으로 다시 쓴다. LPF 자동이면 값 칸을 잠근다. */
function refreshFilterNote(row) {
  const note = row.querySelector(".filter-note");
  if (!note) return;
  const name = row.dataset.filter;
  const hz = filterNumber(row.querySelector(".freq").value, NaN);
  const val = filterNumber(row.querySelector(".extra").value, NaN);
  const auto = row.querySelector(".auto");
  const freqBox = row.querySelector(".freq");
  if (auto) {
    freqBox.readOnly = auto.checked;
    freqBox.classList.toggle("locked", auto.checked);
  }
  const state = `RAW ${row.querySelector(".raw").checked ? "ON" : "OFF"} · FFT ${row.querySelector(".fft").checked ? "ON" : "OFF"}`;
  let eq;
  if (name === "LPF" && auto && auto.checked) {
    eq = "Fs/2 = Nyquist → 필터 우회";
  } else if (!Number.isFinite(hz) || !Number.isFinite(val)) {
    eq = "수치 입력을 확인하세요";
  } else if (name === "HPF") {
    eq = `|H(f)|=1/√(1+(${hz}/f)^(2×${val})), f>0`;
  } else if (name === "LPF") {
    eq = `|H(f)|=1/√(1+(f/${hz})^(2×${val}))`;
  } else {
    eq = `H(f)=1−exp(−(f−${hz})²/(2σ²)), σ=${val}/(2√(2ln2))`;
  }
  note.textContent = `${state} | ${eq}`;
}

/** 페이지 안의 모든 필터 줄 문구를 채운다. 칸을 만든 직후 한 번 부른다. */
function refreshFilterNotes() {
  document.querySelectorAll("[data-filter]").forEach(refreshFilterNote);
}

// 값이나 체크가 바뀌면 그 줄 문구만 다시 쓴다(그래프 다시 그리기는 각 화면의 change 처리가 한다).
document.addEventListener("input", (event) => {
  const row = event.target.closest && event.target.closest("[data-filter]");
  if (row) refreshFilterNote(row);
});
document.addEventListener("change", (event) => {
  const row = event.target.closest && event.target.closest("[data-filter]");
  if (row) refreshFilterNote(row);
});
