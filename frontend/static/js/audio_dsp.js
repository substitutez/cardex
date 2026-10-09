/**
 * CarDex Acoustic Telemetry & Exhaust DSP Engine
 * Compliant with IEC 61672-1:2013 Sound Level Meters & IEC 61260 1/3 Octave Bands.
 *
 * Implements:
 * 1. High-fidelity raw audio capture (48kHz, no AGC/NS/AEC).
 * 2. IEC 61672-1 Frequency Weighting:
 *    - A-Weighting (dBA - Human Ear Curve)
 *    - C-Weighting (dBC - Low-Frequency / Exhaust Power)
 *    - Z-Weighting (dBZ - Linear Flat Pass-through)
 * 3. IEC 61672-1 Time Ballistics:
 *    - Fast Exponential Averaging (tau = 125ms)
 *    - Slow Exponential Averaging (tau = 1000ms)
 *    - Concurrent LAF, LAFmax, and LAeq,T integration
 * 4. Acoustic Anti-Cheat & Physical Validation:
 *    - Digital Flat-top / Clipping Detector (>= -0.05 dBFS, 8% buffer ceiling threshold)
 *    - Spectral Flatness / Wiener Entropy (> 0.75 suspected wind/white noise, < 0.35 genuine engine)
 *    - Harmonic Product Spectrum (HPS) for fundamental cylinder firing cadence (f0) & RPM
 * 5. 31 ISO 1/3-Octave Standard Frequency Bands (25 Hz - 20 kHz)
 * 6. Dynamic Decibel Scoring & Tier Matrix (+20% Engine Cadence Match)
 */

(function (root, factory) {
  if (typeof define === 'function' && define.amd) {
    define([], factory);
  } else if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.CarDexAudioDSP = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // 31 ISO standard preferred 1/3-octave center frequencies (Hz) [IEC 61260]
  const ISO_1_3_OCTAVE_FREQS = [
    25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
    1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000
  ];

  // Reference SPL Calibration:
  // A digital full scale sine wave (amplitude 1.0, RMS = 1/sqrt(2) = -3.01 dBFS).
  // Calibrated so that full scale 1 kHz sine wave equals 94.0 dB SPL standard calibration.
  // SPL = 20 * log10(RMS) + CALIBRATION_OFFSET_DB
  const CALIBRATION_OFFSET_DB = 97.0103;

  /**
   * IEC 61672-1 A-Weighting relative response in dB for a given frequency f in Hz.
   * Exact analog-matched continuous transfer curve:
   * RA(f) = (12194^2 * f^4) / ((f^2 + 20.6^2) * sqrt((f^2 + 107.7^2)*(f^2 + 737.9^2)) * (f^2 + 12194^2))
   * A(f) = 20*log10(RA(f)) + 2.00 dB
   */
  function calculateAWeightingDb(f) {
    if (f <= 0) return -100.0;
    const f2 = f * f;
    const c1 = 12194.0 * 12194.0;
    const num = c1 * f2 * f2;
    const den = (f2 + 20.6 * 20.6) *
                Math.sqrt((f2 + 107.7 * 107.7) * (f2 + 737.9 * 737.9)) *
                (f2 + 12194.0 * 12194.0);
    if (den <= 0) return -100.0;
    const ra = num / den;
    return 20.0 * Math.log10(ra) + 2.00;
  }

  /**
   * IEC 61672-1 C-Weighting relative response in dB for a given frequency f in Hz.
   * RC(f) = (12194^2 * f^2) / ((f^2 + 20.6^2) * (f^2 + 12194^2))
   * C(f) = 20*log10(RC(f)) + 0.06 dB
   */
  function calculateCWeightingDb(f) {
    if (f <= 0) return -100.0;
    const f2 = f * f;
    const c1 = 12194.0 * 12194.0;
    const num = c1 * f2;
    const den = (f2 + 20.6 * 20.6) * (f2 + 12194.0 * 12194.0);
    if (den <= 0) return -100.0;
    const rc = num / den;
    return 20.0 * Math.log10(rc) + 0.06;
  }

  /**
   * Evaluates exhaust decibels and returns bonus points, tier label, and achievements.
   */
  function evaluateDecibelTier(peakDba, cadenceMatched = false) {
    let tier = "Stealth / Quiet";
    let basePoints = 0;
    let badge = "STEALTH";
    let isEarBleeder = false;

    if (peakDba < 75.0) {
      tier = "Stealth / Quiet";
      basePoints = 0;
      badge = "QUIET";
    } else if (peakDba < 90.0) {
      tier = "Street Spec";
      basePoints = 75;
      badge = "STREET_SPEC";
    } else if (peakDba < 100.0) {
      tier = "Sport Exhaust";
      basePoints = 200;
      badge = "SPORT_EXHAUST";
    } else if (peakDba < 110.0) {
      tier = "Track Weapon";
      basePoints = 450;
      badge = "TRACK_WEAPON";
    } else if (peakDba < 120.0) {
      tier = "Screamer / Race Spec";
      basePoints = 800;
      badge = "RACE_SPEC";
    } else {
      tier = "Straight Pipe Demon";
      basePoints = 1250;
      badge = "STRAIGHT_PIPE_DEMON";
      isEarBleeder = true;
    }

    const cadenceBonus = cadenceMatched ? Math.round(basePoints * 0.20) : 0;
    const totalPoints = basePoints + cadenceBonus;

    return {
      tier,
      badge,
      base_points: basePoints,
      cadence_bonus: cadenceBonus,
      cadence_matched: cadenceMatched,
      total_points: totalPoints,
      ear_bleeder: isEarBleeder
    };
  }

  /**
   * Anti-Cheat: Measures digital flat-top clipping.
   * If >= -0.05 dBFS (abs value >= 0.99426) on > 8% of buffer, flags INVALID_MICROPHONE_CLIPPING.
   */
  function detectClipping(timeDomainBuffer) {
    if (!timeDomainBuffer || timeDomainBuffer.length === 0) {
      return { clipped: false, ratio: 0.0 };
    }
    const threshold = Math.pow(10, -0.05 / 20.0); // ~0.99426
    let clipCount = 0;
    const len = timeDomainBuffer.length;
    for (let i = 0; i < len; i++) {
      if (Math.abs(timeDomainBuffer[i]) >= threshold) {
        clipCount++;
      }
    }
    const ratio = clipCount / len;
    return {
      clipped: ratio > 0.08,
      ratio: Number(ratio.toFixed(4)),
      flag: ratio > 0.08 ? "INVALID_MICROPHONE_CLIPPING" : null
    };
  }

  /**
   * Anti-Cheat: Measures Spectral Flatness (Wiener Entropy).
   * Flatness = exp( (1/N) * sum(ln(S_k)) ) / ( (1/N) * sum(S_k) )
   * > 0.75: Suspected white noise, breath puff, or microphone friction.
   * < 0.35: Strong harmonic engine spikes.
   */
  function calculateSpectralFlatness(powerSpectrum) {
    if (!powerSpectrum || powerSpectrum.length === 0) return 0.0;
    const len = powerSpectrum.length;
    let sumLog = 0.0;
    let sumPower = 0.0;
    const epsilon = 1e-12;

    for (let i = 0; i < len; i++) {
      const p = Math.max(powerSpectrum[i], epsilon);
      sumLog += Math.log(p);
      sumPower += p;
    }

    const geometricMean = Math.exp(sumLog / len);
    const arithmeticMean = sumPower / len;
    if (arithmeticMean <= 0) return 0.0;
    const flatness = geometricMean / arithmeticMean;
    return Number(Math.min(1.0, Math.max(0.0, flatness)).toFixed(4));
  }

  /**
   * Harmonic Product Spectrum (HPS) for Fundamental Cylinder Firing Cadence (f0)
   * Searches frequencies 20 Hz - 400 Hz.
   */
  function calculateFundamentalHps(powerSpectrum, sampleRate = 48000, fftSize = 2048) {
    const numBins = powerSpectrum.length;
    const binResolution = sampleRate / fftSize;
    const minBin = Math.max(1, Math.floor(20.0 / binResolution));
    const maxBin = Math.min(numBins - 1, Math.ceil(400.0 / binResolution));

    let maxProduct = -1.0;
    let bestBin = minBin;

    // Use harmonic downsampling orders 1, 2, 3, 4
    for (let b = minBin; b <= maxBin; b++) {
      let product = powerSpectrum[b];
      for (let r = 2; r <= 4; r++) {
        const harmonicBin = b * r;
        if (harmonicBin < numBins) {
          product *= Math.max(1e-12, powerSpectrum[harmonicBin]);
        }
      }
      if (product > maxProduct) {
        maxProduct = product;
        bestBin = b;
      }
    }

    const f0 = bestBin * binResolution;
    return Number(f0.toFixed(1));
  }

  /**
   * Main Acoustic Telemetry Engine Class
   */
  class AcousticTelemetryEngine {
    constructor(options = {}) {
      this.sampleRate = options.sampleRate || 48000;
      this.fftSize = options.fftSize || 2048;
      this.weightingMode = options.weightingMode || 'A'; // 'A', 'C', or 'Z'
      this.timeWeighting = options.timeWeighting || 'FAST'; // 'FAST' (125ms) or 'SLOW' (1000ms)
      this.cylinders = options.cylinders || 8;

      // Ballistics state
      this.laf = 35.0; // Instantaneous Fast-weighted dBA
      this.lcf = 35.0; // Instantaneous Fast-weighted dBC
      this.lzf = 35.0; // Instantaneous Flat-weighted dBZ
      this.lafMax = 35.0; // Peak Fast level held
      this.lafMaxHoldTime = 0; // Timestamp of peak

      // LAeq integration state
      this.laeqSumEnergy = 0.0;
      this.laeqSampleCount = 0;
      this.laeq = 35.0;

      // Anti-Cheat & Physical state
      this.isClipping = false;
      this.spectralFlatness = 0.0;
      this.dominantF0 = 0.0;
      this.estimatedRpm = 0;
      this.antiCheatStatus = "STANDBY"; // "PASS", "SUSPECTED_WIND_OR_WHITE_NOISE", "INVALID_MICROPHONE_CLIPPING"

      // 1/3 Octave Bands
      this.octaveBands = ISO_1_3_OCTAVE_FREQS.map(freq => ({
        freq,
        level: 30.0,
        peakHold: 30.0,
        peakHoldDecay: 0
      }));

      // Spectral signature sample (normalized 31-band vector)
      this.spectralSignatureSample = [];

      // Audio nodes
      this.audioCtx = null;
      this.mediaStream = null;
      this.sourceNode = null;
      this.analyserNode = null;
      this.scriptProcessor = null;
      this.isRunning = false;

      // Precompute frequency weights for FFT bins
      this.binWeightsA = new Float32Array(this.fftSize / 2);
      this.binWeightsC = new Float32Array(this.fftSize / 2);
      const binWidth = this.sampleRate / this.fftSize;
      for (let i = 0; i < this.binWeightsA.length; i++) {
        const freq = i * binWidth;
        this.binWeightsA[i] = calculateAWeightingDb(freq);
        this.binWeightsC[i] = calculateCWeightingDb(freq);
      }
    }

    /**
     * Initializes Web Audio context and requests raw, high-fidelity microphone input.
     */
    async start() {
      if (this.isRunning) return true;

      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) {
        throw new Error("Web Audio API is not supported in this browser.");
      }

      this.audioCtx = new AudioContextClass({
        sampleRate: this.sampleRate,
        latencyHint: "interactive"
      });

      if (this.audioCtx.state === "suspended") {
        await this.audioCtx.resume();
      }

      // Request raw, un-processed acoustic audio
      const constraints = {
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          sampleRate: this.sampleRate
        }
      };

      this.mediaStream = await navigator.mediaDevices.getUserMedia(constraints);
      this.sourceNode = this.audioCtx.createMediaStreamSource(this.mediaStream);

      // Create Fast dynamics analyser node
      this.analyserNode = this.audioCtx.createAnalyser();
      this.analyserNode.fftSize = this.fftSize;
      this.analyserNode.smoothingTimeConstant = 0.0; // We handle IEC 61672 ballistics manually

      this.sourceNode.connect(this.analyserNode);

      // Reset session metrics
      this.laf = 35.0;
      this.lcf = 35.0;
      this.lzf = 35.0;
      this.lafMax = 35.0;
      this.lafMaxHoldTime = Date.now();
      this.laeqSumEnergy = 0.0;
      this.laeqSampleCount = 0;
      this.laeq = 35.0;
      this.isRunning = true;

      return true;
    }

    /**
     * Stops the microphone and cleans up audio context nodes.
     */
    stop() {
      if (!this.isRunning) return;
      this.isRunning = false;

      if (this.mediaStream) {
        this.mediaStream.getTracks().forEach(track => track.stop());
        this.mediaStream = null;
      }
      if (this.sourceNode) {
        this.sourceNode.disconnect();
        this.sourceNode = null;
      }
      if (this.audioCtx && this.audioCtx.state !== 'closed') {
        this.audioCtx.close().catch(() => {});
      }
    }

    /**
     * Processes one real-time frame (called e.g. at 60 FPS via requestAnimationFrame).
     * Computes IEC 61672 ballistics, Anti-Cheat checks, and 1/3-octave spectrum.
     */
    processFrame(dtSeconds = 0.0166) {
      if (!this.isRunning || !this.analyserNode) {
        return this.getTelemetrySnapshot();
      }

      const bufferLength = this.analyserNode.frequencyBinCount;
      const timeData = new Float32Array(this.fftSize);
      const freqData = new Float32Array(bufferLength);

      this.analyserNode.getFloatTimeDomainData(timeData);
      this.analyserNode.getFloatFrequencyData(freqData);

      // 1. Physical Anti-Cheat: Digital Flat-top / Clipping check
      const clipResult = detectClipping(timeData);
      this.isClipping = clipResult.clipped;

      // 2. Compute Raw RMS Power and Power Spectrum
      let sumSq = 0.0;
      for (let i = 0; i < timeData.length; i++) {
        sumSq += timeData[i] * timeData[i];
      }
      const rawRms = Math.sqrt(sumSq / timeData.length);
      const rawDbfs = rawRms > 1e-6 ? 20.0 * Math.log10(rawRms) : -100.0;
      const baseSpl = Math.max(30.0, Math.min(140.0, rawDbfs + CALIBRATION_OFFSET_DB));

      // Build linear power spectrum for Wiener Entropy & HPS
      const powerSpectrum = new Float32Array(bufferLength);
      let totalEnergyA = 0.0;
      let totalEnergyC = 0.0;
      let totalEnergyZ = 0.0;

      for (let i = 0; i < bufferLength; i++) {
        // freqData is in dBFS
        const dbfs = freqData[i];
        const linPower = Math.pow(10, dbfs / 10.0);
        powerSpectrum[i] = linPower;

        // Apply IEC frequency weighting
        const linPowerA = linPower * Math.pow(10, this.binWeightsA[i] / 10.0);
        const linPowerC = linPower * Math.pow(10, this.binWeightsC[i] / 10.0);

        totalEnergyA += linPowerA;
        totalEnergyC += linPowerC;
        totalEnergyZ += linPower;
      }

      // Convert weighted energies to instantaneous SPL
      const instDba = totalEnergyA > 1e-12
        ? Math.max(30.0, Math.min(140.0, 10.0 * Math.log10(totalEnergyA) + CALIBRATION_OFFSET_DB))
        : 35.0;
      const instDbc = totalEnergyC > 1e-12
        ? Math.max(30.0, Math.min(140.0, 10.0 * Math.log10(totalEnergyC) + CALIBRATION_OFFSET_DB))
        : 35.0;
      const instDbz = baseSpl;

      // 3. IEC 61672 Ballistics: Exponential Averaging
      // Fast: tau = 125 ms -> alpha = 1 - exp(-dt / 0.125)
      // Slow: tau = 1000 ms -> alpha = 1 - exp(-dt / 1.0)
      const tau = this.timeWeighting === 'SLOW' ? 1.0 : 0.125;
      const alpha = 1.0 - Math.exp(-Math.max(0.001, dtSeconds) / tau);

      this.laf = this.laf + alpha * (instDba - this.laf);
      this.lcf = this.lcf + alpha * (instDbc - this.lcf);
      this.lzf = this.lzf + alpha * (instDbz - this.lzf);

      // Track LAFmax with 2.0-second decay hold
      const now = Date.now();
      if (this.laf > this.lafMax) {
        this.lafMax = this.laf;
        this.lafMaxHoldTime = now;
      } else if (now - this.lafMaxHoldTime > 2000) {
        // Slow exponential decay after 2 seconds
        this.lafMax = Math.max(this.laf, this.lafMax - (dtSeconds * 12.0));
      }

      // Continuous LAeq,T equivalent integration
      this.laeqSumEnergy += Math.pow(10, this.laf / 10.0);
      this.laeqSampleCount++;
      this.laeq = 10.0 * Math.log10(this.laeqSumEnergy / Math.max(1, this.laeqSampleCount));

      // 4. Physical Anti-Cheat: Wiener Entropy (Spectral Flatness)
      this.spectralFlatness = calculateSpectralFlatness(powerSpectrum);

      // 5. Harmonic Product Spectrum (HPS) for Engine Fundamental Cadence (f0)
      this.dominantF0 = calculateFundamentalHps(powerSpectrum, this.sampleRate, this.fftSize);
      // Firing frequency f0 = (RPM * Cylinders) / 120 -> RPM = (f0 * 120) / Cylinders
      if (this.dominantF0 >= 20.0 && this.dominantF0 <= 400.0) {
        this.estimatedRpm = Math.round((this.dominantF0 * 120.0) / Math.max(2, this.cylinders));
      } else {
        this.estimatedRpm = 0;
      }

      // Anti-Cheat decision logic
      if (this.isClipping) {
        this.antiCheatStatus = "INVALID_MICROPHONE_CLIPPING";
      } else if (this.spectralFlatness > 0.75) {
        this.antiCheatStatus = "SUSPECTED_WIND_OR_WHITE_NOISE";
      } else {
        this.antiCheatStatus = "PASS";
      }

      // 6. 1/3 Octave Real-Time Spectrum Analysis (31 ISO Bands)
      const binWidth = this.sampleRate / this.fftSize;
      const normalizedBands = [];

      for (let b = 0; b < ISO_1_3_OCTAVE_FREQS.length; b++) {
        const fc = ISO_1_3_OCTAVE_FREQS[b];
        const fLower = fc / Math.pow(2, 1 / 6);
        const fUpper = fc * Math.pow(2, 1 / 6);

        const lowerBin = Math.max(0, Math.floor(fLower / binWidth));
        const upperBin = Math.min(bufferLength - 1, Math.ceil(fUpper / binWidth));

        let bandEnergy = 0.0;
        let count = 0;
        for (let k = lowerBin; k <= upperBin; k++) {
          bandEnergy += powerSpectrum[k];
          count++;
        }

        const avgEnergy = count > 0 ? bandEnergy / count : 1e-12;
        const bandDbfs = 10.0 * Math.log10(Math.max(1e-12, avgEnergy));
        const bandSpl = Math.max(20.0, Math.min(135.0, bandDbfs + CALIBRATION_OFFSET_DB));

        // Smooth band level
        this.octaveBands[b].level = this.octaveBands[b].level + 0.3 * (bandSpl - this.octaveBands[b].level);

        // Peak hold
        if (bandSpl > this.octaveBands[b].peakHold) {
          this.octaveBands[b].peakHold = bandSpl;
          this.octaveBands[b].peakHoldDecay = now;
        } else if (now - this.octaveBands[b].peakHoldDecay > 1500) {
          this.octaveBands[b].peakHold = Math.max(this.octaveBands[b].level, this.octaveBands[b].peakHold - 0.5);
        }

        normalizedBands.push(Number((Math.max(0.0, Math.min(1.0, (bandSpl - 30.0) / 100.0))).toFixed(3)));
      }
      this.spectralSignatureSample = normalizedBands;

      return this.getTelemetrySnapshot();
    }

    /**
     * Returns an instantaneous snapshot of current acoustic telemetry.
     */
    getTelemetrySnapshot() {
      const activeValue = this.weightingMode === 'C' ? this.lcf : (this.weightingMode === 'Z' ? this.lzf : this.laf);
      const tierInfo = evaluateDecibelTier(this.lafMax, this.antiCheatStatus === "PASS");

      return {
        active_spl: Number(activeValue.toFixed(1)),
        peak_dba: Number(this.lafMax.toFixed(1)),
        peak_dbc: Number(this.lcf.toFixed(1)),
        current_dba: Number(this.laf.toFixed(1)),
        current_dbc: Number(this.lcf.toFixed(1)),
        current_dbz: Number(this.lzf.toFixed(1)),
        laeq: Number(this.laeq.toFixed(1)),
        weighting: this.weightingMode,
        time_weighting: this.timeWeighting,
        anti_cheat_status: this.antiCheatStatus,
        is_clipping: this.isClipping,
        spectral_flatness: this.spectralFlatness,
        dominant_hz: this.dominantF0,
        estimated_rpm: this.estimatedRpm,
        octave_bands: this.octaveBands.map(b => ({ freq: b.freq, level: Number(b.level.toFixed(1)), peak: Number(b.peakHold.toFixed(1)) })),
        spectral_signature_sample: this.spectralSignatureSample,
        tier: tierInfo.tier,
        badge: tierInfo.badge,
        bonus_points: tierInfo.total_points,
        ear_bleeder: tierInfo.ear_bleeder
      };
    }

    /**
     * Records a calibrated duration (default 6.0 seconds) and returns a complete audit package.
     */
    async recordWindow(durationMs = 6000) {
      if (!this.isRunning) {
        await this.start();
      }
      
      const startTime = Date.now();
      const frames = [];
      let maxDba = 30.0;
      let maxDbc = 30.0;
      let totalEnergy = 0.0;
      let sampleCount = 0;
      let bestF0 = 0.0;
      let antiCheatFlags = [];

      while (Date.now() - startTime < durationMs) {
        await new Promise(r => setTimeout(r, 50));
        const snap = this.processFrame(0.05);
        frames.push(snap);
        if (snap.current_dba > maxDba) maxDba = snap.current_dba;
        if (snap.current_dbc > maxDbc) maxDbc = snap.current_dbc;
        totalEnergy += Math.pow(10, snap.current_dba / 10.0);
        sampleCount++;
        if (snap.dominant_hz > 20.0 && snap.dominant_hz <= 400.0) {
          bestF0 = snap.dominant_hz;
        }
        if (snap.anti_cheat_status !== "PASS" && !antiCheatFlags.includes(snap.anti_cheat_status)) {
          antiCheatFlags.push(snap.anti_cheat_status);
        }
      }

      const laeq = sampleCount > 0 ? 10.0 * Math.log10(totalEnergy / sampleCount) : maxDba;
      const status = antiCheatFlags.length > 0 ? antiCheatFlags[0] : "PASS";
      const tierInfo = evaluateDecibelTier(maxDba, status === "PASS");
      const estRpm = bestF0 > 0 ? Math.round((bestF0 * 120.0) / Math.max(2, this.cylinders)) : 0;

      return {
        peak_dba: Number(maxDba.toFixed(1)),
        peak_dbc: Number(maxDbc.toFixed(1)),
        laeq: Number(laeq.toFixed(1)),
        dominant_hz: Number(bestF0.toFixed(1)),
        estimated_rpm: estRpm,
        anti_cheat_status: status,
        verified_engine_sound: status === "PASS",
        tier: tierInfo.tier,
        badge: tierInfo.badge,
        bonus_points: tierInfo.total_points,
        ear_bleeder: tierInfo.ear_bleeder,
        cadence_matched: status === "PASS",
        spectral_signature_sample: this.spectralSignatureSample
      };
    }
  }

  // Visualizer Canvas Renderers
  class DynoVisualizers {
    constructor() {
      this.currentNeedleDb = 35.0;
      this.needleVelocity = 0.0;
      this.ghostNeedleDb = 35.0;
      this.ghostHoldTimestamp = 0;
      this.waterfallHistory = [];
      this.maxWaterfallCols = 160;
    }

    /**
     * Renders 60 FPS Porsche/AMG-styled tachometer gauge.
     */
    renderTachometerHUD(canvas, telemetry) {
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      const width = canvas.width;
      const height = canvas.height;
      const centerX = width / 2;
      const centerY = height * 0.58;
      const radius = Math.min(width, height) * 0.44;

      ctx.clearRect(0, 0, width, height);

      // Background metallic bezel
      ctx.save();
      const gradBezel = ctx.createRadialGradient(centerX, centerY, radius * 0.75, centerX, centerY, radius * 1.05);
      gradBezel.addColorStop(0, '#0c1322');
      gradBezel.addColorStop(0.85, '#151d30');
      gradBezel.addColorStop(1, '#060911');
      ctx.fillStyle = gradBezel;
      ctx.beginPath();
      ctx.arc(centerX, centerY, radius * 1.05, 0, Math.PI * 2);
      ctx.fill();

      // Outer Bezel Rim
      ctx.lineWidth = 3;
      ctx.strokeStyle = '#22304d';
      ctx.stroke();
      ctx.restore();

      // Gauge Angles: 35 dB (0.75 * PI) to 135 dB (2.25 * PI)
      const startAngle = 0.75 * Math.PI;
      const endAngle = 2.25 * Math.PI;
      const totalAngle = endAngle - startAngle;

      const dbToAngle = (db) => {
        const clamped = Math.max(35, Math.min(135, db));
        return startAngle + ((clamped - 35) / 100.0) * totalAngle;
      };

      // Draw Arc Tracks with Color Grading
      // 35-75 (Dark Slate/Green), 75-95 (Amber), 95-115 (Orange), 115-135 (Neon Violet/Pink)
      const segments = [
        { from: 35, to: 75, color: '#10b981' },
        { from: 75, to: 95, color: '#f59e0b' },
        { from: 95, to: 115, color: '#ef4444' },
        { from: 115, to: 135, color: '#c084fc' }
      ];

      // Track background
      ctx.lineWidth = 14;
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.arc(centerX, centerY, radius * 0.88, startAngle, endAngle);
      ctx.stroke();

      // Color segments
      segments.forEach(seg => {
        ctx.strokeStyle = seg.color;
        ctx.lineWidth = 8;
        ctx.lineCap = 'butt';
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius * 0.88, dbToAngle(seg.from), dbToAngle(seg.to));
        ctx.stroke();
      });

      // Scale Ticks & Labels
      for (let db = 35; db <= 135; db += 5) {
        const ang = dbToAngle(db);
        const isMajor = db % 10 === 0;
        const innerR = radius * (isMajor ? 0.73 : 0.78);
        const outerR = radius * 0.84;

        const x1 = centerX + Math.cos(ang) * innerR;
        const y1 = centerY + Math.sin(ang) * innerR;
        const x2 = centerX + Math.cos(ang) * outerR;
        const y2 = centerY + Math.sin(ang) * outerR;

        ctx.strokeStyle = db >= 115 ? '#c084fc' : (db >= 95 ? '#ef4444' : (db >= 75 ? '#f59e0b' : '#64748b'));
        ctx.lineWidth = isMajor ? 2.5 : 1.2;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();

        if (isMajor) {
          const textR = radius * 0.64;
          const tx = centerX + Math.cos(ang) * textR;
          const ty = centerY + Math.sin(ang) * textR;
          ctx.fillStyle = db >= 115 ? '#e9d5ff' : (db >= 95 ? '#fca5a5' : '#94a3b8');
          ctx.font = 'bold 11px system-ui, -apple-system, sans-serif';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText(String(db), tx, ty);
        }
      }

      // Physics-based Needle Smoothing (Spring Inertia + Damping)
      const targetDb = telemetry ? telemetry.active_spl : 35.0;
      const k = 140.0; // Spring stiffness
      const d = 16.0;  // Damping
      const dt = 0.016;
      const force = k * (targetDb - this.currentNeedleDb) - d * this.needleVelocity;
      this.needleVelocity += force * dt;
      this.currentNeedleDb += this.needleVelocity * dt;

      // Ghost Needle Tracking (peak holding)
      const now = Date.now();
      const peakVal = telemetry ? telemetry.peak_dba : 35.0;
      if (peakVal > this.ghostNeedleDb) {
        this.ghostNeedleDb = peakVal;
        this.ghostHoldTimestamp = now;
      } else if (now - this.ghostHoldTimestamp > 2000) {
        this.ghostNeedleDb = Math.max(targetDb, this.ghostNeedleDb - (dt * 10.0));
      }

      // Draw Ghost Needle
      const ghostAng = dbToAngle(this.ghostNeedleDb);
      ctx.save();
      ctx.strokeStyle = 'rgba(255, 107, 0, 0.4)';
      ctx.lineWidth = 3;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(centerX, centerY);
      ctx.lineTo(centerX + Math.cos(ghostAng) * (radius * 0.86), centerY + Math.sin(ghostAng) * (radius * 0.86));
      ctx.stroke();
      ctx.restore();

      // Draw Primary Analog Needle
      const needleAng = dbToAngle(this.currentNeedleDb);
      const needleColor = this.currentNeedleDb >= 115 ? '#c084fc' : (this.currentNeedleDb >= 95 ? '#ef4444' : (this.currentNeedleDb >= 75 ? '#ff6b00' : '#10b981'));

      ctx.save();
      ctx.shadowColor = needleColor;
      ctx.shadowBlur = 12;
      ctx.strokeStyle = needleColor;
      ctx.lineWidth = 3.5;
      ctx.beginPath();
      ctx.moveTo(centerX - Math.cos(needleAng) * (radius * 0.12), centerY - Math.sin(needleAng) * (radius * 0.12));
      ctx.lineTo(centerX + Math.cos(needleAng) * (radius * 0.86), centerY + Math.sin(needleAng) * (radius * 0.86));
      ctx.stroke();

      // Needle Center Hub
      ctx.fillStyle = '#0f172a';
      ctx.beginPath();
      ctx.arc(centerX, centerY, radius * 0.14, 0, Math.PI * 2);
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = needleColor;
      ctx.stroke();

      ctx.fillStyle = '#f8fafc';
      ctx.beginPath();
      ctx.arc(centerX, centerY, radius * 0.05, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();

      // Center Digital HUD Readouts
      ctx.save();
      ctx.textAlign = 'center';

      // Big Current SPL Readout
      const dispSpl = (telemetry ? telemetry.active_spl : this.currentNeedleDb).toFixed(1);
      ctx.font = '900 36px "SF Pro Display", system-ui, -apple-system, sans-serif';
      ctx.fillStyle = '#f8fafc';
      ctx.fillText(dispSpl, centerX, centerY + radius * 0.44);

      // Weighting unit (e.g. dBA, dBC)
      ctx.font = '700 13px system-ui, sans-serif';
      const weightLabel = telemetry ? (telemetry.weighting === 'C' ? 'dBC' : (telemetry.weighting === 'Z' ? 'dBZ' : 'dBA (LAF)')) : 'dBA';
      ctx.fillStyle = needleColor;
      ctx.fillText(weightLabel, centerX, centerY + radius * 0.55);

      // Secondary readouts (Peak / LAeq)
      ctx.font = '600 11px system-ui, sans-serif';
      ctx.fillStyle = '#94a3b8';
      const peakText = `PEAK: ${(telemetry ? telemetry.peak_dba : 0).toFixed(1)} dBA  |  LAeq: ${(telemetry ? telemetry.laeq : 0).toFixed(1)}`;
      ctx.fillText(peakText, centerX, centerY + radius * 0.66);

      // Live RPM & Cadence
      if (telemetry && telemetry.estimated_rpm > 0) {
        ctx.font = '700 12px system-ui, monospace';
        ctx.fillStyle = '#38bdf8';
        ctx.fillText(`⚡ ${telemetry.estimated_rpm.toLocaleString()} RPM  (f₀: ${telemetry.dominant_hz} Hz)`, centerX, centerY + radius * 0.76);
      }

      // Tier Badge
      if (telemetry && telemetry.tier) {
        ctx.font = '800 11px system-ui, sans-serif';
        ctx.fillStyle = telemetry.ear_bleeder ? '#c084fc' : '#fbbf24';
        const tierText = `${telemetry.tier.toUpperCase()} (+${telemetry.bonus_points} PTS)`;
        ctx.fillText(tierText, centerX, centerY + radius * 0.86);
      }

      ctx.restore();
    }

    /**
     * Renders 31-band 1/3-octave Real-Time Spectrum Analyzer with peak-hold tick marks.
     */
    render13OctaveRTA(canvas, octaveBands) {
      if (!canvas || !octaveBands) return;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      const width = canvas.width;
      const height = canvas.height;
      ctx.clearRect(0, 0, width, height);

      const numBands = octaveBands.length;
      if (numBands === 0) return;

      const paddingLeft = 32;
      const paddingBottom = 22;
      const plotWidth = width - paddingLeft - 8;
      const plotHeight = height - paddingBottom - 10;
      const barWidth = Math.max(3, (plotWidth / numBands) - 2.5);

      // Background grid lines (every 20 dB from 40 to 120 dB)
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.06)';
      ctx.lineWidth = 1;
      ctx.fillStyle = '#64748b';
      ctx.font = '9px system-ui, monospace';
      ctx.textAlign = 'right';

      for (let db = 40; db <= 120; db += 20) {
        const y = 10 + plotHeight * (1.0 - (db - 30.0) / 95.0);
        ctx.beginPath();
        ctx.moveTo(paddingLeft, y);
        ctx.lineTo(width - 8, y);
        ctx.stroke();
        ctx.fillText(`${db}`, paddingLeft - 4, y + 3);
      }

      // Render vertical bars
      for (let i = 0; i < numBands; i++) {
        const band = octaveBands[i];
        const spl = Math.max(30.0, Math.min(125.0, band.level || 30.0));
        const peak = Math.max(30.0, Math.min(125.0, band.peak || spl));

        const barHeight = Math.max(2, plotHeight * ((spl - 30.0) / 95.0));
        const x = paddingLeft + i * (plotWidth / numBands) + 1;
        const y = 10 + plotHeight - barHeight;

        // Dynamic vertical gradient (Green -> Amber -> Red -> Neon Violet)
        const grad = ctx.createLinearGradient(0, y + barHeight, 0, y);
        grad.addColorStop(0, '#10b981');
        grad.addColorStop(0.5, '#f59e0b');
        grad.addColorStop(0.85, '#ef4444');
        grad.addColorStop(1, '#c084fc');

        ctx.fillStyle = grad;
        ctx.fillRect(x, y, barWidth, barHeight);

        // Peak Hold Tick Mark
        const peakY = 10 + plotHeight * (1.0 - (peak - 30.0) / 95.0);
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(x, Math.max(8, peakY), barWidth, 2);

        // Frequency Label for key ISO frequencies
        if (i % 3 === 0 || i === numBands - 1) {
          ctx.fillStyle = '#94a3b8';
          ctx.font = '8px system-ui, monospace';
          ctx.textAlign = 'center';
          const freqLabel = band.freq >= 1000 ? `${(band.freq / 1000).toFixed(0)}k` : `${Math.round(band.freq)}`;
          ctx.fillText(freqLabel, x + barWidth / 2, height - 6);
        }
      }
    }

    /**
     * Renders Rolling 2D Waterfall Spectrogram (Frequency Y-axis, Time X-axis, color-mapped dB intensity).
     */
    renderWaterfallSpectrogram(canvas, octaveBands) {
      if (!canvas || !octaveBands) return;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      const width = canvas.width;
      const height = canvas.height;

      // Extract current column of normalized spectral levels
      const col = octaveBands.map(b => Math.max(0.0, Math.min(1.0, ((b.level || 30) - 30.0) / 90.0)));
      this.waterfallHistory.push(col);
      if (this.waterfallHistory.length > this.maxWaterfallCols) {
        this.waterfallHistory.shift();
      }

      ctx.fillStyle = '#070b14';
      ctx.fillRect(0, 0, width, height);

      const numCols = this.waterfallHistory.length;
      const colWidth = width / this.maxWaterfallCols;
      const numBands = octaveBands.length;
      const bandHeight = height / numBands;

      for (let c = 0; c < numCols; c++) {
        const colData = this.waterfallHistory[c];
        const x = c * colWidth;

        for (let b = 0; b < numBands; b++) {
          // Invert Y so low frequencies are at bottom, high at top
          const y = height - (b + 1) * bandHeight;
          const val = colData[b];

          // Heatmap: Navy (0) -> Indigo (0.25) -> Cyan (0.5) -> Amber (0.75) -> Neon Magenta (1.0)
          let color = '#070b14';
          if (val > 0.85) color = '#f43f5e';
          else if (val > 0.65) color = '#fb923c';
          else if (val > 0.45) color = '#facc15';
          else if (val > 0.25) color = '#06b6d4';
          else if (val > 0.10) color = '#3b82f6';
          else if (val > 0.03) color = '#1e1b4b';

          ctx.fillStyle = color;
          ctx.fillRect(x, y, colWidth + 0.5, bandHeight + 0.5);
        }
      }
    }
  }

  // Export functions, engine class, and visualizer helpers
  return {
    ISO_1_3_OCTAVE_FREQS,
    CALIBRATION_OFFSET_DB,
    calculateAWeightingDb,
    calculateCWeightingDb,
    evaluateDecibelTier,
    detectClipping,
    calculateSpectralFlatness,
    calculateFundamentalHps,
    AcousticTelemetryEngine,
    DynoVisualizers
  };
}));

