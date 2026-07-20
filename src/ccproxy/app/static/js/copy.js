// Delegated clipboard handler.
// Any element with [data-copy] copies its text content into the clipboard
// when clicked and briefly shows "Copied!".

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-copy]");
  if (!el) return;
  e.preventDefault();
  const text = el.getAttribute("data-copy") || el.textContent || "";
  try {
    await navigator.clipboard.writeText(text);
    const original = el.textContent;
    el.textContent = "Copied!";
    el.classList.add("text-green-600");
    setTimeout(() => {
      el.textContent = original;
      el.classList.remove("text-green-600");
    }, 1500);
  } catch (err) {
    console.error("clipboard write failed:", err);
  }
});