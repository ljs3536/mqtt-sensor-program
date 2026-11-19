/* 수신 모니터 버튼과 서버 함수를 연결한다. */
document.querySelector("#clearLog").onclick = () => {
  fetch("/api/monitor/logs/clear", { method: "POST" }).then(() => refresh());
};
document.querySelector("#sensor").onchange = () => {
  fillProfiles();
  syncSensorType(true);
};
document.querySelector("#sensorType")?.addEventListener("change", () => syncSensorType(false));
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

/**
 * 브로커에 연결한다(CSV 저장 체크 포함). 서버는 이 주소와 CSV 체크를 기억해 두었다가
 * '연결 해제'를 누르기 전까지는 서버가 다시 켜져도 같은 값으로 다시 연결한다(설정 화면의 수신 MQTT 칸은 없앰).
 */
document.querySelector("#connect").onclick = async () => {
  const { response, data } = await postJson("/api/monitor/connect", {
    host: host.value, port: Number(port.value), topic: topic.value,
    csv_enabled: document.querySelector("#csvEnabled").checked,
  });
  if (!response.ok) alert(data.detail || "연결 실패");
  else if (data.warning) alert(data.warning);
  refresh();
};
document.querySelector("#disconnect").onclick = async () => {
  if (!confirm("수신을 끊을까요? 끊는 동안 센서 데이터가 저장되지 않고, 서버가 다시 켜져도 자동으로 연결하지 않습니다.")) return;
  const response = await fetch("/api/monitor/disconnect", { method: "POST" });
  const data = await readJson(response).catch(() => ({}));
  if (data.warning) alert(data.warning);
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

/** 보고 있는 장치의 SRSS 한계를 저장하고 그래프를 다시 그린다. */
document.querySelector("#applySrssLimit").onclick = async () => {
  const device = graphDevice || window._monitorDevice || document.querySelector("#manualMac").value.trim();
  if (!device) {
    alert("한계값을 저장할 장치 MAC이 없습니다.");
    return;
  }
  const { response, data } = await postJson("/api/analysis/srss-limit", {
    device, value: num(srssLimit.value, 4.5),
  });
  if (!response.ok) {
    alert(data.detail || "SRSS 한계값 저장 실패");
    return;
  }
  srssLimit.value = data.value;
  pushView();
};
// START는 센서 타입 칸에서 고른 값 그대로 보낸다(수신기 V057과 같음).
// 예전에는 누르는 순간 PIEZO 3, MEMS 4(collection)로 바꿨지만, 이제는 사용자가 고른 0~4를 존중한다.
// collection으로 시작하려면 센서 타입에서 3 또는 4를 고른 뒤 START를 누른다.
document.querySelector("#start").onclick = () => command("start");
document.querySelector("#stop").onclick = () => {
  if (document.querySelector("#sensorType")) {
    sensorType.value = "0";
    syncSensorType(false);
  }
  command("stop");
};
syncSensorType(false);
document.querySelector("#apply").onclick = () => command("apply");
document.querySelector("#query").onclick = () => command("query");
document.querySelector("#device").onchange = () => { graphDevice = device.value; refresh(); };

/** 고른 RAW가 FFT 후 IFFT로 복원되는지 서버에 묻고 결과 칸을 연다. */
document.querySelector("#fftCheck").onclick = async () => {
  const { response, data } = await postJson("/api/monitor/fft-check", { device: graphDevice });
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

loadDefaults()
  .catch(() => {})
  .then(() => pushView())
  .catch(() => {})
  .then(() => { ready = true; });
// 연결 중일 때만 1초마다 최신 파형·로그·MAC 목록을 가져온다.
// 연결이 끊긴 것을 본 조회 다음부터는 서버를 부르지 않는다.
setInterval(() => {
  if (monitorLive) refresh().catch(() => {});
}, 1000);
