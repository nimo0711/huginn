(() => {
  "use strict";

  const boardOrigin = document.currentScript.dataset.boardOrigin;
  const openInBrowser = (event) => {
    const link = event.target.closest?.("a[href], area[href]");
    if (!link || event.defaultPrevented || (event.type === "auxclick" && event.button !== 1)) return;

    let url;
    try {
      url = new URL(link.href, location.href);
    } catch {
      event.preventDefault();
      return;
    }
    const inside = url.protocol === location.protocol && url.host === location.host;
    if (inside && !url.pathname.startsWith("/doc/")) {
      link.removeAttribute("target");
      return;
    }

    event.preventDefault();
    if (!event.isTrusted) return;
    if (inside) url = new URL(url.pathname + url.search + url.hash, boardOrigin);
    if ((url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password) {
      window.runtime?.BrowserOpenURL(url.href);
    }
  };

  window.addEventListener("click", openInBrowser, true);
  window.addEventListener("auxclick", openInBrowser, true);
})();
