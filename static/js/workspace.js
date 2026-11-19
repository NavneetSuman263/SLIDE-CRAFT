(() => {
  const result = document.getElementById("result-area");
  if (!result) return;
  const requestedLayout = new URLSearchParams(window.location.search).get("template") || "";
  let selectedTemplate = requestedLayout || localStorage.getItem("slidecraft-layout") || "";
  const generationLayout = document.getElementById("generation-layout");
  if (generationLayout) {
    if ([...generationLayout.options].some(option => option.value === selectedTemplate)) {
      generationLayout.value = selectedTemplate;
    } else {
      selectedTemplate = "";
    }
    generationLayout.addEventListener("change", () => {
      selectedTemplate = generationLayout.value;
      if (selectedTemplate) localStorage.setItem("slidecraft-layout", selectedTemplate);
      else localStorage.removeItem("slidecraft-layout");
    });
  }

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));

  const setBusy = (form, busy, label) => {
    const btn = form.querySelector("button[type=submit]");
    if (!btn) return;
    if (busy) {
      btn.dataset.original = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `<span class="spinner"></span> ${label || "Working on it..."}`;
    } else {
      btn.disabled = false;
      btn.innerHTML = btn.dataset.original || "Submit";
    }
  };

  const showError = (message) => {
    finishCreationScreen();
    const canRetryGeneration = Boolean(document.getElementById("generate-form"));
    result.innerHTML = `<div class="result-error"><b>Something needs a second look.</b><p>${esc(message)}</p>${canRetryGeneration ? '<button type="button" class="button button-blue" id="retry-generation-button">Try generation again <span>↻</span></button>' : ""}</div>`;
    document.getElementById("retry-generation-button")?.addEventListener("click", () => {
      document.getElementById("generate-form")?.requestSubmit();
    });
    result.scrollIntoView({behavior:"smooth", block:"start"});
  };

  let creationClock = null;
  let generationStatusPoll = null;
  let generationStartedAt = 0;
  let generationInProgress = false;
  let focusBeforeCreation = null;

  const creationStages = [
    { label: "Understand", icon: "⌕", detail: "Reading your topic, audience, and goals." },
    { label: "Generate", icon: "✳", detail: "Building the complete slide narrative." },
    { label: "Discover visuals", icon: "◉", detail: "Finding visual elements for your slides." },
    { label: "Design", icon: "◈", detail: "Preparing slide layouts and visual elements." },
    { label: "Finalize", icon: "↗", detail: "Getting your editable deck ready." }
  ];

  const creationStageIndex = { understand: 0, gemini: 1, groq: 1, offline: 1, visuals: 2, finalize: 4 };

  const updateCreationProgress = (status) => {
    const screen = document.querySelector(".creation-engine");
    if (!screen || !status || !creationStageIndex.hasOwnProperty(status.stage)) return;
    const activeIndex = creationStageIndex[status.stage];
    const completedThrough = Math.max(0, activeIndex - 1);
    const stages = screen.querySelector(".creation-stages");
    if (stages) stages.outerHTML = renderCreationStages(activeIndex, completedThrough);
    screen.querySelector("#creation-status").textContent = status.message;
    screen.querySelector(".creation-engine__activity").setAttribute(
      "aria-valuetext",
      status.message
    );
    if (status.stage === "offline") screen.classList.add("creation-engine--fallback");
  };

  const beginGenerationStatusPolling = (generationId) => {
    if (generationStatusPoll !== null) window.clearInterval(generationStatusPoll);
    const poll = async () => {
      try {
        const response = await fetch(`/api/generation-status/${encodeURIComponent(generationId)}`, {
          headers: { "Accept": "application/json" },
          cache: "no-store"
        });
        if (response.ok) updateCreationProgress(await response.json());
      } catch {
        // The generation response remains authoritative if status polling is unavailable.
      }
    };
    poll();
    generationStatusPoll = window.setInterval(poll, 800);
  };

  const renderCreationStages = (activeIndex, completedThrough = -1, failedIndex = -1, skippedIndex = -1) => `
    <ol class="creation-stages">${creationStages.map((stage, index) => {
      const state = index === failedIndex ? "failed"
        : index === skippedIndex ? "skipped"
        : index <= completedThrough ? "complete"
        : index === activeIndex ? "active" : "pending";
      const stateLabel = { complete: "Complete", active: "In progress", pending: "Waiting", skipped: "Skipped", failed: "Needs attention" }[state];
      return `<li class="creation-stage creation-stage--${state}"><span class="creation-stage__icon" aria-hidden="true">${state === "complete" ? "✓" : esc(stage.icon)}</span><span class="creation-stage__copy"><b>${String(index + 1).padStart(2, "0")} — ${esc(stage.label)}</b><small>${esc(stage.detail)}</small></span><span class="creation-stage__state">${stateLabel}</span></li>`;
    }).join("")}</ol>`;

  const creationScreenHTML = () => `
    <section class="creation-engine" role="dialog" aria-modal="true" aria-labelledby="creation-title" aria-describedby="creation-status">
      <div class="creation-engine__ambient" aria-hidden="true"></div>
      <header class="creation-engine__top"><a class="creation-engine__brand" href="#" tabindex="-1"><span>S</span> SLIDECRAFT <i>AI</i></a><span class="creation-engine__live"><i></i> CREATION ENGINE</span><button type="button" class="creation-engine__theme" aria-label="Switch creation screen theme">☼ <span>Light mode</span></button></header>
      <div class="creation-engine__layout">
        <div class="creation-engine__main">
          <div class="creation-engine__orb creation-engine__orb--thinking" aria-hidden="true"><div class="creation-engine__orbit creation-engine__orbit--one"></div><div class="creation-engine__orbit creation-engine__orbit--two"></div><div class="creation-engine__orbit creation-engine__orbit--three"></div><div class="creation-engine__core"><span>✳</span></div><i class="creation-engine__particle creation-engine__particle--one"></i><i class="creation-engine__particle creation-engine__particle--two"></i><i class="creation-engine__particle creation-engine__particle--three"></i></div>
          <span class="creation-engine__eyebrow">YOUR IDEA, IN MOTION</span>
          <h1 id="creation-title" tabindex="-1">A new story<br><em>is taking shape.</em></h1>
          <p id="creation-status" class="creation-engine__status" role="status" aria-live="polite">Preparing your presentation...</p>
          <p class="creation-engine__explanation">Organizing your ideas into a clear, editable deck.</p>
          <div class="creation-engine__activity" role="progressbar" aria-label="Presentation generation" aria-valuetext="Waiting for the complete slide set"><span></span></div>
          <div class="creation-engine__elapsed"><span>ELAPSED</span><time id="creation-elapsed">00:00</time></div>
        </div>
        <aside class="creation-engine__workflow"><div class="creation-engine__workflow-heading"><div><span>THE WORKFLOW</span><h2>From brief to deck</h2></div><span class="creation-engine__workflow-mark">01—05</span></div>${renderCreationStages(0)}<div class="creation-engine__preview-note"><span aria-hidden="true">✦</span><p><b>Your slide previews are on their way.</b><small>We’ll show your slides as soon as they’re ready.</small></p></div></aside>
      </div>
      <footer class="creation-engine__footer"><span>THOUGHTFULLY MADE WITH SLIDECRAFT</span><span>Your presentation is taking shape.</span></footer>
    </section>`;

  const showCreationScreen = (form) => {
    if (generationInProgress) return false;
    generationInProgress = true;
    focusBeforeCreation = document.activeElement;
    const appLayout = document.querySelector(".app-layout");
    if (appLayout) appLayout.inert = true;
    document.body.classList.add("creation-engine-open");
    document.body.insertAdjacentHTML("beforeend", creationScreenHTML());
    const screen = document.querySelector(".creation-engine");
    screen.addEventListener("keydown", event => {
      if (event.key !== "Tab") return;
      const focusable = Array.from(screen.querySelectorAll('button:not([disabled]), a[href]:not([tabindex="-1"]), [tabindex="0"]'))
        .filter(element => element.getClientRects().length);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const currentIndex = focusable.indexOf(document.activeElement);
      if (event.shiftKey && currentIndex <= 0) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && currentIndex === focusable.length - 1) {
        event.preventDefault();
        first.focus();
      }
    });
    screen.querySelector("#creation-title").focus({ preventScroll: true });
    const preferredTheme = document.documentElement.dataset.theme || "light";
    screen.classList.toggle("creation-engine--light", preferredTheme === "light");
    screen.querySelector(".creation-engine__theme span").textContent = preferredTheme === "light" ? "Dark mode" : "Light mode";
    screen.querySelector(".creation-engine__theme").firstChild.textContent = preferredTheme === "light" ? "☾ " : "☼ ";
    screen.querySelector(".creation-engine__theme").addEventListener("click", () => {
      const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      window.slidecraftSetTheme?.(nextTheme);
    });
    generationStartedAt = Date.now();
    const updateElapsed = () => {
      const seconds = Math.floor((Date.now() - generationStartedAt) / 1000);
      const time = screen.querySelector("#creation-elapsed");
      if (time) time.textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    };
    updateElapsed();
    creationClock = window.setInterval(updateElapsed, 1000);
    form.querySelector("button[type=submit]")?.blur();
    return true;
  };

  const finishCreationScreen = () => {
    if (creationClock !== null) {
      window.clearInterval(creationClock);
      creationClock = null;
    }
    if (generationStatusPoll !== null) {
      window.clearInterval(generationStatusPoll);
      generationStatusPoll = null;
    }
    const screen = document.querySelector(".creation-engine");
    screen?.classList.add("creation-engine--leaving");
    document.body.classList.remove("creation-engine-open");
    screen?.addEventListener("transitionend", () => screen.remove(), { once: true });
    window.setTimeout(() => screen?.remove(), 220);
    const appLayout = document.querySelector(".app-layout");
    if (appLayout) appLayout.inert = false;
    generationInProgress = false;
    if (focusBeforeCreation?.isConnected && !focusBeforeCreation.disabled) focusBeforeCreation.focus();
    else document.querySelector('#generate-form [name="topic"]')?.focus();
    focusBeforeCreation = null;
  };

  const revealCreationSuccess = async (slides) => {
    const screen = document.querySelector(".creation-engine");
    if (!screen) return;
    const photoCount = slides.filter(slide => /^[a-f0-9]{64}$/.test(String(slide.visual?.image_asset || "")) && String(slide.visual?.image_url || "").startsWith("https://images.unsplash.com/")).length;
    const skippedImages = photoCount === 0;
    screen.classList.add("creation-engine--success");
    screen.querySelector(".creation-engine__orb").classList.add("creation-engine__orb--complete");
    screen.querySelector(".creation-engine__core span").textContent = "✓";
    screen.querySelector("#creation-title").innerHTML = "Your presentation<br><em>is ready.</em>";
    screen.querySelector("#creation-status").textContent = `${slides.length} ${slides.length === 1 ? "slide" : "slides"} created${photoCount ? ` · ${photoCount} ${photoCount === 1 ? "photo" : "photos"} integrated` : ""}.`;
    screen.querySelector(".creation-engine__explanation").textContent = skippedImages
      ? "Your slides are ready with clean, editable visuals."
      : "Your slides and visual elements are ready to explore.";
    screen.querySelector(".creation-engine__activity").setAttribute("aria-valuetext", "Presentation ready");
    screen.querySelector(".creation-engine__workflow").innerHTML = `<div class="creation-engine__workflow-heading"><div><span>CREATION COMPLETE</span><h2>Your deck is ready</h2></div><span class="creation-engine__success-check" aria-hidden="true">✓</span></div>${renderCreationStages(-1, 4, -1, skippedImages ? 2 : -1)}<div class="creation-engine__ready-card"><b>${slides.length} ${slides.length === 1 ? "slide" : "slides"} created</b>${photoCount ? `<span>${photoCount} ${photoCount === 1 ? "photo" : "photos"} integrated</span>` : `<span>Designed with editable native visuals</span>`}<small>Your complete, editable presentation is ready.</small></div><button type="button" class="button button-blue creation-engine__open-deck">Open your presentation <span>↗</span></button>`;
    screen.querySelector(".creation-engine__open-deck").addEventListener("click", finishCreationScreen, { once: true });
    await new Promise(resolve => window.setTimeout(resolve, 550));
    finishCreationScreen();
  };

  const completionHTML = (slides) => {
    const photoCount = slides.filter(slide => /^[a-f0-9]{64}$/.test(String(slide.visual?.image_asset || "")) && String(slide.visual?.image_url || "").startsWith("https://images.unsplash.com/")).length;
    const previewSlides = slides.slice(0, 4);
    return `<section class="deck-ready" aria-labelledby="deck-ready-title"><div class="deck-ready__copy"><span class="deck-ready__eyebrow"><i>✓</i> CREATION COMPLETE</span><h2 id="deck-ready-title" tabindex="-1">Your presentation is ready.</h2><p>${slides.length} ${slides.length === 1 ? "slide" : "slides"} created${photoCount ? ` · ${photoCount} ${photoCount === 1 ? "photo" : "photos"} integrated` : ""}.</p><div class="deck-ready__actions"><button type="button" class="button button-blue" id="edit-presentation-button">Edit presentation <span>↓</span></button></div></div><div class="deck-ready__previews" aria-label="Generated slide previews">${previewSlides.map((slide,index)=>`<article class="deck-ready__preview"><span>SLIDE ${String(index+1).padStart(2,"0")}</span>${slideVisualHTML(slide)}<b>${esc(slide.title || "Untitled slide")}</b></article>`).join("")}</div></section>`;
  };

  const metricsHTML = (analysis) => {
    const metrics = analysis.metrics || {};
    return `<div class="metric-grid">${Object.entries(metrics).map(([name, score]) => `
      <div class="metric-card"><span>${esc(name)}</span><b>${esc(score)}<small>/100</small></b><div class="metric-track"><i style="width:${Math.max(0,Math.min(100,Number(score)||0))}%"></i></div></div>
    `).join("")}</div><div class="analysis-summary"><div><small>OVERALL TEXT SCORE</small><b>${esc(analysis.score)}<span>/100</span></b></div><div><small>SLIDES</small><b>${esc(analysis.slide_count)}</b></div><div><small>WORDS</small><b>${esc(analysis.word_count)}</b></div></div>
    ${(analysis.issues || []).length ? `<div class="issues"><h4>Suggestions to review</h4><ul>${analysis.issues.map(x=>`<li>${esc(x)}</li>`).join("")}</ul></div>` : `<p class="success-note">No obvious text-density issues were detected by the basic checks.</p>`}`;
  };

  const layoutLabels = {
    cover: "Cover",
    split: "Photo and content",
    image_left: "Photo on the left",
    full_image: "Full-slide photo",
    chart: "Data chart",
    diagram: "Diagram",
    comparison: "Comparison",
    metrics: "Key insights",
    conclusion: "Closing slide"
  };

  const chartPreviewHTML = (chart) => {
    if (!chart || !Array.isArray(chart.labels) || !Array.isArray(chart.series) || !chart.series.length) return "";
    const values = chart.series.flatMap(series => Array.isArray(series.values) ? series.values.map(Number) : []);
    if (values.length !== chart.labels.length * chart.series.length || values.some(value => !Number.isFinite(value))) return "";
    const palette = ["#3268f5", "#ff8069", "#2ea47e", "#8e63e0", "#f0b13a", "#22a0be"];
    if (chart.type === "pie" || chart.type === "doughnut") {
      const pieValues = chart.series[0].values.map(Number);
      if (pieValues.some(value => value < 0) || pieValues.reduce((sum, value) => sum + value, 0) <= 0) return "";
      const total = pieValues.reduce((sum, value) => sum + value, 0);
      let cursor = 0;
      const stops = pieValues.map((value, index) => {
        const start = cursor;
        cursor += value / total * 100;
        return `${palette[index % palette.length]} ${start}% ${cursor}%`;
      }).join(", ");
      return `<div class="chart-preview"><div class="chart-preview__pie ${chart.type === "doughnut" ? "chart-preview__pie--donut" : ""}" style="background:conic-gradient(${stops})" role="img" aria-label="${esc(chart.title || "Slide chart")}"></div><ul>${chart.labels.map((label,index)=>`<li><i style="--series-color:${palette[index % palette.length]}"></i><span>${esc(label)}</span><b>${esc(pieValues[index])}${esc(chart.units ? ` ${chart.units}` : "")}</b></li>`).join("")}</ul></div>`;
    }
    const min = Math.min(0, ...values);
    const max = Math.max(1, ...values);
    const range = max - min || 1;
    const chartHeight = 150;
    const baseline = chartHeight - (0 - min) / range * 128 - 8;
    const isLine = chart.type === "line";
    const bars = chart.labels.map((label, index) => {
      const groupWidth = 760 / chart.labels.length;
      const valuesForLabel = chart.series.map(series => Number(series.values[index]));
      const markup = valuesForLabel.map((value, seriesIndex) => {
        const x = 24 + index * groupWidth + seriesIndex * Math.min(36, groupWidth / Math.max(1, chart.series.length));
        const valueY = chartHeight - ((value - min) / range) * 128 - 8;
        if (isLine) return `<circle cx="${x + 14}" cy="${valueY}" r="5" fill="${palette[seriesIndex % palette.length]}"><title>${esc(chart.series[seriesIndex].name)} · ${esc(label)}: ${esc(value)}</title></circle>`;
        const y = Math.min(valueY, baseline);
        const height = Math.max(2, Math.abs(baseline - valueY));
        return `<rect x="${x}" y="${y}" width="${Math.max(8, Math.min(34, groupWidth / Math.max(1, chart.series.length) - 5))}" height="${height}" rx="4" fill="${palette[seriesIndex % palette.length]}"><title>${esc(chart.series[seriesIndex].name)} · ${esc(label)}: ${esc(value)}</title></rect>`;
      }).join("");
      return `<g>${markup}<text x="${24 + index * groupWidth + groupWidth/2}" y="146" text-anchor="middle">${esc(label)}</text></g>`;
    }).join("");
    const linePaths = isLine ? chart.series.map((series, seriesIndex) => {
      const groupWidth = 760 / chart.labels.length;
      const points = series.values.map((value,index) => {
        const x = 24 + index * groupWidth + 14;
        const y = chartHeight - ((Number(value) - min) / range) * 128 - 8;
        return `${x},${y}`;
      }).join(" ");
      return `<polyline points="${points}" fill="none" stroke="${palette[seriesIndex % palette.length]}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>`;
    }).join("") : "";
    return `<div class="chart-preview"><svg viewBox="0 0 800 165" role="img" aria-label="${esc(chart.title || "Slide chart")}"><line x1="18" y1="${baseline}" x2="782" y2="${baseline}" stroke="#dce3ef"/>${bars}${linePaths}</svg><div class="chart-preview__legend">${chart.series.map((series,index)=>`<span><i style="--series-color:${palette[index % palette.length]}"></i>${esc(series.name)}</span>`).join("")}</div><small>${esc(chart.illustrative ? "Illustrative data" : `Source: ${chart.source || "Source not provided"}`)}${chart.units ? ` · ${esc(chart.units)}` : ""}</small></div>`;
  };

  const slideVisualHTML = (slide) => {
    const chart = chartPreviewHTML(slide.chart);
    if (chart) return `<div class="slide-preview__visual"><span class="preview-kind">Chart · ${esc(slide.chart.title || "Data")}</span>${chart}</div>`;
    const diagram = slide.diagram;
    if (diagram && Array.isArray(diagram.items) && diagram.items.length >= 2) {
      return `<div class="slide-preview__visual"><span class="preview-kind">${esc(diagram.type || "Process")} diagram</span><div class="diagram-preview">${diagram.items.map((item,index)=>`${index ? '<span class="diagram-preview__arrow" aria-hidden="true">→</span>' : ""}<div>${esc(item)}</div>`).join("")}</div>${diagram.caption ? `<small>${esc(diagram.caption)}</small>` : ""}</div>`;
    }
    const asset = String(slide.visual?.image_asset || "");
    const imageUrl = String(slide.visual?.image_url || "");
    if (/^[a-f0-9]{64}$/.test(asset) && slide.visual?.image_provider === "adobe_firefly") {
      return `<div class="slide-preview__visual"><img class="slide-preview__photo" src="/api/slide-image/${asset}" alt="${esc(slide.visual?.image_alt || slide.title)}"><small>Generated with Adobe Firefly</small></div>`;
    }
    if (/^[a-f0-9]{64}$/.test(asset) && imageUrl.startsWith("https://images.unsplash.com/")) {
      const photographerLink = String(slide.visual?.photographer_url || "");
      const unsplashLink = String(slide.visual?.unsplash_url || "");
      return `<div class="slide-preview__visual"><img class="slide-preview__photo" src="${esc(imageUrl)}" alt="${esc(slide.visual?.image_alt || slide.title)}"><small>Photo by ${photographerLink.startsWith("https://unsplash.com/") ? `<a href="${esc(photographerLink)}" target="_blank" rel="noopener noreferrer">${esc(slide.visual?.image_credit || "Unsplash photographer")}</a>` : esc(slide.visual?.image_credit || "Unsplash photographer")} · ${unsplashLink.startsWith("https://unsplash.com/") ? `<a href="${esc(unsplashLink)}" target="_blank" rel="noopener noreferrer">Unsplash</a>` : "Unsplash"}</small></div>`;
    }
    return `<div class="slide-preview__visual slide-preview__placeholder"><span aria-hidden="true">✦</span><b>Suggested visual — not yet added</b></div>`;
  };

  const slideEditorHTML = (slides) => `
  <div class="slides-section-heading"><div class="slides-section-title"><span>02 · YOUR PRESENTATION</span><h2>Your slides</h2><p>Choose any slide below to edit it. Your changes appear in the preview.</p></div><div class="slides-section-actions"><span class="slide-total-badge"><b>${slides.length}</b> ${slides.length === 1 ? "slide" : "slides"}</span><div class="pdf-export-control"><button id="export-button" class="button button-blue">Download PDF ↓</button><button type="button" class="pdf-menu-toggle" aria-label="Show download format options" aria-expanded="false" aria-controls="pdf-export-menu">⌄</button><div class="pdf-export-menu" id="pdf-export-menu" hidden><b>Download format</b><button type="button" data-export-format="pptx">PowerPoint presentation (.pptx)</button><button type="button" data-export-format="word">Microsoft Word document (.docx)</button><span class="pdf-export-menu__label">PDF layout</span><button type="button" data-pdf-preset="presentation">Presentation PDF · 16:9</button><button type="button" data-pdf-preset="print">Print-ready PDF</button><button type="button" data-pdf-preset="a4_landscape">A4 landscape</button><button type="button" data-pdf-preset="a4_portrait">A4 portrait</button><button type="button" data-pdf-preset="letter">US Letter</button><button type="button" data-pdf-preset="compressed">Compressed PDF</button><button type="button" data-pdf-preset="speaker_notes">Speaker notes PDF</button><span class="pdf-export-menu__label">Handout · slides per page</span><button type="button" data-pdf-preset="handout_2">2 slides per page</button><button type="button" data-pdf-preset="handout_4">4 slides per page</button><button type="button" data-pdf-preset="handout_6">6 slides per page</button></div></div></div></div>
  <div class="generation-summary" role="status"><b>Your presentation is ready.</b><span>${slides.length} ${slides.length === 1 ? "slide" : "slides"} created. Refine your content or choose a download format.</span></div>
    <div id="slide-editor" class="slide-editor">${slides.map((slide,index)=>{
      const normalized = { ...slide, body: slide.body || "", bullets: Array.isArray(slide.bullets) ? slide.bullets : [], notes: slide.notes || "", visual: { ...(slide.visual || {}), layout: slide.visual?.layout || "split" }, chart: slide.chart || null, diagram: slide.diagram || null };
      const layoutOptions = Object.entries(layoutLabels).map(([value,label]) => `<option value="${value}" ${normalized.visual.layout === value ? "selected" : ""}>${label}</option>`).join("");
      const fireflyPrompt = `Create a polished presentation image for "${normalized.title}". ${normalized.body} ${normalized.bullets.join(". ")}`.slice(0, 1024);
      return `<article class="editable-slide" data-slide="${index}">
        <div class="editable-slide-top"><span>Slide ${index+1} of ${slides.length}</span><span>${esc(layoutLabels[normalized.visual.layout] || "Photo and content")}</span></div>
        <section class="slide-preview">${slideVisualHTML(normalized)}<div class="slide-preview__copy"><h3 class="preview-title">${esc(normalized.title || "Untitled slide")}</h3><p class="preview-body">${esc(normalized.body)}</p><ul class="preview-bullets">${normalized.bullets.map(bullet=>`<li>${esc(bullet)}</li>`).join("")}</ul></div></section>
        <label>Slide title<input class="slide-title-input" value="${esc(normalized.title || "")}" maxlength="160"></label>
        <label>Slide explanation<textarea class="slide-body-input" rows="3" maxlength="1000">${esc(normalized.body)}</textarea><small>Explain the slide's main idea and context.</small></label>
        <label>Key content<textarea class="slide-bullets-input" rows="${Math.max(3,Math.min(7,normalized.bullets.length+1))}">${esc(normalized.bullets.join("\n"))}</textarea><small>Enter one key point per line.</small></label>
        <details class="speaker-notes-editor"><summary>Speaker notes (optional)</summary><textarea class="slide-notes-input" rows="3" placeholder="Add helpful speaking notes">${esc(normalized.notes)}</textarea></details>
        <details class="firefly-studio" data-firefly-studio>
          <summary>Adobe Firefly · generate an image</summary>
          <p class="firefly-status" role="status" aria-live="polite">Checking Adobe Firefly availability…</p>
          <label>Image prompt<textarea class="firefly-prompt" rows="3" maxlength="1024">${esc(fireflyPrompt)}</textarea></label>
          <label>Exclude from image<textarea class="firefly-negative-prompt" rows="2" maxlength="1024" placeholder="Optional details to avoid"></textarea></label>
          <div class="form-grid"><label>Model<select class="firefly-model"><option value="image4_standard">Firefly Image 4 Standard</option><option value="image3">Firefly Image 3</option></select></label><label>Aspect ratio<select class="firefly-aspect"><option value="16:9">Widescreen · 16:9</option><option value="4:3">Classic · 4:3</option><option value="1:1">Square · 1:1</option></select></label></div>
          <label>Variations<select class="firefly-variations"><option value="1">1 preview</option><option value="2" selected>2 previews</option><option value="3">3 previews</option><option value="4">4 previews</option></select></label>
          <button type="button" class="button button-outline firefly-generate" disabled>Generate previews</button>
          <div class="firefly-results" aria-live="polite"></div>
        </details>
        <details class="advanced-slide-options"><summary>Advanced options</summary>
          <label>Slide style<select class="slide-layout-input">${layoutOptions}</select></label>
          <details class="structured-visual-editor"><summary>Chart data</summary><p>Factual charts need a source. Use illustrative=true for example data.</p><textarea class="slide-chart-input" rows="8" spellcheck="false">${normalized.chart ? esc(JSON.stringify(normalized.chart,null,2)) : ""}</textarea></details>
          <details class="structured-visual-editor"><summary>Diagram data</summary><p>Optional editable process, timeline, comparison, or hierarchy.</p><textarea class="slide-diagram-input" rows="5" spellcheck="false">${normalized.diagram ? esc(JSON.stringify(normalized.diagram,null,2)) : ""}</textarea></details>
        </details>
      </article>`;
    }).join("")}</div>`;

  const readOptionalJSON = (card, selector) => {
    const input = card.querySelector(selector);
    if (!input) return null;
    const value = input.value.trim();
    if (!value) return null;
    try { return JSON.parse(value); }
    catch { throw new Error(`Slide ${Number(card.dataset.slide) + 1}: check the advanced chart or diagram data.`); }
  };

  const getSlidesFromEditor = () => Array.from(document.querySelectorAll(".editable-slide")).map(card => {
    const prior = card.slideData || {};
    const priorVisual = prior.visual || {};
    return {
      ...prior,
      title: card.querySelector(".slide-title-input").value.trim() || "Untitled slide",
      body: card.querySelector(".slide-body-input").value.trim(),
      bullets: card.querySelector(".slide-bullets-input").value.split("\n").map(s=>s.trim()).filter(Boolean),
      notes: card.querySelector(".slide-notes-input").value,
      visual: { ...priorVisual, layout: card.querySelector(".slide-layout-input").value },
      chart: readOptionalJSON(card, ".slide-chart-input"),
      diagram: readOptionalJSON(card, ".slide-diagram-input")
    };
  });

  const refreshPreview = (card) => {
    const title = card.querySelector(".slide-title-input").value.trim() || "Untitled slide";
    const body = card.querySelector(".slide-body-input").value.trim();
    const bullets = card.querySelector(".slide-bullets-input").value.split("\n").map(item=>item.trim()).filter(Boolean);
    const prior = card.slideData || {};
    card.querySelector(".preview-title").textContent = title;
    card.querySelector(".preview-body").textContent = body;
    card.querySelector(".preview-bullets").innerHTML = bullets.map(bullet=>`<li>${esc(bullet)}</li>`).join("");
    const visual = card.querySelector(".slide-preview__visual");
    if (visual) visual.outerHTML = slideVisualHTML({
      ...prior,
      title,
      body,
      bullets,
      chart: readOptionalJSON(card, ".slide-chart-input"),
      diagram: readOptionalJSON(card, ".slide-diagram-input")
    });
    card.slideData = {
      ...prior,
      title,
      body,
      bullets,
      visual: { ...(prior.visual || {}), layout: card.querySelector(".slide-layout-input").value },
      chart: readOptionalJSON(card, ".slide-chart-input"),
      diagram: readOptionalJSON(card, ".slide-diagram-input")
    };
    const count = card.querySelector(".editable-slide-top span:last-child");
    count.textContent = layoutLabels[card.querySelector(".slide-layout-input").value] || "Photo and content";
  };

  const initializeFireflyStudio = () => {
    const panels = Array.from(document.querySelectorAll("[data-firefly-studio]"));
    if (!panels.length) return;
    fetch("/api/firefly/status")
      .then(async response => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "Adobe Firefly status is unavailable.");
        panels.forEach(panel => {
          panel.querySelector(".firefly-status").textContent = data.message;
          panel.querySelector(".firefly-generate").disabled = !data.available;
        });
      })
      .catch(error => {
        panels.forEach(panel => {
          panel.querySelector(".firefly-status").textContent =
            error.message || "Adobe Firefly status is unavailable.";
          panel.querySelector(".firefly-generate").disabled = true;
        });
      });

    panels.forEach(panel => {
      const card = panel.closest(".editable-slide");
      const button = panel.querySelector(".firefly-generate");
      const status = panel.querySelector(".firefly-status");
      const results = panel.querySelector(".firefly-results");
      panel.addEventListener("click", async event => {
        const acceptButton = event.target.closest("[data-firefly-accept]");
        if (acceptButton) {
          acceptButton.disabled = true;
          try {
            const response = await fetch("/api/firefly/accept", {
              method: "POST",
              headers: buildHeaders(true),
              body: JSON.stringify({
                job_id: acceptButton.dataset.jobId,
                output_index: Number(acceptButton.dataset.fireflyAccept),
              }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || "The image could not be accepted.");
            card.slideData = {
              ...card.slideData,
              visual: {
                ...(card.slideData.visual || {}),
                ...data.visual,
                layout: card.querySelector(".slide-layout-input").value,
              },
            };
            refreshPreview(card);
            results.innerHTML = "";
            status.textContent = "Image accepted. It is included in the editable preview and exports.";
          } catch (error) {
            status.textContent = error.message || "The image could not be accepted.";
            acceptButton.disabled = false;
          }
          return;
        }
        if (!event.target.closest(".firefly-generate")) return;
        const prompt = panel.querySelector(".firefly-prompt").value.trim();
        const negativePrompt = panel.querySelector(".firefly-negative-prompt").value.trim();
        if (!prompt) {
          status.textContent = "Enter an image prompt before generating.";
          panel.querySelector(".firefly-prompt").focus();
          return;
        }
        button.disabled = true;
        button.textContent = "Generating previews…";
        results.innerHTML = "";
        try {
          const response = await fetch("/api/firefly/generate", {
            method: "POST",
            headers: buildHeaders(true),
            body: JSON.stringify({
              slide_index: Number(card.dataset.slide),
              prompt,
              negative_prompt: negativePrompt,
              model: panel.querySelector(".firefly-model").value,
              aspect_ratio: panel.querySelector(".firefly-aspect").value,
              num_variations: Number(panel.querySelector(".firefly-variations").value),
            }),
          });
          const data = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(data.error || "Adobe Firefly could not start generation.");
          status.textContent = "Adobe Firefly is creating your image previews…";
          let outputData;
          for (let attempt = 0; attempt < 60; attempt += 1) {
            await new Promise(resolve => window.setTimeout(resolve, 2000));
            const pollResponse = await fetch(`/api/firefly/jobs/${encodeURIComponent(data.job_id)}`);
            const pollData = await pollResponse.json().catch(() => ({}));
            if (!pollResponse.ok) throw new Error(pollData.error || "Image generation status could not be checked.");
            if (pollData.status === "failed") throw new Error(pollData.error || "Adobe Firefly could not generate images.");
            if (pollData.status === "succeeded") {
              outputData = pollData.outputs;
              break;
            }
          }
          if (!Array.isArray(outputData) || !outputData.length) {
            throw new Error("Image generation is taking longer than expected. Please try again.");
          }
          results.innerHTML = outputData.map((output, index) => {
            if (!/^[a-f0-9]{64}$/.test(String(output.asset || ""))) return "";
            return `<article class="firefly-result"><img src="/api/slide-image/${output.asset}" alt="${esc(output.alt || "Generated image preview")}"><button type="button" class="button button-outline" data-firefly-accept="${index}" data-job-id="${esc(data.job_id)}">Use this image</button></article>`;
          }).join("");
          status.textContent = `${outputData.length} image ${outputData.length === 1 ? "preview is" : "previews are"} ready. Choose one to add it to this slide.`;
        } catch (error) {
          status.textContent = error.message || "Adobe Firefly image generation failed.";
        } finally {
          button.disabled = false;
          button.textContent = "Generate previews";
        }
      });
    });
  };

  const initializeSlideEditor = (slides) => {
    const cards = Array.from(document.querySelectorAll(".editable-slide"));
    cards.forEach((card,index) => { card.slideData = slides[index] || {}; });
    document.getElementById("slide-editor").addEventListener("input", event => {
      const card = event.target.closest(".editable-slide");
      if (card && (event.target.matches(".slide-title-input") || event.target.matches(".slide-body-input") || event.target.matches(".slide-bullets-input") || event.target.matches(".slide-chart-input") || event.target.matches(".slide-diagram-input"))) {
        try { refreshPreview(card); }
        catch { return; }
      }
    });
    document.getElementById("slide-editor").addEventListener("change", event => {
      if (event.target.matches(".slide-layout-input")) refreshPreview(event.target.closest(".editable-slide"));
    });
    document.getElementById("slide-editor").addEventListener("error", event => {
      if (event.target.matches(".slide-preview__photo")) {
        const visual = event.target.closest(".slide-preview__visual");
        visual.outerHTML = `<div class="slide-preview__visual slide-preview__placeholder"><span aria-hidden="true">✦</span><b>Suggested visual — not yet added</b></div>`;
      }
    }, true);
    initializeFireflyStudio();
  };

  const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  const buildHeaders = (isJSON = false) => {
    const headers = new Headers();
    if (isJSON) headers.set("Content-Type", "application/json");
    headers.set("X-CSRFToken", csrfToken());
    return headers;
  };

  const bindExport = () => {
    const btn = document.getElementById("export-button");
    if (!btn) return;
    const menuToggle = document.querySelector(".pdf-menu-toggle");
    const menu = document.getElementById("pdf-export-menu");
    let status = document.getElementById("export-status");
    if (!status) {
      status = document.createElement("small");
      status.id = "export-status";
      status.className = "export-status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      btn.parentElement.parentElement.append(status);
    }
    const notesButton = menu?.querySelector('[data-pdf-preset="speaker_notes"]');
    const updateNotesOption = () => {
      if (notesButton) {
        notesButton.disabled = !Array.from(document.querySelectorAll(".slide-notes-input"))
          .some(input => input.value.trim());
      }
    };
    updateNotesOption();
    document.querySelectorAll(".slide-notes-input").forEach(input => {
      input.addEventListener("input", updateNotesOption);
    });
    menuToggle?.addEventListener("click", () => {
      const open = menu.hidden;
      menu.hidden = !open;
      menuToggle.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", event => {
      if (menu && !menu.hidden && !event.target.closest(".pdf-export-control")) {
        menu.hidden = true;
        menuToggle?.setAttribute("aria-expanded", "false");
      }
    });
    menu?.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        menu.hidden = true;
        menuToggle?.setAttribute("aria-expanded", "false");
        menuToggle?.focus();
      }
    });

    const getExportTitle = () => {
      if (result.dataset.presentationTitle) return result.dataset.presentationTitle;
      const topic = document.querySelector('#generate-form [name="topic"]')?.value.trim();
      return topic || getSlidesFromEditor()[0]?.title || "SlideCraft Presentation";
    };

    const responseFilename = (response, fallback) => {
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="?([^";]+)"?/i);
      return match?.[1] || fallback;
    };

    const downloadExport = async ({ format, preset, trigger }) => {
      if (btn.disabled) return;
      const oldText = btn.innerHTML;
      btn.disabled = true;
      if (trigger) trigger.disabled = true;
      btn.textContent = format === "pdf" ? "Preparing PDF..." : `Preparing ${format.toUpperCase()}...`;
      status.textContent = "";
      try {
        const endpoint = format === "word" ? "/api/export/docx"
          : format === "pptx" ? "/api/export/pptx"
          : "/api/export/pdf";
        const payload = { slides: getSlidesFromEditor(), title: getExportTitle() };
        if (format === "pdf") payload.preset = preset;
        const response = await fetch(endpoint, {
          method:"POST",
          headers: buildHeaders(true),
          body:JSON.stringify(payload)
        });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.error || `${format.toUpperCase()} export failed.`);
        }
        const blob = await response.blob();
        const expectedType = format === "word"
          ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          : format === "pptx"
            ? "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            : "application/pdf";
        if (blob.type !== expectedType) throw new Error(`${format.toUpperCase()} export failed.`);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = responseFilename(
          response,
          `slidecraft_presentation_${Date.now()}.${format === "word" ? "docx" : format}`
        );
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        status.textContent = `${format.toUpperCase()} download started.`;
        if (menu) menu.hidden = true;
        menuToggle?.setAttribute("aria-expanded", "false");
        menuToggle?.focus();
      } catch (error) {
        status.textContent = error.message || `We couldn't prepare your ${format.toUpperCase()}. Please try again.`;
      } finally {
        btn.disabled = false;
        btn.innerHTML = oldText;
        if (trigger) trigger.disabled = false;
      }
    };

    btn.addEventListener("click", () => downloadExport({ format: "pdf", preset: "presentation", trigger: btn }));
    menu?.querySelectorAll("[data-export-format]").forEach(option => {
      option.addEventListener("click", () => downloadExport({ format: option.dataset.exportFormat, trigger: option }));
    });
    menu?.querySelectorAll("[data-pdf-preset]").forEach(option => {
      option.addEventListener("click", () => downloadExport({ format: "pdf", preset: option.dataset.pdfPreset, trigger: option }));
    });
  };

  const bindForm = (id, endpoint, handler) => {
    const form = document.getElementById(id);
    if (!form) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (id === "generate-form" && generationInProgress) return;
      if (id === "generate-form" && !showCreationScreen(form)) return;
      setBusy(form, true);
      if (id !== "generate-form") result.innerHTML = `<div class="loading-card"><span class="spinner spinner-dark"></span><b>Working on your presentation...</b><p>This may take a few moments.</p></div>`;
      try {
        const isJSON = ["generate-form", "shorten-form", "template-form"].includes(form.id);
        const formData = new FormData(form);
        formData.set("csrf_token", csrfToken());
        const controller = id === "generate-form" ? new AbortController() : null;
        if (id === "generate-form") {
          const generationId = globalThis.crypto?.randomUUID
            ? globalThis.crypto.randomUUID()
            : `${Date.now()}_${Math.random().toString(36).slice(2)}_${Math.random().toString(36).slice(2)}`;
          formData.set("generation_id", generationId);
          formData.set("template", generationLayout?.value || selectedTemplate);
          beginGenerationStatusPolling(generationId);
        }
        const requestTimeout = controller ? window.setTimeout(() => controller.abort(), 900000) : null;
        let response;
        try {
          response = await fetch(endpoint, {
            method:"POST",
            headers: isJSON ? buildHeaders(true) : buildHeaders(false),
            body: isJSON ? JSON.stringify(Object.fromEntries(formData.entries())) : formData,
            ...(controller ? { signal: controller.signal } : {})
          });
        } finally {
          if (requestTimeout !== null) window.clearTimeout(requestTimeout);
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "Request failed.");
        await handler(data);
      } catch (e) {
        const message = e.name === "AbortError"
          ? "Generation took too long to respond. Check your connection and try again."
          : (e instanceof TypeError
            ? "We couldn't reach SlideCraft. Check your connection and try again."
            : (e.message || "The request failed. Please try again."));
        showError(message);
      }
      finally { setBusy(form, false); }
    });
  };

  bindForm("generate-form", "/api/generate", async data => {
    const slides = Array.isArray(data.slides) ? data.slides : [];
    if (!slides.length) {
      showError("No slides were returned. Please try generating the presentation again.");
      return;
    }
    result.dataset.presentationTitle =
      document.querySelector('#generate-form [name="topic"]')?.value.trim() || slides[0].title || "";
    await revealCreationSuccess(slides);
    result.innerHTML = completionHTML(slides) + slideEditorHTML(slides) +
      `      <div class="analysis-panel"><div class="result-header"><div><span class="eyebrow eyebrow-dark">QUICK CONTENT CHECK</span><h2>Content check</h2></div></div>${metricsHTML(data.analysis || {})}</div>`;
    initializeSlideEditor(slides);
    document.getElementById("deck-ready-title")?.focus({ preventScroll: true });
    document.getElementById("edit-presentation-button")?.addEventListener("click", () => {
      document.getElementById("slide-editor")?.scrollIntoView({ behavior: "smooth", block: "start" });
      document.querySelector(".slide-title-input")?.focus({ preventScroll: true });
    });
    const slidesNavigation = document.getElementById("slides-nav-link");
    if (slidesNavigation) slidesNavigation.hidden = false;
    bindExport();
    result.scrollIntoView({behavior:"smooth", block:"start"});
  });

  bindForm("analyze-form", "/api/analyze", data => {
    result.innerHTML = `<div class="analysis-panel"><div class="result-header"><div><span class="eyebrow eyebrow-dark">PRESENTATION REVIEW</span><h2>Analysis results</h2><p>These scores use extracted text only.</p></div></div>${metricsHTML(data.analysis || {})}<h3 class="result-subheading">Extracted slide text</h3><div class="extracted-list">${(data.slides||[]).map((s,i)=>`<div><span>${String(i+1).padStart(2,"0")}</span><b>${esc(s.title)}</b><small>${(s.bullets||[]).length} text items</small></div>`).join("")}</div></div>`;
    result.scrollIntoView({behavior:"smooth",block:"start"});
  });

  bindForm("compare-form", "/api/compare", data => {
    result.innerHTML = `<div class="analysis-panel"><span class="eyebrow eyebrow-dark">SIDE BY SIDE</span><h2>Comparison results</h2><p>${esc(data.verdict)}</p><div class="comparison-grid"><div><h3>Presentation A</h3>${metricsHTML(data.a)}</div><div><h3>Presentation B</h3>${metricsHTML(data.b)}</div></div></div>`;
    result.scrollIntoView({behavior:"smooth",block:"start"});
  });

  bindForm("optimize-form", "/api/optimize", data => {
    const slides = Array.isArray(data.optimized) ? data.optimized : [];
    result.innerHTML = slideEditorHTML(slides) + `<div class="editor-hint">Optimized slide text is editable. Export creates a newly designed PowerPoint.</div>`;
    initializeSlideEditor(slides);
    bindExport();
    result.scrollIntoView({behavior:"smooth",block:"start"});
  });

  bindForm("quiz-form", "/api/quiz", data => {
    const questions = Array.isArray(data.questions) ? data.questions : [];
    let score = 0;
    let answered = 0;
    result.innerHTML = `<div class="analysis-panel"><div class="result-header"><div><span class="eyebrow eyebrow-dark">PRESENTATION QUIZ</span><h2>Test your understanding</h2><p>${esc(data.difficulty)} · ${questions.length} questions</p>${data.local_fallback ? `<p class="editor-hint">Created directly from slide text because AI generation was unavailable. Review answers against the deck.</p>` : ""}</div><div class="quiz-export-control"><button type="button" class="button button-blue" id="download-quiz-ppt">Download Quiz PPT</button><small class="quiz-export-status" id="quiz-export-status" role="status" aria-live="polite"></small></div></div><div class="quiz-list">${questions.map((item,index)=>`
      <article class="quiz-card" data-answer="${esc(item.answer)}" data-index="${index}">
        <span class="quiz-number">${String(index+1).padStart(2,"0")} · ${esc(item.type)}</span>
        <h3>${esc(item.question)}</h3>
        ${item.options?.length ? `<div class="quiz-options">${item.options.map(option=>`<label><input type="radio" name="quiz-${index}" value="${esc(option)}"><span>${esc(option)}</span></label>`).join("")}</div>` : `<label class="short-answer">Your answer<input type="text" name="quiz-${index}" autocomplete="off"></label>`}
        <button type="button" class="button button-outline quiz-submit">Submit answer</button>
        <p class="quiz-feedback" aria-live="polite"></p>
      </article>`).join("")}</div><div class="quiz-score" hidden><b>Final score</b><strong id="quiz-score-value">0/${questions.length}</strong><button type="button" class="button button-blue" id="quiz-restart">Restart quiz</button></div></div>`;
    document.getElementById("download-quiz-ppt").addEventListener("click", async event => {
      const button = event.currentTarget;
      const status = document.getElementById("quiz-export-status");
      const originalLabel = button.textContent;
      button.disabled = true;
      button.textContent = "Preparing PowerPoint...";
      status.textContent = "";
      try {
        const response = await fetch("/api/quiz/export/pptx", {
          method: "POST",
          headers: buildHeaders(true),
          body: JSON.stringify({ questions, difficulty: data.difficulty })
        });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.error || "Quiz PowerPoint export failed.");
        }
        const blob = await response.blob();
        if (blob.type !== "application/vnd.openxmlformats-officedocument.presentationml.presentation") {
          throw new Error("Quiz PowerPoint export failed.");
        }
        const disposition = response.headers.get("Content-Disposition") || "";
        const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || "slidecraft_quiz.pptx";
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        status.textContent = "PowerPoint download started.";
      } catch (error) {
        status.textContent = error.message || "Quiz PowerPoint export failed. Please try again.";
      } finally {
        button.disabled = false;
        button.textContent = originalLabel;
      }
    });
    const cards = Array.from(result.querySelectorAll(".quiz-card"));
    cards.forEach(card => {
      card.querySelectorAll(".quiz-options input").forEach(input => {
        input.addEventListener("change", () => {
          card.querySelectorAll(".quiz-options label").forEach(label => {
            label.classList.toggle("is-selected", label.contains(input) && input.checked);
          });
        });
      });
      card.querySelector(".quiz-submit").addEventListener("click", () => {
        if (card.dataset.answered === "true") return;
        const input = card.querySelector("input:checked") || card.querySelector("input[type=text]");
        const value = input?.value.trim() || "";
        if (!value) {
          card.querySelector(".quiz-feedback").textContent = "Choose or enter an answer first.";
          return;
        }
        card.dataset.answered = "true";
        const answer = card.dataset.answer.toLowerCase();
        const correct = value.toLowerCase() === answer || (answer && value.toLowerCase().includes(answer)) || (answer && answer.includes(value.toLowerCase()));
        if (correct) score += 1;
        answered += 1;
        const item = questions[Number(card.dataset.index)];
        const feedback = card.querySelector(".quiz-feedback");
        feedback.className = `quiz-feedback ${correct ? "quiz-correct" : "quiz-wrong"}`;
        feedback.innerHTML = `<b>${correct ? "Correct" : "Not quite"}</b> · ${esc(correct ? "Your answer matches the slide content." : `Correct answer: ${item.answer}`)}${item.explanation ? `<small>${esc(item.explanation)}</small>` : ""}`;
        card.querySelectorAll("input").forEach(field => field.disabled = true);
        card.querySelector(".quiz-submit").disabled = true;
        if (answered === cards.length) {
          const scorePanel = result.querySelector(".quiz-score");
          scorePanel.hidden = false;
          document.getElementById("quiz-score-value").textContent = `${score}/${cards.length}`;
          document.getElementById("quiz-restart").addEventListener("click", () => {
            document.getElementById("quiz-form")?.requestSubmit();
          });
        }
      });
    });
    result.scrollIntoView({behavior:"smooth",block:"start"});
  });

  bindForm("readiness-form", "/api/readiness", data => {
    const ready = data.readiness || {};
    const metricCards = Object.entries(ready.metrics || {}).map(([name,score]) => `
      <div class="metric-card"><span>${esc(name)}</span><b>${esc(score)}<small>/100</small></b><div class="metric-track"><i style="width:${Math.max(0,Math.min(100,Number(score)||0))}%"></i></div></div>`).join("");
    result.innerHTML = `<div class="analysis-panel"><div class="result-header"><div><span class="eyebrow eyebrow-dark">READINESS REPORT</span><h2>Presentation readiness</h2></div></div><div class="readiness-score"><span>Overall score</span><b>${esc(ready.overall)}<small>/100</small></b></div><div class="metric-grid">${metricCards}</div><div class="readiness-columns"><div><h3>Strengths</h3><ul>${(ready.strengths||[]).map(item=>`<li>${esc(item)}</li>`).join("")}</ul></div><div><h3>Areas to improve</h3><ul>${(ready.weaknesses||[]).map(item=>`<li>${esc(item)}</li>`).join("")}</ul></div></div><h3 class="result-subheading">Actionable suggestions</h3><ul class="suggestion-list">${(ready.suggestions||[]).map(item=>`<li>${esc(item)}</li>`).join("")}</ul><h3 class="result-subheading">Slides to review</h3><div class="extracted-list">${(ready.slides_to_review||[]).map(item=>`<div><span>${String(item.slide).padStart(2,"0")}</span><b>${esc(item.title)}</b><small>${esc(item.reason)}</small></div>`).join("") || '<div><span>✓</span><b>No critical text-density issues detected</b></div>'}</div><p class="editor-hint">${esc(ready.limitation)}</p></div>`;
    result.scrollIntoView({behavior:"smooth",block:"start"});
  });

  bindForm("shorten-form", "/api/shorten", data => {
    result.innerHTML = `<div class="analysis-panel"><div class="result-header"><div><span class="eyebrow eyebrow-dark">CONTENT IMPROVEMENT</span><h2>Shortened version</h2></div></div><div class="content-comparison"><div><h3>Original</h3><textarea readonly>${esc(data.original)}</textarea></div><div><h3>Suggested shorter text</h3><textarea id="shortened-content">${esc(data.shortened)}</textarea></div></div><p class="editor-hint">${esc(data.layout_check)}</p><div class="shortener-actions"><button type="button" class="button button-blue" id="accept-shortening">Accept suggestion</button><button type="button" class="button button-outline" id="reject-shortening">Reject</button></div><ul class="suggestion-list">${(data.changes||[]).map(item=>`<li>${esc(item)}</li>`).join("")}</ul></div>`;
    document.getElementById("accept-shortening")?.addEventListener("click", () => {
      const text = document.getElementById("shortened-content");
      text.select();
      document.execCommand("copy");
      text.closest(".analysis-panel").querySelector(".editor-hint").textContent = "Accepted and copied to the clipboard. Review it in the slide editor before applying.";
    });
    document.getElementById("reject-shortening")?.addEventListener("click", () => {
      document.getElementById("shortened-content").value = data.original;
      document.querySelector(".shortener-actions").remove();
    });
    result.scrollIntoView({behavior:"smooth",block:"start"});
  });

  bindForm("template-form", "/api/templates/recommend", data => {
    const recommendations = Array.isArray(data.recommendations) ? data.recommendations : [];
    result.innerHTML = `<div class="analysis-panel"><div class="result-header"><div><span class="eyebrow eyebrow-dark">SMART LAYOUT SELECTION</span><h2>Recommended slide layouts</h2><p>${data.ai_enhanced ? "Recommendations include AI-analyzed requirements." : "Recommendations use topic, audience, and purpose matching."}</p></div></div><div class="template-grid">${recommendations.map(item=>`
      <article class="template-card">
        <div class="template-card__body"><div class="template-card__heading"><h3>${esc(item.name)}</h3><div class="template-score"><b>${esc(item.score)}%</b><span>match</span></div></div><p>${esc(item.description)}</p><small>${esc(item.reasons.join(" · "))}</small><button type="button" class="button button-outline template-select" data-layout="${esc(item.layout)}">Use this layout</button></div>
      </article>`).join("")}</div></div>`;
    result.querySelectorAll(".template-select").forEach(button => {
      button.addEventListener("click", () => {
        selectedTemplate = button.dataset.layout;
        localStorage.setItem("slidecraft-layout", selectedTemplate);
        window.location.assign(`/create?template=${encodeURIComponent(selectedTemplate)}`);
      });
    });
    result.scrollIntoView({behavior:"smooth",block:"start"});
  });
})();
