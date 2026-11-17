/* 분석 화면이 파일, 미리보기, 송신을 서버와 주고받는 부분. */

/** 송신 줄에서 해당 센서 체크를 켠다. */
function enableSensor(name) {
  const box = document.querySelector(`[data-sensor="${name}"] .enabled`);
  if (box) box.checked = true;
}

/** 고른 센서의 X/Y/Z CSV를 서버에 올리고 첫 세트를 그린다. */
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
  const response = await fetch("/api/generator/upload-adxl-csv", { method: "POST", body });
  const data = await readJson(response);
  if (!response.ok) throw new Error(data.detail || "XYZ CSV 로드 실패");
  document.querySelector("#sensor").value = "ADXL";
  enableSensor("ADXL");
  showPanels();
  await move(1);
}

/** PIEZO 또는 초음파 파일 하나를 서버에 올리고 첫 패킷을 그린다. */
async function upload(sensor, file) {
  const body = new FormData();
  body.append("sensor", sensor);
  body.append("file", file);
  const response = await fetch("/api/generator/upload", { method: "POST", body });
  const data = await readJson(response);
  if (!response.ok) throw new Error(data.detail || "업로드 실패");
  document.querySelector("#sensor").value = sensor;
  enableSensor(sensor);
  showPanels();
  await move(1);
}

/** 서버에 미리보기를 요청하고 받은 곡선을 그린다. */
async function move(index) {
  const sensor = document.querySelector("#sensor").value;
  const { response, data } = await postJson("/api/generator/preview", {
    sensor, index, options: viewOptions(),
  });
  if (!response.ok) throw new Error(data.detail || "미리보기 실패");
  draw(data);
}

/** 송신 칸의 주기, Topic, 손실 설정을 서버 본문으로 모은다. */
function config() {
  const rows = [...document.querySelectorAll("[data-sensor]")];
  const pick = (cls) =>
    Object.fromEntries(rows.map((row) => [row.dataset.sensor, row.querySelector(cls).value]));
  const checked = (cls) =>
    Object.fromEntries(rows.map((row) => [row.dataset.sensor, row.querySelector(cls).checked]));
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

/** 저장된 MEMS 눈금과 보정값을 입력칸에 채운다. */
async function loadDefaults() {
  const data = await (await fetch("/api/analysis/defaults")).json();
  const adxl = data.adxl || {};
  const correction = adxl.correction || {};
  if (adxl.g_per_lsb != null) adxlG.value = adxl.g_per_lsb;
  freqOn.checked = !!correction.frequency_enabled;
  sensOn.checked = !!correction.sensitivity_enabled;
  if (correction.frequency_divisor != null) freqDiv.value = correction.frequency_divisor;
  if (correction.low_hz != null) lowHz.value = correction.low_hz;
  if (correction.high_hz != null) highHz.value = correction.high_hz;
  if (correction.low_sensitivity != null) lowS.value = correction.low_sensitivity;
  if (correction.mid_intercept != null) midA.value = correction.mid_intercept;
  if (correction.mid_slope != null) midB.value = correction.mid_slope;
  if (correction.high_sensitivity != null) highS.value = correction.high_sensitivity;
}

/** 그래프에 패킷이 올라가 있는지 본다. 없으면 설정 변경으로 다시 그리지 않는다. */
function loadedWave() {
  const box = document.querySelector("#packetInfo") || document.querySelector("#position");
  if (!box) return false;
  const text = box.textContent || "";
  return text.trim() !== "" && !text.includes("선택하세요");
}

/** 분석 설정을 저장하고, 패킷이 있으면 같은 위치를 다시 그린다. */
async function saveJson(url, body) {
  const { response, data } = await postJson(url, body);
  if (!response.ok) throw new Error(data.detail || "저장 실패");
  if (loadedWave()) {
    await move(Number(document.querySelector("#index").value));
  }
}

/** 송신 횟수와 로그를 1초마다 갱신한다. 브로커 접속 상태는 아니다. */
async function pollStatus() {
  const data = await (await fetch("/api/generator/status")).json();
  stats.textContent =
    `Publish ${data.sent} / PUBACK ${data.acked || 0} / Drop ${data.dropped} | ` +
    Object.entries(data.counter)
      .map(([name, count]) => `${name} ${count}`)
      .join(", ") +
    (data.running ? " | 전송 중" : " | 대기");
  log.textContent = data.logs.join("\n");
  if (log) log.scrollTop = log.scrollHeight;
}
