// 센서 데이터 분석 화면. 계산과 송신은 generator.js와 같은 API를 쓴다.
const workspace = document.querySelector("#workspace");
const settings = document.querySelector("#settings");
const openDrawer = document.querySelector("#openDrawer");
const sensorPick = document.querySelector("#sensor");

/** 고른 센서만 설정 칸과 송신 줄에 보여 준다. */
function showPanels() {
  const sensor = sensorPick.value;
  piezoBox.hidden = sensor !== "PIEZO";
  adxlBox.hidden = sensor !== "ADXL";
  ultraBox.hidden = sensor !== "초음파";
  fftCard.hidden = sensor === "초음파";
  // 초음파는 320점 스펙트럼만 있어 FFT·PSD·시간 이력·FFT 검증 설정을 서버가 쓰지 않는다 → 공통 칸을 숨긴다(2026-11-25).
  const common = document.getElementById("commonBox");
  if (common) common.style.display = sensor === "초음파" ? "none" : ""; // .stack의 display가 hidden 속성을 덮으므로
  const single = document.getElementById("singleFile");
  const xyz = document.getElementById("xyzFiles");
  if (single) single.hidden = sensor === "ADXL";
  if (xyz) xyz.hidden = sensor !== "ADXL";
  settingsTitle.textContent = sensor === "PIEZO" ? "PIEZO 설정" : sensor === "ADXL" ? "MEMS 설정" : "초음파 설정";
  document.querySelectorAll("#sensors .sensor-line").forEach((row) => {
    const on = row.dataset.sensor === sensor;
    row.hidden = !on;
    const box = row.querySelector(".enabled");
    if (box) box.checked = on;
  });
  document.querySelectorAll("[data-sensor-pick]").forEach((button) => {
    button.classList.toggle("active", button.dataset.sensorPick === sensor);
  });
}

/** 서랍의 설정 탭과 송신 탭을 바꾼다. */
function showDrawerTab(name) {
  document.querySelectorAll("#drawerTabs button, .studio-settings .tabs button").forEach((button) => {
    if (!button.dataset.tab) return;
    button.classList.toggle("active", button.dataset.tab === name);
  });
  settingsTab.classList.toggle("active", name === "settings");
  transmitTab.classList.toggle("active", name === "transmit");
}

/** 서랍을 열거나 닫고 차트 크기를 다시 맞춘다. */
function setDrawerOpen(open) {
  settings.classList.toggle("hidden-drawer", !open);
  openDrawer.classList.toggle("active", open || workspace.classList.contains("pinned"));
  requestAnimationFrame(() => {
    rawChart.resize();
    if (!fftCard.hidden) fftChart.resize();
  });
}

document.querySelectorAll("[data-sensor-pick]").forEach((button) => {
  button.addEventListener("click", () => {
    sensorPick.value = button.dataset.sensorPick;
    showPanels();
    sensorPick.dispatchEvent(new Event("change"));
  });
});

document.querySelectorAll(".studio-settings .tabs button").forEach((button) => {
  button.addEventListener("click", () => showDrawerTab(button.dataset.tab));
});

openDrawer.addEventListener("click", () => {
  if (workspace.classList.contains("pinned")) {
    workspace.classList.toggle("collapsed");
    openDrawer.classList.add("active");
    return;
  }
  setDrawerOpen(settings.classList.contains("hidden-drawer"));
});
document.querySelector("#menuMode").addEventListener("click", () => {
  workspace.classList.remove("pinned", "collapsed");
  setDrawerOpen(true);
});
document.querySelector("#pinMode").addEventListener("click", () => {
  workspace.classList.add("pinned");
  workspace.classList.remove("collapsed");
  setDrawerOpen(true);
});
document.querySelector("#pinClose").addEventListener("click", () => {
  if (workspace.classList.contains("pinned")) {
    workspace.classList.add("collapsed");
    return;
  }
  setDrawerOpen(false);
});

document.querySelector("#closeFftCheck").addEventListener("click", () => {
  document.querySelector("#fftCheckBox").hidden = true;
});

const stageInput = document.getElementById("stageFile");
if (stageInput) {
  stageInput.addEventListener("change", () => {
    const file = stageInput.files && stageInput.files[0];
    if (!file) return;
    stageFileName.textContent = file.name;
    upload(sensorPick.value, file).catch((error) => alert(error.message));
  });
}

const originalDraw = draw;
/** 미리보기 위에 패킷 번호도 같이 보여 준다. */
draw = function studioDraw(preview) {
  originalDraw(preview);
  const count = preview.count || "-";
  packetLabel.textContent = `${preview.index || "-"} / ${count}`;
  showPanels();
};

showPanels();
showDrawerTab("settings");
if (document.querySelector("#pinMode")) document.querySelector("#pinMode").click();
