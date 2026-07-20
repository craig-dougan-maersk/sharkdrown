// Show the #modal-root <dialog> when htmx swaps content into it.
// Closes on Escape (native) and on backdrop click.

const dialog = document.getElementById("modal-root");
if (dialog) {
  document.body.addEventListener("htmx:afterRequest", (e) => {
    if (e.detail.target === dialog && dialog.childNodes.length > 0) {
      dialog.showModal();
    }
  });

  // Clicking the backdrop closes the dialog.
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) {
      dialog.close();
    }
  });
}