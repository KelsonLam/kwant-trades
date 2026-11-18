// Apply the saved/system theme before first paint (no flash). Kept as a separate file so the page needs no inline script.
// Preference is "system" (default), "light" or "dark"; data-theme always holds the resolved value.
(function () {
  var pref = null;
  try { pref = localStorage.getItem("kwanttrades.theme"); } catch (e) { pref = null; }
  if (pref !== "light" && pref !== "dark") pref = "system";
  var t = pref;
  if (t === "system") t = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  var root = document.documentElement;
  root.setAttribute("data-theme", t);
  root.setAttribute("data-theme-pref", pref);
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "light" ? "#f2f2f7" : "#000000");
})();
