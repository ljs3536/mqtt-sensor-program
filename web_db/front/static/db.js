// 센서 데이터 분석의 DB 열기. CSV 파일 선택은 generator.js 그대로 둔다.
const dbMac = document.querySelector("#dbMac");
const dbStatus = document.querySelector("#dbStatus");
const dbMode = document.querySelector("#dbMode");
const dbLimit = document.querySelector("#dbLimit");
const dbFrom = document.querySelector("#dbFrom");
const dbTo = document.querySelector("#dbTo");
const dbFromWrap = document.querySelector("#dbFromWrap");
const dbToWrap = document.querySelector("#dbToWrap");

function dbSensor() {
  return document.querySelector("#sensor").value;
}

function describeStats(stats) {
  if (!stats || !stats.measurements) return "측정 통계 없음";
  const srss = stats.srss_avg == null ? "-" : Number(stats.srss_avg).toPrecision(6);
  const rms = stats.rms_avg == null ? "-" : Number(stats.rms_avg).toPrecision(6);
  const peak = stats.peak_max == null ? "-" : Number(stats.peak_max).toPrecision(6);
  return `측정 ${stats.measurements} | SRSS 평균 ${srss} | RMS 평균 ${rms} | PEAK 최대 ${peak} ${stats.unit || ""}`.trim();
}

async function loadDbMacs() {
  const response = await fetch("/api/db/devices");
  const data = await response.json();
  const current = dbMac.value;
  dbMac.innerHTML = "";
  if (!data.length) {
    dbMac.innerHTML = '<option value="">저장된 MAC 없음</option>';
    dbStatus.textContent = "SQLite에 수신 기록이 없습니다.";
    return;
  }
  data.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.mac;
    const name = item.display_name ? ` ${item.display_name}` : "";
    option.textContent = `${item.mac}${name} (${item.packets})`;
    dbMac.appendChild(option);
  });
  if ([...dbMac.options].some((option) => option.value === current)) dbMac.value = current;
}

async function showDbStats() {
  if (!dbMac.value) return;
  const response = await fetch(`/api/db/stats?mac=${encodeURIComponent(dbMac.value)}`);
  const data = await response.json();
  if (!response.ok) {
    dbStatus.textContent = data.detail || "통계 조회 실패";
    return;
  }
  dbStatus.textContent = describeStats(data);
}

document.querySelector("#dbReload").onclick = () => {
  loadDbMacs().then(showDbStats).catch((error) => {
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
    alert(data.detail || "DB 열기 실패");
    return;
  }
  document.querySelector("#sensor").value = sensor;
  const how = mode === "range" ? "기간" : `최근 ${limit}`;
  dbStatus.textContent = `${how} ${data.count}세트를 열었습니다. ${describeStats(data.stats)}`;
  draw(data.preview);
};

dbMode.addEventListener("change", () => {
  const range = dbMode.value === "range";
  dbFromWrap.hidden = !range;
  dbToWrap.hidden = !range;
  dbStatus.textContent = range ? "시작~끝 시각의 세트를 엽니다. 최대 200세트입니다." : "최근 개수만큼 세트를 엽니다.";
});

loadDbMacs().catch((error) => {
  dbStatus.textContent = error.message;
});
