const deviceRows = document.querySelector("#deviceRows");
const settingsNote = document.querySelector("#settingsNote");

/** 설정 화면 안내 문장을 바꾼다. */
function note(text) {
  settingsNote.textContent = text;
}

/** SRSS 한계 입력이 0보다 큰 숫자인지 확인한다. 아니면 null. */
function limitValue(input) {
  const value = Number(input.value);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}

/** 표시 이름과 SRSS 한계를 서버에 저장한다. */
async function saveDevice(mac, name, limit) {
  const nameResponse = await fetch("/api/db/device-name", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mac, display_name: name }),
  });
  const nameData = await nameResponse.json();
  if (!nameResponse.ok) throw new Error(nameData.detail || "이름 저장 실패");
  const limitResponse = await fetch("/api/analysis/srss-limit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device: mac, value: limit }),
  });
  const limitData = await limitResponse.json();
  if (!limitResponse.ok) throw new Error(limitData.detail || "SRSS 한계 저장 실패");
  return { name: nameData.display_name, limit: limitData.value };
}

/** 장치 한 줄을 표에 넣고 저장·삭제 버튼을 서버에 연결한다. */
function appendRow(item) {
  const tr = document.createElement("tr");
  const macCell = document.createElement("td");
  macCell.className = "settings-mac";
  macCell.textContent = item.mac;
  const nameCell = document.createElement("td");
  const nameInput = document.createElement("input");
  nameInput.className = "w-name";
  nameInput.value = item.display_name || "";
  nameCell.appendChild(nameInput);
  const limitCell = document.createElement("td");
  const limitInput = document.createElement("input");
  limitInput.className = "w-limit";
  limitInput.value = item.limit == null ? "4.5" : String(item.limit);
  limitCell.appendChild(limitInput);
  const packetCell = document.createElement("td");
  packetCell.className = "settings-meta";
  packetCell.textContent = String(item.packets || 0);
  const timeCell = document.createElement("td");
  timeCell.className = "settings-meta";
  timeCell.textContent = item.last_at || "";
  const actionCell = document.createElement("td");
  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.textContent = "저장";
  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "stop";
  deleteButton.textContent = "이름 삭제";
  deleteButton.style.marginLeft = "6px";
  actionCell.append(saveButton, deleteButton);
  tr.append(macCell, nameCell, limitCell, packetCell, timeCell, actionCell);

  saveButton.onclick = async () => {
    const limit = limitValue(limitInput);
    if (limit == null) {
      note("SRSS 한계는 0보다 큰 숫자여야 합니다.");
      return;
    }
    try {
      const saved = await saveDevice(item.mac, nameInput.value.trim(), limit);
      note(`${item.mac} 이름 '${saved.name}' , SRSS 한계 ${saved.limit} mm/s 를 저장했습니다.`);
      await load();
    } catch (error) {
      note(error.message);
    }
  };
  deleteButton.onclick = async () => {
    const message = Number(item.packets) > 0
      ? `${item.mac} 표시 이름을 지웁니다. 수신 기록은 남습니다.`
      : `${item.mac} 은 수신 기록이 없어 목록에서도 빠집니다.`;
    if (!confirm(message)) return;
    const response = await fetch(`/api/db/device-name?mac=${encodeURIComponent(item.mac)}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) {
      note(data.detail || "이름 삭제 실패");
      return;
    }
    note(data.removed ? `${data.mac} 을 목록에서 뺐습니다.` : `${data.mac} 이름을 지웠습니다.`);
    await load();
  };
  deviceRows.appendChild(tr);
}

/** 서버의 장치 목록으로 설정 표를 다시 그린다. */
async function load() {
  const response = await fetch("/api/db/devices");
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "장치 목록 실패");
  deviceRows.replaceChildren();
  if (!data.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "등록된 MAC이 없습니다.";
    tr.appendChild(td);
    deviceRows.appendChild(tr);
    return;
  }
  data.forEach(appendRow);
}

document.querySelector("#addDevice").onclick = async () => {
  const mac = document.querySelector("#newMac").value.trim();
  if (!mac) {
    note("MAC을 입력하세요.");
    return;
  }
  const limit = limitValue(document.querySelector("#newLimit"));
  if (limit == null) {
    note("SRSS 한계는 0보다 큰 숫자여야 합니다.");
    return;
  }
  try {
    const saved = await saveDevice(mac, document.querySelector("#newName").value.trim(), limit);
    document.querySelector("#newMac").value = "";
    document.querySelector("#newName").value = "";
    note(`${mac} 이름 '${saved.name}' , SRSS 한계 ${saved.limit} mm/s 를 추가했습니다.`);
    await load();
  } catch (error) {
    note(error.message);
  }
};

load().catch((error) => note(error.message));

// ---------- 수신 MQTT 자동 연결 (/api/site/mqtt) ----------
const mqttFields = {
  host: document.querySelector("#mqttHost"),
  port: document.querySelector("#mqttPort"),
  topic: document.querySelector("#mqttTopic"),
  csv: document.querySelector("#mqttCsv"),
  auto: document.querySelector("#mqttAuto"),
};
const mqttNote = document.querySelector("#mqttNote");

/** 지금 수신 연결 상태를 알약 모양 표시에 넣는다. */
function showReceiver(receiver) {
  const pill = document.querySelector("#mqttState");
  pill.className = "pill " + (receiver.connected ? "on" : "off");
  pill.textContent = receiver.connected
    ? `연결됨 ${receiver.host}:${receiver.port}${receiver.origin === "auto" ? " (자동)" : ""}`
    : receiver.active ? "연결 시도 중" : "연결 안 됨";
}

/** 저장된 값과 연결 상태를 읽어 칸을 채운다. */
async function loadMqtt() {
  const response = await fetch("/api/site/mqtt");
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "MQTT 설정 읽기 실패");
  const cfg = data.settings;
  mqttFields.host.value = cfg.host;
  mqttFields.port.value = cfg.port;
  mqttFields.topic.value = cfg.topic;
  mqttFields.csv.checked = !!cfg.csv;
  mqttFields.auto.checked = !!cfg.auto;
  showReceiver(data.receiver);
}

document.querySelector("#mqttSave").onclick = async () => {
  const body = {
    host: mqttFields.host.value.trim(),
    port: Number(mqttFields.port.value),
    topic: mqttFields.topic.value.trim(),
    csv: mqttFields.csv.checked,
    auto: mqttFields.auto.checked,
  };
  const response = await fetch("/api/site/mqtt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    mqttNote.textContent = data.detail || "저장 실패";
    return;
  }
  showReceiver(data.receiver);
  mqttNote.textContent = data.started
    ? "저장했습니다. 이 값으로 연결을 시작했습니다(브로커가 꺼져 있으면 계속 다시 시도합니다)."
    : "저장했습니다. 자동 연결이 꺼져 있어 지금 연결은 바꾸지 않았습니다.";
  // 백그라운드 연결이 붙었는지 잠시 뒤 한 번 더 본다.
  setTimeout(() => loadMqtt().catch(() => {}), 2000);
};
loadMqtt().catch((error) => (mqttNote.textContent = error.message));
