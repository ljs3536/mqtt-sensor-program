// 센서 데이터 분석 화면. 계산과 송신은 generator.js와 같은 API를 쓴다.
const workspace = document.querySelector("#workspace");
const settings = document.querySelector("#settings");
const openDrawer = document.querySelector("#openDrawer");
const sensorPick = document.querySelector("#sensor");

function showPanels() {
  const sensor = sensorPick.value;
  piezoBox.hidden = sensor !== "PIEZO";
  adxlBox.hidden = sensor !== "ADXL";
  ultraBox.hidden = sensor !== "초음파";
  fftCard.hidden = sensor === "초음파";
  singleFile.hidden = sensor === "ADXL";
  xyzFiles.hidden = sensor !== "ADXL";
  settingsTitle.textContent = sensor === "PIEZO" ? "PIEZO 설정" : sensor === "ADXL" ? "MEMS 설정" : "초음파 설정";
  document.querySelectorAll("[data-sensor-pick]").forEach((button) => {
    button.classList.toggle("active", button.dataset.sensorPick === sensor);
  });
}

function showDrawerTab(name) {
  document.querySelectorAll("#drawerTabs button, .studio-settings .tabs button").forEach((button) => {
    if (!button.dataset.tab) return;
    button.classList.toggle("active", button.dataset.tab === name);
  });
  settingsTab.classList.toggle("active", name === "settings");
  transmitTab.classList.toggle("active", name === "transmit");
}

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

document.querySelector("#stageFile").addEventListener("change", () => {
  const file = stageFile.files && stageFile.files[0];
  if (!file) return;
  stageFileName.textContent = file.name;
  upload(sensorPick.value, file).catch((error) => alert(error.message));
});

const originalDraw = draw;
draw = function studioDraw(preview) {
  originalDraw(preview);
  const count = preview.count || "-";
  packetLabel.textContent = `${preview.index || "-"} / ${count}`;
  showPanels();
};

showPanels();
showDrawerTab("settings");
