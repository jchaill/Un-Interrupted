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

    // Episode table: select-all + enable the batch actions only when needed.
    const batchForm = document.getElementById("batch-form");
    if (batchForm) {
      const selectAll = document.getElementById("episode-select-all");
      const processBtn = document.getElementById("batch-process");
      const reprocessBtn = document.getElementById("batch-reprocess");
      const deleteBtn = document.getElementById("batch-delete");
      const count = document.getElementById("batch-count");
      const boxes = () =>
        Array.from(
          document.querySelectorAll('input.episode-checkbox[form="batch-form"]')
        );
      const update = () => {
        const all = boxes();
        const selected = all.filter((box) => box.checked);
        const processable = selected.filter(
          (box) => box.dataset.processable === "1"
        ).length;
        if (processBtn) processBtn.disabled = processable === 0;
        if (reprocessBtn) reprocessBtn.disabled = selected.length === 0;
        if (deleteBtn) deleteBtn.disabled = selected.length === 0;
        if (count) {
          count.textContent = selected.length + " selected";
          if (selected.length && processable < selected.length) {
            count.textContent += " · " + processable + " ready to process";
          }
        }
        if (selectAll) {
          selectAll.disabled = all.length === 0;
          selectAll.checked = all.length > 0 && selected.length === all.length;
          selectAll.indeterminate =
            selected.length > 0 && selected.length < all.length;
        }
      };
      // Delegated: also covers rows re-rendered after a swap.
      document.addEventListener("change", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement)) return;
        if (target === selectAll) {
          boxes().forEach((box) => {
            box.checked = selectAll.checked;
          });
          update();
        } else if (target.classList.contains("episode-checkbox")) {
          update();
        }
      });
      update();
      // Browsers may restore checkbox state after DOMContentLoaded.
      window.addEventListener("load", update);
    }
  });

  // HTMX: keep scroll position for the queue partial refresh.
  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.target && event.target.id === "queue-live") {
      // no-op; placeholder for future scroll preservation
    }
  });
})();
