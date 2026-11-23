// 8081 수신 모니터. 설정 적용 JSON과 표시 이름을 SQLite에 남긴다.
const nameMac = document.querySelector("#nameMac");
const deviceName = document.querySelector("#deviceName");
const savedConfig = document.querySelector("#savedConfig");

/** 화면에 있는 MAC과 직접 입력 MAC을 이름 선택 목록으로 모은다. */
function listedMacs() {
  const values = [...document.querySelectorAll("#macs input")].map((input) => input.value).filter(Boolean);
  const manual = document.querySelector("#manualMac").value.trim();
  if (manual && !values.includes(manual)) values.push(manual);
  return values;
}

/** 이름 대상 MAC 선택 상자를 수신 목록과 맞춘다. */
function refreshNameMacs() {
  const current = nameMac.value;
  const values = listedMacs();
  nameMac.innerHTML = "";
  if (!values.length) {
    nameMac.innerHTML = '<option value="">MAC 없음</option>';
    return;
  }
  values.forEach((mac) => {
    const option = document.createElement("option");
    option.value = mac;
    // 이름이 있으면 "MAC · 이름"으로 보여 준다. 값은 MAC 그대로다.
    option.textContent = macNames[mac] ? `${mac} · ${macNames[mac]}` : mac;
    nameMac.appendChild(option);
  });
  if (values.includes(current)) nameMac.value = current;
}

/** 고른 MAC의 표시 이름과 저장된 설정 JSON을 읽어 칸에 넣는다. */
async function loadProfile() {
  if (!nameMac.value) {
    deviceName.value = "";
    return;
  }
  const response = await fetch(`/api/db/profile?mac=${encodeURIComponent(nameMac.value)}`);
  const data = await response.json();
  if (!response.ok) {
    savedConfig.textContent = data.detail || "이름 조회 실패";
    return;
  }
  deviceName.value = data.display_name || "";
  if (data.payload) {
    savedConfig.textContent = `저장된 설정 ${data.action || ""} ${data.updated_at || ""} ${JSON.stringify(data.payload)}`;
  } else {
    savedConfig.textContent = "저장된 설정 JSON이 없습니다. 설정 적용을 누르면 이 MAC에 남습니다.";
  }
}

document.querySelector("#macs").addEventListener("change", (event) => {
  const input = event.target;
  if (input && input.value && input.checked) {
    refreshNameMacs();
    nameMac.value = input.value;
    loadProfile().catch((error) => {
      savedConfig.textContent = error.message;
    });
  }
});

nameMac.addEventListener("change", () => {
  loadProfile().catch((error) => {
    savedConfig.textContent = error.message;
  });
});

document.querySelector("#saveDeviceName").onclick = async () => {
  refreshNameMacs();
  if (!nameMac.value) {
    alert("이름을 저장할 MAC을 선택하세요.");
    return;
  }
  const response = await fetch("/api/db/device-name", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mac: nameMac.value, display_name: deviceName.value.trim() }),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.detail || "이름 저장 실패");
    return;
  }
  savedConfig.textContent = `${data.mac} 이름을 '${data.display_name}'로 저장했습니다.`;
  // MAC 칩의 이름을 바로 바꾼다. 연결이 끊겨 있으면 1초 갱신이 없으므로 직접 한 번 읽는다.
  refresh().catch(() => {});
};

/**
 * 설정 적용 뒤에 이름 목록과 저장된 JSON을 다시 읽는다.
 * 원래 command()의 결과(보냈으면 true)를 그대로 돌려준다. STOP 버튼이 이 값을 보고
 * 센서 타입 칸을 normal로 돌려놓는다(예전에는 결과를 버려서 칸이 그대로 남았다).
 */
const monitorCommand = command;
command = async function saveCommand(action) {
  const sent = await monitorCommand(action);
  if (action === "apply" || action === "start" || action === "stop") {
    refreshNameMacs();
    await loadProfile();
  }
  return sent;
};

new MutationObserver(refreshNameMacs).observe(document.querySelector("#macs"), {
  childList: true,
  subtree: true,
});
refreshNameMacs();
