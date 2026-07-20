// Live updates via Server-Sent Events.
// POST /v1/messages publishes "new_request" events; we refresh the list.

if (typeof EventSource !== "undefined") {
  const source = new EventSource("/stream");
  source.addEventListener("new_request", () => {
    const list = document.getElementById("list");
    if (list && typeof window.htmx !== "undefined") {
      window.htmx.trigger(list, "refresh");
    }
  });
}