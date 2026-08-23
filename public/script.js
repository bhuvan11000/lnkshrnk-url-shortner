(() => {
  const form = document.getElementById("shorten-form");
  const urlInput = document.getElementById("url-input");
  const customInput = document.getElementById("custom-input");
  const submitBtn = document.getElementById("submit-btn");
  const errorMsg = document.getElementById("error-msg");
  const result = document.getElementById("result");
  const shortLink = document.getElementById("short-link");
  const copyBtn = document.getElementById("copy-btn");
  const copyFeedback = document.getElementById("copy-feedback");
  const btnLabel = submitBtn.querySelector(".btn-label");

  function showError(message) {
    errorMsg.textContent = message;
    errorMsg.classList.remove("hidden");
    result.classList.add("hidden");
    errorMsg.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function hideError() {
    errorMsg.textContent = "";
    errorMsg.classList.add("hidden");
  }

  function showResult(shortUrl) {
    shortLink.textContent = shortUrl;
    shortLink.href = shortUrl;
    // re-trigger ticket animation
    result.classList.add("hidden");
    void result.offsetWidth;
    result.classList.remove("hidden");
    copyFeedback.classList.add("hidden");
    copyBtn.querySelector(".copy-text").textContent = "Copy";
    // subtle focus for keyboard users
    shortLink.focus({ preventScroll: true });
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    hideError();
    result.classList.add("hidden");
    copyFeedback.classList.add("hidden");

    const url = urlInput.value.trim();
    const custom = customInput.value.trim();

    if (!url) {
      showError("Enter a URL starting with http:// or https://");
      urlInput.focus();
      return;
    }

    submitBtn.disabled = true;
    if (btnLabel) btnLabel.textContent = "Shortening…";
    else submitBtn.textContent = "Shortening…";
    submitBtn.setAttribute("aria-busy", "true");

    const payload = { url };
    if (custom) payload.custom = custom;

    try {
      const res = await fetch("/api/shorten", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        const msg = data.detail || data.error || `Request failed (${res.status})`;
        showError(msg);
        return;
      }

      if (!data.short) {
        showError("Unexpected response from server.");
        return;
      }

      showResult(data.short);
    } catch {
      showError("Network error — check your connection and try again.");
    } finally {
      submitBtn.disabled = false;
      submitBtn.removeAttribute("aria-busy");
      if (btnLabel) btnLabel.textContent = "Shorten URL";
      else submitBtn.textContent = "Shorten URL";
    }
  });

  copyBtn.addEventListener("click", async () => {
    const text = shortLink.textContent;
    if (!text) return;

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      copyFeedback.classList.remove("hidden");
      const ct = copyBtn.querySelector(".copy-text");
      if (ct) ct.textContent = "Copied!";
      else copyBtn.textContent = "Copied!";
      setTimeout(() => {
        copyFeedback.classList.add("hidden");
        const ct2 = copyBtn.querySelector(".copy-text");
        if (ct2) ct2.textContent = "Copy";
        else copyBtn.textContent = "Copy";
      }, 2000);
    } catch {
      showError("Copy failed — select the link and copy manually.");
    }
  });

  // live clear error on input
  urlInput.addEventListener("input", hideError);
  customInput.addEventListener("input", hideError);
})();
