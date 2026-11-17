// Apply the saved/system theme before first paint (no flash). Kept as a separate file so the page needs no inline script.
(function () {
  var t = null;
  try { t = localStorage.getItem("kwanttrades.theme"); } catch (e) { t = null; }
  if (t !== "light" && t !== "dark") t = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
})();
