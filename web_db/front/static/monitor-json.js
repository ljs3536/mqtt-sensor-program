/*
 * 센서 수신 모니터 · 센서 명령의 '보낼 JSON / 현재 retain' 칸 (2026-11-27).
 *
 * 전에는 '전송 JSON' 칸 하나에 방금 보낸 JSON과, MAC 하나를 고르면 1초마다 그 MAC의 retain 값이 번갈아 보였다.
 * 이름과 내용이 달라 두 탭으로 나눴다.
 *
 * 보낼 JSON (고칠 수 있음)
 * - 위 칸(종류·주기·Fs+N·센서 타입·G Range·게인)을 바꾸면 서버가 '설정 적용'으로 보낼 JSON을 만들어 채운다
 *   (POST /api/monitor/command/preview. 실제 전송과 같은 함수라 화면과 실제가 어긋나지 않는다).
 * - 직접 고치면 '고친 내용'으로 표시하고, 그 뒤로는 칸을 바꿔도 덮어쓰지 않는다. '칸 값으로 다시 채우기'로 되돌린다.
 * - 'JSON 그대로 보내기': 체크한 MAC(+직접 입력 MAC)의 S/{MAC}에 그대로 retain으로 보낸다(action "raw").
 *   설정 적용과 같은 QoS 1·retain·DB 기록. JSON 형식과 1024바이트(CR 포함)는 입력할 때와 서버에서 두 번 본다.
 *   SENSORTYPE "0"(정지)이 있으면 STOP 3단계가 아니라 한 번만 보낸다고 경고한다. 숫자 값(4)은 그대로 숫자로 나간다고 알린다
 *   (기존 명령은 모두 글자 "4").
 * - START·STOP·설정 적용·조회 버튼은 지금처럼 칸 값으로 바로 보낸다(이 칸의 고친 내용과 상관없다).
 *
 * 현재 retain (읽기 전용)
 * - 명령 대상이 MAC 하나일 때 그 MAC의 마지막 S/{MAC} 설정(서버가 받은 retain 또는 방금 보낸 것)과 받은 시각.
 * - '보낼 JSON으로 가져오기'로 지금 값을 고쳐 다시 보낼 수 있다.
 *
 * 위 칸 맨 오른쪽(jsonMeta)에는 마지막으로 보낸 명령(시각·종류·대상 수)을 보인다. 보낸 JSON 전체는 마우스를 올리면 보인다.
 */
const MonitorJson = (() => {
  const $ = (selector) => document.querySelector(selector);
  const raw = $("#rawJson");
  const rawNote = $("#rawNote");
  const rawReset = $("#rawReset");
  const sendButton = $("#sendRaw");
  const meta = $("#jsonMeta");
  const retainBox = $("#retainJson");
  const retainNote = $("#retainNote");
  const retainDot = $("#retainDot");
  const retainToSend = $("#retainToSend");
  const COMMAND_MAX_BYTES = 1024; // 서버 COMMAND_MAX_BYTES와 같다(CR 포함)

  let previewText = ""; // 칸 값으로 만든 마지막 JSON(보기 좋게 줄 바꿈)
  let previewNote = "";
  let edited = false; // 사용자가 직접 고쳤는지
  let previewTimer = 0;
  let previewSeq = 0;
  let configs = {}; // 서버 상태의 MAC별 마지막 설정
  let configTimes = {};

  const pretty = (value) => JSON.stringify(value, null, 2);
  const byteLength = (text) => new TextEncoder().encode(text).length;

  /* ---------- 탭 ---------- */
  function showTab(name) {
    document.querySelectorAll("[data-json-tab]").forEach((tab) => {
      const on = tab.dataset.jsonTab === name;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll("[data-json-pane]").forEach((pane) => { pane.hidden = pane.dataset.jsonPane !== name; });
  }
  document.querySelectorAll("[data-json-tab]").forEach((tab) => tab.addEventListener("click", () => showTab(tab.dataset.jsonTab)));

  /* ---------- 보낼 JSON: 칸 값 → 서버 미리보기 ---------- */
  /** 위 칸 값으로 서버에 '설정 적용' JSON을 만들어 달라고 한다. 칸을 빠르게 바꾸면 마지막 한 번만 부른다. */
  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(loadPreview, 200);
  }
  async function loadPreview() {
    const my = ++previewSeq;
    try {
      const { response, data } = await postJson("/api/monitor/command/preview", commandFields());
      if (my !== previewSeq) return; // 그 사이 칸이 또 바뀌었다
      if (!response.ok) {
        previewText = "";
        previewNote = data.detail || "칸 값을 확인하세요";
      } else {
        previewText = pretty(data.payload);
        previewNote = data.note || "";
      }
    } catch (error) {
      if (my !== previewSeq) return;
      previewNote = "미리보기를 만들지 못했습니다";
    }
    if (!edited) raw.value = previewText;
    checkRaw();
  }

  /** 지금 칸의 JSON을 검사해 아래 안내·보내기 버튼을 맞춘다. 돌려주는 값: 보낼 객체 또는 null. */
  function checkRaw() {
    rawReset.hidden = !edited;
    const text = raw.value.trim();
    if (!text) {
      setNote(rawNote, previewNote || "보낼 JSON이 없습니다", previewNote ? "warn" : "");
      sendButton.disabled = true;
      return null;
    }
    let value;
    try {
      value = JSON.parse(text);
    } catch (error) {
      setNote(rawNote, `JSON 형식 오류: ${error.message}`, "bad");
      sendButton.disabled = true;
      return null;
    }
    if (!value || typeof value !== "object" || Array.isArray(value) || !Object.keys(value).length) {
      setNote(rawNote, "JSON 객체({ … })여야 합니다", "bad");
      sendButton.disabled = true;
      return null;
    }
    const size = byteLength(JSON.stringify(value)) + 1;
    if (size > COMMAND_MAX_BYTES) {
      setNote(rawNote, `${size}바이트 · ${COMMAND_MAX_BYTES}바이트(CR 포함)를 넘어 보낼 수 없습니다`, "bad");
      sendButton.disabled = true;
      return null;
    }
    const parts = [edited ? "직접 고친 내용" : "칸 값으로 만든 '설정 적용' JSON", `${size}바이트`];
    if (!edited && previewNote) parts.push(previewNote);
    setNote(rawNote, parts.join(" · "), edited ? "edit" : (previewNote ? "warn" : ""));
    sendButton.disabled = false;
    return value;
  }

  function setNote(box, text, tone) {
    box.textContent = text;
    box.dataset.tone = tone || "";
  }

  raw.addEventListener("input", () => {
    edited = raw.value.trim() !== previewText.trim();
    checkRaw();
  });
  rawReset.addEventListener("click", () => {
    edited = false;
    raw.value = previewText;
    checkRaw();
  });
  // 위 칸이 바뀌면 다시 만든다(종류를 바꾸면 Fs+N 목록·센서 타입도 같이 바뀌므로 change를 칸 묶음에서 받는다).
  ["change", "input"].forEach((type) => $(".cmd-grid").addEventListener(type, schedulePreview));

  /* ---------- JSON 그대로 보내기 ---------- */
  sendButton.addEventListener("click", async () => {
    const value = checkRaw();
    if (!value) return;
    const macs = commandTargets();
    if (!macs.length) {
      alert("체크된 MAC이 없고, 직접 입력한 MAC도 없습니다.");
      return;
    }
    const compact = JSON.stringify(value);
    const warnings = [];
    if (String(value.SENSORTYPE ?? "").trim() === "0") {
      warnings.push("· SENSORTYPE 0(정지)이 들어 있습니다. STOP 버튼은 3단계(STOP → retain 삭제 → normal)로 보내지만, 여기서는 이 JSON을 한 번만 보냅니다.");
    }
    const numbers = Object.entries(value).filter(([, item]) => typeof item === "number").map(([key]) => key);
    if (numbers.length) warnings.push(`· 숫자 값이 있습니다(${numbers.join(", ")}). 기존 명령은 모두 글자("4")로 보냅니다. 적힌 그대로 숫자로 보냅니다.`);
    const message = `${macs.join(", ")} 주소의 S/{MAC}에 아래 JSON을 retain으로 보낼까요?\n\n${compact}` +
      (warnings.length ? `\n\n${warnings.join("\n")}` : "");
    if (!confirm(message)) return;
    sendButton.disabled = true;
    const { response, data } = await postJson("/api/monitor/command", { action: "raw", macs, raw: raw.value })
      .finally(() => { sendButton.disabled = false; });
    if (!response.ok) {
      alert(data.detail || "전송 실패");
      return;
    }
    sent("JSON 그대로", macs, data.payload);
    refresh().catch(() => {});
  });

  /* ---------- 현재 retain ---------- */
  /** 상태 응답의 MAC별 설정을 받아 둔다(monitor-api.js refresh가 부른다). */
  function update(data) {
    configs = data.configs || {};
    configTimes = data.config_times || {};
    showRetain();
  }

  /** 명령 대상이 MAC 하나면 그 MAC의 retain을 보인다. 선택이 바뀔 때도 부른다. */
  function showRetain() {
    const targets = commandTargets();
    const mac = targets.length === 1 ? targets[0] : "";
    const value = mac ? configs[mac] : null;
    retainDot.hidden = !value;
    retainToSend.disabled = !value;
    retainBox.textContent = value ? pretty(value) : "";
    if (!targets.length) setNote(retainNote, "MAC을 하나 고르면 그 MAC의 S/{MAC} retain 값을 보여 줍니다", "");
    else if (!mac) setNote(retainNote, `${targets.length}개를 골랐습니다. retain은 MAC 하나를 골랐을 때만 보입니다`, "");
    else if (!value) setNote(retainNote, `S/${mac} · 아직 받은 설정이 없습니다. '현재 설정 조회'로 물어볼 수 있습니다`, "warn");
    else setNote(retainNote, `S/${mac}${configTimes[mac] ? ` · 받은 시각 ${configTimes[mac]}` : ""}`, "");
  }
  retainToSend.addEventListener("click", () => {
    if (retainToSend.disabled) return;
    raw.value = retainBox.textContent;
    edited = raw.value.trim() !== previewText.trim();
    showTab("send");
    checkRaw();
    raw.focus();
  });
  // 고른 MAC이 바뀌면 바로 다시 본다(1초 갱신을 기다리지 않음).
  // monitor.js의 #macs.onchange(selectedMacs 갱신)보다 먼저 불릴 수 있어 한 박자 뒤에 본다.
  $("#macs").addEventListener("change", () => setTimeout(showRetain));
  $("#manualMac").addEventListener("input", showRetain);
  ["#selectAll", "#clearMacs"].forEach((id) => $(id).addEventListener("click", () => setTimeout(showRetain)));

  /* ---------- 보낸 기록 ---------- */
  /** 마지막으로 보낸 명령을 위 칸 오른쪽에 짧게 보인다. payload = 서버가 실제로 보낸 글자. */
  function sent(label, macs, payload) {
    const time = new Date().toTimeString().slice(0, 8);
    meta.textContent = `${time} ${label} → ${macs.length === 1 ? macs[0] : `${macs.length}개 MAC`}`;
    meta.title = `보낸 JSON:\n${payload || ""}\n\n대상: ${macs.join(", ")}`;
    meta.dataset.tone = "ok";
  }
  function status(text) {
    meta.textContent = text;
    meta.title = "";
    meta.dataset.tone = "busy";
  }

  checkRaw();
  showRetain();
  return { schedulePreview, update, showRetain, sent, status };
})();

/** 명령·미리보기에 보내는 칸 값(monitor-api.js command와 같은 이름). */
function commandFields() {
  return {
    sensor: sensor.value,
    period: period.value.trim(),
    profile: profile.value,
    custom: !!document.querySelector("#customFsn")?.checked,
    custom_rate: document.querySelector("#customRate")?.value.trim() || "",
    custom_count: document.querySelector("#customCount")?.value.trim() || "",
    sensor_type: document.querySelector("#sensorType") ? sensorType.value : "",
    g_range: document.querySelector("#gRange") ? gRange.value : "16",
    piezo_gain: document.querySelector("#piezoGain") ? piezoGain.value : "30",
  };
}
