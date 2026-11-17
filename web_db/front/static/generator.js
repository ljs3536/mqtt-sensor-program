/* 분석 화면 버튼과 서버 함수를 연결한다. */
const loadAdxlButton = document.querySelector("#loadAdxlCsv");
if (loadAdxlButton) {
  loadAdxlButton.onclick = () => uploadAdxlCsv().catch((error) => alert(error.message));
  for (const id of ["adxlX", "adxlY", "adxlZ"]) {
    document.querySelector("#" + id).addEventListener("change", () => {
      if (adxlX.files[0] && adxlY.files[0] && adxlZ.files[0]) {
        uploadAdxlCsv().catch((error) => alert(error.message));
      }
    });
  }
}

sensorBox.addEventListener("change", (event) => {
  const input = event.target.closest(".file");
  if (!input || !input.files[0]) return;
  const sensor = input.closest("[data-sensor]").dataset.sensor;
  upload(sensor, input.files[0]).catch((error) => alert(error.message));
});

document.querySelector("#prev").onclick = () =>
  move(Number(document.querySelector("#index").value) - 1).catch((error) => alert(error.message));
document.querySelector("#next").onclick = () =>
  move(Number(document.querySelector("#index").value) + 1).catch((error) => alert(error.message));
document.querySelector("#go").onclick = () =>
  move(Number(document.querySelector("#index").value)).catch((error) => alert(error.message));
document.querySelector("#sensor").onchange = () => {
  showPanels();
  move(Number(document.querySelector("#index").value) || 1).catch(() => {});
};

/** 지금 패킷의 RAW가 FFT 후 복원되는지 서버에 묻고 결과 칸을 연다. */
document.querySelector("#fftCheck").onclick = async () => {
  const sensor = document.querySelector("#sensor").value;
  const { response, data } = await postJson("/api/generator/fft-check", { sensor });
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

document.querySelector("#start").onclick = async () => {
  const { response, data } = await postJson("/api/generator/start", config());
  if (!response.ok) alert(data.detail || "시작 실패");
  // 시작되었으면 running=true를 받아 1초 갱신이 켜진다.
  await pollStatus().catch(() => {});
};
document.querySelector("#stop").onclick = async () => {
  await fetch("/api/generator/stop", { method: "POST" });
  // running=false를 받아 1초 갱신이 멈추고, 마지막 숫자와 '전송 중지' 로그가 남는다.
  await pollStatus().catch(() => {});
};
document.querySelector("#testBroker").onclick = async () => {
  const { response, data } = await postJson("/api/generator/test", config());
  await pollStatus().catch(() => {});
  alert(response.ok ? "MQTT 연결 성공" : data.detail || "연결 실패");
};
document.querySelector("#resetZoom").onclick = () => {
  loosen(rawChart);
  loosen(fftChart);
  rawChart.update();
  fftChart.update();
};
document.querySelector("#analysis").addEventListener("change", () => {
  if (!ready) return;
  if (!loadedWave()) return;
  move(Number(document.querySelector("#index").value)).catch((error) => alert(error.message));
});
document.querySelector("#applySrssLimit").onclick = async () => {
  const deviceId = window._srssDevice || document.querySelector("#mac").value.trim();
  if (!deviceId) {
    alert("MEMS 장치 MAC이 없습니다. ADXL 파일을 먼저 불러오세요.");
    return;
  }
  const { response, data } = await postJson("/api/analysis/srss-limit", {
    device: deviceId, value: num(srssLimit.value, 4.5),
  });
  if (!response.ok) {
    alert(data.detail || "SRSS 한계값 저장 실패");
    return;
  }
  srssLimit.value = data.value;
  if (loadedWave()) await move(Number(document.querySelector("#index").value));
};
document.querySelector("#saveG").onclick = () =>
  saveJson("/api/analysis/mems-scale", { g_per_lsb: num(adxlG.value, 0.00390625) })
    .catch((error) => alert(error.message));
document.querySelector("#saveCorrection").onclick = () =>
  saveJson("/api/analysis/mems-correction", { correction: viewOptions().adxl.correction })
    .catch((error) => alert(error.message));

showPanels();
loadDefaults()
  .then(() => { ready = true; })
  .catch(() => { ready = true; });
// 처음 한 번은 꼭 읽는다. 페이지를 다시 열었을 때 전송이 아직 돌고 있으면 그대로 이어서 갱신한다.
pollStatus().catch(() => {});
// 전송 중일 때만 1초마다 Publish/PUBACK/Drop과 로그를 가져온다.
setInterval(() => {
  if (replayLive) pollStatus().catch(() => {});
}, 1000);

const clearLog = document.querySelector("#clearLog");
if (clearLog) {
  clearLog.onclick = () => {
    fetch("/api/generator/logs/clear", { method: "POST" });
    log.textContent = "";
  };
}
