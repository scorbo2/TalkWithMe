// Mobile layout (issue #143): a ☰ button that
// opens the sidebar as a drawer on narrow screens (see mobile.css).
(function () {
    const topbar = document.getElementById("topbar");
    const sidebar = document.getElementById("sidebar");
    if (!topbar || !sidebar) return;

    const toggle = document.createElement("button");
    toggle.id = "btn-sidebar-toggle";
    toggle.type = "button";
    toggle.title = "Chat room, personas";
    toggle.setAttribute("aria-label", "Toggle sidebar");
    toggle.setAttribute("aria-controls", "sidebar");
    toggle.innerHTML = "&#9776;";
    topbar.insertBefore(toggle, topbar.firstChild);

    const backdrop = document.createElement("div");
    backdrop.id = "sidebar-backdrop";
    document.body.appendChild(backdrop);

    const setOpen = (open) => {
        document.body.classList.toggle("sidebar-open", open);
        toggle.setAttribute("aria-expanded", String(open));
    };
    toggle.addEventListener("click", () =>
        setOpen(!document.body.classList.contains("sidebar-open")));
    backdrop.addEventListener("click", () => setOpen(false));

    // Picking a chat room closes the drawer, so the chat is visible again.
    const room = document.getElementById("chat-room-dropdown");
    if (room) room.addEventListener("change", () => setOpen(false));

    // Back on a wide screen: no drawer state left behind.
    window.matchMedia("(min-width: 769px)").addEventListener("change", (e) => {
        if (e.matches) setOpen(false);
    });
})();
