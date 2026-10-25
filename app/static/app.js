(function () {
  "use strict";

  function toast(text) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.remove("show"), 2200);
  }

  document.addEventListener("click", async function (event) {
    const btn = event.target.closest("[data-copy]");
    if (btn) {
      const text = btn.getAttribute("data-copy") || "";
      try {
        await navigator.clipboard.writeText(text);
        toast("Copied to clipboard");
      } catch (err) {
        const area = document.createElement("textarea");
        area.value = text;
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
        toast("Copied to clipboard");
      }
      return;
    }

    const confirmEl = event.target.closest("[data-confirm]");
    if (confirmEl) {
      if (!window.confirm(confirmEl.getAttribute("data-confirm"))) {
        event.preventDefault();
        event.stopPropagation();
      }
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".flash").forEach((el) => {
      setTimeout(() => {
        el.classList.add("fading");
        setTimeout(() => el.remove(), 400);
      }, 6500);
    });

    const qr = document.getElementById("qr-panel");
    if (qr && typeof qr.showModal !== "function") {
      qr.setAttribute("open", "");
    }
  });

  // HTMX: keep scroll position for the queue partial refresh.
  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.target && event.target.id === "queue-live") {
      // no-op; placeholder for future scroll preservation
    }
  });
})();
