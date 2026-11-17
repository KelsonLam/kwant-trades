// Sign-in screen. Standalone on purpose: the app's own modules are behind the login.
(function () {
  var form = document.getElementById("form");
  var pw = document.getElementById("pw");
  var msg = document.getElementById("msg");
  var go = document.getElementById("go");
  var toggle = document.getElementById("toggle");
  var timer = 0;

  function show(text, kind) { msg.textContent = text || ""; msg.className = "login-msg" + (text ? " " + kind : ""); }
  function setBusy(b) { go.disabled = b; go.textContent = b ? "Signing in…" : "Sign in"; }
  function clearTimer() { if (timer) { clearInterval(timer); timer = 0; } }
  function countdown(secs) {
    clearTimer();
    var end = Date.now() + secs * 1000;
    var tick = function () {
      var left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      if (left <= 0) { clearTimer(); go.disabled = false; show("You can try again now.", "note"); return; }
      var m = Math.floor(left / 60), s = left % 60;
      go.disabled = true;
      show("Too many wrong passwords. Try again in " + (m ? m + " min " : "") + s + " s.", "err");
    };
    tick(); timer = setInterval(tick, 1000);
  }

  var params = new URLSearchParams(location.search);
  if (params.get("expired")) show("Your session expired. Please sign in again.", "note");

  // Already signed in (or not in remote mode)? Go straight to the app.
  fetch("/api/session", { cache: "no-store", credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (s) {
    if (s && (!s.remote || s.authenticated)) location.replace("/");
  }).catch(function () { /* the form still works; errors are shown on submit */ });

  toggle.addEventListener("click", function () {
    var on = pw.type === "password";
    pw.type = on ? "text" : "password";
    toggle.textContent = on ? "Hide" : "Show";
    toggle.setAttribute("aria-pressed", String(on));
    pw.focus();
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!pw.value) { show("Enter the password.", "err"); pw.focus(); return; }
    setBusy(true); show("");
    fetch("/api/login", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ password: pw.value })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) { return { res: res, j: j }; });
    }).then(function (o) {
      if (o.res.ok) { location.replace("/"); return; }
      setBusy(false);
      if (o.res.status === 429) { pw.value = ""; countdown(Number(o.j.retry_after) || Number(o.res.headers.get("Retry-After")) || 60); return; }
      if (o.res.status === 401) { show("Wrong password. Check it and try again.", "err"); pw.select(); return; }
      show((o.j && o.j.error) || "The server refused the request (" + o.res.status + ").", "err");
    }).catch(function () {
      setBusy(false);
      show("Can't reach the Kwant Trades server. Check that the computer is on, awake and on the same network.", "err");
    });
  });
})();
