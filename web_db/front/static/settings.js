const deviceRows = document.querySelector("#deviceRows");
const settingsNote = document.querySelector("#settingsNote");

/** 설정 화면 안내 문장을 바꾼다. */
function note(text) {
  settingsNote.textContent = text;
}

/** 위험 기준(SRSS 한계) 입력이 0보다 큰 숫자인지 확인한다. 아니면 null. */
function limitValue(input) {
  const value = Number(input.value);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}

/**
 * 표시 이름·설치 위치와 진동 구간(양호 상한·주의 상한·위험 기준)을 서버에 저장한다. zones = {good, caution, limit}.
 * 위치(device.location, 2026-11-24)는 이름과 같은 요청으로 보낸다. 통계 화면 이름 아래에 보인다.
 */
async function saveDevice(mac, name, zones, location) {
  const nameResponse = await fetch("/api/db/device-name", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mac, display_name: name, location }),
  });
  const nameData = await nameResponse.json();
  if (!nameResponse.ok) throw new Error(nameData.detail || "이름 저장 실패");
  // 양호·주의 상한을 둘 다 비우면 서버가 위험 기준의 ISO 비율(1.4/4.5, 2.8/4.5)로 계산한다(limits.zone_bounds).
  const zoneResponse = await fetch("/api/db/zones", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mac, good: zones.good, caution: zones.caution, limit: zones.limit }),
  });
  const zoneData = await zoneResponse.json();
  if (!zoneResponse.ok) throw new Error(zoneData.detail || "진동 구간 저장 실패");
  return { name: nameData.display_name, location: nameData.location || "", zones: zoneData };
}

/* ---------- 센서 사진 (MAC마다 한 장, 2026-11-23) ----------
 * 대문(센서 현황) 정보 카드 위쪽에 보인다. 서버: /api/site/sensors/{mac}/photo (routes/site.py).
 * 휴대폰 사진은 수 MB라서 올리기 전에 브라우저에서 긴 변 PHOTO_EDGE px JPEG로 줄인다.
 * 줄이지 못하면(브라우저가 못 읽는 형식 등) 원본을 그대로 보내고 서버가 형식·크기를 확인한다. */
const PHOTO_EDGE = 1600;

/** 이미지 파일을 긴 변 PHOTO_EDGE 이하 JPEG Blob으로 줄인다. 이미 작으면 원본 그대로. */
async function shrinkPhoto(file) {
  const url = URL.createObjectURL(file);
  try {
    const image = await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("이미지를 읽지 못했습니다"));
      img.src = url;
    });
    const scale = Math.min(1, PHOTO_EDGE / Math.max(image.naturalWidth, image.naturalHeight));
    if (scale === 1 && file.size <= 1.5 * 1024 * 1024) return { blob: file, name: file.name };
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(image.naturalWidth * scale);
    canvas.height = Math.round(image.naturalHeight * scale);
    canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
    return blob ? { blob, name: "photo.jpg" } : { blob: file, name: file.name };
  } catch {
    return { blob: file, name: file.name };
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** 표의 '사진' 칸: 미리보기(누르면 크게), 올리기/바꾸기, 삭제. */
function photoCell(item) {
  const cell = document.createElement("td");
  cell.className = "settings-photo";
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp";
  input.hidden = true;
  if (item.photo) {
    const link = document.createElement("a");
    link.href = item.photo;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = "크게 보기";
    const img = document.createElement("img");
    img.src = item.photo;
    img.alt = `${item.mac} 사진`;
    img.loading = "lazy";
    link.appendChild(img);
    cell.appendChild(link);
  }
  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "secondary";
  pick.textContent = item.photo ? "바꾸기" : "올리기";
  pick.onclick = () => input.click();
  cell.append(input, pick);
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    pick.disabled = true;
    note(`${item.mac} 사진을 올리는 중…`);
    try {
      const { blob, name } = await shrinkPhoto(file);
      const form = new FormData();
      form.append("file", blob, name);
      const response = await fetch(`/api/site/sensors/${encodeURIComponent(item.mac)}/photo`, { method: "POST", body: form });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "사진 올리기 실패");
      note(`${item.mac} 사진을 저장했습니다. 센서 현황에서 그 센서를 누르면 보입니다.`);
      await load();
    } catch (error) {
      note(error.message);
      pick.disabled = false;
    }
  };
  if (item.photo) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "stop";
    remove.textContent = "삭제";
    remove.onclick = async () => {
      if (!confirm(`${item.mac} 사진을 지웁니다.`)) return;
      const response = await fetch(`/api/site/sensors/${encodeURIComponent(item.mac)}/photo`, { method: "DELETE" });
      const data = await response.json();
      if (!response.ok) {
        note(data.detail || "사진 삭제 실패");
        return;
      }
      note(`${item.mac} 사진을 지웠습니다.`);
      await load();
    };
    cell.appendChild(remove);
  }
  return cell;
}

/** 장치 한 줄을 표에 넣고 저장·삭제 버튼을 서버에 연결한다. */
function appendRow(item) {
  const tr = document.createElement("tr");
  const macCell = document.createElement("td");
  macCell.className = "settings-mac";
  macCell.textContent = item.mac;
  // '표시'(2026-11-24): 끄면 이름이 있어도 센서 현황·분석·통계에서 숨긴다. 누르는 즉시 저장(수신·통계 계산은 그대로).
  // 이름이 없으면 어차피 안 보이므로 칸을 끈다.
  const showCell = document.createElement("td");
  showCell.className = "settings-show";
  const showBox = document.createElement("input");
  showBox.type = "checkbox";
  showBox.checked = item.visible == null ? true : !!item.visible;
  showBox.disabled = !String(item.display_name || "").trim();
  showBox.title = showBox.disabled ? "이름이 있어야 화면에 보입니다" : "센서 현황·분석·통계에 보이기";
  showCell.appendChild(showBox);
  showBox.onchange = async () => {
    const response = await fetch("/api/db/device-visible", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mac: item.mac, visible: showBox.checked }),
    });
    const data = await response.json();
    if (!response.ok) {
      showBox.checked = !showBox.checked;
      note(data.detail || "표시 저장 실패");
      return;
    }
    note(data.visible ? `${item.mac} 을 센서 현황·분석·통계에 보입니다.` : `${item.mac} 을 센서 현황·분석·통계에서 숨깁니다(수신·통계 계산은 계속).`);
  };
  const nameCell = document.createElement("td");
  const nameInput = document.createElement("input");
  nameInput.className = "w-name";
  nameInput.value = item.display_name || "";
  nameCell.appendChild(nameInput);
  // 설치 위치(예: '2공장 펌프 #3 모터측'). 비워도 된다.
  const locationCell = document.createElement("td");
  const locationInput = document.createElement("input");
  locationInput.className = "w-location";
  locationInput.value = item.location || "";
  locationInput.placeholder = "설치 위치";
  locationInput.maxLength = 100;
  locationCell.appendChild(locationInput);
  // 진동 구간 세 칸. 양호·주의 상한은 사람이 저장한 값(custom)일 때만 채우고, 자동이면 비운 채 자동 값을 흐리게 보인다.
  const zones = item.zones || { good: 1.4, caution: 2.8, limit: item.limit ?? 4.5, custom: false };
  const round = (value) => String(Math.round(Number(value) * 100) / 100);
  const zoneInput = (value, auto, title) => {
    const input = document.createElement("input");
    input.className = "w-limit";
    input.value = value;
    input.placeholder = auto;
    input.title = title;
    return input;
  };
  const limitCell = document.createElement("td");
  const zoneBox = document.createElement("div");
  zoneBox.className = "settings-zones";
  // 흐린 안내 글자는 '비우면 쓰일 값' = 위험 기준 × ISO 비율(1.4/4.5, 2.8/4.5, 서버 limits.zone_bounds와 같음).
  const autoGood = round((Number(zones.limit) * 1.4) / 4.5);
  const autoCaution = round((Number(zones.limit) * 2.8) / 4.5);
  const goodInput = zoneInput(zones.custom ? round(zones.good) : "", `자동 ${autoGood}`, "양호 상한: 이보다 작으면 양호, 이상이면 주의. 비우면 자동(위험 기준 × 1.4/4.5)");
  const cautionInput = zoneInput(zones.custom ? round(zones.caution) : "", `자동 ${autoCaution}`, "주의 상한: 이상이면 경고. 비우면 자동(위험 기준 × 2.8/4.5)");
  const limitInput = zoneInput(round(zones.limit), "4.5", "위험 기준(SRSS 한계): 이상이면 위험");
  const dot = (state) => {
    const mark = document.createElement("i");
    mark.className = `state-${state}`;
    mark.style.background = "var(--c)";
    return mark;
  };
  zoneBox.append(dot("good"), goodInput, dot("caution"), cautionInput, dot("warn"), limitInput, dot("alarm"));
  limitCell.appendChild(zoneBox);
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
  tr.append(showCell, macCell, nameCell, locationCell, limitCell, photoCell(item), packetCell, timeCell, actionCell);

  saveButton.onclick = async () => {
    const limit = limitValue(limitInput);
    if (limit == null) {
      note("위험 기준은 0보다 큰 숫자여야 합니다.");
      return;
    }
    const good = goodInput.value.trim();
    const caution = cautionInput.value.trim();
    if ((good === "") !== (caution === "")) {
      note("양호 상한과 주의 상한은 둘 다 넣거나 둘 다 비워 주세요(비우면 자동).");
      return;
    }
    try {
      const saved = await saveDevice(item.mac, nameInput.value.trim(), { good, caution, limit }, locationInput.value.trim());
      const z = saved.zones;
      note(`${item.mac} 이름 '${saved.name}'${saved.location ? `, 위치 '${saved.location}'` : ""}, 진동 구간 양호 < ${round(z.good)} ≤ 주의 < ${round(z.caution)} ≤ 경고 < ${round(z.limit)} ≤ 위험` +
        `${z.custom ? "" : " (양호·주의 상한 자동)"} 을 저장했습니다.`);
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
    td.colSpan = 9;
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
    note("위험 기준은 0보다 큰 숫자여야 합니다.");
    return;
  }
  try {
    // 추가할 때는 위험 기준만 받고 양호·주의 상한은 자동(ISO 비율). 표에서 따로 고칠 수 있다.
    const saved = await saveDevice(mac, document.querySelector("#newName").value.trim(), { good: "", caution: "", limit },
      document.querySelector("#newLocation").value.trim());
    document.querySelector("#newMac").value = "";
    document.querySelector("#newName").value = "";
    document.querySelector("#newLocation").value = "";
    note(`${mac} 이름 '${saved.name}', 위험 기준 ${saved.zones.limit} mm/s(양호·주의 상한 자동)를 추가했습니다.`);
    await load();
  } catch (error) {
    note(error.message);
  }
};

load().catch((error) => note(error.message));

/* ---------- 설정 탭(장치·수신 / 평면도 배치 / 통계 데이터) ----------
 * 주소 끝 #devices #plan #stats 로 바로 연다(대문의 ⚙ 버튼은 /settings#plan).
 * 평면도 배치 탭은 static/site.js(편집 모드)가 그린다. */
const TABS = ["devices", "plan", "stats"];
function showTab(name) {
  const tab = TABS.includes(name) ? name : "devices";
  document.querySelectorAll("[data-tab]").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll("[data-panel]").forEach((panel) => (panel.hidden = panel.dataset.panel !== tab));
  if (tab === "stats") {
    loadCsvImport();
    if (window.loadStatProfile) window.loadStatProfile();
  }
}
document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    history.replaceState(null, "", `#${button.dataset.tab}`);
    showTab(button.dataset.tab);
  });
});
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
showTab(location.hash.slice(1));

/* ---------- 통계 데이터: CSV 데이터 가져오기(2026-11-24, src/csv_import.py) ----------
 * POST /api/db/csv-import 로 시작하면 서버가 백그라운드로 data_csv_web의 CSV를 packet에 넣는다.
 * 도는 동안 1초마다 GET으로 진행(읽은 양 / 전체 CSV 크기)을 본다. 통계는 만들지 않으므로,
 * 끝나면 위 '지금 기준으로 다시 계산'을 누르라고 안내한다. 이미 DB에 있는 패킷은 서버가 건너뛴다. */
let csvTimer = null;
function csvText(id, text) {
  document.querySelector(id).textContent = text;
}
function paintCsvImport(data) {
  if (data.folder) csvText("#csvFolder", data.folder);
  if (data.found) csvText("#csvFound", `${data.found.files}개 파일 · ${data.found.folders}개 폴더 · ${data.found.mb} MB`);
  if (data.packets) {
    const p = data.packets;
    const fill = Number(p.fill) || 0;  // 실측이 빈 시간을 채운 줄(2026-11-24)
    csvText("#csvPackets", `${(p.live + p.csv + fill).toLocaleString()}개 (실시간 수신 ${p.live.toLocaleString()} · CSV에서 가져옴 ${p.csv.toLocaleString()}` +
      `${fill ? ` · 빈 시간 채움 ${fill.toLocaleString()}` : ""})`);
  }
  const running = !!data.running;
  document.querySelector("#csvImportRun").disabled = running;
  csvText("#csvPhase", running ? `${data.phase}…` : "");
  const bar = document.querySelector("#csvProgress");
  bar.hidden = !running;
  bar.firstElementChild.style.width = data.total ? `${Math.max(2, Math.round((data.done / data.total) * 100))}%` : "2%";
  const result = document.querySelector("#csvResult");
  result.classList.toggle("error", !!data.error);
  if (data.error) {
    result.textContent = `실패: ${data.error}`;
    result.title = "";
  } else if (data.result) {
    const r = data.result;
    const time = (value) => String(value || "").replace("T", " ").slice(0, 16);
    const range = r.first_at ? ` CSV 기간 ${time(r.first_at)} ~ ${time(r.last_at)}.` : "";
    const types = Object.entries(r.by_type || {}).map(([kind, n]) => `${kind} ${n}`).join(", ");
    const extra = [r.duplicates ? `이미 있어 건너뜀 ${r.duplicates}` : "", r.skipped ? `읽지 못한 줄 ${r.skipped}` : ""]
      .filter(Boolean).join(" · ");
    result.textContent = `${r.message}${range} 읽은 줄 ${r.rows}(${types}) · 넣음 ${r.saved}${extra ? " · " + extra : ""} · ${r.seconds}초`;
    result.title = [`수신 저장이 기다린 최대 시간 약 ${r.max_lock_ms} ms(묶음 ${r.chunks}번)`, ...(r.skipped_examples || [])].join(String.fromCharCode(10));
  } else {
    result.textContent = "";
  }
}
async function loadCsvImport() {
  const state = await fetch("/api/db/csv-import").then((response) => response.json());
  paintCsvImport(state);
  clearTimeout(csvTimer);
  if (state.running) csvTimer = setTimeout(loadCsvImport, 1000);
  // 방금 끝났으면 파일·패킷 수를 다시 받아 둔다(도는 동안 GET은 진행만 돌려준다).
  else if (state.result && !state.packets) loadCsvImport();
}
document.querySelector("#csvImportRun").onclick = async () => {
  if (!confirm("CSV 폴더의 데이터를 DB에 넣습니다. 이미 있는 패킷은 건너뜁니다. 데이터가 많으면 수십 분 걸릴 수 있고, 그동안에도 수신은 계속됩니다. 계속할까요?")) return;
  const response = await fetch("/api/db/csv-import", { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(data.detail || "시작하지 못했습니다");
    return;
  }
  loadCsvImport();
};
