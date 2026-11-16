function attachGraphZoom(chart) {
  const canvas = chart.canvas;
  const buttons = { left: false, right: false };
  let drag = null;
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());
  canvas.addEventListener("mousedown", (event) => {
    if (event.button === 0) buttons.left = true;
    if (event.button === 2) buttons.right = true;
    if (event.button === 0) {
      drag = {
        x: event.clientX,
        y: event.clientY,
        xMin: chart.scales.x.min,
        xMax: chart.scales.x.max,
        yMin: chart.scales.y.min,
        yMax: chart.scales.y.max,
      };
    }
  });
  window.addEventListener("mouseup", (event) => {
    if (event.button === 0) buttons.left = false;
    if (event.button === 2) buttons.right = false;
    if (event.button === 0) drag = null;
  });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const mode = buttons.right ? "y" : buttons.left ? "x" : "xy";
    zoomGraph(chart, event, mode);
    chart.$userZoom = true;
  }, { passive: false });
  canvas.addEventListener("mousemove", (event) => {
    if (!drag || !buttons.left || buttons.right) return;
    panGraph(chart, drag, event);
    chart.$userZoom = true;
  });
  canvas.addEventListener("dblclick", () => resetGraphZoom(chart));
}

function zoomGraph(chart, event, mode) {
  const rect = chart.canvas.getBoundingClientRect();
  const factor = event.deltaY < 0 ? 0.8 : 1.25;
  const sx = chart.scales.x;
  const sy = chart.scales.y;
  if (mode !== "y" && Number.isFinite(sx.min) && Number.isFinite(sx.max) && sx.max !== sx.min) {
    const value = sx.getValueForPixel(event.clientX - rect.left);
    const span = (sx.max - sx.min) * factor;
    const ratio = (value - sx.min) / (sx.max - sx.min);
    chart.options.scales.x.min = value - span * ratio;
    chart.options.scales.x.max = chart.options.scales.x.min + span;
  }
  if (mode !== "x" && Number.isFinite(sy.min) && Number.isFinite(sy.max) && sy.max !== sy.min) {
    const value = sy.getValueForPixel(event.clientY - rect.top);
    const span = (sy.max - sy.min) * factor;
    const ratio = (value - sy.min) / (sy.max - sy.min);
    chart.options.scales.y.min = value - span * ratio;
    chart.options.scales.y.max = chart.options.scales.y.min + span;
  }
  chart.update("none");
}

function panGraph(chart, drag, event) {
  const area = chart.chartArea;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  const xSpan = drag.xMax - drag.xMin;
  const ySpan = drag.yMax - drag.yMin;
  if (area && area.width && Number.isFinite(xSpan)) {
    const shift = dx * xSpan / area.width;
    chart.options.scales.x.min = drag.xMin - shift;
    chart.options.scales.x.max = drag.xMax - shift;
  }
  if (area && area.height && Number.isFinite(ySpan)) {
    const shift = dy * ySpan / area.height;
    chart.options.scales.y.min = drag.yMin + shift;
    chart.options.scales.y.max = drag.yMax + shift;
  }
  chart.update("none");
}

function resetGraphZoom(chart) {
  chart.$userZoom = false;
  delete chart.options.scales.x.min;
  delete chart.options.scales.x.max;
  delete chart.options.scales.y.min;
  delete chart.options.scales.y.max;
  chart.update();
}
