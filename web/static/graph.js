const GraphView = (() => {
  const font = '"Malgun Gothic", "Segoe UI", sans-serif';

  function title(text) {
    return {
      display: true,
      text: text || "",
      color: "#17324d",
      font: { size: 13, weight: "bold", family: font },
      padding: { top: 6, bottom: 4 },
    };
  }

  function xText(label) {
    const text = String(label || "");
    if (text.includes("ms")) return "시간 (ms)";
    if (text.startsWith("Time")) return "시간 (s)";
    if (text.includes("kHz")) return "주파수 (kHz)";
    if (text.includes("Hz")) return "주파수 (Hz)";
    if (text === "Packet") return "패킷 번호";
    return text || "X";
  }

  function yText(label, fallback) {
    const text = String(label || "").trim();
    return text || fallback || "값";
  }

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

  function chart(id) {
    const instance = new Chart(document.getElementById(id), {
      type: "line",
      plugins: [midGrid],
      data: { datasets: [] },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        interaction: { mode: "nearest", intersect: false },
        layout: { padding: { top: 2, right: 12, bottom: 2, left: 2 } },
        plugins: {
          legend: { position: "top", labels: { boxWidth: 12, color: "#1c2833", font: { size: 12, family: font } } },
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

  function axes(plot, xLabel, yLabel) {
    plot.options.scales.x.title = title(xText(xLabel));
    plot.options.scales.y.title = title(yText(yLabel));
  }

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

  function closeMenu() {
    document.querySelectorAll(".graph-menu").forEach((node) => node.remove());
  }

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

  function hold(plot, axis) {
    const scale = plot.scales[axis];
    if (!scale || !Number.isFinite(scale.min) || !Number.isFinite(scale.max)) return;
    plot.options.scales[axis].min = scale.min;
    plot.options.scales[axis].max = scale.max;
  }

  function setLock(plot, axis, locked) {
    if (axis === "x") plot.$lockX = locked;
    else plot.$lockY = locked;
    if (locked) hold(plot, axis);
    plot.update("none");
  }

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

  function release(plot, axis) {
    delete plot.options.scales[axis].min;
    delete plot.options.scales[axis].max;
    if (axis === "x") plot.$lockX = false;
    else plot.$lockY = false;
    plot.update();
  }

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

  return { chart, axes, xText, yText, bindAxisBar };
})();
