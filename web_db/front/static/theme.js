/* 전체 페이지 테마. White / Gray / Dark 를 저장하고 헤더의 알약 모양 버튼 세 개로 고른다.
 *
 * - 고른 값은 localStorage 'sensor-theme'. 각 페이지 <head>의 짧은 스크립트가 그리기 전에 읽어
 *   html[data-theme]를 정하므로 깜빡이지 않는다.
 * - 바꿀 때 페이지를 새로 고치지 않는다(분석 화면에 열어 둔 데이터가 남는다). data-theme만 바꾸고
 *   watch()로 등록된 차트의 눈금·글자 색을 다시 칠한 뒤 'sensor-theme' 이벤트를 낸다.
 *   차트 색을 스스로 정하는 화면(통계 dashboard.html)은 이 이벤트를 받아 다시 그린다.
 */
(function () {
  var key = "sensor-theme";
  var THEMES = [
    ["white", "White"],
    ["gray", "Gray"],
    ["dark", "Dark"],
  ];
  var theme = document.documentElement.getAttribute("data-theme") || "white";
  var charts = [];
  var brand = document.querySelector("header .brand");
  if (brand && !document.querySelector("#themePick")) {
    var side = document.createElement("div");
    side.className = "header-side";
    brand.parentNode.insertBefore(side, brand);
    side.appendChild(brand);
    // 버튼 세 개. 동그라미(swatch)가 그 테마의 바탕색 미리보기다.
    var pick = document.createElement("div");
    pick.id = "themePick";
    pick.className = "theme-switch";
    pick.setAttribute("role", "radiogroup");
    pick.setAttribute("aria-label", "화면 스타일");
    THEMES.forEach(function (item) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "theme-opt";
      button.dataset.theme = item[0];
      button.setAttribute("role", "radio");
      button.title = item[1] + " 화면";
      button.innerHTML = '<span class="theme-swatch swatch-' + item[0] + '"></span><span>' + item[1] + "</span>";
      button.addEventListener("click", function () {
        setTheme(item[0]);
      });
      pick.appendChild(button);
    });
    side.appendChild(pick);
    mark();
  }

  /** 버튼의 선택 표시를 지금 테마에 맞춘다. */
  function mark() {
    document.querySelectorAll("#themePick .theme-opt").forEach(function (button) {
      var on = button.dataset.theme === theme;
      button.classList.toggle("active", on);
      button.setAttribute("aria-checked", on ? "true" : "false");
    });
  }

  /** 테마를 바로 바꾼다(새로고침 없음). */
  function setTheme(name) {
    if (name === theme) return;
    theme = name;
    try {
      localStorage.setItem(key, name);
    } catch (error) {
      /* 저장이 막혀도 지금 화면은 바뀐다 */
    }
    document.documentElement.setAttribute("data-theme", name);
    mark();
    window.SensorTheme.dark = name !== "white";
    charts.forEach(function (chart) {
      paint(chart);
      chart.update("none");
    });
    window.dispatchEvent(new CustomEvent("sensor-theme", { detail: { theme: name } }));
  }

  /** 차트 눈금 색을 현재 테마에 맞춘다. */
  function paint(chart) {
    if (!chart || !chart.options) return;
    var dark = theme !== "white";
    var scales = chart.options.scales || {};
    Object.keys(scales).forEach(function (name) {
      var scale = scales[name];
      if (!scale) return;
      if (scale.ticks) scale.ticks.color = dark ? "#9aa8b5" : "#3e5164";
      if (scale.grid) scale.grid.color = dark ? "rgba(170, 200, 170, 0.16)" : "rgba(30, 51, 72, 0.16)";
      if (scale.border) scale.border.color = dark ? "#3c4c3c" : "#7f93a8";
      if (scale.title) scale.title.color = dark ? "#c9d7b8" : "#17324d";
    });
    var legend = chart.options.plugins && chart.options.plugins.legend;
    if (legend && legend.labels) legend.labels.color = dark ? "#d7e8c8" : "#1c2833";
    var title = chart.options.plugins && chart.options.plugins.title;
    if (title) title.color = dark ? "#d7e8c8" : "#17324d";
  }

  /** 차트가 다시 그려질 때마다 테마 색을 입힌다. */
  function watch(chart) {
    if (!chart || chart.__themePaint) return;
    charts.push(chart);
    var update = chart.update.bind(chart);
    chart.update = function () {
      paint(chart);
      return update.apply(chart, arguments);
    };
    chart.__themePaint = true;
    paint(chart);
    chart.update("none");
  }

  // 나중에 만드는 차트(속도 RMS 시간 이력 등)도 같은 테마 색을 쓰도록 밖에 내놓는다.
  window.SensorTheme = { watch: watch, dark: theme !== "white", set: setTheme };
  if (typeof rawChart !== "undefined") watch(rawChart);
  if (typeof fftChart !== "undefined") watch(fftChart);
})();
