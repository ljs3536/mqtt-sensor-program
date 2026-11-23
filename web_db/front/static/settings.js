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
  tr.append(macCell, nameCell, limitCell, photoCell(item), packetCell, timeCell, actionCell);

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
    td.colSpan = 7;
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

/* ---------- 설정 탭(장치·수신 / 평면도 배치 / 통계 데이터) ----------
 * 주소 끝 #devices #plan #stats 로 바로 연다(대문의 ⚙ 버튼은 /settings#plan).
 * 평면도 배치 탭은 static/site.js(편집 모드)가 그린다. */
const TABS = ["devices", "plan", "stats"];
function showTab(name) {
  const tab = TABS.includes(name) ? name : "devices";
  document.querySelectorAll("[data-tab]").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll("[data-panel]").forEach((panel) => (panel.hidden = panel.dataset.panel !== tab));
  if (tab === "stats") {
    loadCsvStats();
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

/* ---------- 통계 데이터: CSV로 시간별 통계 다시 만들기 ----------
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
    const hourText = (value) => String(value || "").replace("T", " ").slice(0, 13) + "시";
    const range = r.first_hour ? ` (${hourText(r.first_hour)} ~ ${hourText(r.last_hour)})` : "";
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
  if (!confirm("지금 통계를 모두 지우고 CSV 폴더의 데이터로 시간별 통계를 다시 만듭니다(통계 계산 기준의 가장 최근 버전). 계속할까요?")) return;
  const response = await fetch("/api/db/csv-stats", { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(data.detail || "시작하지 못했습니다");
    return;
  }
  loadCsvStats();
};
