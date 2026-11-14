// Small helpers for the screens; HTMX does the rest.

// "Copy reply" buttons: <button data-copy="#id">
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const source = document.querySelector(button.dataset.copy);
  if (!source) return;
  navigator.clipboard.writeText(source.innerText.trim()).then(() => {
    const label = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = label; }, 1500);
  });
});

// Stopwatch for the Go Live wizard: <span class="stopwatch" data-started="ISO time">
function tickStopwatches() {
  document.querySelectorAll(".stopwatch[data-started]").forEach((el) => {
    if (el.dataset.finished) return;
    const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(el.dataset.started)) / 1000));
    const m = String(Math.floor(seconds / 60)).padStart(2, "0");
    const s = String(seconds % 60).padStart(2, "0");
    el.textContent = `${m}:${s}`;
  });
}
setInterval(tickStopwatches, 1000);
document.addEventListener("DOMContentLoaded", tickStopwatches);

// Keep the chat scrolled to the newest message
function scrollChat() {
  const box = document.querySelector(".messages");
  if (box) box.scrollTop = box.scrollHeight;
}
document.addEventListener("DOMContentLoaded", scrollChat);
document.addEventListener("htmx:afterSwap", scrollChat);

// Ctrl+Enter sends the chat message
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    const form = event.target.closest("form.composer");
    if (form) form.requestSubmit();
  }
});

// The business switcher submits on change
document.addEventListener("change", (event) => {
  if (event.target.matches("select[data-autosubmit]")) event.target.form.requestSubmit();
});
