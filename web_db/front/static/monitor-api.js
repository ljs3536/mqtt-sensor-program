/* 수신 모니터가 서버 /api/monitor 와 그래프 설정을 주고받는 부분. */

/*
 * 마지막 상태 조회에서 MQTT가 연결되어 있었는지.
 * monitor.js의 1초 갱신은 이 값이 true일 때만 서버를 부른다. 끊겨 있으면 새 패킷이 없으니
 * 화면이 바뀔 일이 없다. 연결·해제·로그 지우기·장치 선택·그래프 옵션 변경은 각자 refresh를 부른다.
 */
let monitorLive = false;

/** 연결 상태, MAC 목록, 최신 곡선을 받아 화면에 반영한다. */
async function refresh() {
  const query = graphDevice ? `?device=${encodeURIComponent(graphDevice)}` : "";
  const data = await (await fetch(`/api/monitor/state${query}`)).json();
  monitorLive = !!data.connected;
  profiles = data.profiles || profiles;
  if (!document.querySelector("#profile").options.length) fillProfiles();
  const linkStatus = document.querySelector("#linkStatus");
  linkStatus.className = data.connected ? "pill on" : "pill off";
  linkStatus.textContent = data.connected
    ? `CONNECTED ${data.messages} packets | CSV ${data.csv_saved} / 오류 ${data.csv_errors}`
    : "DISCONNECTED";
  csvInfo.textContent = data.csv_dir
    ? `저장 폴더: ${data.csv_dir}`
    : "CSV 체크 시 연결 중 수신 데이터를 바로 저장합니다. HEX와 decoded CSV를 저장 폴더에 나눕니다.";
  log.textContent = (data.logs || []).join("\n");
  const logBox = document.querySelector("#log");
  if (logBox) logBox.scrollTop = logBox.scrollHeight;
  window._monitorDevice = graphDevice || data.latest_device || "";
  renderMacs(data.devices || [], data.arrivals || {}, data.names || {});
  const box = document.querySelector("#device");
  const devices = data.graph_devices || Object.keys(data.latest || {});
  const previous = box.value;
  box.innerHTML = `<option value="">최근 수신</option>` + devices.map((id) => `<option>${id}</option>`).join("");
  if (previous && devices.includes(previous)) box.value = previous;
  else box.value = "";
  graphDevice = box.value;
  const shown = box.value || data.latest_device || "";
  draw((data.latest || {})[shown] || {});
  const mac = selectedList()[0];
  if (mac && data.configs && data.configs[mac]) {
    preview.textContent = JSON.stringify(data.configs[mac], null, 2);
  }
}

/** 화면의 그래프 옵션을 서버에 보내고 곡선을 다시 그린다. */
async function pushView() {
  await postJson("/api/monitor/view", { options: viewOptions(), window: "전체", device: graphDevice });
  await refresh();
}

/** 고른 MAC으로 설정 적용, START, STOP, 설정 조회를 보낸다. */
async function command(action) {
  const macs = commandTargets();
  if (!macs.length) {
    alert("체크된 MAC이 없고, 직접 입력한 MAC도 없습니다.");
    return;
  }
  const names = {
    apply: "설정 적용",
    start: "START",
    stop: "STOP",
    query: "현재 설정 조회",
  };
  const label = names[action] || action;
  if (!confirm(`${macs.join(", ")} 주소로 ${label}하겠습니까?`)) return;
  const { response, data } = await postJson("/api/monitor/command", {
    action,
    macs,
    sensor: sensor.value,
    period: Number(period.value),
    profile: profile.value,
    sensor_type: document.querySelector("#sensorType") ? sensorType.value : "",
    g_range: document.querySelector("#gRange") ? gRange.value : "16",
    piezo_gain: document.querySelector("#piezoGain") ? piezoGain.value : "30",
  });
  if (!response.ok) {
    alert(data.detail || "명령 실패");
    return;
  }
  preview.textContent = data.payload || "";
}

/** 저장된 g/LSB와 보정 스위치를 화면에 채운다. */
async function loadDefaults() {
  const data = await (await fetch("/api/analysis/defaults")).json();
  const adxl = data.adxl || {};
  const correction = adxl.correction || {};
  if (adxl.g_per_lsb != null) adxlG.value = adxl.g_per_lsb;
  freqOn.checked = !!correction.frequency_enabled;
  sensOn.checked = !!correction.sensitivity_enabled;
}
