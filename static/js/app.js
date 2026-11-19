document.addEventListener("DOMContentLoaded", () => {
  const savedTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  document.querySelectorAll(".theme-toggle:not(.cursor-toggle)").forEach(button => {
    const dark = savedTheme === "dark";
    button.innerHTML = `${dark ? "☾" : "☼"} <span>${dark ? "Dark" : "Light"}</span>`;
    button.setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} theme`);
    button.setAttribute("aria-pressed", String(dark));
    button.addEventListener("click", () => {
      const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      window.slidecraftSetTheme?.(nextTheme);
    });
  });

  const savedCursorEnabled = localStorage.getItem("slidecraft-cursor") !== "off";
  const cursorIcon = '<svg aria-hidden="true" focusable="false" viewBox="0 0 20 24" width="14" height="16"><path fill="currentColor" d="M3 2.5v17l4.8-4.6 3.2 7.1 2.7-1.2-3.3-7h6.9L3 2.5z"/></svg>';
  document.querySelectorAll(".cursor-toggle").forEach(button => {
    const updateButton = enabled => {
      button.innerHTML = `${cursorIcon} <span>${enabled ? "On" : "Off"}</span>`;
      button.setAttribute("aria-label", `Custom cursor effect: ${enabled ? "on" : "off"} (click to turn ${enabled ? "off" : "on"})`);
      button.setAttribute("aria-pressed", String(enabled));
    };
    updateButton(savedCursorEnabled);
    button.addEventListener("click", () => {
      const enabled = button.getAttribute("aria-pressed") !== "true";
      window.slidecraftSetCursorEnabled?.(enabled);
      updateButton(enabled);
    });
  });

  window.addEventListener("slidecraft-cursor-change", event => {
    document.querySelectorAll(".cursor-toggle").forEach(button => {
      const enabled = event.detail.enabled;
      button.innerHTML = `${cursorIcon} <span>${enabled ? "On" : "Off"}</span>`;
      button.setAttribute("aria-label", `Custom cursor effect: ${enabled ? "on" : "off"} (click to turn ${enabled ? "off" : "on"})`);
      button.setAttribute("aria-pressed", String(enabled));
    });
  });

  const stack = document.querySelector(".flash-stack");
  if (stack) {
    setTimeout(() => {
      stack.classList.add("flash-hide");
      setTimeout(() => stack.remove(), 450);
    }, 4200);
  }

  const facebookDialog = document.getElementById("facebook-coming-soon-dialog");
  const facebookTrigger = document.querySelector(".provider-badge");
  if (facebookDialog && facebookTrigger) {
    let returnFocus = null;
    const closeDialog = () => {
      facebookDialog.hidden = true;
      document.body.classList.remove("facebook-modal-open");
      document.removeEventListener("keydown", handleDialogKeydown);
      if (returnFocus?.isConnected) returnFocus.focus();
    };
    const handleDialogKeydown = event => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDialog();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(facebookDialog.querySelectorAll("button:not([disabled])"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    facebookTrigger.addEventListener("click", () => {
      returnFocus = facebookTrigger;
      facebookDialog.hidden = false;
      document.body.classList.add("facebook-modal-open");
      document.addEventListener("keydown", handleDialogKeydown);
      facebookDialog.querySelector(".facebook-modal__close").focus();
    });
    facebookDialog.addEventListener("click", event => {
      if (event.target === facebookDialog) closeDialog();
    });
    facebookDialog.querySelector(".facebook-modal__close").addEventListener("click", closeDialog);
    facebookDialog.querySelector(".facebook-modal__done").addEventListener("click", closeDialog);
  }
});
