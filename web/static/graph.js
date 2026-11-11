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
      ticks: { color: "#3e5164", font: { size: 11, family: font }, maxTicksLimit: 8 },
      grid: { color: "rgba(30, 51, 72, 0.1)" },
      border: { color: "#7f93a8" },
    };
  }

  function chart(id) {
    const instance = new Chart(document.getElementById(id), {
      type: "line",
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
            pan: {
              enabled: true,
              mode: "xy",
              onPanStart(context) {
                const plot = context.chart;
                if (plot.$axisLock) return false;
                const event = context.event && (context.event.srcEvent || context.event);
                return !(event && event.button != null && event.button !== 0);
              },
            },
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
    const next = {};
    for (const axis of ["x", "y"]) {
      const current = plot.scales[axis];
      next[axis] = axes.includes(axis)
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
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 && event.button !== 2) return;
      held.add(event.button);
      wheeled = false;
      plot.$axisLock = false;
      closeMenu();
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

  return { chart, axes, xText, yText };
})();
