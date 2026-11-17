/* 전체 페이지 테마. White / Gray / Dark 를 저장하고 헤더에서 고른다. */
(function () {
  var key = "sensor-theme";
  var theme = document.documentElement.getAttribute("data-theme") || "white";
  var brand = document.querySelector("header .brand");
  if (brand && !document.querySelector("#themePick")) {
    var side = document.createElement("div");
    side.className = "header-side";
    brand.parentNode.insertBefore(side, brand);
    side.appendChild(brand);
    var pick = document.createElement("select");
    pick.id = "themePick";
    pick.setAttribute("aria-label", "화면 스타일");
    ["white", "gray", "dark"].forEach(function (name) {
      var option = document.createElement("option");
      option.value = name;
      option.textContent = name === "white" ? "White" : name === "gray" ? "Gray" : "Dark";
      if (name === theme) option.selected = true;
      pick.appendChild(option);
    });
    pick.addEventListener("change", function () {
      localStorage.setItem(key, pick.value);
      location.reload();
    });
    side.appendChild(pick);
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
    var update = chart.update.bind(chart);
    chart.update = function () {
      paint(chart);
      return update.apply(chart, arguments);
    };
    chart.__themePaint = true;
    paint(chart);
    chart.update("none");
  }

  if (typeof rawChart !== "undefined") watch(rawChart);
  if (typeof fftChart !== "undefined") watch(fftChart);
})();
