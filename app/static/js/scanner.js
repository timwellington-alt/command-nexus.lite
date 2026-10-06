/*
 * Shared barcode scanner — camera + USB keyboard-wedge.
 *
 * Module exposes a single Scanner class. Consumers pass in:
 *   - a <video> element (for camera mode)
 *   - a scan handler: (event) => void
 *
 * Event contract (matches INVENTORY_MODULE_IMPLEMENTATION_v2 §4.4):
 *   {
 *     raw_value:       "…",
 *     format:          "code_128" | "qr_code" | "unknown" | …,
 *     scanned_at:      "2026-04-18T01:23:45.678Z",
 *     scanner_source:  "native" | "polyfill" | "usb_wedge",
 *   }
 *
 * Decode path preference:
 *   1. native BarcodeDetector (Chrome/Edge/Android Chrome)
 *   2. ZXing-js polyfill loaded from /static/vendor/zxing-browser.min.js
 *      (iOS Safari, Firefox)
 *
 * Usage:
 *   const scanner = new Scanner({ video, onScan });
 *   await scanner.start();                 // camera mode
 *   scanner.attachUsbWedge(inputEl);       // USB wedge on a <input>
 *   scanner.toggleTorch();                 // if hardware supports it
 *   scanner.stop();
 */

const FORMATS = [
  "code_128", "code_39", "code_93",
  "ean_13", "ean_8", "itf",
  "qr_code", "data_matrix",
];

const DEBOUNCE_MS = 1500;
const ZXING_URL = "/static/vendor/zxing-browser.min.js";

// Default optical zoom applied after the stream attaches. 2.5x matches the
// Chromebook scanner's long-standing default — close enough to read most
// asset-tag barcodes without blowing out on wall-size codes. Clamped to
// whatever range the camera actually advertises.
const DEFAULT_ZOOM = 2.5;

let _zxingLoader = null;
function loadZxing() {
  if (window.ZXingBrowser) return Promise.resolve();
  if (_zxingLoader) return _zxingLoader;
  _zxingLoader = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = ZXING_URL;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("Failed to load ZXing polyfill"));
    document.head.appendChild(s);
  });
  return _zxingLoader;
}

export class Scanner {
  constructor({ video, onScan, onStatus }) {
    this.video = video;
    this.onScan = onScan || (() => {});
    this.onStatus = onStatus || (() => {});

    this._stream = null;
    this._track = null;
    this._detector = null;          // native BarcodeDetector
    this._zxingControls = null;     // polyfill controls (stop())
    this._scanLoop = null;          // rAF handle for native path
    this._lastValue = "";
    this._lastAt = 0;
    this._source = null;            // 'native' | 'polyfill'
    this._wedgeHandler = null;
    this._wedgeEl = null;
  }

  async start() {
    this.onStatus("Starting camera…");
    // Request portrait lock — only succeeds in PWA / fullscreen contexts, no-ops
    // elsewhere. Manifest sets orientation: portrait for installed PWAs; this
    // is the runtime belt-and-suspenders for in-browser sessions.
    try { await screen.orientation?.lock?.("portrait-primary"); } catch {}
    // Let the video element finish layout before attaching a stream —
    // iOS Safari refuses to play on a 0×0 element.
    await new Promise((r) => requestAnimationFrame(r));

    try {
      await this._attachStream();
    } catch (e) {
      this.onStatus(`Camera access denied: ${e?.message || e}`);
      throw e;
    }

    // Pick decode path. Native > polyfill.
    if (typeof window.BarcodeDetector === "function") {
      try {
        this._detector = new window.BarcodeDetector({ formats: FORMATS });
        this._source = "native";
        this._runNativeLoop();
        this.onStatus("Point at a barcode");
        return;
      } catch (e) {
        // Some Chrome versions expose BarcodeDetector but fail with
        // "Unsupported format" — fall through to ZXing.
        this._detector = null;
      }
    }

    try {
      await loadZxing();
      this._source = "polyfill";
      this._runZxingLoop();
      this.onStatus("Point at a barcode");
    } catch (e) {
      this.onStatus(`Scanner library failed: ${e?.message || e}`);
      throw e;
    }
  }

  stop() {
    if (this._scanLoop) {
      cancelAnimationFrame(this._scanLoop);
      this._scanLoop = null;
    }
    if (this._zxingControls && typeof this._zxingControls.stop === "function") {
      try { this._zxingControls.stop(); } catch {}
      this._zxingControls = null;
    }
    if (this.video) {
      try { this.video.pause(); } catch {}
      try { this.video.srcObject = null; } catch {}
    }
    if (this._stream) {
      this._stream.getTracks().forEach((t) => { try { t.stop(); } catch {} });
      this._stream = null;
    }
    this._track = null;
    this._detector = null;
    try { screen.orientation?.unlock?.(); } catch {}
  }

  // ── USB keyboard-wedge ─────────────────────────────────────────
  // Scanners plugged into a USB port act as keyboards: they type the
  // value character-by-character and send Enter. Any <input> can be a
  // scan target — we just listen for Enter and fire the scan event.
  attachUsbWedge(inputEl) {
    if (!inputEl) return;
    this.detachUsbWedge();
    this._wedgeEl = inputEl;
    this._wedgeHandler = (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const raw = inputEl.value.trim();
      if (!raw) return;
      inputEl.value = "";
      this._emit(raw, "unknown", "usb_wedge");
    };
    inputEl.addEventListener("keydown", this._wedgeHandler);
    // Keep wedge input focused so scans always land somewhere.
    inputEl.focus();
  }

  detachUsbWedge() {
    if (this._wedgeEl && this._wedgeHandler) {
      this._wedgeEl.removeEventListener("keydown", this._wedgeHandler);
    }
    this._wedgeEl = null;
    this._wedgeHandler = null;
  }

  // ── Torch / zoom (optional) ────────────────────────────────────

  async toggleTorch() {
    if (!this._track) return false;
    const caps = this._track.getCapabilities ? this._track.getCapabilities() : {};
    if (!caps.torch) return false;
    const settings = this._track.getSettings ? this._track.getSettings() : {};
    const next = !settings.torch;
    try {
      await this._track.applyConstraints({ advanced: [{ torch: next }] });
      return next;
    } catch {
      return false;
    }
  }

  getZoomCapabilities() {
    if (!this._track) return null;
    const caps = this._track.getCapabilities ? this._track.getCapabilities() : {};
    return caps.zoom || null;
  }

  async setZoom(value) {
    if (!this._track) return;
    try {
      await this._track.applyConstraints({ advanced: [{ zoom: value }] });
    } catch {}
  }

  // ── Internals ──────────────────────────────────────────────────

  async _attachStream() {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
      audio: false,
    });
    this._stream = stream;
    this._track = stream.getVideoTracks()[0] || null;

    if (this._track && this._track.getCapabilities) {
      const caps = this._track.getCapabilities();
      if (caps.focusMode && caps.focusMode.includes("continuous")) {
        this._track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(() => {});
      }
      // Apply default zoom once — lets consumers still call setZoom() later
      // to expose a slider. Silently no-ops on cameras without zoom support.
      if (caps.zoom && typeof caps.zoom.min === "number") {
        const z = Math.min(Math.max(DEFAULT_ZOOM, caps.zoom.min), caps.zoom.max);
        this._track.applyConstraints({ advanced: [{ zoom: z }] }).catch(() => {});
      }
    }

    this.video.muted = true;
    this.video.setAttribute("playsinline", "");
    this.video.setAttribute("webkit-playsinline", "");
    this.video.srcObject = stream;
    if (this.video.readyState < 1) {
      await new Promise((resolve) => {
        const done = () => { this.video.removeEventListener("loadedmetadata", done); resolve(); };
        this.video.addEventListener("loadedmetadata", done);
        setTimeout(done, 1500);
      });
    }
    await this.video.play();
  }

  _runNativeLoop() {
    const detector = this._detector;
    const video = this.video;
    const tick = async () => {
      if (!this._detector) return;  // stopped
      if (video.readyState >= 2) {
        try {
          const codes = await detector.detect(video);
          if (codes && codes.length) {
            const c = codes[0];
            this._emit(c.rawValue, c.format || "unknown", "native");
          }
        } catch {
          // Detector transient errors — keep looping
        }
      }
      this._scanLoop = requestAnimationFrame(tick);
    };
    this._scanLoop = requestAnimationFrame(tick);
  }

  async _runZxingLoop() {
    const { BrowserMultiFormatReader } = window.ZXingBrowser;
    const reader = new BrowserMultiFormatReader();
    const controls = await reader.decodeFromVideoElement(this.video, (result) => {
      if (!result) return;
      // ZXing format → string
      let format = "unknown";
      try {
        const f = result.getBarcodeFormat ? result.getBarcodeFormat() : null;
        if (f !== null && f !== undefined) format = String(f).toLowerCase();
      } catch {}
      this._emit(result.getText(), format, "polyfill");
    });
    this._zxingControls = controls;
  }

  _emit(raw, format, source) {
    if (!raw) return;
    const now = Date.now();
    if (raw === this._lastValue && now - this._lastAt < DEBOUNCE_MS) return;
    this._lastValue = raw;
    this._lastAt = now;
    this.onScan({
      raw_value: raw,
      format: format || "unknown",
      scanned_at: new Date(now).toISOString(),
      scanner_source: source,
    });
  }
}

// Convenience: resolve a scan via the inventory API.
export async function resolveScan(rawValue, context = null) {
  const res = await fetch("/api/inventory/scan/resolve", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // CSRF middleware requires this header on mutating requests.
      "X-Requested-With": "CommandNexus",
    },
    body: JSON.stringify({ raw_value: rawValue, context }),
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail;
    try { detail = (await res.json()).detail; } catch { detail = res.statusText; }
    throw new Error(`resolve failed: ${res.status} ${detail || ""}`.trim());
  }
  return await res.json();
}
