/* 계측 예시 전용. 왼쪽 설정을 펼치고 RAW/FFT 눈금을 어두운 배경에 맞춘다. */
const pinMode = document.querySelector("#pinMode");
if (pinMode) pinMode.click();

function darkenChart(chart) {
  const scales = chart.options.scales || {};
  Object.keys(scales).forEach((key) => {
    const scale = scales[key];
    if (!scale) return;
    if (scale.ticks) scale.ticks.color = "#9aa8b5";
    if (scale.grid) scale.grid.color = "rgba(170, 200, 170, 0.16)";
    if (scale.border) scale.border.color = "#3c4c3c";
    if (scale.title) scale.title.color = "#c9d7b8";
  });
  const legend = chart.options.plugins && chart.options.plugins.legend;
  if (legend && legend.labels) legend.labels.color = "#d7e8c8";
  const title = chart.options.plugins && chart.options.plugins.title;
  if (title) title.color = "#d7e8c8";
}

function keepDark(chart) {
  if (!chart || chart.__deckDark) return;
  const update = chart.update.bind(chart);
  chart.update = (...args) => {
    darkenChart(chart);
    return update(...args);
  };
  chart.__deckDark = true;
  darkenChart(chart);
  chart.update("none");
}

keepDark(rawChart);
keepDark(fftChart);
