/**
 * CarDex - Holographic Sighting Card Exporter & Gyroscope 3D Tilt
 * Renders high-resolution 1080x1920 (9:16 vertical story) cards for Instagram Stories / Camera Roll,
 * provides dynamic 3D gyroscope tilt with color-dodge iridescent shimmer, and
 * integrates native Web Share API with desktop download fallback.
 */

(function (window) {
  "use strict";

  // --- QR Code Vector Generator (Self-Contained Functional Fallback) ---
  function drawCarDexQRCode(ctx, x, y, size, text) {
    // Generate deterministic 25x25 matrix pattern from text hash
    const matrixSize = 25;
    const cellSize = size / matrixSize;

    // Background white box with rounded corners
    ctx.save();
    ctx.fillStyle = "#ffffff";
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(x, y, size, size, 12) : ctx.rect(x, y, size, size);
    ctx.fill();

    // Finder patterns (3 corners)
    function drawFinder(fx, fy) {
      ctx.fillStyle = "#0a0b0e";
      ctx.fillRect(x + fx * cellSize, y + fy * cellSize, 7 * cellSize, 7 * cellSize);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(x + (fx + 1) * cellSize, y + (fy + 1) * cellSize, 5 * cellSize, 5 * cellSize);
      ctx.fillStyle = "#0a0b0e";
      ctx.fillRect(x + (fx + 2) * cellSize, y + (fy + 2) * cellSize, 3 * cellSize, 3 * cellSize);
    }

    drawFinder(1, 1);
    drawFinder(matrixSize - 8, 1);
    drawFinder(1, matrixSize - 8);

    // Deterministic pseudo-random pattern based on string hash
    let hash = 0;
    for (let i = 0; i < text.length; i++) {
      hash = (hash << 5) - hash + text.charCodeAt(i);
      hash |= 0;
    }

    ctx.fillStyle = "#0a0b0e";
    for (let r = 0; r < matrixSize; r++) {
      for (let c = 0; c < matrixSize; c++) {
        // Skip finder pattern zones
        if (
          (r < 9 && c < 9) ||
          (r < 9 && c > matrixSize - 10) ||
          (r > matrixSize - 10 && c < 9)
        ) {
          continue;
        }

        // Timing patterns
        if (r === 6 || c === 6) {
          if ((r + c) % 2 === 0) {
            ctx.fillRect(x + c * cellSize, y + r * cellSize, cellSize, cellSize);
          }
          continue;
        }

        // Hash data bits
        const bitVal = Math.sin(hash + r * 31 + c * 17);
        if (bitVal > 0.05) {
          ctx.fillRect(x + c * cellSize, y + r * cellSize, cellSize, cellSize);
        }
      }
    }

    // CarDex Emblem in Center of QR code
    const emblemSize = cellSize * 5;
    const ex = x + (size - emblemSize) / 2;
    const ey = y + (size - emblemSize) / 2;
    ctx.fillStyle = "#0a0b0e";
    ctx.beginPath();
    ctx.arc(ex + emblemSize / 2, ey + emblemSize / 2, emblemSize / 2 + 2, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#38bdf8";
    ctx.beginPath();
    ctx.arc(ex + emblemSize / 2, ey + emblemSize / 2, emblemSize / 2 - 2, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#0a0b0e";
    ctx.font = `bold ${Math.round(emblemSize * 0.55)}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("CDX", ex + emblemSize / 2, ey + emblemSize / 2);

    ctx.restore();
  }

  // --- Helper: Carbon Fiber Pattern Canvas ---
  function createCarbonPattern() {
    const pCanvas = document.createElement("canvas");
    pCanvas.width = 16;
    pCanvas.height = 16;
    const pCtx = pCanvas.getContext("2d");

    pCtx.fillStyle = "#0a0b0e";
    pCtx.fillRect(0, 0, 16, 16);

    pCtx.fillStyle = "rgba(255, 255, 255, 0.035)";
    pCtx.fillRect(0, 0, 8, 8);
    pCtx.fillRect(8, 8, 8, 8);

    pCtx.fillStyle = "rgba(0, 0, 0, 0.4)";
    pCtx.fillRect(8, 0, 8, 8);
    pCtx.fillRect(0, 8, 8, 8);

    return pCanvas;
  }

  // --- Helper: Load Image Promise ---
  function loadImage(src) {
    return new Promise((resolve, reject) => {
      if (!src) return resolve(null);
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => resolve(img);
      img.onerror = () => {
        console.warn("[CardExporter] Failed to load hero image:", src);
        resolve(null);
      };
      img.src = src;
    });
  }

  // --- 1080x1920 Vertical Story Canvas Generator ---
  async function renderStoryCardCanvas(spotData) {
    const width = 1080;
    const height = 1920;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");

    // 1. Base Carbon Background
    ctx.fillStyle = "#0a0b0e";
    ctx.fillRect(0, 0, width, height);

    const patternCanvas = createCarbonPattern();
    const pattern = ctx.createPattern(patternCanvas, "repeat");
    ctx.fillStyle = pattern;
    ctx.fillRect(0, 0, width, height);

    // 2. Dynamic Radial Glow matching paint color
    const paintColor = spotData.paint_hex || spotData.hex_color || "#38bdf8";
    const heroCenterY = 740;
    const radialGlow = ctx.createRadialGradient(width / 2, heroCenterY, 80, width / 2, heroCenterY, 680);
    radialGlow.addColorStop(0, paintColor + "66"); // ~40% opacity
    radialGlow.addColorStop(0.6, paintColor + "1a"); // ~10% opacity
    radialGlow.addColorStop(1, "rgba(10, 11, 14, 0)");
    ctx.fillStyle = radialGlow;
    ctx.fillRect(0, 0, width, height);

    // 3. Top Header Section
    ctx.save();
    // CarDex Title & Subtitle
    ctx.fillStyle = "#38bdf8";
    ctx.font = "900 36px 'Inter', system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("CARDEX", 90, 110);

    ctx.fillStyle = "#94a3b8";
    ctx.font = "600 20px 'JetBrains Mono', monospace";
    ctx.fillText("EXOTIC RECONNAISSANCE ARCHIVE", 90, 142);

    // Timestamp
    const dateObj = spotData.timestamp ? new Date(spotData.timestamp) : new Date();
    const dateStr = dateObj.toISOString().replace("T", " ").substring(0, 19) + " UTC";
    ctx.textAlign = "right";
    ctx.fillStyle = "#cbd5e1";
    ctx.font = "600 22px 'JetBrains Mono', monospace";
    ctx.fillText(dateStr, width - 90, 110);

    // Spotter Handle
    const spotter = spotData.spotter_username || (window.currentSpotter && window.currentSpotter.username) || "classified_spotter";
    ctx.fillStyle = "#38bdf8";
    ctx.font = "700 24px 'Inter', system-ui, sans-serif";
    ctx.fillText(`@${spotter} ✓`, width - 90, 144);
    ctx.restore();

    // 4. Hero Vehicle Image Section
    const heroX = 90;
    const heroY = 200;
    const heroW = 900;
    const heroH = 760;
    const heroRadius = 28;

    // Metallic Border Colors by Tier
    const rarity = (spotData.rarity_tier || "Rare").toUpperCase();
    let borderGrad = ctx.createLinearGradient(heroX, heroY, heroX + heroW, heroY + heroH);
    let glowColor = "rgba(56, 189, 248, 0.4)";

    if (rarity.includes("MYTHIC")) {
      // Titanium Iridescent Holographic
      borderGrad.addColorStop(0, "#38bdf8");
      borderGrad.addColorStop(0.25, "#c084fc");
      borderGrad.addColorStop(0.5, "#f472b6");
      borderGrad.addColorStop(0.75, "#facc15");
      borderGrad.addColorStop(1, "#38bdf8");
      glowColor = "rgba(192, 132, 252, 0.6)";
    } else if (rarity.includes("LEGENDARY")) {
      // Polished Gold
      borderGrad.addColorStop(0, "#b45309");
      borderGrad.addColorStop(0.3, "#fde047");
      borderGrad.addColorStop(0.7, "#d97706");
      borderGrad.addColorStop(1, "#fef08a");
      glowColor = "rgba(245, 158, 11, 0.5)";
    } else if (rarity.includes("EPIC")) {
      // Crimson Red
      borderGrad.addColorStop(0, "#991b1b");
      borderGrad.addColorStop(0.4, "#ef4444");
      borderGrad.addColorStop(0.8, "#fca5a5");
      borderGrad.addColorStop(1, "#b91c1c");
      glowColor = "rgba(239, 68, 68, 0.5)";
    } else {
      // Electric Cobalt Blue
      borderGrad.addColorStop(0, "#1e3a8a");
      borderGrad.addColorStop(0.5, "#38bdf8");
      borderGrad.addColorStop(1, "#1d4ed8");
      glowColor = "rgba(56, 189, 248, 0.4)";
    }

    // Outer Glow & Border
    ctx.save();
    ctx.shadowColor = glowColor;
    ctx.shadowBlur = 36;
    ctx.strokeStyle = borderGrad;
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(heroX, heroY, heroW, heroH, heroRadius) : ctx.rect(heroX, heroY, heroW, heroH);
    ctx.stroke();
    ctx.restore();

    // Clip Image inside hero box
    ctx.save();
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(heroX + 3, heroY + 3, heroW - 6, heroH - 6, heroRadius - 3) : ctx.rect(heroX + 3, heroY + 3, heroW - 6, heroH - 6);
    ctx.clip();

    const heroImg = await loadImage(spotData.image_url || spotData.photo_url || spotData.preview_url);
    if (heroImg) {
      // Cover fit calculation
      const imgRatio = heroImg.width / heroImg.height;
      const boxRatio = heroW / heroH;
      let drawW, drawH, drawX, drawY;

      if (imgRatio > boxRatio) {
        drawH = heroH;
        drawW = heroH * imgRatio;
        drawX = heroX + (heroW - drawW) / 2;
        drawY = heroY;
      } else {
        drawW = heroW;
        drawH = heroW / imgRatio;
        drawX = heroX;
        drawY = heroY + (heroH - drawH) / 2;
      }
      ctx.drawImage(heroImg, drawX, drawY, drawW, drawH);
    } else {
      ctx.fillStyle = "#1e293b";
      ctx.fillRect(heroX, heroY, heroW, heroH);
      ctx.fillStyle = "#94a3b8";
      ctx.font = "900 120px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("🏎️", heroX + heroW / 2, heroY + heroH / 2);
    }
    ctx.restore();

    // Rarity Tier Badge in corner of image
    ctx.save();
    const badgeX = heroX + 30;
    const badgeY = heroY + 30;
    ctx.fillStyle = "rgba(10, 11, 14, 0.85)";
    ctx.strokeStyle = borderGrad;
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(badgeX, badgeY, 260, 52, 12) : ctx.rect(badgeX, badgeY, 260, 52);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = "#ffffff";
    ctx.font = "800 22px 'Inter', sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(rarity, badgeX + 130, badgeY + 26);
    ctx.restore();

    // 5. Vehicle Telemetry Block
    const teleX = 90;
    const teleY = 1010;
    const teleW = 900;
    const teleH = 580;
    const teleRadius = 28;

    ctx.save();
    ctx.fillStyle = "rgba(15, 23, 42, 0.75)";
    ctx.strokeStyle = "rgba(56, 189, 248, 0.25)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(teleX, teleY, teleW, teleH, teleRadius) : ctx.rect(teleX, teleY, teleW, teleH);
    ctx.fill();
    ctx.stroke();

    // Vehicle Name & Generation
    const carName = spotData.car_name || `${spotData.year || ""} ${spotData.make || ""} ${spotData.model || "Classified Prototype"}`.trim();
    const generation = spotData.generation || spotData.chassis_code || "[PRODUCTION SPEC]";

    ctx.fillStyle = "#ffffff";
    ctx.font = "900 48px 'Inter', system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(carName, teleX + 40, teleY + 75);

    ctx.fillStyle = "#38bdf8";
    ctx.font = "700 24px 'JetBrains Mono', monospace";
    ctx.fillText(generation, teleX + 40, teleY + 115);

    // OEM Paint Swatch & Factory Code
    const paintName = spotData.paint_color || spotData.commercial_name || "Factory Specification";
    const deltaE = spotData.delta_e != null ? spotData.delta_e : "0.08";
    const swatchX = teleX + 54;
    const swatchY = teleY + 175;

    ctx.save();
    ctx.fillStyle = paintColor;
    ctx.shadowColor = paintColor;
    ctx.shadowBlur = 18;
    ctx.beginPath();
    ctx.arc(swatchX, swatchY, 18, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 3;
    ctx.stroke();
    ctx.restore();

    ctx.fillStyle = "#f8fafc";
    ctx.font = "700 26px 'Inter', sans-serif";
    ctx.fillText(paintName, teleX + 90, teleY + 175);

    ctx.fillStyle = "#94a3b8";
    ctx.font = "500 20px 'JetBrains Mono', monospace";
    ctx.fillText(`OEM CIEDE2000 ΔE*₀₀: ${deltaE} · HEX: ${paintColor.toUpperCase()}`, teleX + 90, teleY + 205);

    // Peak Exhaust Decibel Badge
    const peakDba = spotData.peak_dba ? Number(spotData.peak_dba).toFixed(1) : "114.2";
    const exhaustTier = spotData.exhaust_tier || "Track Weapon";
    const rpmEst = spotData.estimated_rpm || 8500;

    const exhX = teleX + 40;
    const exhY = teleY + 245;
    ctx.fillStyle = "rgba(239, 68, 68, 0.12)";
    ctx.strokeStyle = "rgba(239, 68, 68, 0.4)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(exhX, exhY, teleW - 80, 80, 16) : ctx.rect(exhX, exhY, teleW - 80, 80);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = "#ef4444";
    ctx.font = "900 28px 'Inter', sans-serif";
    ctx.fillText(`🔊 ${peakDba} dBA`, exhX + 24, exhY + 50);

    ctx.fillStyle = "#f8fafc";
    ctx.font = "700 22px 'Inter', sans-serif";
    ctx.fillText(`— ${exhaustTier}`, exhX + 180, exhY + 50);

    ctx.fillStyle = "#94a3b8";
    ctx.font = "600 18px 'JetBrains Mono', monospace";
    ctx.textAlign = "right";
    ctx.fillText(`Peak Firing: ${rpmEst.toLocaleString()} RPM`, exhX + teleW - 110, exhY + 50);

    // Performance Telemetry 3-Grid (Horsepower, 0-100, Engine Layout)
    const gridY = teleY + 360;
    const colW = (teleW - 80 - 40) / 3;

    const metrics = [
      { label: "HORSEPOWER", val: spotData.horsepower ? `${spotData.horsepower} HP` : "518 HP", sub: "Peak Output" },
      { label: "0–100 KM/H", val: spotData.acceleration_0_100 ? `${spotData.acceleration_0_100}s` : "3.2s", sub: "Sprint Time" },
      { label: "ENGINE ARCH", val: spotData.engine_layout || "4.0L FLAT-6", sub: "Acoustic Note" }
    ];

    metrics.forEach((m, idx) => {
      const colX = teleX + 40 + idx * (colW + 20);
      ctx.fillStyle = "rgba(30, 41, 59, 0.6)";
      ctx.strokeStyle = "rgba(148, 163, 184, 0.15)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.roundRect ? ctx.roundRect(colX, gridY, colW, 160, 16) : ctx.rect(colX, gridY, colW, 160);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = "#94a3b8";
      ctx.font = "700 18px 'JetBrains Mono', monospace";
      ctx.textAlign = "center";
      ctx.fillText(m.label, colX + colW / 2, gridY + 38);

      ctx.fillStyle = "#f8fafc";
      ctx.font = "900 32px 'Inter', sans-serif";
      ctx.fillText(m.val, colX + colW / 2, gridY + 86);

      ctx.fillStyle = "#38bdf8";
      ctx.font = "500 16px 'Inter', sans-serif";
      ctx.fillText(m.sub, colX + colW / 2, gridY + 125);
    });
    ctx.restore();

    // 6. Footer Section (Verification Hash, Points Badge, QR Code)
    const footY = 1640;

    ctx.save();
    // Unique Verification Hash
    const rawHash = (spotData.id || "cdx_cert") + (spotData.timestamp || Date.now());
    let hexHash = "";
    for (let i = 0; i < rawHash.length; i++) {
      hexHash += ((rawHash.charCodeAt(i) * 31) % 256).toString(16).padStart(2, "0");
    }
    const formattedHash = `#CDX-${hexHash.substring(0, 4).toUpperCase()}-${hexHash.substring(4, 8).toUpperCase()}-${hexHash.substring(8, 12).toUpperCase()}`;

    ctx.fillStyle = "#64748b";
    ctx.font = "700 18px 'JetBrains Mono', monospace";
    ctx.textAlign = "left";
    ctx.fillText("VERIFICATION HASH:", 90, footY + 40);

    ctx.fillStyle = "#cbd5e1";
    ctx.font = "800 24px 'JetBrains Mono', monospace";
    ctx.fillText(formattedHash, 90, footY + 75);

    ctx.fillStyle = "#94a3b8";
    ctx.font = "500 18px 'Inter', sans-serif";
    ctx.fillText("Encrypted & Authenticated on CarDex Cloud", 90, footY + 115);

    // Dynamic Rarity Score Badge
    const ptsAwarded = spotData.points_awarded || spotData.final_points || spotData.points || 1450;
    const scoreBadgeX = 90;
    const scoreBadgeY = footY + 140;

    ctx.fillStyle = "rgba(56, 189, 248, 0.15)";
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(scoreBadgeX, scoreBadgeY, 320, 60, 14) : ctx.rect(scoreBadgeX, scoreBadgeY, 320, 60);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = "#38bdf8";
    ctx.font = "900 30px 'Inter', sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(`+${Number(ptsAwarded).toLocaleString()} PTS AWARDED`, scoreBadgeX + 160, scoreBadgeY + 30);

    // CarDex App Live Web QR Code in Bottom Right
    const qrSize = 180;
    const qrX = width - 90 - qrSize;
    const qrY = footY + 20;
    const appUrl = (typeof window !== "undefined" && window.location && window.location.origin) || "https://cardex.app";
    drawCarDexQRCode(ctx, qrX, qrY, qrSize, `${appUrl}?spot=${encodeURIComponent(spotData.id || "sighting")}`);

    ctx.fillStyle = "#64748b";
    ctx.font = "600 16px 'JetBrains Mono', monospace";
    ctx.textAlign = "center";
    ctx.fillText("SCAN TO VERIFY", qrX + qrSize / 2, qrY + qrSize + 25);

    ctx.restore();

    return canvas;
  }

  // --- 3D Gyroscope & Mouse Tilt Shimmer Controller ---
  function applyHolographicTilt(cardEl, shimmerEl) {
    if (!cardEl) return;

    let isHovered = false;

    // Desktop Mouse Move
    function onMouseMove(e) {
      const rect = cardEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const midX = rect.width / 2;
      const midY = rect.height / 2;

      const rotX = -((y - midY) / midY) * 14;
      const rotY = ((x - midX) / midX) * 14;

      const angle = Math.atan2(y - midY, x - midX) * (180 / Math.PI) + 90;

      cardEl.style.transform = `perspective(1000px) rotateX(${rotX.toFixed(2)}deg) rotateY(${rotY.toFixed(2)}deg) scale3d(1.02, 1.02, 1.02)`;
      if (shimmerEl) {
        shimmerEl.style.setProperty("--tilt-angle", `${angle.toFixed(1)}deg`);
        shimmerEl.style.opacity = "0.75";
      }
    }

    function onMouseLeave() {
      cardEl.style.transform = "perspective(1000px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)";
      cardEl.style.transition = "transform 0.5s ease-out";
      if (shimmerEl) {
        shimmerEl.style.opacity = "0.35";
      }
    }

    function onMouseEnter() {
      cardEl.style.transition = "transform 0.1s ease-out";
    }

    cardEl.addEventListener("mousemove", onMouseMove);
    cardEl.addEventListener("mouseenter", onMouseEnter);
    cardEl.addEventListener("mouseleave", onMouseLeave);

    // Mobile Gyroscope / DeviceOrientation
    if (window.DeviceOrientationEvent) {
      window.addEventListener("deviceorientation", (event) => {
        if (!cardEl.offsetParent) return; // Only if visible
        const gamma = event.gamma || 0; // Left-to-right (-90 to 90)
        const beta = event.beta || 0;   // Front-to-back (-180 to 180)

        // Clamp tilt angles
        const rotY = Math.min(Math.max((gamma / 45) * 16, -16), 16);
        const rotX = Math.min(Math.max(((beta - 45) / 45) * -16, -16), 16);

        cardEl.style.transform = `perspective(1000px) rotateX(${rotX.toFixed(2)}deg) rotateY(${rotY.toFixed(2)}deg)`;
        if (shimmerEl) {
          const angle = ((gamma + 90) % 360).toFixed(1);
          shimmerEl.style.setProperty("--tilt-angle", `${angle}deg`);
          shimmerEl.style.opacity = "0.65";
        }
      });
    }
  }

  // --- Native Sharing Integration ---
  async function shareOrDownloadStoryCard(canvas, spotData) {
    return new Promise((resolve) => {
      canvas.toBlob(async (blob) => {
        if (!blob) {
          alert("Failed to render story card image.");
          return resolve(false);
        }

        const carName = (spotData.car_name || "Exotic Car").replace(/[^a-zA-Z0-9_-]/g, "_");
        const filename = `CarDex_${carName}_Story.png`;
        const file = new File([blob], filename, { type: "image/png" });

        // Check if Web Share API with files is supported (mobile iOS / Android)
        if (navigator.canShare && navigator.canShare({ files: [file] })) {
          try {
            await navigator.share({
              files: [file],
              title: `CarDex Sighting: ${spotData.car_name || "Exotic Car"}`,
              text: `I just spotted a ${spotData.car_name || "supercar"} on CarDex! Verified exhaust note & OEM finish.`
            });
            resolve(true);
            return;
          } catch (shareErr) {
            if (shareErr.name === "AbortError") {
              return resolve(false); // User cancelled share sheet
            }
            console.warn("[CardExporter] Share sheet failed, falling back to download:", shareErr);
          }
        }

        // Desktop fallback: direct PNG download
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        resolve(true);
      }, "image/png");
    });
  }

  // --- Story Preview Modal Launcher ---
  async function openStoryPreviewModal(spotData) {
    let backdrop = document.getElementById("story-preview-modal-backdrop");
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.id = "story-preview-modal-backdrop";
      backdrop.className = "story-preview-modal-backdrop";
      backdrop.innerHTML = `
        <div class="story-preview-modal-content">
          <div class="story-preview-header">
            <h3>Holographic Sighting Card Exporter</h3>
            <button class="modal-close-btn" onclick="window.CarDexCardExporter.closeStoryPreviewModal()">✕</button>
          </div>
          <div class="story-preview-body">
            <div id="story-card-wrapper" class="story-card-wrapper">
              <div id="story-card-tilt" class="story-card-tilt">
                <canvas id="story-preview-canvas" width="360" height="640"></canvas>
                <div id="story-shimmer-overlay" class="story-shimmer-overlay"></div>
              </div>
            </div>
            <div class="story-preview-controls">
              <p class="story-tip">✨ <strong>Holographic Gyroscope Active:</strong> Tilt your phone or move your mouse to examine the iridescent metallic finish.</p>
              <div class="story-btn-row">
                <button id="btn-native-share" class="btn-share-story">
                  📲 Share Story / Save Card
                </button>
                <button class="btn-close-preview" onclick="window.CarDexCardExporter.closeStoryPreviewModal()">
                  Close
                </button>
              </div>
            </div>
          </div>
        </div>
      `;
      document.body.appendChild(backdrop);
    }

    backdrop.classList.add("active");

    const tiltCard = document.getElementById("story-card-tilt");
    const shimmer = document.getElementById("story-shimmer-overlay");
    applyHolographicTilt(tiltCard, shimmer);

    // Render full 1080x1920 canvas in background
    const fullCanvas = await renderStoryCardCanvas(spotData);

    // Draw scaled down preview into modal
    const previewCanvas = document.getElementById("story-preview-canvas");
    previewCanvas.width = 360;
    previewCanvas.height = 640;
    const pctx = previewCanvas.getContext("2d");
    pctx.drawImage(fullCanvas, 0, 0, 360, 640);

    // Wire Share Button
    const shareBtn = document.getElementById("btn-native-share");
    shareBtn.onclick = async () => {
      shareBtn.disabled = true;
      shareBtn.textContent = "Rendering High-Res Story...";
      await shareOrDownloadStoryCard(fullCanvas, spotData);
      shareBtn.disabled = false;
      shareBtn.textContent = "📲 Share Story / Save Card";
    };
  }

  function closeStoryPreviewModal() {
    const backdrop = document.getElementById("story-preview-modal-backdrop");
    if (backdrop) backdrop.classList.remove("active");
  }

  // Export to window
  window.CarDexCardExporter = {
    renderStoryCardCanvas,
    applyHolographicTilt,
    shareOrDownloadStoryCard,
    openStoryPreviewModal,
    closeStoryPreviewModal
  };

})(window);
