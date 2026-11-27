/* 수신 모니터가 서버 /api/monitor 와 그래프 설정을 주고받는 부분. */

/*
 * 마지막 상태 조회에서 MQTT가 연결되어 있었는지.
 * monitor.js의 1초 갱신은 이 값이 true일 때만 서버를 부른다. 끊겨 있으면 새 패킷이 없으니
 * 화면이 바뀔 일이 없다. 연결·해제·로그 지우기·장치 선택·그래프 옵션 변경은 각자 refresh를 부른다.
 */
let monitorLive = false;

/*
 * 연결 정보 표시(2026-11-19). 서버의 수신 연결은 하나뿐이고, 설정 화면의 자동 연결로 붙었을 수도 있다.
 * 그래서 연결 중에는 입력칸 기본값(127.0.0.1)이 아니라 **서버가 실제로 붙은 값**을 칸에 채우고 잠근다.
 * 끊겨 있으면 칸을 풀고, 처음 한 번은 마지막으로 연결했던 값으로 채운다.
 * 연결은 이 화면에서만 한다. 서버는 마지막 연결을 기억해, 다시 켜지면 그 주소로 다시 연결한다(origin "auto").
 */
let savedMqttFilled = false;
let wasLive = null; // 직전 상태가 연결 중이었는지(null = 페이지를 막 연 상태)
const ORIGIN_TEXT = { auto: "서버 재시작 후 자동으로 다시 연결", manual: "이 화면에서 연결" };
function showConnection(data) {
  const live = !!data.active;
  const csvBox = document.querySelector("#csvEnabled");
  const fields = ["#host", "#port", "#topic", "#qos"].map((id) => document.querySelector(id));
  if (live) {
    fields[0].value = data.host || "";
    fields[1].value = data.port || "";
    fields[2].value = data.topic || "";
    if (data.qos != null) fields[3].value = String(data.qos);
    if (typeof data.csv_enabled === "boolean") csvBox.checked = data.csv_enabled; // 지금 연결의 실제 CSV 저장 여부
  }
  csvBox.disabled = live;
  // 끊겨 있을 때 CSV 저장은 늘 켜진 상태에서 시작한다(필요할 때만 사용자가 끈다). 연결 중에는 실제 값.
  if (!live && wasLive !== false) csvBox.checked = true;
  wasLive = live;
  // 연결 중에는 다른 값으로 착각하지 않게 칸을 잠근다. 바꾸려면 연결 해제 후 다시 연결.
  fields.forEach((input) => (input.disabled = live));
  document.querySelector("#connect").disabled = live;
  const where = `${data.host}:${data.port}`;
  const origin = ORIGIN_TEXT[data.origin] ? ` · ${ORIGIN_TEXT[data.origin]}` : "";
  const linkStatus = document.querySelector("#linkStatus");
  linkStatus.className = data.connected ? "pill on" : live ? "pill wait" : "pill off";
  linkStatus.textContent = data.connected
    ? `CONNECTED ${where} | ${data.messages} packets | CSV ${data.csv_enabled ? `${data.csv_saved} / 오류 ${data.csv_errors}` : "끔"}`
    : live ? `연결 시도 중 ${where}` : "DISCONNECTED";
  linkStatus.title = live ? `브로커 ${where}, 구독 ${data.topic} QoS ${data.qos}${origin}` : "";
  const note = document.querySelector("#connInfo");
  if (note) {
    note.textContent = live
      ? `서버 수신 연결: ${where} · 구독 ${data.topic} (QoS ${data.qos})${origin}. '연결 해제' 전까지는 서버가 다시 켜져도 이 주소로 다시 연결합니다. 바꾸려면 '연결 해제' 후 다시 연결하세요.`
      : "연결되어 있지 않습니다. 센서 데이터가 저장되지 않습니다. 칸의 값은 마지막으로 연결했던 주소입니다.";
  }
  if (!live && !savedMqttFilled) fillSavedMqtt();
}
/** 끊겨 있을 때 한 번, 마지막으로 연결했던 값으로 칸을 채운다(/api/site/mqtt). */
async function fillSavedMqtt() {
  savedMqttFilled = true;
  try {
    const data = await (await fetch("/api/site/mqtt")).json();
    const cfg = data.settings || {};
    if (data.receiver && data.receiver.active) return; // 그 사이 연결됐으면 연결 값이 우선
    if (cfg.host) document.querySelector("#host").value = cfg.host;
    if (cfg.port) document.querySelector("#port").value = cfg.port;
    if (cfg.topic) document.querySelector("#topic").value = cfg.topic;
    if (cfg.qos != null) document.querySelector("#qos").value = String(cfg.qos);
  } catch (error) {
    /* 못 읽으면 칸 기본값 그대로 */
  }
}

/*
 * 화면에 그려 둔 그래프의 지문(서버 latest_key). 2026-11-26 LTE 데이터 절약.
 * 1초 갱신은 이 값을 have로 보내고, 서버는 그래프가 그대로면 그래프(약 417KB)를 빼고 상태·로그만 보낸다.
 */
let latestKey = "";

/**
 * 연결 상태, MAC 목록, 최신 곡선을 받아 화면에 반영한다.
 * reuse=true(1초 자동 갱신·탭 복귀)면 지금 그래프의 지문을 보내 바뀌지 않은 그래프는 받지 않는다.
 * 버튼·장치 선택·그래프 옵션 변경 등 직접 부를 때는 기본값 false라 늘 그래프 전체를 받아 다시 그린다
 * (화면에서만 바뀌는 옵션도 확실히 다시 그리기 위해).
 */
async function refresh(reuse = false) {
  const params = new URLSearchParams();
  if (graphDevice) params.set("device", graphDevice);
  if (reuse && latestKey) params.set("have", latestKey);
  const query = params.toString() ? `?${params}` : "";
  const data = await (await fetch(`/api/monitor/state${query}`)).json();
  monitorLive = !!data.connected;
  profiles = data.profiles || profiles;
  if (!document.querySelector("#profile").options.length) fillProfiles();
  showConnection(data);
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
  // latest_same = 서버가 '화면 그래프 그대로'라고 알려 준 경우. 다시 그리지 않는다(확대 상태 등도 유지).
  if (!data.latest_same) draw((data.latest || {})[shown] || {});
  latestKey = data.latest_key || "";
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

/** 고른 MAC으로 설정 적용, START, STOP, 설정 조회를 보낸다. 보냈으면 true. */
async function command(action) {
  const macs = commandTargets();
  if (!macs.length) {
    alert("체크된 MAC이 없고, 직접 입력한 MAC도 없습니다.");
    return false;
  }
  const names = {
    apply: "설정 적용",
    start: "START",
    stop: "STOP",
    query: "현재 설정 조회",
  };
  const label = names[action] || action;
  if (!confirm(`${macs.join(", ")} 주소로 ${label}하겠습니까?`)) return false;
  // STOP은 MAC마다 3단계(STOP → retain 삭제 → normal)를 보내고 단계마다 PUBACK을 기다린다(최대 10초씩).
  const buttons = ["#start", "#stop", "#apply", "#query"].map((id) => document.querySelector(id)).filter(Boolean);
  buttons.forEach((button) => (button.disabled = true));
  // 설정 적용에서 센서 타입 0을 고르면 서버가 STOP 3단계로 보낸다(수신기 V063).
  const stopping = action === "stop" || (action === "apply" && document.querySelector("#sensorType") && sensorType.value === "0");
  if (stopping) preview.textContent = "STOP 3단계 전송 중(STOP → retain 삭제 → normal, 단계마다 PUBACK 대기)…";
  const { response, data } = await postJson("/api/monitor/command", {
    action,
    macs,
    sensor: sensor.value,
    // 칸 글자 그대로 보낸다. 0 이상의 정수인지는 서버가 수신기 V057과 같은 규칙으로 검사한다
    // (예전에는 Number()로 바꿔 빈칸이 0이 되고, 서버가 0을 1로 바꿨다).
    period: period.value.trim(),
    profile: profile.value,
    // 직접 입력을 켰으면 목록 대신 이 두 값(서버가 custom_profile로 검사, 잘못되면 아무것도 보내지 않음).
    custom: !!document.querySelector("#customFsn")?.checked,
    custom_rate: document.querySelector("#customRate")?.value.trim() || "",
    custom_count: document.querySelector("#customCount")?.value.trim() || "",
    sensor_type: document.querySelector("#sensorType") ? sensorType.value : "",
    g_range: document.querySelector("#gRange") ? gRange.value : "16",
    piezo_gain: document.querySelector("#piezoGain") ? piezoGain.value : "30",
  }).finally(() => buttons.forEach((button) => (button.disabled = false)));
  if (!response.ok) {
    alert(data.detail || "명령 실패");
    return false;
  }
  preview.textContent = data.payload || "";
  return true;
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
