/**
 * CarDex WebGL2 Night-Boost & Tactical Reticle HUD Shader
 * 
 * Hardware-accelerated camera stream processing pipeline:
 * - Real-time Night Vision & Contrast Booster fragment shader
 * - Adaptive tone-mapping: boosts underexposed shadow pixels (Y < 0.25)
 * - Highlight preservation: prevents headlight/streetlamp clipping & washout
 * - 3x3 Laplacian unsharp masking for vehicle body lines, emblems, and wheel spokes
 * - Tactical HUD reticle: dynamic corner targeting, rule-of-thirds grid, and live gyro artificial horizon
 */

const CarDexViewfinderShader = (function () {
  let gl = null;
  let glCanvas = null;
  let videoEl = null;
  let reticleCanvas = null;
  let reticleCtx = null;
  let shaderProgram = null;
  let texture = null;
  let isNightBoostActive = false;
  let animFrameId = null;

  // Device orientation state for artificial horizon
  let currentRollDeg = 0;
  let currentPitchDeg = 0;
  let hasOrientation = false;

  const VS_SOURCE = `#version 300 es
    in vec2 a_position;
    in vec2 a_texCoord;
    out vec2 v_texCoord;
    void main() {
      gl_Position = vec4(a_position, 0.0, 1.0);
      v_texCoord = a_texCoord;
    }
  `;

  const FS_SOURCE = `#version 300 es
    precision mediump float;
    uniform sampler2D u_image;
    uniform vec2 u_resolution;
    uniform bool u_nightBoost;
    in vec2 v_texCoord;
    out vec4 outColor;

    void main() {
      vec2 texCoord = v_texCoord;
      vec4 centerColor = texture(u_image, texCoord);

      if (!u_nightBoost) {
        outColor = centerColor;
        return;
      }

      // Calculate Rec. 709 Luminance
      float lum = dot(centerColor.rgb, vec3(0.2126, 0.7152, 0.0722));

      // 1. Adaptive Tone Mapping & Shadow Exposure Boost (Y < 0.25)
      vec3 boosted = centerColor.rgb;
      if (lum < 0.25) {
        // Smooth logarithmic shadow lift up to 2.4x
        float boostFactor = 1.0 + 2.2 * pow((0.25 - lum) / 0.25, 1.2);
        boosted = centerColor.rgb * boostFactor;
      } else if (lum < 0.65) {
        // Mild midtone curve expansion
        boosted = pow(centerColor.rgb, vec3(0.85));
      } else {
        // Highlight preservation: soft shoulder to prevent headlight washout
        boosted = mix(centerColor.rgb, 1.0 - exp(-centerColor.rgb * 1.15), 0.3);
      }

      // 2. Unsharp Masking Kernel (3x3 Laplacian edge enhancement)
      vec2 step = 1.0 / u_resolution;
      vec3 top    = texture(u_image, texCoord + vec2(0.0, -step.y)).rgb;
      vec3 bottom = texture(u_image, texCoord + vec2(0.0, step.y)).rgb;
      vec3 left   = texture(u_image, texCoord + vec2(-step.x, 0.0)).rgb;
      vec3 right  = texture(u_image, texCoord + vec2(step.x, 0.0)).rgb;

      vec3 edge = 4.0 * centerColor.rgb - (top + bottom + left + right);
      vec3 sharpened = boosted + edge * 0.45;

      // Slight saturation bump to reveal vehicle paint hue in dark environments
      float newLum = dot(sharpened, vec3(0.2126, 0.7152, 0.0722));
      vec3 saturated = mix(vec3(newLum), sharpened, 1.18);

      outColor = vec4(clamp(saturated, 0.0, 1.0), centerColor.a);
    }
  `;

  function initGL() {
    if (!glCanvas) return;
    gl = glCanvas.getContext("webgl2", { preserveDrawingBuffer: true });
    if (!gl) {
      console.warn("WebGL2 not supported; falling back to 2D context.");
      return;
    }

    const vertShader = createShader(gl, gl.VERTEX_SHADER, VS_SOURCE);
    const fragShader = createShader(gl, gl.FRAGMENT_SHADER, FS_SOURCE);
    shaderProgram = gl.createProgram();
    gl.attachShader(shaderProgram, vertShader);
    gl.attachShader(shaderProgram, fragShader);
    gl.linkProgram(shaderProgram);

    if (!gl.getProgramParameter(shaderProgram, gl.LINK_STATUS)) {
      console.error("Shader program failed to link:", gl.getProgramInfoLog(shaderProgram));
      return;
    }

    // Full screen quad
    const positionBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([
        -1.0, -1.0, 1.0, -1.0, -1.0, 1.0,
        -1.0, 1.0, 1.0, -1.0, 1.0, 1.0,
      ]),
      gl.STATIC_DRAW
    );

    const posAttr = gl.getAttribLocation(shaderProgram, "a_position");
    gl.enableVertexAttribArray(posAttr);
    gl.vertexAttribPointer(posAttr, 2, gl.FLOAT, false, 0, 0);

    // Texture coordinates (flip Y for standard video orientation)
    const texCoordBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, texCoordBuffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([
        0.0, 1.0, 1.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 1.0, 1.0, 0.0,
      ]),
      gl.STATIC_DRAW
    );

    const texAttr = gl.getAttribLocation(shaderProgram, "a_texCoord");
    gl.enableVertexAttribArray(texAttr);
    gl.vertexAttribPointer(texAttr, 2, gl.FLOAT, false, 0, 0);

    // Create camera texture
    texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  }

  function createShader(glCtx, type, source) {
    const shader = glCtx.createShader(type);
    glCtx.shaderSource(shader, source);
    glCtx.compileShader(shader);
    if (!glCtx.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      console.error("Shader compile error:", glCtx.getShaderInfoLog(shader));
      glCtx.deleteShader(shader);
      return null;
    }
    return shader;
  }

  function renderLoop() {
    if (videoEl && videoEl.readyState >= videoEl.HAVE_CURRENT_DATA) {
      // 1. Render WebGL Video
      if (gl && shaderProgram) {
        if (glCanvas.width !== videoEl.videoWidth || glCanvas.height !== videoEl.videoHeight) {
          glCanvas.width = videoEl.videoWidth || 640;
          glCanvas.height = videoEl.videoHeight || 480;
          gl.viewport(0, 0, glCanvas.width, glCanvas.height);
        }

        gl.useProgram(shaderProgram);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, videoEl);

        const uImage = gl.getUniformLocation(shaderProgram, "u_image");
        const uRes = gl.getUniformLocation(shaderProgram, "u_resolution");
        const uNight = gl.getUniformLocation(shaderProgram, "u_nightBoost");

        gl.uniform1i(uImage, 0);
        gl.uniform2f(uRes, glCanvas.width, glCanvas.height);
        gl.uniform1i(uNight, isNightBoostActive ? 1 : 0);

        gl.drawArrays(gl.TRIANGLES, 0, 6);
      }

      // 2. Render Tactical HUD Reticle
      renderTacticalReticle();
    }
    animFrameId = requestAnimationFrame(renderLoop);
  }

  function renderTacticalReticle() {
    if (!reticleCtx || !reticleCanvas) return;

    const w = reticleCanvas.clientWidth || 360;
    const h = reticleCanvas.clientHeight || 240;
    if (reticleCanvas.width !== w || reticleCanvas.height !== h) {
      reticleCanvas.width = w;
      reticleCanvas.height = h;
    }

    reticleCtx.clearRect(0, 0, w, h);

    // Rule of Thirds Grid (Subtle guidance)
    reticleCtx.strokeStyle = "rgba(255, 255, 255, 0.12)";
    reticleCtx.lineWidth = 1;
    reticleCtx.beginPath();
    // Vertical thirds
    reticleCtx.moveTo(w / 3, 0);
    reticleCtx.lineTo(w / 3, h);
    reticleCtx.moveTo((2 * w) / 3, 0);
    reticleCtx.lineTo((2 * w) / 3, h);
    // Horizontal thirds
    reticleCtx.moveTo(0, h / 3);
    reticleCtx.lineTo(w, h / 3);
    reticleCtx.moveTo(0, (2 * h) / 3);
    reticleCtx.lineTo(w, (2 * h) / 3);
    reticleCtx.stroke();

    // Center Focal Target
    const cx = w / 2;
    const cy = h / 2;
    const targetSize = Math.min(w, h) * 0.38;

    reticleCtx.strokeStyle = isNightBoostActive ? "#10b981" : "#ff6b00";
    reticleCtx.lineWidth = 2;

    // Corner targeting brackets
    const bLen = 22;
    const half = targetSize / 2;

    // Top-Left
    reticleCtx.beginPath();
    reticleCtx.moveTo(cx - half, cy - half + bLen);
    reticleCtx.lineTo(cx - half, cy - half);
    reticleCtx.lineTo(cx - half + bLen, cy - half);
    // Top-Right
    reticleCtx.moveTo(cx + half - bLen, cy - half);
    reticleCtx.lineTo(cx + half, cy - half);
    reticleCtx.lineTo(cx + half, cy - half + bLen);
    // Bottom-Left
    reticleCtx.moveTo(cx - half, cy + half - bLen);
    reticleCtx.lineTo(cx - half, cy + half);
    reticleCtx.lineTo(cx - half + bLen, cy + half);
    // Bottom-Right
    reticleCtx.moveTo(cx + half - bLen, cy + half);
    reticleCtx.lineTo(cx + half, cy + half);
    reticleCtx.lineTo(cx + half, cy + half - bLen);
    reticleCtx.stroke();

    // Center Crosshair
    reticleCtx.beginPath();
    reticleCtx.moveTo(cx - 8, cy);
    reticleCtx.lineTo(cx + 8, cy);
    reticleCtx.moveTo(cx, cy - 8);
    reticleCtx.lineTo(cx, cy + 8);
    reticleCtx.stroke();

    // Live Artificial Horizon / Level Line
    if (hasOrientation) {
      reticleCtx.save();
      reticleCtx.translate(cx, cy);
      reticleCtx.rotate((currentRollDeg * Math.PI) / 180);

      reticleCtx.strokeStyle = Math.abs(currentRollDeg) < 1.5 ? "#10b981" : "rgba(0, 240, 255, 0.7)";
      reticleCtx.lineWidth = 1.5;
      reticleCtx.setLineDash([6, 4]);

      reticleCtx.beginPath();
      reticleCtx.moveTo(-w * 0.28, 0);
      reticleCtx.lineTo(-w * 0.08, 0);
      reticleCtx.moveTo(w * 0.08, 0);
      reticleCtx.lineTo(w * 0.28, 0);
      reticleCtx.stroke();

      reticleCtx.restore();
      reticleCtx.setLineDash([]);
    }
  }

  function setupOrientationListener() {
    if (window.DeviceOrientationEvent) {
      window.addEventListener(
        "deviceorientation",
        (event) => {
          if (event.gamma !== null && event.beta !== null) {
            hasOrientation = true;
            // Gamma: roll angle (-90 to 90)
            currentRollDeg = event.gamma;
            currentPitchDeg = event.beta;
          }
        },
        true
      );
    }
  }

  return {
    init: function (videoId, glCanvasId, reticleCanvasId) {
      videoEl = document.getElementById(videoId);
      glCanvas = document.getElementById(glCanvasId);
      reticleCanvas = document.getElementById(reticleCanvasId);

      if (reticleCanvas) {
        reticleCtx = reticleCanvas.getContext("2d");
      }

      initGL();
      setupOrientationListener();

      if (!animFrameId) {
        renderLoop();
      }
    },

    toggleNightBoost: function () {
      isNightBoostActive = !isNightBoostActive;
      const btn = document.getElementById("camera-nightboost-btn");
      if (btn) {
        btn.classList.toggle("active", isNightBoostActive);
        btn.innerHTML = isNightBoostActive
          ? `<span>🌙</span> <span>Night Boost: ON</span>`
          : `<span>🌙</span> <span>Night Boost (ISO+)</span>`;
      }
      return isNightBoostActive;
    },

    isNightBoostEnabled: function () {
      return isNightBoostActive;
    },

    getCaptureCanvas: function () {
      // Returns WebGL canvas if night boost is active, otherwise raw video is processed
      if (isNightBoostActive && glCanvas) {
        return glCanvas;
      }
      return null;
    },

    destroy: function () {
      if (animFrameId) {
        cancelAnimationFrame(animFrameId);
        animFrameId = null;
      }
    },
  };
})();

window.CarDexViewfinderShader = CarDexViewfinderShader;
