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

/** 브로커 연결과 CSV 저장 여부를 서버에 보낸다. */
document.querySelector("#connect").onclick = async () => {
  const { response, data } = await postJson("/api/monitor/connect", {
    host: host.value, port: Number(port.value), topic: topic.value,
    csv_enabled: csvEnabled.checked,
  });
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
document.querySelector("#start").onclick = () => {
  if (document.querySelector("#sensorType")) {
    sensorType.value = sensor.value === "PIEZO" ? "3" : "4";
    syncSensorType(false);
  }
  command("start");
};
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
setInterval(refresh, 1000);
