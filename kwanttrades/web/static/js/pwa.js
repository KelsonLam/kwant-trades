// Registers the service worker only in a secure context (https or localhost). Plain http on a LAN is not one, and
// the app works fully without it.
(function () {
  try {
    if (window.isSecureContext && "serviceWorker" in navigator) {
      window.addEventListener("load", function () { navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {}); });
    }
  } catch (e) { /* optional enhancement */ }
})();
