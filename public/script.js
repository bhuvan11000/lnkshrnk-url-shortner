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

  function showError(message) {
    errorMsg.textContent = message;
    errorMsg.classList.remove("hidden");
    result.classList.add("hidden");
  }

  function hideError() {
    errorMsg.textContent = "";
    errorMsg.classList.add("hidden");
  }

  function showResult(shortUrl) {
    shortLink.textContent = shortUrl;
    shortLink.href = shortUrl;
    result.classList.remove("hidden");
    copyFeedback.classList.add("hidden");
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    hideError();
    result.classList.add("hidden");
    copyFeedback.classList.add("hidden");

    const url = urlInput.value.trim();
    const custom = customInput.value.trim();

    if (!url) {
      showError("Please enter a URL.");
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = "Shortening…";

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
    } catch (err) {
      showError("Network error — please check your connection and try again.");
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Shorten";
    }
  });

  copyBtn.addEventListener("click", async () => {
    const text = shortLink.textContent;
    if (!text) return;

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for older browsers
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
      copyBtn.textContent = "Copied!";
      setTimeout(() => {
        copyFeedback.classList.add("hidden");
        copyBtn.textContent = "Copy";
      }, 2000);
    } catch {
      showError("Failed to copy — please copy the link manually.");
    }
  });
})();
