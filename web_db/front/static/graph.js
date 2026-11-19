// RAW/FFT 두 차트의 확대, 이동, 축 고정.
// 휠은 X·Y, 왼쪽 버튼을 누른 채 휠은 X만, 오른쪽 버튼을 누른 채 휠은 Y만.
// 라이브러리 pan은 끄고, 왼쪽 드래그는 pointer capture로 직접 움직인다.
// X 눈금 사이에는 세로 점선을 그린다. 기능 설명은 docs/웹-기능상세.md.
const GraphView = (() => {
  const font = '"Malgun Gothic", "Segoe UI", sans-serif';

  /** 축 제목 스타일. */
  function title(text) {
    return {
      display: true,
      text: text || "",
      color: "#17324d",
      font: { size: 13, weight: "bold", family: font },
      padding: { top: 6, bottom: 4 },
    };
  }

  /** 서버 x축 이름을 화면 문구로 바꾼다. */
  function xText(label) {
    const text = String(label || "");
    if (text.includes("ms")) return "시간 (ms)";
    if (text.startsWith("Time")) return "시간 (s)";
    if (text.includes("kHz")) return "주파수 (kHz)";
    if (text.includes("Hz")) return "주파수 (Hz)";
    if (text === "Packet") return "패킷 번호";
    return text || "X";
  }

  /** 서버 y축 이름을 그대로 쓰거나 기본 문구를 쓴다. */
  function yText(label, fallback) {
    const text = String(label || "").trim();
    return text || fallback || "값";
  }

  /** 선형 축 옵션을 만든다. */
  function scale(text) {
    return {
      type: "linear",
      title: title(text),
      ticks: { color: "#3e5164", font: { size: 11, family: font }, maxTicksLimit: 14, autoSkip: true },
      grid: { color: "rgba(30, 51, 72, 0.16)", lineWidth: 1 },
      border: { color: "#7f93a8" },
    };
  }

  const midGrid = {
    id: "midGrid",
    afterDraw(plot) {
      const area = plot.chartArea;
      const scale = plot.scales.x;
      if (!area || !scale || !scale.ticks || scale.ticks.length < 2) return;
      const ctx = plot.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.setLineDash([3, 4]);
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(31, 106, 165, 0.45)";
      for (let i = 0; i < scale.ticks.length - 1; i += 1) {
        const left = scale.getPixelForValue(scale.ticks[i].value);
        const right = scale.getPixelForValue(scale.ticks[i + 1].value);
        if (!Number.isFinite(left) || !Number.isFinite(right)) continue;
        const mid = (left + right) / 2;
        ctx.moveTo(mid, area.top);
        ctx.lineTo(mid, area.bottom);
      }
      ctx.stroke();
      ctx.restore();
    },
  };

  /**
   * 카드 제목 줄 범례(분석 화면). 그래프 안 범례는 위에 한 줄(약 30px)을 차지해서 그래프가 작아진다.
   * 대신 같은 항목(색 + 이름)을 카드 제목(RAW/FFT) 오른쪽에 작게 그린다. 누르면 그 선을 숨기고/보인다.
   * 항목은 Chart.js 기본 generateLabels로 만들어 색·점선이 그래프와 같다.
   */
  const cardLegend = {
    id: "cardLegend",
    afterUpdate(instance) {
      const card = instance.canvas.closest(".chart-card");
      const head = card && card.querySelector("h2");
      if (!head) return;
      let box = head.querySelector(".card-legend");
      if (!box) {
        box = document.createElement("span");
        box.className = "card-legend";
        head.appendChild(box);
        box.addEventListener("click", (event) => {
          const item = event.target.closest("[data-index]");
          if (!item) return;
          const index = Number(item.dataset.index);
          instance.setDatasetVisibility(index, !instance.isDatasetVisible(index));
          instance.update("none");
        });
      }
      const items = Chart.defaults.plugins.legend.labels.generateLabels(instance);
      const html = items
        .map((item) => {
          const dash = item.lineDash && item.lineDash.length ? " dashed" : "";
          return `<button type="button" class="legend-item${item.hidden ? " off" : ""}" data-index="${item.datasetIndex}" title="눌러서 숨기기/보이기">` +
            `<i class="legend-swatch${dash}" style="border-color:${item.strokeStyle}"></i>${String(item.text).replace(/[&<>]/g, "")}</button>`;
        })
        .join("");
      if (box.innerHTML !== html) box.innerHTML = html; // 같으면 다시 그리지 않는다(마우스 깜빡임 방지)
    },
  };

  /**
   * 빈 꺾은선 차트를 만들고 확대와 메뉴를 붙인다.
   * options.cardLegend: true면 그래프 안 범례를 끄고 카드 제목 줄 범례(cardLegend)를 쓴다(분석 화면).
   */
  function chart(id, opts = {}) {
    const instance = new Chart(document.getElementById(id), {
      type: "line",
      plugins: opts.cardLegend ? [midGrid, cardLegend] : [midGrid],
      data: { datasets: [] },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        interaction: { mode: "nearest", intersect: false },
        layout: { padding: { top: 2, right: 12, bottom: 2, left: 2 } },
        plugins: {
          legend: {
            display: !opts.cardLegend,
            position: "top",
            labels: { boxWidth: 12, color: "#1c2833", font: { size: 12, family: font } },
          },
          tooltip: { enabled: true },
          zoom: {
            zoom: { wheel: { enabled: false }, pinch: { enabled: true }, mode: "xy" },
            pan: { enabled: false },
          },
        },
        scales: { x: scale("X"), y: scale("Y") },
        elements: { point: { radius: 0 }, line: { borderWidth: 1.5 } },
      },
    });
    if (typeof instance.resetZoom === "function") {
      const pluginReset = instance.resetZoom.bind(instance);
      instance.resetZoom = (mode) => {
        pluginReset(mode);
        for (const axis of ["x", "y"]) {
          delete instance.options.scales[axis].min;
          delete instance.options.scales[axis].max;
        }
        instance.update("none");
      };
    }
    bind(instance);
    return instance;
  }

  /** x, y 축 제목을 바꾼다. */
  function axes(plot, xLabel, yLabel) {
    plot.options.scales.x.title = title(xText(xLabel));
    plot.options.scales.y.title = title(yText(yLabel));
  }

  /** 커서 위치 기준으로 보이는 구간을 확대하거나 줄인다. */
  function span(current, cursor, factor) {
    if (!current || !Number.isFinite(cursor) || !Number.isFinite(current.min) || !Number.isFinite(current.max)) return null;
    let min = current.min;
    let max = current.max;
    if (current.type === "logarithmic") {
      if (!(min > 0 && max > 0 && cursor > 0)) return null;
      const center = Math.log10(cursor);
      min = 10 ** (center + (Math.log10(min) - center) * factor);
      max = 10 ** (center + (Math.log10(max) - center) * factor);
    } else {
      min = cursor + (min - cursor) * factor;
      max = cursor + (max - cursor) * factor;
    }
    return { min, max };
  }

  /** 고른 축만 휠 배율로 눈금을 바꾼다. */
  function applyZoom(plot, axes, cursors, factor) {
    let active = axes;
    if (plot.$lockX) active = active.replace("x", "");
    if (plot.$lockY) active = active.replace("y", "");
    const next = {};
    for (const axis of ["x", "y"]) {
      const current = plot.scales[axis];
      next[axis] = active.includes(axis)
        ? span(current, cursors[axis], factor)
        : current && { min: current.min, max: current.max };
    }
    for (const axis of ["x", "y"]) {
      const range = next[axis];
      if (!range || !Number.isFinite(range.min) || !Number.isFinite(range.max) || range.min === range.max) continue;
      plot.options.scales[axis].min = range.min;
      plot.options.scales[axis].max = range.max;
    }
    plot.update("none");
  }

  /** 차트 우클릭 메뉴를 닫는다. */
  function closeMenu() {
    document.querySelectorAll(".graph-menu").forEach((node) => node.remove());
  }

  /** 차트 우클릭 메뉴를 연다. */
  function openMenu(plot, x, y) {
    closeMenu();
    const menu = document.createElement("div");
    menu.className = "graph-menu";
    const actions = [
      ["X축 확대", () => applyZoom(plot, "x", { x: (plot.scales.x.min + plot.scales.x.max) / 2 }, 0.75)],
      ["X축 축소", () => applyZoom(plot, "x", { x: (plot.scales.x.min + plot.scales.x.max) / 2 }, 1.35)],
      ["전체 보기", () => { if (typeof plot.resetZoom === "function") plot.resetZoom(); }],
    ];
    actions.forEach(([label, run]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        run();
        closeMenu();
      });
      menu.appendChild(button);
    });
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
    document.body.appendChild(menu);
    const box = menu.getBoundingClientRect();
    if (box.right > window.innerWidth) menu.style.left = `${Math.max(8, x - box.width)}px`;
    if (box.bottom > window.innerHeight) menu.style.top = `${Math.max(8, y - box.height)}px`;
  }

  /** 휠 확대, 드래그 이동, 우클릭 메뉴를 차트에 붙인다. */
  function bind(plot) {
    const canvas = plot.canvas;
    const held = new Set();
    let wheeled = false;
    canvas.style.touchAction = "none";
    canvas.style.cursor = "grab";
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 && event.button !== 2) return;
      held.add(event.button);
      wheeled = false;
      plot.$axisLock = false;
      closeMenu();
      if (event.button === 0) {
        event.preventDefault();
        canvas.setPointerCapture(event.pointerId);
        canvas.style.cursor = "grabbing";
        plot.$drag = {
          id: event.pointerId,
          x: event.clientX,
          y: event.clientY,
          xMin: plot.scales.x.min,
          xMax: plot.scales.x.max,
          yMin: plot.scales.y.min,
          yMax: plot.scales.y.max,
        };
      }
    }, true);
    window.addEventListener("pointermove", (event) => {
      const drag = plot.$drag;
      if (!drag || event.pointerId !== drag.id || plot.$axisLock) return;
      const area = plot.chartArea;
      if (!area || !area.width || !area.height) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (dx === 0 && dy === 0) return;
      if (!plot.$lockX && Number.isFinite(drag.xMin) && Number.isFinite(drag.xMax)) {
        const shift = dx * (drag.xMax - drag.xMin) / area.width;
        plot.options.scales.x.min = drag.xMin - shift;
        plot.options.scales.x.max = drag.xMax - shift;
      }
      if (!plot.$lockY && Number.isFinite(drag.yMin) && Number.isFinite(drag.yMax)) {
        const shift = dy * (drag.yMax - drag.yMin) / area.height;
        plot.options.scales.y.min = drag.yMin + shift;
        plot.options.scales.y.max = drag.yMax + shift;
      }
      plot.update("none");
    });
    window.addEventListener("pointerup", (event) => {
      if (plot.$drag && plot.$drag.id === event.pointerId) {
        plot.$drag = null;
        canvas.style.cursor = "grab";
      }
    });
    window.addEventListener("pointercancel", (event) => {
      if (plot.$drag && plot.$drag.id === event.pointerId) {
        plot.$drag = null;
        canvas.style.cursor = "grab";
      }
    });
    window.addEventListener("pointerup", (event) => {
      if (!held.has(event.button)) return;
      const show = event.button === 2 && !wheeled;
      held.delete(event.button);
      if (!held.size) plot.$axisLock = false;
      if (show) openMenu(plot, event.clientX, event.clientY);
    });
    canvas.addEventListener("contextmenu", (event) => event.preventDefault());
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      const left = held.has(0) || (event.buttons & 1) !== 0;
      const right = held.has(2) || (event.buttons & 2) !== 0;
      if (left || right) {
        wheeled = true;
        plot.$axisLock = true;
      }
      const rect = canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const factor = event.deltaY < 0 ? 0.82 : 1.22;
      const cursors = {
        x: plot.scales.x.getValueForPixel(px),
        y: plot.scales.y.getValueForPixel(py),
      };
      if (right) applyZoom(plot, "y", cursors, factor);
      else if (left) applyZoom(plot, "x", cursors, factor);
      else applyZoom(plot, "xy", cursors, factor);
    }, { passive: false });
    document.addEventListener("pointerdown", (event) => {
      if (!event.target.closest(".graph-menu")) closeMenu();
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenu();
  });

  /** 축 고정 여부를 돌려준다. */
  function hold(plot, axis) {
    const scale = plot.scales[axis];
    if (!scale || !Number.isFinite(scale.min) || !Number.isFinite(scale.max)) return;
    plot.options.scales[axis].min = scale.min;
    plot.options.scales[axis].max = scale.max;
  }

  /** 축 고정 상태를 저장한다. */
  function setLock(plot, axis, locked) {
    if (axis === "x") plot.$lockX = locked;
    else plot.$lockY = locked;
    if (locked) hold(plot, axis);
    plot.update("none");
  }

  /** 입력한 최소·최대로 축을 고정한다. */
  function applyRange(plot, axis, min, max) {
    const low = Number(min);
    const high = Number(max);
    if (!Number.isFinite(low) || !Number.isFinite(high) || low === high) {
      throw new Error("최소와 최대를 서로 다른 숫자로 입력하세요");
    }
    const lower = Math.min(low, high);
    const upper = Math.max(low, high);
    plot.options.scales[axis].min = lower;
    plot.options.scales[axis].max = upper;
    if (axis === "x") plot.$lockX = true;
    else plot.$lockY = true;
    plot.update("none");
  }

  /** 축 고정을 풀고 자동 눈금으로 되돌린다. */
  function release(plot, axis) {
    delete plot.options.scales[axis].min;
    delete plot.options.scales[axis].max;
    if (axis === "x") plot.$lockX = false;
    else plot.$lockY = false;
    plot.update();
  }

  /** 그래프 아래 축 고정 버튼을 RAW와 FFT 차트에 연결한다. */
  function bindAxisBar(charts) {
    const targetBox = document.querySelector("#axisTarget");
    const lockX = document.querySelector("#lockX");
    const lockY = document.querySelector("#lockY");
    if (!targetBox || !lockX || !lockY) return;
    const chosen = () => charts[targetBox.value] || Object.values(charts)[0];
    const sync = () => {
      const plot = chosen();
      if (!plot) return;
      lockX.checked = !!plot.$lockX;
      lockY.checked = !!plot.$lockY;
      xMin.value = Number.isFinite(plot.options.scales.x.min) ? plot.options.scales.x.min : "";
      xMax.value = Number.isFinite(plot.options.scales.x.max) ? plot.options.scales.x.max : "";
      yMin.value = Number.isFinite(plot.options.scales.y.min) ? plot.options.scales.y.min : "";
      yMax.value = Number.isFinite(plot.options.scales.y.max) ? plot.options.scales.y.max : "";
    };
    targetBox.onchange = sync;
    lockX.onchange = () => { setLock(chosen(), "x", lockX.checked); sync(); };
    lockY.onchange = () => { setLock(chosen(), "y", lockY.checked); sync(); };
    document.querySelector("#applyAxis").onclick = () => {
      const plot = chosen();
      try {
        if (String(xMin.value).trim() && String(xMax.value).trim()) applyRange(plot, "x", xMin.value, xMax.value);
        if (String(yMin.value).trim() && String(yMax.value).trim()) applyRange(plot, "y", yMin.value, yMax.value);
      } catch (error) {
        alert(error.message);
      }
      sync();
    };
    document.querySelector("#autoAxis").onclick = () => {
      const plot = chosen();
      release(plot, "x");
      release(plot, "y");
      sync();
    };
    sync();
  }

  /** FFT 검증용 작은 꺾은선을 캔버스에 그린다. */
  function paintLine(canvas, xs, ys, color) {
    if (!canvas) return;
    const width = Math.max(200, Math.floor(canvas.clientWidth || 320));
    const height = 110;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#f7fafc";
    ctx.fillRect(0, 0, width, height);
    const finite = [];
    for (let i = 0; i < (xs || []).length; i += 1) {
      const x = Number(xs[i]);
      const y = Number((ys || [])[i]);
      if (Number.isFinite(x) && Number.isFinite(y)) finite.push([x, y]);
    }
    if (finite.length < 2) {
      ctx.fillStyle = "#5d6e80";
      ctx.font = "12px sans-serif";
      ctx.fillText("미리보기 없음", 12, 24);
      return;
    }
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    finite.forEach(([x, y]) => {
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
    });
    const pad = 10;
    const sx = (x) => pad + ((x - minX) / (maxX - minX || 1)) * (width - pad * 2);
    const sy = (y) => height - pad - ((y - minY) / (maxY - minY || 1)) * (height - pad * 2);
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.4;
    finite.forEach(([x, y], index) => {
      const px = sx(x);
      const py = sy(y);
      if (index === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
  }

  /** FFT 검증 결과와 미리보기 그림을 결과 칸에 채운다. */
  function showFftCheck(box, data) {
    if (!box) return;
    box.hidden = false;
    const payload = data || {};
    const note = box.querySelector(".fft-check-note");
    const text = box.querySelector(".fft-check-text");
    const axisBox = box.querySelector(".fft-check-axis");
    const axisLabel = box.querySelector(".fft-check-axis-label");
    if (note) note.textContent = payload.note || "";
    const lines = [];
    if (!payload.ok) {
      lines.push(payload.message || "선택된 RAW 데이터가 없습니다.");
      lines.push("이 경우는 PASS로 처리하지 않습니다.");
    } else {
      lines.push(payload.passed ? "IFFT PASS" : "IFFT FAIL");
      lines.push("최대 절대 오차는 각 축 줄의 최대 오차입니다.");
      (payload.steps || []).forEach((step) => lines.push(step));
      (payload.axes || []).forEach((axis) => lines.push(axis.text));
    }
    if (text) text.textContent = lines.join("\n");
    const axes = payload.axes || [];
    const drawAxis = () => {
      const item = axes[Number(axisBox && axisBox.value) || 0];
      paintLine(box.querySelector("canvas.two"), item && item.two_sided && item.two_sided.f, item && item.two_sided && item.two_sided.y, "#9467BD");
      paintLine(box.querySelector("canvas.one"), item && item.single_sided && item.single_sided.f, item && item.single_sided && item.single_sided.y, "#d62728");
    };
    if (axisBox) {
      axisBox.innerHTML = axes.map((axis, index) => `<option value="${index}">${axis.label}</option>`).join("");
      axisBox.onchange = (event) => {
        event.stopPropagation();
        drawAxis();
      };
    }
    if (axisLabel) axisLabel.hidden = axes.length < 2;
    drawAxis();
    requestAnimationFrame(drawAxis);
  }

  return { chart, axes, xText, yText, bindAxisBar, showFftCheck };
})();
