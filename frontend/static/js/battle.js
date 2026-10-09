/**
 * CarDex Battle Arena: Interactive Garage 1v1 PvP Stat Clash
 * 
 * Features:
 * - Asynchronous Top Trumps / Pokemon-style card clash engine
 * - 5 duel stat attributes: HP, Acceleration (100/s), Top Speed, Exhaust dBA, Rarity Tier
 * - Web Audio API synthesized tire screech and twin-turbo engine roar
 * - 3-second high-octane telemetry clash animation
 * - Holographic card reveals and instant point balance sync
 */

const CarDexBattleArena = (function () {
  let garageSpots = [];
  let selectedSpotId = null;
  let selectedAttribute = "horsepower";
  let currentWager = 100;
  let isBattling = false;
  let audioCtx = null;

  const ATTRIBUTES = [
    { id: "horsepower", label: "Horsepower", icon: "⚡", unit: "HP" },
    { id: "acceleration", label: "Acceleration (100/s)", icon: "⏱️", unit: "pts" },
    { id: "top_speed", label: "Top Speed", icon: "🚀", unit: "km/h" },
    { id: "exhaust_decibels", label: "Exhaust Loudness", icon: "🔊", unit: "dBA" },
    { id: "rarity_tier", label: "Collector Rarity", icon: "💎", unit: "pts" },
  ];

  function getAudioContext() {
    if (!audioCtx) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (AudioContextClass) {
        audioCtx = new AudioContextClass();
      }
    }
    if (audioCtx && audioCtx.state === "suspended") {
      audioCtx.resume();
    }
    return audioCtx;
  }

  /**
   * Synthesize tire screech and engine roar via Web Audio oscillators
   */
  function playTireScreechAndRoar() {
    try {
      const ctx = getAudioContext();
      if (!ctx) return;
      const now = ctx.currentTime;

      playEngineRoarSound(ctx, now);
      playTireScreechSound(ctx, now);
    } catch (e) {
      console.warn("Web Audio clash sound warning:", e);
    }
  }

  function playEngineRoarSound(ctx, now) {
    if (!ctx) ctx = getAudioContext();
    if (!ctx) return;
    if (!now) now = ctx.currentTime;
    const engineOsc = ctx.createOscillator();
    const engineGain = ctx.createGain();
    const engineFilter = ctx.createBiquadFilter();

    engineOsc.type = "sawtooth";
    engineOsc.frequency.setValueAtTime(65, now);
    engineOsc.frequency.exponentialRampToValueAtTime(320, now + 1.2);
    engineOsc.frequency.exponentialRampToValueAtTime(140, now + 2.8);

    engineFilter.type = "lowpass";
    engineFilter.frequency.setValueAtTime(450, now);
    engineFilter.frequency.exponentialRampToValueAtTime(1200, now + 1.2);

    engineGain.gain.setValueAtTime(0.01, now);
    engineGain.gain.linearRampToValueAtTime(0.35, now + 0.3);
    engineGain.gain.exponentialRampToValueAtTime(0.001, now + 2.9);

    engineOsc.connect(engineFilter);
    engineFilter.connect(engineGain);
    engineGain.connect(ctx.destination);

    engineOsc.start(now);
    engineOsc.stop(now + 3.0);
  }

  function playTireScreechSound(ctx, now) {
    if (!ctx) ctx = getAudioContext();
    if (!ctx) return;
    if (!now) now = ctx.currentTime;
    const screechOsc = ctx.createOscillator();
    const screechGain = ctx.createGain();
    const screechFilter = ctx.createBiquadFilter();

    screechOsc.type = "triangle";
    screechOsc.frequency.setValueAtTime(2400, now + 0.5);
    screechOsc.frequency.linearRampToValueAtTime(1800, now + 1.8);
    screechOsc.frequency.linearRampToValueAtTime(1200, now + 2.5);

    screechFilter.type = "bandpass";
    screechFilter.frequency.setValueAtTime(2100, now + 0.5);
    screechFilter.Q.setValueAtTime(8, now + 0.5);

    screechGain.gain.setValueAtTime(0.001, now);
    screechGain.gain.setValueAtTime(0.22, now + 0.6);
    screechGain.gain.exponentialRampToValueAtTime(0.001, now + 2.4);

    screechOsc.connect(screechFilter);
    screechFilter.connect(screechGain);
    screechGain.connect(ctx.destination);

    screechOsc.start(now + 0.5);
    screechOsc.stop(now + 2.6);
  }

  function playVictoryFanfare() {
    try {
      const ctx = getAudioContext();
      if (!ctx) return;
      const notes = [523.25, 659.25, 783.99, 1046.50]; // C5, E5, G5, C6
      const start = ctx.currentTime;
      notes.forEach((freq, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, start + idx * 0.12);
        gain.gain.setValueAtTime(0.18, start + idx * 0.12);
        gain.gain.exponentialRampToValueAtTime(0.001, start + idx * 0.12 + 0.4);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(start + idx * 0.12);
        osc.stop(start + idx * 0.12 + 0.45);
      });
    } catch (e) {}
  }

  async function loadGarage() {
    try {
      const resp = await fetch("/api/battle/garage");
      if (!resp.ok) throw new Error("Failed to fetch battle garage");
      const data = await resp.json();
      garageSpots = Array.isArray(data) ? data : (data.spots || []);
      if (garageSpots.length > 0 && !selectedSpotId) {
        selectedSpotId = garageSpots[0].id;
      }
      renderGarageCarousel();
      renderPlayerCard();
    } catch (err) {
      console.warn("Battle garage load error:", err);
    }
  }

  function renderGarageCarousel() {
    const listEl = document.getElementById("battle-garage-selector");
    if (!listEl) return;

    if (garageSpots.length === 0) {
      listEl.innerHTML = `<div class="arena-empty-msg">No spotted cars in garage yet. Spot a car to enter duels!</div>`;
      return;
    }

    listEl.innerHTML = garageSpots
      .map((s) => {
        const activeClass = s.id === selectedSpotId ? "selected" : "";
        const carName = s.car_name || s.make_model || "Supercar";
        const tier = (s.rarity_tier || "rare").toUpperCase();
        const img = s.image_url || "https://images.unsplash.com/photo-1614162692292-7ac56d7f7f1e?w=400";
        return `
          <div class="battle-car-chip ${activeClass}" onclick="CarDexBattleArena.selectSpot('${s.id}')">
            <img src="${img}" alt="${carName}" class="battle-chip-thumb" />
            <div class="battle-chip-info">
              <span class="battle-chip-name">${carName}</span>
              <span class="battle-chip-tier tier-${tier.toLowerCase()}">${tier}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function selectSpot(spotId) {
    if (isBattling) return;
    selectedSpotId = spotId;
    renderGarageCarousel();
    renderPlayerCard();
  }

  function selectAttribute(attr) {
    if (isBattling) return;
    selectedAttribute = attr;
    document.querySelectorAll(".battle-attr-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.attr === attr);
    });
    renderPlayerCard();
  }

  function setWager(amt) {
    if (isBattling) return;
    currentWager = parseInt(amt, 10);
    document.querySelectorAll(".wager-chip").forEach((chip) => {
      chip.classList.toggle("active", parseInt(chip.dataset.wager, 10) === currentWager);
    });
    const potDisplay = document.getElementById("battle-pot-display");
    if (potDisplay) potDisplay.textContent = `${currentWager * 2} PTS`;
  }

  function renderPlayerCard() {
    const cardEl = document.getElementById("battle-player-card");
    if (!cardEl) return;

    const spot = garageSpots.find((s) => s.id === selectedSpotId) || garageSpots[0];
    if (!spot) return;

    const stats = spot.battle_stats || {};
    const carName = spot.car_name || spot.make_model || "Your Car";
    const tier = (spot.rarity_tier || "rare").toUpperCase();
    const img = spot.image_url || "https://images.unsplash.com/photo-1614162692292-7ac56d7f7f1e?w=800";
    const color = spot.colorway || "Standard";

    cardEl.innerHTML = `
      <div class="arena-card-inner player-holo">
        <div class="arena-card-badge">${tier}</div>
        <div class="arena-card-img-wrap">
          <img src="${img}" alt="${carName}" class="arena-card-img" />
        </div>
        <div class="arena-card-header">
          <h4 class="arena-card-title">${carName}</h4>
          <span class="arena-card-subtitle">${color}</span>
        </div>
        <div class="arena-stats-list">
          ${ATTRIBUTES.map((a) => {
            const isHighlight = a.id === selectedAttribute;
            const val = stats[a.id] !== undefined ? stats[a.id] : "---";
            return `
              <div class="arena-stat-row ${isHighlight ? "highlight-stat" : ""}">
                <span class="arena-stat-icon">${a.icon}</span>
                <span class="arena-stat-label">${a.label}</span>
                <span class="arena-stat-val">${val} <small>${a.unit}</small></span>
              </div>
            `;
          }).join("")}
        </div>
      </div>
    `;
  }

  async function initiateDuel() {
    if (isBattling) return;
    if (!selectedSpotId) {
      alert("Please select a car from your garage first.");
      return;
    }

    isBattling = true;
    const clashBtn = document.getElementById("battle-clash-btn");
    if (clashBtn) {
      clashBtn.disabled = true;
      clashBtn.classList.add("battling");
      clashBtn.innerHTML = `<span>⏳</span> <span>INITIALIZING CLASH...</span>`;
    }

    // Play synthesized tire screech and twin-turbo rev
    playTireScreechAndRoar();

    // Show 3-second animated countdown
    const arenaOverlay = document.getElementById("arena-clash-overlay");
    const countDisplay = document.getElementById("arena-countdown-display");
    if (arenaOverlay) arenaOverlay.style.display = "flex";

    let count = 3;
    if (countDisplay) countDisplay.textContent = count;

    const timer = setInterval(() => {
      count -= 1;
      if (count > 0) {
        if (countDisplay) countDisplay.textContent = count;
      } else {
        clearInterval(timer);
        if (countDisplay) countDisplay.textContent = "CLASH!";
      }
    }, 900);

    // Call API in parallel
    let result = null;
    try {
      const resp = await fetch("/api/battle/challenge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          spot_id: selectedSpotId,
          attribute: selectedAttribute,
          wager: currentWager,
        }),
      });
      result = await resp.json();
    } catch (err) {
      console.warn("Battle challenge failed:", err);
    }

    // Wait for full 3-second clash animation
    setTimeout(() => {
      if (arenaOverlay) arenaOverlay.style.display = "none";
      if (clashBtn) {
        clashBtn.disabled = false;
        clashBtn.classList.remove("battling");
        clashBtn.innerHTML = `<span>⚔️</span> <span>START BATTLE CLASH</span>`;
      }
      isBattling = false;

      if (result && !result.error) {
        displayBattleResolution(result);
      } else {
        alert(result ? result.error : "Battle challenge encountered an error.");
      }
    }, 3200);
  }

  function displayBattleResolution(res) {
    const modal = document.getElementById("battle-result-modal");
    if (!modal) return;

    if (res.winner === "player") {
      playVictoryFanfare();
    }

    const titleEl = document.getElementById("battle-result-title");
    const commentaryEl = document.getElementById("battle-result-commentary");
    const deltaEl = document.getElementById("battle-result-delta");
    const opponentCardEl = document.getElementById("battle-opponent-card");

    if (titleEl) {
      if (res.winner === "player") {
        titleEl.innerHTML = `🏆 <span style="color:#10b981;">VICTORY!</span>`;
      } else if (res.winner === "opponent") {
        titleEl.innerHTML = `💥 <span style="color:#ef4444;">DEFEAT!</span>`;
      } else {
        titleEl.innerHTML = `🤝 <span style="color:#f59e0b;">DEAD HEAT (DRAW)!</span>`;
      }
    }

    if (commentaryEl) {
      commentaryEl.textContent = res.commentary;
    }

    if (deltaEl) {
      const pts = res.points_delta;
      const prefix = pts > 0 ? "+" : "";
      const col = pts > 0 ? "#10b981" : pts < 0 ? "#ef4444" : "#f59e0b";
      deltaEl.innerHTML = `<span style="color:${col}; font-weight:900; font-size:1.4rem;">${prefix}${pts} PTS</span> (Pot: ${res.pot} PTS)`;
    }

    // Render revealed opponent card
    if (opponentCardEl && res.opponent_spot) {
      const opp = res.opponent_spot;
      const tier = (opp.rarity_tier || "rare").toUpperCase();
      const img = opp.image_url || "https://images.unsplash.com/photo-1592198084033-aade902d1aae?w=800";
      opponentCardEl.innerHTML = `
        <div class="arena-card-inner rival-holo">
          <div class="arena-card-badge">${tier}</div>
          <div class="arena-card-img-wrap">
            <img src="${img}" alt="${opp.car_name}" class="arena-card-img" />
          </div>
          <div class="arena-card-header">
            <h4 class="arena-card-title">${opp.car_name}</h4>
            <span class="arena-card-subtitle">Pilot: ${opp.username || "Rival Spotter"}</span>
          </div>
          <div class="arena-duel-stat-clash">
            <div class="clash-stat-item">
              <span>Your ${res.attribute_label}</span>
              <strong style="color:${res.player_power >= res.opponent_power ? "#10b981" : "#fff"};">${res.player_power}</strong>
            </div>
            <div class="clash-vs-badge">VS</div>
            <div class="clash-stat-item">
              <span>Rival ${res.attribute_label}</span>
              <strong style="color:${res.opponent_power >= res.player_power ? "#10b981" : "#fff"};">${res.opponent_power}</strong>
            </div>
          </div>
        </div>
      `;
    }

    modal.classList.add("active");

    // Refresh quota/profile points display if function exists
    if (typeof fetchUserQuota === "function") {
      fetchUserQuota();
    }
  }

  function closeResultModal() {
    const modal = document.getElementById("battle-result-modal");
    if (modal) modal.classList.remove("active");
  }

  return {
    init: function () {
      loadGarage();
    },
    selectSpot,
    selectAttribute,
    setWager,
    initiateDuel,
    closeResultModal,
    playEngineRoarSound,
    playTireScreechSound,
    playVictoryFanfare,
  };
})();

window.CarDexBattleArena = CarDexBattleArena;
