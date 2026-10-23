(() => {
  const url = new URL(window.location.href);
  const requested = url.searchParams.get("ui");
  const valid = ["mobile", "desktop", "auto"].includes(requested);
  let preference = "auto";
  try {
    preference = localStorage.getItem("lexvault-ui") || "auto";
    if (valid) localStorage.setItem("lexvault-ui", requested);
  } catch { /* Private browsing may disable storage; the URL still works. */ }
  if (valid) preference = requested;
  const onMobile = url.pathname === "/mobile";
  // An explicit mobile URL remains mobile unless this visit requests a mode.
  if (onMobile && !valid) {
    try { localStorage.setItem("lexvault-ui", "mobile"); } catch { /* URL routing still works. */ }
    return;
  }
  const mobile = preference === "mobile" ||
    (preference !== "desktop" && window.matchMedia("(max-width: 900px)").matches);
  const target = mobile ? "/mobile" : "/";
  if (url.pathname === target) return;
  url.pathname = target;
  // Keep ui, auth_error and other query state across the single startup redirect.
  window.location.replace(url.pathname + url.search + url.hash);
})();
