// 센서 데이터 분석의 조회. CSV 파일 선택은 generator.js 그대로 둔다.
const dbMac = document.querySelector("#dbMac");
const dbStatus = document.querySelector("#dbStatus");
const dbMode = document.querySelector("#dbMode");
const dbLimit = document.querySelector("#dbLimit");
const dbFrom = document.querySelector("#dbFrom");
const dbTo = document.querySelector("#dbTo");
const dbFromWrap = document.querySelector("#dbFromWrap");
const dbToWrap = document.querySelector("#dbToWrap");

/** 지금 고른 분석 센서 이름을 돌려준다. */
function dbSensor() {
  return document.querySelector("#sensor").value;
}

/** measurement 한 줄을 상태 문장으로 만든다. */
function describeStats(stats) {
  if (!stats || !stats.measurements) return "측정 통계 없음";
  const srss =
    stats.srss_avg == null ? "-" : Number(stats.srss_avg).toPrecision(6);
  const rms =
    stats.rms_avg == null ? "-" : Number(stats.rms_avg).toPrecision(6);
  const peak =
    stats.peak_max == null ? "-" : Number(stats.peak_max).toPrecision(6);
  return `측정 ${stats.measurements} | SRSS 평균 ${srss} | RMS 평균 ${rms} | PEAK 최대 ${peak} ${stats.unit || ""}`.trim();
}

/**
 * 저장된 MAC 목록을 선택 상자에 채운다.
 * 이름을 붙인 센서만(?named=true, 2026-11-24). 이름 없는 MAC은 설정 > 장치·센서 수신 모니터에서 보고 이름을 붙이면 여기 나온다.
 * 종류별 표시(2026-11-25): 지금 탭(PIEZO / MEMS / 초음파)의 종류를 설정 > 장치에서 숨긴 MAC은 목록에서 뺀다
 * (kind_view 값 0 숨김, -1 새 종류 자동 숨김). 탭을 바꾸면 서버에 다시 묻지 않고 받아 둔 목록으로 다시 그린다.
 */
let dbDevices = [];
const DB_KIND = { PIEZO: "PIEZO", ADXL: "ADXL", 초음파: "ULTRASONIC" };

async function loadDbMacs() {
  const response = await fetch("/api/db/devices?named=true");
  dbDevices = await response.json();
  fillDbMacs();
}

/** 받아 둔 MAC 목록(dbDevices)에서 지금 탭 종류가 보이는 MAC만 선택 상자에 넣는다. */
function fillDbMacs() {
  const current = dbMac.value;
  const kind = DB_KIND[dbSensor()] || "";
  const data = dbDevices.filter((item) => {
    const value = (item.kind_view || {})[kind];
    return value == null || Number(value) === 1; // 받은 적 없는 종류(키 없음)는 예전처럼 보인다
  });
  dbMac.innerHTML = "";
  if (!data.length) {
    dbMac.innerHTML = '<option value="">이름 붙인 센서 없음</option>';
    dbStatus.textContent = dbDevices.length
      ? "이 종류로 보이는 센서가 없습니다. 설정 > 장치의 종류 칸을 확인하세요."
      : "이름을 붙인 센서가 없습니다. 설정 > 장치에서 이름을 지정하세요.";
    return;
  }
  data.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.mac;
    // 패킷 수는 보이지 않는다. ADXL은 축마다 한 줄이라 세트 수와 맞지 않아 헷갈린다.
    // 이름을 앞에(같은 이름의 센서가 여럿일 수 있어 MAC도 붙인다).
    option.textContent = `${item.display_name} · ${item.mac}`;
    dbMac.appendChild(option);
  });
  if ([...dbMac.options].some((option) => option.value === current))
    dbMac.value = current;
}

// 탭을 바꾸면 그 종류 기준으로 목록을 다시 그린다(탭 처리 뒤에 #sensor 값이 바뀌므로 한 박자 늦게).
document.querySelector("#sensor").addEventListener("change", () => fillDbMacs());
document.querySelectorAll("[data-sensor-pick]").forEach((tab) =>
  tab.addEventListener("click", () => setTimeout(fillDbMacs, 0)));

/** 고른 MAC의 통계 한 줄을 상태 칸에 보여 준다. */
async function showDbStats() {
  if (!dbMac.value) return;
  const response = await fetch(
    `/api/db/stats?mac=${encodeURIComponent(dbMac.value)}`,
  );
  const data = await response.json();
  if (!response.ok) {
    dbStatus.textContent = data.detail || "통계 조회 실패";
    return;
  }
  dbStatus.textContent = describeStats(data);
}

document.querySelector("#dbReload").onclick = () => {
  loadDbMacs()
    .then(showDbStats)
    .catch((error) => {
      dbStatus.textContent = error.message;
    });
};

dbMac.onchange = () => {
  showDbStats().catch((error) => {
    dbStatus.textContent = error.message;
  });
};

document.querySelector("#dbLoad").onclick = async () => {
  if (!dbMac.value) {
    dbStatus.textContent = "열 MAC을 선택하세요.";
    return;
  }
  const mode = dbMode.value;
  const limit = Math.max(1, Math.min(Number(dbLimit.value) || 50, 200));
  if (mode === "range" && (!dbFrom.value || !dbTo.value)) {
    dbStatus.textContent = "기간의 시작과 끝을 입력하세요.";
    return;
  }
  const sensor = dbSensor();
  const response = await fetch("/api/db/load", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      mac: dbMac.value,
      sensor,
      mode,
      limit,
      start: dbFrom.value,
      end: dbTo.value,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.detail || "DB조회 실패");
    return;
  }
  document.querySelector("#sensor").value = sensor;
  const how = mode === "range" ? "기간" : `최근 ${limit}`;
  dbStatus.textContent = `${how} ${data.count}세트를 열었습니다. ${describeStats(data.stats)}`;
  draw(data.preview);
  // 'SQLite … 로드' 로그를 바로 보이게 한다. 전송 중이 아니면 1초 갱신이 없기 때문이다.
  pollStatus().catch(() => {});
};

dbMode.addEventListener("change", () => {
  const range = dbMode.value === "range";
  dbFromWrap.hidden = !range;
  dbToWrap.hidden = !range;
  dbStatus.textContent = range
    ? "시작~끝 시각의 세트를 엽니다. 최대 200세트입니다."
    : "최근 개수만큼 세트를 엽니다.";
});

/*
 * 센서 현황 팝업의 '자세히 보기'로 왔으면 그 센서를 바로 연다(2026-11-23 피드백).
 * site.js가 sessionStorage('studio-focus')에 {mac, kind}를 넣고 이 화면으로 온다. 한 번 읽고 바로 지운다
 * (주소에 MAC을 남기지 않고, 새로고침하면 다시 열지 않는다).
 * 1. 센서 종류 탭(PIEZO / MEMS / 초음파)을 고른다.  2. MAC 목록을 읽어 그 MAC을 고른다.  3. 조회를 누른다.
 */
function takeStudioFocus() {
  try {
    const focus = JSON.parse(sessionStorage.getItem("studio-focus") || "null");
    sessionStorage.removeItem("studio-focus");
    return focus && focus.mac ? focus : null;
  } catch (error) {
    return null;
  }
}

async function openFocusedSensor(focus) {
  // 센서 현황의 종류(kind) → 분석 화면의 센서 탭. 종류를 모르면(패킷 없이 설정만 있는 장치) MEMS.
  const pick = { ADXL: "ADXL", PIEZO: "PIEZO", ULTRASONIC: "초음파" }[focus.kind] || "ADXL";
  const tab = document.querySelector(`[data-sensor-pick="${pick}"]`);
  if (tab) tab.click();
  await loadDbMacs();
  if (![...dbMac.options].some((option) => option.value === focus.mac)) {
    // 목록에 없는 경우: 저장된 수신 기록이 없거나, 설정 > 장치에서 이 종류(또는 센서)를 숨겼다.
    dbStatus.textContent = `${focus.mac}는 이 종류로 열 수 있는 기록이 없습니다(수신 기록 없음 또는 설정에서 숨김).`;
    return;
  }
  dbMac.value = focus.mac;
  await showDbStats();
  document.querySelector("#dbLoad").click();
}

const studioFocus = takeStudioFocus();
(studioFocus ? openFocusedSensor(studioFocus) : loadDbMacs()).catch((error) => {
  dbStatus.textContent = error.message;
});
