const colors = { X: "#1f77b4", Y: "#2ca02c", Z: "#d62728", PIEZO: "#1377a6", ULTRASONIC: "#c05b13" };
const selectedMacs = new Set();
let profiles = { ADXL: [], PIEZO: [] };
let graphDevice = "";

function chart(id, title) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      parsing: false,
      plugins: { legend: { position: "top" }, title: { display: true, text: title } },
      scales: { x: { type: "linear", title: { display: true, text: "Time (s)" } } },
      elements: { point: { radius: 0 }, line: { borderWidth: 1 } },
    },
  });
}
const rawChart = chart("raw", "파형");
const rmsChart = chart("rms", "g RMS");

function fillProfiles() {
  const sensor = document.querySelector("#sensor").value;
  const box = document.querySelector("#profile");
  const previous = box.value;
  const items = profiles[sensor] || [];
  box.innerHTML = items.map((item) => `<option>${item}</option>`).join("");
  if (items.includes(previous)) box.value = previous;
  else if (items.length) box.value = items[items.length - 1];
}

function selectedList() {
  return [...selectedMacs];
}

function renderMacs(devices) {
  const box = document.querySelector("#macs");
  box.innerHTML = devices.map((mac) => `
    <label><input type="checkbox" value="${mac}" ${selectedMacs.has(mac) ? "checked" : ""}> ${mac}</label>
  `).join("");
  document.querySelector("#macCount").textContent = `${selectedMacs.size}개 선택`;
}

function draw(device) {
  const axes = Object.keys(device || {});
  rawChart.data.datasets = [];
  rmsChart.data.datasets = [];
  const notes = [];
  for (const axis of axes) {
    const item = device[axis];
    const color = colors[axis] || "#444";
    if (item.kind === "spectrum") {
      rawChart.data.datasets.push({
        label: axis, borderColor: color,
        data: item.x.map((x, i) => ({ x, y: item.y[i] })),
      });
      notes.push(`${axis} spectrum`);
    } else {
      rawChart.data.datasets.push({
        label: `${axis} (${item.unit})`, borderColor: color,
        data: item.t.map((x, i) => ({ x, y: item.y[i] })),
      });
      rmsChart.data.datasets.push({
        label: axis, borderColor: color,
        data: item.t.map((x, i) => ({ x, y: item.rms[i] })),
      });
      notes.push(`${axis} ${item.unit} RMS=${item.rms_value} N=${item.n} Window=${item.window} Fs=${item.fs}`);
    }
  }
  document.querySelector("#info").textContent = notes.join(" | ") || "수신 대기";
  rawChart.update();
  rmsChart.update();
}

async function refresh() {
  const data = await (await fetch("/api/monitor/state")).json();
  profiles = data.profiles || profiles;
  if (!document.querySelector("#profile").options.length) fillProfiles();
  status.textContent = data.connected
    ? `CONNECTED ${data.messages} packets | CSV ${data.csv_saved} / 오류 ${data.csv_errors}`
    : "DISCONNECTED";
  csvInfo.textContent = data.csv_dir
    ? `저장 폴더: ${data.csv_dir}`
    : "연결하면 data_csv 아래에 세션 폴더를 만들고 HEX와 decoded CSV를 저장합니다.";
  log.textContent = (data.logs || []).join("\n");
  renderMacs(data.devices || []);
  const box = document.querySelector("#device");
  const devices = Object.keys(data.latest || {});
  const previous = box.value || graphDevice;
  box.innerHTML = devices.map((id) => `<option>${id}</option>`).join("");
  if (previous && devices.includes(previous)) box.value = previous;
  graphDevice = box.value;
  draw((data.latest || {})[graphDevice] || {});
  const mac = selectedList()[0];
  if (mac && data.configs && data.configs[mac]) {
    preview.textContent = JSON.stringify(data.configs[mac], null, 2);
  }
}

async function command(action) {
  const response = await fetch("/api/monitor/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action,
      macs: selectedList(),
      sensor: sensor.value,
      period: Number(period.value),
      profile: profile.value,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.detail || "명령 실패");
    return;
  }
  preview.textContent = data.payload || "";
}

document.querySelector("#sensor").onchange = fillProfiles;
document.querySelector("#macs").onchange = (event) => {
  const input = event.target;
  if (input.checked) selectedMacs.add(input.value);
  else selectedMacs.delete(input.value);
  document.querySelector("#macCount").textContent = `${selectedMacs.size}개 선택`;
};
document.querySelector("#selectAll").onclick = () => {
  document.querySelectorAll("#macs input").forEach((input) => {
    input.checked = true;
    selectedMacs.add(input.value);
  });
  document.querySelector("#macCount").textContent = `${selectedMacs.size}개 선택`;
};
document.querySelector("#clearMacs").onclick = () => {
  selectedMacs.clear();
  document.querySelectorAll("#macs input").forEach((input) => { input.checked = false; });
  document.querySelector("#macCount").textContent = "0개 선택";
};
document.querySelector("#connect").onclick = async () => {
  const response = await fetch("/api/monitor/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host: host.value, port: Number(port.value), topic: topic.value,
      user: user.value, password: password.value, csv_enabled: csvEnabled.checked,
    }),
  });
  const data = await response.json();
  if (!response.ok) alert(data.detail || "연결 실패");
  refresh();
};
document.querySelector("#disconnect").onclick = async () => {
  await fetch("/api/monitor/disconnect", { method: "POST" });
  refresh();
};
document.querySelector("#rmsWindow").onchange = () => {
  fetch("/api/monitor/window", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ window: rmsWindow.value }),
  }).then(refresh);
};
document.querySelector("#start").onclick = () => command("start");
document.querySelector("#stop").onclick = () => command("stop");
document.querySelector("#apply").onclick = () => command("apply");
document.querySelector("#query").onclick = () => command("query");
document.querySelector("#device").onchange = () => { graphDevice = device.value; refresh(); };
setInterval(refresh, 1000);
refresh();
