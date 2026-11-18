(async (version) => {
  const record = window.go?.main?.UISmoke?.Report;
  if (!record) return;

  try {
    if (document.title !== "Forkrail" || !document.querySelector("#main")) {
      const error = document.querySelector("#forkrail-start-error")?.textContent?.trim();
      throw new Error(error || "Forkrail board DOM is missing");
    }

    const healthResponse = await fetch("/api/health", { cache: "no-store" });
    const health = await healthResponse.json();
    if (!healthResponse.ok || health.ok !== true || health.app !== "Forkrail" ||
        health.api !== 1 || health.version !== version || !health.instance) {
      throw new Error("WebView did not reach the bundled Forkrail service");
    }

    const updateInfo = window.go?.main?.AppUpdates?.Info;
    if (!updateInfo) throw new Error("Native update bindings are missing");
    const update = await updateInfo();
    if (update.available !== true || update.current_version !== version || update.status !== "idle" ||
        !["macos-arm64", "macos-x86_64", "windows-x64"].includes(update.target) ||
        update.os !== (update.target === "windows-x64" ? "windows" : "macos")) {
      throw new Error("Bundled native update metadata does not match this app");
    }

    const sessionsResponse = await fetch("/api/sessions", { cache: "no-store" });
    const sessions = await sessionsResponse.json();
    if (!sessionsResponse.ok || !Array.isArray(sessions.sessions)) {
      throw new Error("WebView could not load board sessions");
    }

    const catalogResponse = await fetch("/api/actions", { cache: "no-store" });
    const catalog = await catalogResponse.json();
    if (!catalogResponse.ok || !catalog.token) {
      throw new Error("WebView could not load the board action token");
    }

    const request = {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Work-Board-Token": catalog.token },
      body: JSON.stringify({ action: "unsupported_desktop_smoke_probe" }),
    };
    const response = await fetch("/api/actions", request);
    const data = await response.json().catch(() => ({}));
    if (response.status !== 400 || data.error !== "지원하지 않는 동작입니다.") {
      throw new Error(`WebView action request did not pass the CSRF and body checks (HTTP ${response.status})`);
    }

    request.headers["X-Work-Board-Token"] = "invalid-smoke-token";
    const denied = await fetch("/api/actions", request);
    if (denied.status !== 403) {
      throw new Error("The board accepted an invalid action token");
    }

    await record("");
  } catch (error) {
    await record(String(error.message || error));
  }
})
