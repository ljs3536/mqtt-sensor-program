/*
 * 설정 > 통계 데이터 > '통계 계산 기준'(2026-11-23). 서버: /api/db/stat-profile, /api/db/stat-rebuild.
 *
 * - 칸은 지금 버전(없으면 가장 최근 버전)의 기준으로 채운다. 고친 뒤 '저장하고 다시 계산'을 누르면
 *   새 버전으로 저장되고 서버가 packet 전체를 그 기준으로 다시 계산한다. 끝나면 그 버전이 '지금 버전'이 된다.
 *   계산 중에도 통계 화면·센서 현황은 이전 통계를 보인다.
 * - '그래프 설정 파일 값 가져오기': 그래프가 쓰는 JSON 파일(g/LSB, MEMS 보정) 값으로 환산 칸만 채운다(저장 아님).
 * - 'JSON 내보내기/가져오기': 칸의 기준을 파일로 주고받는다(다른 서버·데스크톱 프로그램과 맞출 때). 가져오기도 칸만 채운다.
 * - 재계산이 도는 동안 1초마다 진행을 다시 읽는다.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  if (!$("statProfileCard")) return;
  let lastData = null;
  let timer = null;

  /* 숫자 칸 값. 비었거나 숫자가 아니면 NaN(서버가 칸 이름과 함께 오류를 알려 준다). */
  const num = (id) => Number($(id).value);

  function fillSelect(id, values, current) {
    const box = $(id);
    if (!box.options.length) {
      values.forEach((value) => box.add(new Option(value, value)));
    }
    box.value = String(current);
  }

  /* 기준 dict → 칸 */
  function fill(settings) {
    if (!settings) return;
    const c = settings.correction || {};
    const f = settings.filters || {};
    $("spLsb").value = settings.g_per_lsb;
    $("spFreqOn").checked = !!c.frequency_enabled;
    $("spDivisor").value = c.frequency_divisor;
    $("spSensOn").checked = !!c.sensitivity_enabled;
    $("spLowHz").value = c.low_hz;
    $("spHighHz").value = c.high_hz;
    $("spLowSens").value = c.low_sensitivity;
    $("spMidIntercept").value = c.mid_intercept;
    $("spMidSlope").value = c.mid_slope;
    $("spHighSens").value = c.high_sensitivity;
    $("spHpfOn").checked = !!(f.hpf && f.hpf.on);
    $("spHpfHz").value = f.hpf ? f.hpf.hz : "";
    $("spHpfOrder").value = f.hpf ? f.hpf.order : "";
    $("spLpfOn").checked = !!(f.lpf && f.lpf.on);
    $("spLpfHz").value = f.lpf ? f.lpf.hz : "";
    $("spLpfOrder").value = f.lpf ? f.lpf.order : "";
    $("spNotchOn").checked = !!(f.notch && f.notch.on);
    $("spNotchHz").value = f.notch ? f.notch.hz : "";
    $("spNotchWidth").value = f.notch ? f.notch.width : "";
    const w = settings.window || {};
    const psd = settings.psd || {};
    fillSelect("spWindow", (lastData && lastData.windows) || [w.name], w.name || "Blackman");
    $("spBeta").value = w.beta ?? 8.6;
    fillSelect("spSegment", (lastData && lastData.segments) || [psd.segment], psd.segment || "전체");
    $("spOverlap").value = psd.overlap ?? 50;
    $("spTarget").value = psd.target ?? 30;
  }

  /* 칸 → 기준 dict(검사는 서버 stat_profile.validate가 한다) */
  function read() {
    return {
      g_per_lsb: num("spLsb"),
      correction: {
        frequency_enabled: $("spFreqOn").checked,
        sensitivity_enabled: $("spSensOn").checked,
        frequency_divisor: num("spDivisor"),
        low_hz: num("spLowHz"),
        high_hz: num("spHighHz"),
        low_sensitivity: num("spLowSens"),
        mid_intercept: num("spMidIntercept"),
        mid_slope: num("spMidSlope"),
        high_sensitivity: num("spHighSens"),
      },
      filters: {
        hpf: { on: $("spHpfOn").checked, hz: num("spHpfHz"), order: num("spHpfOrder") },
        lpf: { on: $("spLpfOn").checked, hz: num("spLpfHz"), order: num("spLpfOrder") },
        notch: { on: $("spNotchOn").checked, hz: num("spNotchHz"), width: num("spNotchWidth") },
      },
      window: { name: $("spWindow").value, beta: num("spBeta") },
      psd: { segment: $("spSegment").value, overlap: num("spOverlap"), target: num("spTarget") },
    };
  }

  const stamp = (text) => String(text || "").replace("T", " ").slice(0, 16);
  const escape = (text) => String(text || "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));

  function paintJob(job) {
    const running = !!(job && job.running);
    ["spSave", "spRebuild"].forEach((id) => ($(id).disabled = running));
    $("spPhase").textContent = running ? `${job.phase}…` : "";
    const bar = $("spProgress");
    bar.hidden = !running;
    bar.firstElementChild.style.width = job && job.total ? `${Math.round((job.done / job.total) * 100)}%` : "5%";
    const result = $("spResult");
    result.classList.toggle("error", !!(job && job.error));
    if (job && job.error) {
      result.textContent = `다시 계산 실패: ${job.error} (지금 통계는 그대로입니다)`;
    } else if (job && job.result) {
      const r = job.result;
      const extra = [r.failed_sets ? `계산 못 한 세트 ${r.failed_sets}개` : "",
        r.notes && r.notes.length ? `메모: ${r.notes.join(" / ")}` : ""].filter(Boolean).join(" · ");
      result.textContent = `${r.message} · ${r.seconds}초 · ${stamp(job.finished_at)}${extra ? " · " + extra : ""}`;
    } else {
      result.textContent = "";
    }
    return running;
  }

  function paintHistory(data) {
    $("spHistory").innerHTML = (data.items || []).map((item) => `
      <tr class="${item.active ? "active" : ""}">
        <td>v${item.id}${item.active ? " (지금)" : ""}</td>
        <td>${stamp(item.created_at)}</td>
        <td>${escape(item.summary)}</td>
        <td>${escape(item.note)}</td>
        <td>${item.settings ? `<button type="button" data-load="${item.id}">칸에 불러오기</button>` : ""}</td>
      </tr>`).join("");
    $("spHistory").querySelectorAll("[data-load]").forEach((button) => {
      button.onclick = () => {
        const item = data.items.find((row) => String(row.id) === button.dataset.load);
        if (item) fill(item.settings);
        $("spNote").value = `v${item.id} 기준으로 되돌림`;
      };
    });
  }

  let wasRunning = false;
  async function load() {
    const data = await (await fetch("/api/db/stat-profile")).json();
    const first = lastData === null;
    lastData = data;
    const shown = data.items.find((item) => item.id === data.active) || data.items[0];
    $("spActive").textContent = data.active
      ? `· 지금 v${data.active}`
      : "· 아직 계산 전(옛 하루 통계 표시 중)";
    paintHistory(data);
    clearTimeout(timer);
    const running = paintJob(data.job);
    // 처음 열 때와, 재계산이 막 끝났을 때만 칸을 지금 버전으로 채운다(고치던 칸을 덮지 않게).
    if (shown && (first || (wasRunning && !running))) fill(shown.settings);
    wasRunning = running;
    if (running) timer = setTimeout(load, 1000);
  }
  window.loadStatProfile = () => load().catch((error) => ($("spResult").textContent = error.message));
  // settings.js가 이 파일보다 먼저 돌아 #stats 탭을 이미 열었으면(주소 /settings#stats) 여기서 바로 불러온다.
  const panel = $("statProfileCard").closest("[data-panel]");
  if (panel && !panel.hidden) window.loadStatProfile();

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "실패했습니다");
    return data;
  }

  $("spSave").onclick = async () => {
    if (!confirm("이 기준을 새 버전으로 저장하고, 지금까지 받은 데이터로 통계를 다시 계산합니다. 계속할까요?")) return;
    try {
      await post("/api/db/stat-profile", { settings: read(), note: $("spNote").value });
      $("spNote").value = "";
      await load();
    } catch (error) {
      alert(error.message);
    }
  };
  $("spRebuild").onclick = async () => {
    if (!confirm("지금 기준으로 통계를 처음부터 다시 계산합니다. 계속할까요?")) return;
    try {
      await post("/api/db/stat-rebuild");
      await load();
    } catch (error) {
      alert(error.message);
    }
  };
  $("spFromFiles").onclick = () => {
    if (!lastData) return;
    fill(Object.assign({}, read(), lastData.files));
    $("spNote").value = "그래프 설정 파일 값 반영";
  };
  $("spDefaults").onclick = () => {
    if (lastData) fill(lastData.defaults);
  };
  $("spExport").onclick = () => {
    const blob = new Blob([JSON.stringify(read(), null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `stat_profile_${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  };
  $("spImport").onchange = async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      // 통계 기준 파일, 또는 데스크톱 프로그램의 mems_lsb_scale.json · mems_frequency_sensitivity.json도 받는다.
      const current = read();
      if (data.filters || data.psd || data.window) {
        fill(Object.assign(current, data));
      } else if ("g_per_lsb" in data) {
        current.g_per_lsb = data.g_per_lsb;
        fill(current);
      } else if ("frequency_divisor" in data) {
        current.correction = Object.assign(current.correction, data);
        fill(current);
      } else {
        throw new Error("통계 기준·g/LSB·MEMS 보정 JSON이 아닙니다");
      }
      $("spNote").value = `${file.name} 가져옴`;
    } catch (error) {
      alert(`가져오지 못했습니다: ${error.message}`);
    }
  };
})();
