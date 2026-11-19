(() => {
  const preferenceKey = "slidecraft-cursor";
  const supportsLens = window.matchMedia("(hover: hover) and (pointer: fine)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const size = 72;

  let lens = null;
  let targetX = -size;
  let targetY = -size;
  let currentX = targetX;
  let currentY = targetY;
  let animationFrame = 0;
  let visible = false;
  let listening = false;

  const isEnabled = () => localStorage.getItem(preferenceKey) !== "off";
  const canRun = () => isEnabled() && supportsLens.matches && !reducedMotion.matches;

  const animate = () => {
    currentX += (targetX - currentX) * 0.24;
    currentY += (targetY - currentY) * 0.24;
    lens.style.transform = `translate3d(${currentX - size / 2}px, ${currentY - size / 2}px, 0)`;
    if (Math.abs(targetX - currentX) > 0.2 || Math.abs(targetY - currentY) > 0.2) {
      animationFrame = window.requestAnimationFrame(animate);
    } else {
      animationFrame = 0;
    }
  };

  const move = event => {
    if (!lens || event.pointerType === "touch" || reducedMotion.matches) return;
    targetX = event.clientX;
    targetY = event.clientY;
    if (!visible) {
      visible = true;
      lens.classList.add("is-visible");
    }
    if (!animationFrame) animationFrame = window.requestAnimationFrame(animate);
  };

  const hide = () => {
    visible = false;
    lens?.classList.remove("is-visible", "is-pressed");
  };
  const press = () => lens?.classList.add("is-pressed");
  const release = () => lens?.classList.remove("is-pressed");

  const addListeners = () => {
    if (listening) return;
    window.addEventListener("pointermove", move, { passive: true });
    window.addEventListener("pointerleave", hide, { passive: true });
    window.addEventListener("pointerdown", press, { passive: true });
    window.addEventListener("pointerup", release, { passive: true });
    window.addEventListener("blur", hide);
    listening = true;
  };

  const removeListeners = () => {
    if (!listening) return;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerleave", hide);
    window.removeEventListener("pointerdown", press);
    window.removeEventListener("pointerup", release);
    window.removeEventListener("blur", hide);
    listening = false;
  };

  const stop = () => {
    removeListeners();
    if (animationFrame) window.cancelAnimationFrame(animationFrame);
    animationFrame = 0;
    hide();
    lens?.remove();
    lens = null;
  };

  const start = () => {
    if (!canRun() || lens) return;
    lens = document.createElement("div");
    lens.className = "liquid-cursor-lens";
    lens.setAttribute("aria-hidden", "true");
    document.body.appendChild(lens);
    targetX = -size;
    targetY = -size;
    currentX = targetX;
    currentY = targetY;
    addListeners();
  };

  const update = () => {
    if (canRun()) start();
    else stop();
  };

  const setEnabled = enabled => {
    localStorage.setItem(preferenceKey, enabled ? "on" : "off");
    update();
    window.dispatchEvent(new CustomEvent("slidecraft-cursor-change", {
      detail: { enabled: enabled && supportsLens.matches && !reducedMotion.matches }
    }));
  };

  window.slidecraftSetCursorEnabled = setEnabled;
  supportsLens.addEventListener("change", update);
  reducedMotion.addEventListener("change", update);
  window.addEventListener("pagehide", stop, { once: true });
  update();
})();
