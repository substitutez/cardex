/**
 * CarDex - Offline Field Mode & Background Synchronization Queue
 * Implements an IndexedDB-backed offline stash (cardex_offline_db / pending_spots),
 * network auto-detection (online/offline), 10s upload timeout fallback, and
 * sequential background sync & reconciliation with quota management.
 */

(function (window) {
  "use strict";

  const DB_NAME = "cardex_offline_db";
  const DB_VERSION = 1;
  const STORE_NAME = "pending_spots";

  let dbInstance = null;
  let isSyncing = false;

  // --- IndexedDB Infrastructure ---

  function openDB() {
    if (dbInstance) return Promise.resolve(dbInstance);

    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);

      request.onupgradeneeded = (event) => {
        const db = event.target.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          const store = db.createObjectStore(STORE_NAME, { keyPath: "id" });
          store.createIndex("timestamp", "timestamp", { unique: false });
          store.createIndex("status", "status", { unique: false });
        }
      };

      request.onsuccess = (event) => {
        dbInstance = event.target.result;
        resolve(dbInstance);
      };

      request.onerror = (event) => {
        console.error("[OfflineSync] Failed to open IndexedDB:", event.target.error);
        reject(event.target.error);
      };
    });
  }

  function generateUUID() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    return "spot_" + Date.now() + "_" + Math.random().toString(36).substring(2, 9);
  }

  async function savePendingSpot({ imageBlob, audioBlob, coordinates, message, previewUrl }) {
    const db = await openDB();
    const id = generateUUID();
    const record = {
      id,
      timestamp: Date.now(),
      image_blob: imageBlob,
      audio_blob: audioBlob || null,
      coordinates: coordinates || null,
      message: message || "Exotic field sighting",
      status: "pending",
      preview_url: previewUrl || (imageBlob ? URL.createObjectURL(imageBlob) : null)
    };

    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      const store = tx.objectStore(STORE_NAME);
      const req = store.add(record);

      req.onsuccess = () => {
        updateNetworkHUD();
        resolve(record);
      };
      req.onerror = (e) => reject(e.target.error);
    });
  }

  async function getAllPendingSpots() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const store = tx.objectStore(STORE_NAME);
      const req = store.getAll();

      req.onsuccess = () => {
        const spots = req.result || [];
        spots.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
        resolve(spots);
      };
      req.onerror = (e) => reject(e.target.error);
    });
  }

  async function getPendingCount() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const store = tx.objectStore(STORE_NAME);
      const req = store.count();
      req.onsuccess = () => resolve(req.result || 0);
      req.onerror = () => resolve(0);
    });
  }

  async function updateSpotStatus(id, status) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      const store = tx.objectStore(STORE_NAME);
      const getReq = store.get(id);

      getReq.onsuccess = () => {
        const item = getReq.result;
        if (!item) return resolve(null);
        item.status = status;
        const putReq = store.put(item);
        putReq.onsuccess = () => resolve(item);
        putReq.onerror = (e) => reject(e.target.error);
      };
      getReq.onerror = (e) => reject(e.target.error);
    });
  }

  async function deletePendingSpot(id) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      const store = tx.objectStore(STORE_NAME);
      const req = store.delete(id);
      req.onsuccess = () => {
        updateNetworkHUD();
        resolve(true);
      };
      req.onerror = (e) => reject(e.target.error);
    });
  }

  // --- Utility: Blob to Base64 ---

  function blobToDataURL(blob) {
    return new Promise((resolve, reject) => {
      if (!blob) return resolve(null);
      if (typeof blob === "string" && blob.startsWith("data:")) return resolve(blob);
      const reader = new FileReader();
      reader.onloadend = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  }

  function dataURLToBlob(dataURL) {
    if (!dataURL) return null;
    if (dataURL instanceof Blob) return dataURL;
    const parts = dataURL.split(",");
    const mimeMatch = parts[0].match(/:(.*?);/);
    const mime = mimeMatch ? mimeMatch[1] : "image/jpeg";
    const binary = atob(parts[1]);
    const array = [];
    for (let i = 0; i < binary.length; i++) {
      array.push(binary.charCodeAt(i));
    }
    return new Blob([new Uint8Array(array)], { type: mime });
  }

  // --- Cockpit HUD Notifications & Status Banner ---

  function showCockpitNotification(message, type = "info") {
    let container = document.getElementById("hud-notification-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "hud-notification-container";
      container.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        z-index: 10000;
        display: flex;
        flex-direction: column;
        gap: 10px;
        pointer-events: none;
      `;
      document.body.appendChild(container);
    }

    const toast = document.createElement("div");
    toast.className = `hud-toast hud-toast-${type}`;
    const bg = type === "warning" ? "rgba(245, 158, 11, 0.95)" :
               type === "error" ? "rgba(239, 68, 68, 0.95)" :
               type === "success" ? "rgba(16, 185, 129, 0.95)" :
               "rgba(15, 23, 42, 0.95)";
    const border = type === "warning" ? "#fbbf24" :
                   type === "error" ? "#f87171" :
                   type === "success" ? "#34d399" :
                   "#38bdf8";

    toast.style.cssText = `
      background: ${bg};
      color: #ffffff;
      border: 1px solid ${border};
      border-radius: 8px;
      padding: 12px 18px;
      font-size: 0.88rem;
      font-weight: 600;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
      backdrop-filter: blur(8px);
      display: flex;
      align-items: center;
      gap: 10px;
      animation: hudToastIn 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards;
      pointer-events: auto;
      max-width: 380px;
    `;

    const icon = type === "warning" ? "📡" :
                 type === "error" ? "⚠️" :
                 type === "success" ? "🏁" : "🏎️";

    toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;
    container.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = "0";
      toast.style.transform = "translateX(40px)";
      toast.style.transition = "all 0.3s ease";
      setTimeout(() => toast.remove(), 320);
    }, 4500);
  }

  async function updateNetworkHUD() {
    let banner = document.getElementById("offline-status-banner");
    const count = await getPendingCount();
    const isOnline = navigator.onLine;

    if (!isOnline || count > 0) {
      if (!banner) {
        banner = document.createElement("div");
        banner.id = "offline-status-banner";
        banner.className = "offline-status-banner";
        const header = document.querySelector(".header") || document.body;
        header.parentNode.insertBefore(banner, header.nextSibling);
      }

      if (!isOnline) {
        banner.className = "offline-status-banner offline";
        banner.innerHTML = `
          <div class="banner-content">
            <span class="banner-pulse red"></span>
            <strong>FIELD MODE (OFFLINE)</strong>
            <span>— Zero Signal. Sightings stored in local encrypted stash (${count} pending sync).</span>
          </div>
        `;
      } else {
        banner.className = "offline-status-banner syncing";
        banner.innerHTML = `
          <div class="banner-content">
            <span class="banner-pulse amber"></span>
            <strong>NETWORK RESTORED</strong>
            <span>— ${count} offline sighting(s) ready to sync. <button onclick="window.CarDexOfflineSync.syncPendingSpots()" class="banner-sync-btn">Sync Stash Now</button></span>
          </div>
        `;
      }
      banner.style.display = "block";
    } else {
      if (banner) {
        banner.style.display = "none";
      }
    }
  }

  // --- Offline Placeholder Card Renderer ---

  function renderOfflinePendingCard(containerEl, spotRecord) {
    const card = document.createElement("div");
    card.id = `pending-card-${spotRecord.id}`;
    card.className = "offline-pending-card";
    const dateStr = new Date(spotRecord.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    let imgHtml = "";
    if (spotRecord.preview_url) {
      imgHtml = `<img src="${spotRecord.preview_url}" class="pending-card-thumb" alt="Pending Sighting" />`;
    } else {
      imgHtml = `<div class="pending-card-placeholder">🏎️</div>`;
    }

    card.innerHTML = `
      <div class="pending-card-header">
        <span class="sync-pending-badge">
          <span class="sync-pulse-dot"></span> SYNC PENDING
        </span>
        <span class="pending-time">${dateStr}</span>
      </div>
      <div class="pending-card-body">
        ${imgHtml}
        <div class="pending-card-info">
          <h4 class="pending-title">Offline Field Sighting</h4>
          <p class="pending-desc">Vehicle captured without carrier signal. Stored in local flash memory.</p>
          <div class="pending-meta">
            ${spotRecord.coordinates ? `<span>📍 GPS Locked</span>` : `<span>📍 Local Cell</span>`}
            ${spotRecord.audio_blob ? `<span>🔊 Exhaust Telemetry Attached</span>` : ``}
          </div>
        </div>
      </div>
      <div class="pending-card-footer">
        <span class="pending-hash">STASH ID: #${spotRecord.id.slice(-8).toUpperCase()}</span>
        <button class="btn-retry-sync" onclick="window.CarDexOfflineSync.syncPendingSpots()">
          Sync Now
        </button>
      </div>
    `;

    if (containerEl) {
      containerEl.appendChild(card);
      containerEl.scrollTop = containerEl.scrollHeight;
    }
    return card;
  }

  // --- Background Synchronization Queue ---

  async function syncPendingSpots() {
    if (isSyncing) return;
    if (!navigator.onLine) {
      showCockpitNotification("Cannot sync: device is currently offline.", "warning");
      return;
    }

    const pendingSpots = await getAllPendingSpots();
    if (pendingSpots.length === 0) return;

    isSyncing = true;
    showCockpitNotification(`Reconnecting: Syncing ${pendingSpots.length} pending sighting(s) to CarDex Cloud...`, "info");
    updateNetworkHUD();

    try {
      for (const spot of pendingSpots) {
        await updateSpotStatus(spot.id, "uploading");

        // Convert blobs to DataURLs for backend JSON API
        const imageDataUrl = spot.image_blob ? await blobToDataURL(spot.image_blob) : null;
        const audioDataUrl = spot.audio_blob ? await blobToDataURL(spot.audio_blob) : null;

        const currentSpotter = window.currentSpotter || null;
        const currentToken = window.currentToken || null;

        const payload = {
          message: spot.message || "Exotic car sighting (Offline Stash)",
          image: imageDataUrl,
          audio: audioDataUrl,
          user_id: currentSpotter ? currentSpotter.user_id : "spotter_1",
          username: currentSpotter ? currentSpotter.username : "Guest",
          offline_synced_id: spot.id
        };

        if (spot.coordinates) {
          payload.latitude = spot.coordinates.lat;
          payload.longitude = spot.coordinates.lng;
        } else if (window.currentGpsLocation) {
          payload.latitude = window.currentGpsLocation.latitude;
          payload.longitude = window.currentGpsLocation.longitude;
        }

        const headers = { "Content-Type": "application/json" };
        if (currentToken) {
          headers["Authorization"] = `Bearer ${currentToken}`;
        }

        try {
          const res = await fetch("/api/spot", {
            method: "POST",
            headers,
            body: JSON.stringify(payload)
          });

          const raw = await res.text();
          let data;
          try {
            data = JSON.parse(raw);
          } catch {
            throw new Error(`Non-JSON response (HTTP ${res.status})`);
          }

          // Check if quota limit reached during batch sync
          if (
            res.status === 429 ||
            data.quota_exceeded ||
            data.error === "QUOTA_EXHAUSTED" ||
            (typeof data.status === "string" && data.status.includes("QUOTA_EXHAUSTED")) ||
            data.code === "QUOTA_EXHAUSTED"
          ) {
            await updateSpotStatus(spot.id, "pending");
            showCockpitNotification("Daily Spotting Quota Reached — Remaining spots saved in stash.", "warning");
            if (typeof window.openQuotaModal === "function") {
              window.openQuotaModal();
            }
            break; // Stop batch syncing remaining spots
          }

          if (!res.ok) {
            console.warn(`[OfflineSync] Spot ${spot.id} upload returned status ${res.status}`);
            await updateSpotStatus(spot.id, "failed");
            continue;
          }

          // Success: delete from IndexedDB
          await deletePendingSpot(spot.id);

          // Reconcile placeholder card in DOM if active
          const cardEl = document.getElementById(`pending-card-${spot.id}`);
          if (cardEl && typeof window.renderReply === "function") {
            cardEl.innerHTML = "";
            cardEl.className = "agent-bubble verified-sync-card";
            window.renderReply(cardEl, data.parts || []);
          }

          // Update user quota & stats
          if (data.quota && typeof window.updateQuotaDisplay === "function") {
            window.updateQuotaDisplay(data.quota);
          } else if (typeof window.fetchUserQuota === "function") {
            window.fetchUserQuota();
          }

          if (data.user && currentSpotter && data.user.user_id === currentSpotter.user_id) {
            if (typeof window.updateSpotterState === "function") {
              window.updateSpotterState(data.user, currentToken);
            }
          }

          const carTitle = data.car_name || "Exotic Vehicle";
          const pointsEarned = data.points_awarded || (data.parts && data.parts.find(p => p.data && p.data.final_points)?.data.final_points) || 150;
          showCockpitNotification(`✓ Synced ${carTitle}: +${pointsEarned} PTS awarded!`, "success");

        } catch (uploadErr) {
          console.error(`[OfflineSync] Network error uploading spot ${spot.id}:`, uploadErr);
          await updateSpotStatus(spot.id, "pending");
          // If network aborted mid-sync, cease loop
          if (!navigator.onLine) break;
        }
      }
    } finally {
      isSyncing = false;
      updateNetworkHUD();
      // If garage modal is open, refresh submissions list
      const modal = document.getElementById("submissions-modal-backdrop");
      if (modal && modal.classList.contains("active") && typeof window.openSubmissionsModal === "function") {
        window.openSubmissionsModal();
      }
    }
  }

  // --- Network Event Listeners ---

  window.addEventListener("online", () => {
    console.log("[OfflineSync] Network status: ONLINE");
    updateNetworkHUD();
    showCockpitNotification("Network Restored — Initializing Background Sync...", "success");
    syncPendingSpots();
  });

  window.addEventListener("offline", () => {
    console.log("[OfflineSync] Network status: OFFLINE");
    updateNetworkHUD();
    showCockpitNotification("No Signal — Offline Field Mode Engaged", "warning");
  });

  // Export module to window
  window.CarDexOfflineSync = {
    openDB,
    savePendingSpot,
    getAllPendingSpots,
    getPendingCount,
    deletePendingSpot,
    updateSpotStatus,
    syncPendingSpots,
    updateNetworkHUD,
    renderOfflinePendingCard,
    showCockpitNotification,
    dataURLToBlob,
    blobToDataURL
  };

  // Initialize on load
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      openDB().then(() => updateNetworkHUD());
    });
  } else {
    openDB().then(() => updateNetworkHUD());
  }

})(window);
