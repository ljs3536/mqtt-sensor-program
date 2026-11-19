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

/* ---------- 설정 탭(장치·수신 / 평면도 배치 / 통계 데이터) ----------
 * 주소 끝 #devices #plan #stats 로 바로 연다(대문의 ⚙ 버튼은 /settings#plan).
 * 평면도 배치 탭은 static/site.js(편집 모드)가 그린다. */
const TABS = ["devices", "plan", "stats"];
function showTab(name) {
  const tab = TABS.includes(name) ? name : "devices";
  document.querySelectorAll("[data-tab]").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll("[data-panel]").forEach((panel) => (panel.hidden = panel.dataset.panel !== tab));
  if (tab === "stats") loadCsvStats();
}
document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    history.replaceState(null, "", `#${button.dataset.tab}`);
    showTab(button.dataset.tab);
  });
});
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
showTab(location.hash.slice(1));

/* ---------- 통계 데이터: CSV로 일자별 통계 다시 만들기 ----------
 * POST /api/db/csv-stats 로 시작하면 서버가 백그라운드로 돈다. 도는 동안 1초마다 GET으로 진행을 본다.
 * 결과는 measurement를 통째로 바꾸므로 확인을 받는다. */
let csvTimer = null;
function csvText(id, text) {
  document.querySelector(id).textContent = text;
}
function paintCsvStats(data, dashboard) {
  csvText("#csvFolder", data.folder || "-");
  csvText("#csvGroups", `${data.groups_found ?? "-"}개 (MAC별 X/Y/Z CSV 세트 파일 묶음)`);
  if (dashboard) {
    const items = (dashboard.items || []).filter((item) => item.set_count);
    const sets = items.reduce((sum, item) => sum + item.set_count, 0);
    const stamp = String(dashboard.updated_at || "").replace("T", " ").slice(0, 16);
    csvText("#csvCurrent", items.length ? `${items.length}개 MAC · ${sets}세트 · 마지막 계산 ${stamp}` : "통계 없음");
  }
  const running = !!data.running;
  document.querySelector("#csvStatsRun").disabled = running;
  csvText("#csvPhase", running ? `${data.phase}…` : "");
  const bar = document.querySelector("#csvProgress");
  bar.hidden = !running;
  bar.firstElementChild.style.width = data.total ? `${Math.round((data.done / data.total) * 100)}%` : "5%";
  const result = document.querySelector("#csvResult");
  result.classList.toggle("error", !!data.error);
  if (data.error) {
    result.textContent = `실패: ${data.error}`;
  } else if (data.result) {
    const r = data.result;
    const range = r.first_day ? ` (${r.first_day} ~ ${r.last_day})` : "";
    const extra = [r.failed ? `계산 못 한 세트 ${r.failed}개` : "", r.duplicates ? `중복 ${r.duplicates}개 제외` : "",
      r.skipped_groups && r.skipped_groups.length ? `읽지 못한 묶음 ${r.skipped_groups.length}개` : ""].filter(Boolean).join(" · ");
    result.textContent = `${r.message}${range} · ${r.sets}세트 · ${r.seconds}초${extra ? " · " + extra : ""}`;
    result.title = (r.skipped_groups || []).join("\n");
  } else {
    result.textContent = "";
  }
}
async function loadCsvStats() {
  const [state, dashboard] = await Promise.all([
    fetch("/api/db/csv-stats").then((response) => response.json()),
    fetch("/api/db/dashboard").then((response) => response.json()),
  ]);
  paintCsvStats(state, dashboard);
  clearTimeout(csvTimer);
  if (state.running) csvTimer = setTimeout(loadCsvStats, 1000);
}
document.querySelector("#csvStatsRun").onclick = async () => {
  if (!confirm("지금 통계를 모두 지우고 CSV 폴더의 데이터로 일자별 통계를 다시 만듭니다. 계속할까요?")) return;
  const response = await fetch("/api/db/csv-stats", { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(data.detail || "시작하지 못했습니다");
    return;
  }
  loadCsvStats();
};
