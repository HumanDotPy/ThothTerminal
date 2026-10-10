(function () {
  let galleryItems = [];
  let galleryIndex = 0;

  function ensureModal() {
    let modal = document.getElementById("media-modal");
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = "media-modal";
    modal.className = "media-modal";
    modal.innerHTML = `
      <div class="media-modal-backdrop" data-media-close></div>
      <div class="media-modal-inner" role="dialog" aria-modal="true" aria-label="Media viewer">
        <button class="media-modal-close" type="button" data-media-close aria-label="Close">&times;</button>
        <button class="media-modal-nav prev" type="button" data-media-prev aria-label="Previous">&lsaquo;</button>
        <img class="media-modal-img" alt="" />
        <button class="media-modal-nav next" type="button" data-media-next aria-label="Next">&rsaquo;</button>
        <div class="media-modal-caption">
          <div class="media-modal-counter"></div>
          <div class="media-modal-label"></div>
          <div class="media-modal-meta"></div>
          <div class="media-modal-note"></div>
          <a class="media-modal-open-link" target="_blank" rel="noopener noreferrer">Open original</a>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
    return modal;
  }

  function closeModal() {
    const modal = document.getElementById("media-modal");
    if (!modal) return;
    modal.classList.remove("open");
    document.body.classList.remove("media-modal-open");
    const img = modal.querySelector(".media-modal-img");
    if (img) img.setAttribute("src", "");
    galleryItems = [];
    galleryIndex = 0;
  }

  function renderCurrent() {
    const modal = ensureModal();
    const img = modal.querySelector(".media-modal-img");
    const counter = modal.querySelector(".media-modal-counter");
    const label = modal.querySelector(".media-modal-label");
    const meta = modal.querySelector(".media-modal-meta");
    const note = modal.querySelector(".media-modal-note");
    const openLink = modal.querySelector(".media-modal-open-link");
    const prevBtn = modal.querySelector("[data-media-prev]");
    const nextBtn = modal.querySelector("[data-media-next]");
    const item = galleryItems[galleryIndex];
    if (!item) return;
    if (img) {
      img.setAttribute("src", item.src || "");
      img.setAttribute("alt", item.label || "");
    }
    if (counter) {
      counter.textContent = galleryItems.length > 1 ? `${galleryIndex + 1} / ${galleryItems.length}` : "1 / 1";
    }
    if (label) label.textContent = item.label || "Media";
    if (meta) meta.textContent = item.meta || "";
    if (note) {
      note.textContent = item.note || "";
      note.style.display = item.note ? "block" : "none";
    }
    if (openLink) {
      if (item.openUrl) {
        openLink.setAttribute("href", item.openUrl);
        openLink.style.display = "inline-flex";
      } else {
        openLink.removeAttribute("href");
        openLink.style.display = "none";
      }
    }
    if (prevBtn) prevBtn.disabled = galleryItems.length <= 1;
    if (nextBtn) nextBtn.disabled = galleryItems.length <= 1;
  }

  function openModal(items, index) {
    if (!Array.isArray(items) || !items.length) return;
    const modal = ensureModal();
    galleryItems = items;
    galleryIndex = Math.max(0, Math.min(Number(index || 0), items.length - 1));
    renderCurrent();
    modal.classList.add("open");
    document.body.classList.add("media-modal-open");
  }

  function shiftGallery(delta) {
    if (!galleryItems.length) return;
    galleryIndex = (galleryIndex + delta + galleryItems.length) % galleryItems.length;
    renderCurrent();
  }

  document.addEventListener("click", (e) => {
    const target = e.target;
    const btn = target && target.closest ? target.closest("[data-media-full]") : null;
    if (btn) {
      e.preventDefault();
      const scope = btn.closest(".media-grid") || document;
      const buttons = Array.from(scope.querySelectorAll("[data-media-full]"));
      const items = buttons.map((node) => ({
        src: node.getAttribute("data-media-full") || "",
        openUrl: node.getAttribute("data-media-open") || "",
        label: node.getAttribute("data-media-label") || "",
        meta: node.getAttribute("data-media-meta") || "",
        note: node.getAttribute("data-media-note") || "",
      }));
      const index = Math.max(0, buttons.indexOf(btn));
      openModal(items, index);
      return;
    }
    const prev = target && target.closest ? target.closest("[data-media-prev]") : null;
    if (prev) {
      e.preventDefault();
      shiftGallery(-1);
      return;
    }
    const next = target && target.closest ? target.closest("[data-media-next]") : null;
    if (next) {
      e.preventDefault();
      shiftGallery(1);
      return;
    }
    const closer = target && target.closest ? target.closest("[data-media-close]") : null;
    if (closer) {
      e.preventDefault();
      closeModal();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
    if (e.key === "ArrowLeft") shiftGallery(-1);
    if (e.key === "ArrowRight") shiftGallery(1);
  });
})();
