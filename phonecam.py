#!/usr/bin/env python3
"""Turn a phone's browser camera into a virtual webcam named PhoneCam.

Run:
    python3 phonecam.py

Open the printed link on the phone, accept the certificate warning,
allow the camera, and tap Broadcast. Vesktop, Discord, and OBS can
then select the camera "PhoneCam".
"""

from __future__ import annotations

import argparse
import base64
import collections
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CERT = ROOT / "cert.pem"
KEY = ROOT / "key.pem"
LABEL = "PhoneCam"
WIDTH = 1280
HEIGHT = 720
MAX_CAMERA_EDGE = 1920
LAST_SIZE = ROOT / ".last-size"
FPS = 30
MAX_FRAME = 16_000_000
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CODECS = {"h264": "H.264", "jpeg": "JPEG", "png": "PNG (lossless)"}
DECODERS = {
    "h264": ["h264parse", "!", "avdec_h264", "max-threads=1"],
    "jpeg": ["jpegparse", "!", "jpegdec"],
    "png": ["pngparse", "!", "pngdec"],
}

VIDIOC_S_FMT = 0xC0D05605
VIDIOC_S_PARM = 0xC0CC5616
V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_PIX_FMT_YUYV = int.from_bytes(b"YUYV", "little")
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_SRGB = 8
V4L2_CAP_TIMEPERFRAME = 0x1000
F_SETPIPE_SZ = 1031
FRAME_HEADER = struct.Struct("<Bd")
MAX_QUEUE_AGE = 0.15

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e1110">
<title>PhoneCam</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0e1110;
    --card: #171c1a;
    --text: #f4f7f5;
    --muted: #9aa59e;
    --line: #2a332e;
    --go: #3ddc84;
    --go-ink: #062214;
    --stop: #ff5d5d;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    height: 100%;
    background: var(--bg);
    color: var(--text);
  }
  body {
    font: 14px/1.3 system-ui, sans-serif;
    height: 100dvh;
    overflow: hidden;
    padding: max(10px, env(safe-area-inset-top)) 12px max(12px, env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  header h1 {
    margin: 0;
    font-size: 1.2rem;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  #dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--line);
  }
  #dot.live { background: var(--go); box-shadow: 0 0 8px var(--go); }
  .code {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--muted);
    font-size: 0.75rem;
  }
  .code input {
    width: 7.2em;
    font: 600 0.95rem/1 system-ui, sans-serif;
    letter-spacing: 0.18em;
    text-align: center;
    padding: 7px 6px;
  }
  #stage {
    flex: 1 1 0;
    min-height: 0;
    display: grid;
    place-items: center;
  }
  #frame {
    background: #000;
    border-radius: 14px;
    overflow: hidden;
  }
  #frame video {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
    background: #000;
  }
  #frame video.mirror { transform: scaleX(-1); }
  .settings {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }
  .settings .wide { grid-column: span 2; }
  label {
    display: grid;
    gap: 3px;
    min-width: 0;
    color: var(--muted);
    font-size: 0.7rem;
  }
  select, input {
    width: 100%;
    min-width: 0;
    font: 600 0.85rem/1.2 system-ui, sans-serif;
    padding: 7px 6px;
    border-radius: 9px;
    border: 1px solid var(--line);
    background: var(--card);
    color: var(--text);
  }
  .metrics {
    margin: 0;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1px;
    background: var(--line);
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
  }
  .metrics div {
    background: var(--card);
    padding: 5px 7px;
    min-width: 0;
  }
  .metrics dt {
    color: var(--muted);
    font-size: 0.65rem;
  }
  .metrics dd {
    margin: 0;
    font-size: 0.8rem;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  #status {
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  #status.bad { color: var(--stop); }
  #go {
    font: 700 1.05rem/1 system-ui, sans-serif;
    border: 0;
    border-radius: 14px;
    padding: 15px;
    background: var(--go);
    color: var(--go-ink);
    touch-action: manipulation;
  }
  #go.live {
    background: var(--stop);
    color: #2a0606;
  }
  button:disabled, select:disabled { opacity: 0.45; }
</style>
</head>
<body>
  <header>
    <h1><span id="dot"></span>PhoneCam</h1>
    <label class="code">Code
      <input id="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="000000">
    </label>
  </header>
  <div id="stage">
    <div id="frame"><video id="cam" playsinline autoplay muted></video></div>
  </div>
  <section class="settings">
    <label class="wide">Camera
      <select id="camera"><option value="">Default</option></select>
    </label>
    <label>Format
      <select id="codec">
        <option value="h264">H.264</option>
        <option value="jpeg">JPEG</option>
        <option value="png">PNG lossless</option>
      </select>
    </label>
    <label>Resolution
      <select id="res">
        <option value="640">640 px</option>
        <option value="960">960 px</option>
        <option value="1280">1280 (720p)</option>
        <option value="1920">1920 (1080p)</option>
        <option value="0">Max</option>
      </select>
    </label>
    <label>Frame rate
      <select id="fps">
        <option value="15">15 fps</option>
        <option value="24">24 fps</option>
        <option value="30">30 fps</option>
        <option value="60">60 fps</option>
      </select>
    </label>
    <label>Quality
      <select id="quality">
        <option value="low">Low</option>
        <option value="medium">Medium</option>
        <option value="high">High</option>
        <option value="max">Max</option>
      </select>
    </label>
  </section>
  <dl class="metrics">
    <div><dt>Camera</dt><dd id="m-cam">–</dd></div>
    <div><dt>Sending</dt><dd id="m-out">–</dd></div>
    <div><dt>Phone fps</dt><dd id="m-fps">–</dd></div>
    <div><dt>PC fps</dt><dd id="m-pc">–</dd></div>
    <div><dt>Bitrate</dt><dd id="m-rate">–</dd></div>
    <div><dt>Avg frame</dt><dd id="m-size">–</dd></div>
    <div><dt>Encode</dt><dd id="m-enc">–</dd></div>
    <div><dt>Frame delay</dt><dd id="m-delay">–</dd></div>
  </dl>
  <div id="status">Starting camera…</div>
  <button id="go" type="button" disabled>Broadcast</button>
<script>
const $ = (id) => document.getElementById(id);
const video = $("cam");
const stage = $("stage");
const frameBox = $("frame");
const statusEl = $("status");
const dot = $("dot");
const go = $("go");
const codeInput = $("code");
const sel = {
  camera: $("camera"), codec: $("codec"),
  res: $("res"), fps: $("fps"), quality: $("quality")
};
const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d", { alpha: false });

// The PC camera's current shape; the PC reports changes while live.
let [outW, outH] = "__OUTPUT__".split("x").map(Number);
let cropped = false;
const JPEG_QUALITY = { low: 0.6, medium: 0.75, high: 0.88, max: 0.95 };
const H264_BITS_PER_PIXEL = { low: 0.04, medium: 0.08, high: 0.15, max: 0.3 };
const CODEC_NAMES = { h264: "H.264", jpeg: "JPEG", png: "PNG" };
const H264_CODECS = ["avc1.42E034", "avc1.42E033", "avc1.42E028", "avc1.42E01F", "avc1.4D0034", "avc1.640034"];
const hasH264 = "VideoEncoder" in window && "VideoFrame" in window;

let stream = null;
let ws = null;
let live = false;
let wakeLock = null;
let cameraKey = "";
let lastSend = 0;
let sentConfig = "";
let inflight = 0;
let encoder = null;
let encoderKey = "";
let encoderPending = false;
let preferSoftware = false;
let forceKey = false;
let frameNo = 0;
let lastFrameBytes = 0;
let lastSentTs = 0;
let lastAckTs = 0;
const encodeStart = new Map();
let meter = freshMeter();
let pcStats = null;

function freshMeter() {
  return { t: performance.now(), frames: 0, bytes: 0, encodeMs: 0, encodes: 0, skipped: 0, delayMs: 0, delays: 0 };
}

function frameHeader(key, ts) {
  const head = new DataView(new ArrayBuffer(9));
  head.setUint8(0, key ? 1 : 0);
  head.setFloat64(1, ts, true);
  return head.buffer;
}

// Frames the PC has not acknowledged yet are sitting in a network queue.
// Sending more on top only makes every later frame arrive later.
function backlogged(now, fps) {
  if (lastSentTs <= lastAckTs) return false;
  if (now - lastSentTs > 1500) {
    lastAckTs = lastSentTs;
    forceKey = true;
    return false;
  }
  return lastSentTs - lastAckTs > Math.max(150, 2.5 * 1000 / fps);
}

function setStatus(text, bad) {
  statusEl.textContent = text;
  statusEl.classList.toggle("bad", !!bad);
}

function settings() {
  return {
    camera: sel.camera.value,
    codec: sel.codec.value,
    res: Number(sel.res.value),
    fps: Number(sel.fps.value),
    quality: sel.quality.value
  };
}

function savedSettings() {
  try { return JSON.parse(localStorage.getItem("phonecam") || "{}"); } catch (err) { return {}; }
}

function loadSettings() {
  if (!hasH264) {
    sel.codec.options[0].disabled = true;
    sel.codec.options[0].textContent = "H.264 (n/a)";
  }
  const want = Object.assign(
    { codec: hasH264 ? "h264" : "jpeg", res: "1280", fps: "30", quality: "high" },
    savedSettings()
  );
  if (want.codec === "h264" && !hasH264) want.codec = "jpeg";
  for (const name of ["codec", "res", "fps", "quality"]) {
    const value = String(want[name]);
    if ([...sel[name].options].some((opt) => opt.value === value && !opt.disabled)) sel[name].value = value;
  }
  sel.quality.disabled = sel.codec.value === "png";
}

function saveSettings() {
  try { localStorage.setItem("phonecam", JSON.stringify(settings())); } catch (err) {}
}

function layout() {
  const box = stage.getBoundingClientRect();
  let width = box.width;
  const aspect = previewAspect();
  let height = width / aspect;
  if (height > box.height) {
    height = box.height;
    width = height * aspect;
  }
  frameBox.style.width = Math.floor(width) + "px";
  frameBox.style.height = Math.floor(height) + "px";
}

// Live: show exactly what the PC camera shows. Idle: the camera's own shape,
// which is what the PC will switch to.
function previewAspect() {
  if (live) return outW / outH;
  return video.videoWidth && video.videoHeight ? video.videoWidth / video.videoHeight : outW / outH;
}

function cameraName(device, index, seen) {
  const label = device.label || "";
  let base = /front|user/i.test(label) ? "Front" : /back|rear|environment/i.test(label) ? "Back" : "";
  if (!base) return label || "Camera " + (index + 1);
  seen[base] = (seen[base] || 0) + 1;
  return seen[base] > 1 ? base + " " + seen[base] : base;
}

async function listCameras(currentId) {
  let devices = [];
  try {
    devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
  } catch (err) {}
  if (!devices.length) return;
  const seen = {};
  sel.camera.replaceChildren(...devices.map((device, index) => {
    const option = document.createElement("option");
    option.value = device.deviceId;
    option.textContent = cameraName(device, index, seen);
    return option;
  }));
  if (currentId) sel.camera.value = currentId;
}

async function openCamera() {
  const s = settings();
  const edge = s.res || 4096;
  const aspect = 4 / 3;
  if (stream) {
    for (const track of stream.getTracks()) track.stop();
    stream = null;
  }
  const want = {
    width: { ideal: edge },
    height: { ideal: Math.round(edge / aspect) },
    frameRate: { ideal: s.fps }
  };
  const wanted = s.camera || savedSettings().camera;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: Object.assign({}, want, wanted ? { deviceId: { exact: wanted } } : { facingMode: { ideal: "user" } })
    });
  } catch (err) {
    if (!wanted || err.name === "NotAllowedError") throw err;
    stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: want });
  }
  const track = stream.getVideoTracks()[0];
  const info = track.getSettings();
  const front = info.facingMode ? info.facingMode === "user" : /front/i.test(track.label);
  video.srcObject = stream;
  video.classList.toggle("mirror", front);
  await video.play();
  await listCameras(info.deviceId);
  cameraKey = [sel.camera.value, s.res, s.fps].join(",");
  saveSettings();
  layout();
  go.disabled = false;
  if (!live) setStatus("Camera ready. Tap Broadcast.");
  scheduleFrame();
}

let frameScheduled = false;
function scheduleFrame() {
  if (frameScheduled) return;
  frameScheduled = true;
  const run = (now) => {
    frameScheduled = false;
    if (live) sendFrame(typeof now === "number" ? now : performance.now());
    scheduleFrame();
  };
  if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(run);
  else setTimeout(run, 1000 / (settings().fps * 2));
}

function grab(s) {
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) return false;
  const sw = vw, sh = vh;
  const scale = s.res ? Math.min(1, s.res / Math.max(sw, sh)) : 1;
  const width = Math.max(2, Math.round(sw * scale)) & ~1;
  const height = Math.max(2, Math.round(sh * scale)) & ~1;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  ctx.drawImage(video, 0, 0, sw, sh, 0, 0, width, height);
  return true;
}

function sendFrame(now) {
  const s = settings();
  if (now - lastSend < (1000 / s.fps) * 0.85) return;
  if (!ws || ws.readyState !== 1) return;
  if (backlogged(now, s.fps) || ws.bufferedAmount > Math.max(64000, lastFrameBytes * 2)) {
    meter.skipped++;
    return;
  }
  if (!grab(s)) return;
  lastSend = now;
  const config = [s.codec, canvas.width, canvas.height, s.fps].join(",");
  if (config !== sentConfig) {
    closeEncoder();
    ws.send(JSON.stringify({ type: "config", codec: s.codec, width: canvas.width, height: canvas.height, fps: s.fps }));
    sentConfig = config;
    forceKey = true;
  }
  if (s.codec === "h264") encodeH264(s);
  else encodeImage(s);
}

function record(encodeMs, bytes) {
  meter.frames++;
  meter.bytes += bytes;
  lastFrameBytes = bytes;
  if (encodeMs > 0) {
    meter.encodeMs += encodeMs;
    meter.encodes++;
  }
}

function encodeImage(s) {
  if (inflight >= 2) {
    meter.skipped++;
    return;
  }
  inflight++;
  const started = performance.now();
  const config = sentConfig;
  const type = s.codec === "png" ? "image/png" : "image/jpeg";
  canvas.toBlob((blob) => {
    inflight--;
    if (!blob || !live || !ws || ws.readyState !== 1 || config !== sentConfig) return;
    ws.send(new Blob([frameHeader(true, started), blob]));
    lastSentTs = Math.max(lastSentTs, started);
    record(performance.now() - started, blob.size + 9);
  }, type, JPEG_QUALITY[s.quality]);
}

function closeEncoder() {
  if (encoder && encoder.state !== "closed") {
    try { encoder.close(); } catch (err) {}
  }
  encoder = null;
  encoderKey = "";
  encodeStart.clear();
}

function onChunk(chunk) {
  const started = encodeStart.get(chunk.timestamp);
  encodeStart.delete(chunk.timestamp);
  if (!live || !ws || ws.readyState !== 1) return;
  const ts = chunk.timestamp / 1000;
  const out = new Uint8Array(9 + chunk.byteLength);
  out.set(new Uint8Array(frameHeader(chunk.type === "key", ts)), 0);
  chunk.copyTo(out.subarray(9));
  ws.send(out);
  lastSentTs = Math.max(lastSentTs, ts);
  record(started ? performance.now() - started : 0, out.byteLength);
}

async function setupEncoder(s, width, height, key) {
  encoderPending = true;
  closeEncoder();
  try {
    const bitrate = Math.round(Math.min(40e6, Math.max(5e5, width * height * s.fps * H264_BITS_PER_PIXEL[s.quality])));
    const base = {
      width, height, bitrate,
      framerate: s.fps,
      latencyMode: "realtime",
      bitrateMode: "variable",
      avc: { format: "annexb" }
    };
    const accel = preferSoftware ? ["prefer-software", "no-preference"] : ["prefer-hardware", "no-preference"];
    let config = null;
    for (const codec of H264_CODECS) {
      for (const hardwareAcceleration of accel) {
        const candidate = Object.assign({}, base, { codec, hardwareAcceleration });
        try {
          const result = await VideoEncoder.isConfigSupported(candidate);
          if (result.supported) { config = candidate; break; }
        } catch (err) {}
      }
      if (config) break;
    }
    if (!config) throw new Error("This browser cannot encode H.264 at that size. Pick JPEG.");
    encoder = new VideoEncoder({
      output: onChunk,
      error: (err) => {
        if (!preferSoftware) preferSoftware = true;
        else setStatus("H.264 encoder failed: " + err.message, true);
        closeEncoder();
      }
    });
    encoder.configure(config);
    encoderKey = key;
    forceKey = true;
  } catch (err) {
    setStatus(err.message || "H.264 is not available.", true);
    encoderKey = "failed:" + key;
  } finally {
    encoderPending = false;
  }
}

function encodeH264(s) {
  const key = [canvas.width, canvas.height, s.fps, s.quality, preferSoftware].join(",");
  if (encoderKey !== key || !encoder || encoder.state !== "configured") {
    if (!encoderPending && encoderKey !== "failed:" + key) setupEncoder(s, canvas.width, canvas.height, key);
    meter.skipped++;
    return;
  }
  if (encoder.encodeQueueSize > 2) {
    meter.skipped++;
    return;
  }
  const timestamp = Math.round(performance.now() * 1000);
  const frame = new VideoFrame(canvas, { timestamp });
  encodeStart.set(timestamp, performance.now());
  if (encodeStart.size > 120) encodeStart.clear();
  const keyFrame = forceKey || frameNo % (s.fps * 2) === 0;
  forceKey = false;
  frameNo++;
  try { encoder.encode(frame, { keyFrame }); } catch (err) {}
  frame.close();
}

function connect(code) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket("wss://" + location.host + "/ws");
    socket.binaryType = "arraybuffer";
    let settled = false;
    const fail = (err) => {
      if (settled) return;
      settled = true;
      try { socket.close(); } catch (e) {}
      reject(err);
    };
    socket.onopen = () => socket.send(code);
    socket.onmessage = (ev) => {
      if (settled) return;
      settled = true;
      if (ev.data === "ok") resolve(socket);
      else fail(new Error("That code does not match the PC. Use the code in the terminal."));
    };
    socket.onerror = () => fail(new Error("Could not reach the PC."));
    socket.onclose = () => fail(new Error("The PC closed the connection."));
    setTimeout(() => fail(new Error("The PC did not answer.")), 8000);
  });
}

function onServerMessage(ev) {
  if (typeof ev.data !== "string") return;
  let msg;
  try { msg = JSON.parse(ev.data); } catch (err) { return; }
  if (msg.type === "ack") {
    if (msg.t > lastAckTs) lastAckTs = msg.t;
    meter.delayMs += performance.now() - msg.t;
    meter.delays++;
  } else if (msg.type === "output") {
    outW = msg.width;
    outH = msg.height;
    layout();
    if (msg.cropped) {
      setStatus("An app has PhoneCam open in the old shape, so it is cropped. Turn the camera off and on there to switch.");
    } else if (cropped) {
      setStatus("Broadcasting to the PC.");
    }
    cropped = msg.cropped;
  } else if (msg.type === "stats") pcStats = msg;
  else if (msg.type === "keyframe") forceKey = true;
}

async function start() {
  const code = codeInput.value.trim();
  if (!/^\d{6}$/.test(code)) {
    setStatus("Enter the 6-digit code printed on the PC (top right).", true);
    codeInput.focus();
    return;
  }
  go.disabled = true;
  setStatus("Connecting…");
  try {
    ws = await connect(code);
  } catch (err) {
    ws = null;
    go.disabled = false;
    setStatus(err.message || "Could not connect.", true);
    return;
  }
  live = true;
  sentConfig = "";
  lastSend = 0;
  frameNo = 0;
  lastSentTs = 0;
  lastAckTs = 0;
  pcStats = null;
  meter = freshMeter();
  ws.onmessage = onServerMessage;
  ws.onclose = () => {
    if (!live) return;
    stop(false);
    setStatus("Connection to the PC dropped.", true);
  };
  go.disabled = false;
  go.textContent = "Stop";
  go.classList.add("live");
  dot.classList.add("live");
  codeInput.disabled = true;
  cropped = false;
  layout();
  setStatus("Broadcasting to the PC.");
  try { wakeLock = await navigator.wakeLock.request("screen"); } catch (err) {}
  scheduleFrame();
}

function stop(tellStatus) {
  live = false;
  inflight = 0;
  closeEncoder();
  if (ws) {
    try { ws.close(); } catch (err) {}
    ws = null;
  }
  if (wakeLock) {
    wakeLock.release().catch(() => {});
    wakeLock = null;
  }
  go.textContent = "Broadcast";
  go.classList.remove("live");
  go.disabled = false;
  dot.classList.remove("live");
  codeInput.disabled = false;
  layout();
  if (tellStatus !== false) setStatus("Stopped. The PC is still watching for you.");
}

function show(id, text) {
  $(id).textContent = text;
}

setInterval(() => {
  const now = performance.now();
  const seconds = Math.max(0.001, (now - meter.t) / 1000);
  const track = stream && stream.getVideoTracks()[0];
  const cam = track ? track.getSettings() : {};
  show("m-cam", cam.width ? `${cam.width}×${cam.height}@${Math.round(cam.frameRate || 0)}` : "–");
  if (live) {
    const s = settings();
    const fps = meter.frames / seconds;
    show("m-out", canvas.width ? `${canvas.width}×${canvas.height} ${CODEC_NAMES[s.codec]}` : "–");
    show("m-fps", fps.toFixed(1) + (meter.skipped ? ` (${meter.skipped} skip)` : ""));
    show("m-rate", (meter.bytes * 8 / seconds / 1e6).toFixed(1) + " Mb/s");
    show("m-size", meter.frames ? Math.round(meter.bytes / meter.frames / 1024) + " KB" : "–");
    show("m-enc", meter.encodes ? Math.round(meter.encodeMs / meter.encodes) + " ms" : "–");
    show("m-pc", pcStats ? pcStats.out_fps.toFixed(1) + (pcStats.dropped ? ` (${pcStats.dropped} drop)` : "") : "–");
    show("m-delay", meter.delays ? Math.round(meter.delayMs / meter.delays) + " ms" : "–");
  } else {
    for (const id of ["m-out", "m-fps", "m-rate", "m-size", "m-enc", "m-pc", "m-delay"]) show(id, "–");
  }
  meter = freshMeter();
}, 1000);

async function applySettings() {
  const s = settings();
  sel.quality.disabled = s.codec === "png";
  saveSettings();
  layout();
  if (s.codec === "png" && !live) setStatus("PNG is lossless but large. Expect a few fps over Wi-Fi.");
  if ([s.camera, s.res, s.fps].join(",") !== cameraKey && stream) {
    try {
      await openCamera();
    } catch (err) {
      setStatus("The camera refused those settings. Try a lower resolution or frame rate.", true);
    }
  }
}

for (const element of Object.values(sel)) element.addEventListener("change", applySettings);
go.addEventListener("click", () => { live ? stop(true) : start(); });
window.addEventListener("resize", layout);
video.addEventListener("resize", layout);
if (window.ResizeObserver) new ResizeObserver(layout).observe(stage);

const fromHash = (location.hash || "").replace("#", "").trim();
if (/^\d{6}$/.test(fromHash)) codeInput.value = fromHash;
loadSettings();
layout();
if (!window.isSecureContext || !navigator.mediaDevices) {
  setStatus("The camera needs the https:// link, after you accept the certificate warning.", true);
} else {
  openCamera().catch((err) => {
    const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
    setStatus(denied
      ? "Camera permission was blocked. Allow it in the browser, or accept the certificate warning and reload."
      : "No camera is available in this browser.", true);
  });
}
</script>
</body>
</html>
"""

PROCS: set[subprocess.Popen[bytes]] = set()
PROCS_LOCK = threading.Lock()


def say(text: str = "") -> None:
    print(text, flush=True)


def local_ipv4s() -> list[str]:
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-brief", "addr", "show", "scope", "global"], text=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        out = ""
    ips: list[str] = []
    virtual = ("virbr", "docker", "veth", "br-", "tun", "tap")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] == "-" or parts[0].startswith(virtual):
            continue
        ips.append(parts[2].split("/")[0])
    if ips:
        return ips
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return [sock.getsockname()[0]]
    except OSError:
        raise SystemExit(
            "Could not find this PC's local IP. Connect to the network and try again."
        ) from None
    finally:
        sock.close()


def primary_ip(ips: list[str]) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        chosen = sock.getsockname()[0]
    except OSError:
        chosen = ips[0]
    finally:
        sock.close()
    return chosen if chosen in ips else ips[0]


def cert_covers(ips: list[str]) -> bool:
    if not CERT.exists() or not KEY.exists():
        return False
    try:
        text = subprocess.check_output(
            ["openssl", "x509", "-in", str(CERT), "-noout", "-ext", "subjectAltName"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return False
    return all(ip in text for ip in ips)


def ensure_cert(ips: list[str]) -> None:
    if cert_covers(ips):
        return
    san = ",".join([f"IP:{ip}" for ip in ips] + ["IP:127.0.0.1", "DNS:localhost"])
    say("Creating a local certificate so the phone browser can use the camera.")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(KEY), "-out", str(CERT),
            "-days", "825", "-nodes",
            "-subj", "/CN=PhoneCam",
            "-addext", f"subjectAltName={san}",
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(KEY, 0o600)
    os.chmod(CERT, 0o644)


def spawn(cmd: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)  # type: ignore[call-overload]
    with PROCS_LOCK:
        PROCS.add(proc)
    return proc


def kill_proc(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    with PROCS_LOCK:
        PROCS.discard(proc)


def kill_all() -> None:
    with PROCS_LOCK:
        procs = list(PROCS)
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def drain_stderr(proc: subprocess.Popen[bytes], prefix: str) -> None:
    def run() -> None:
        assert proc.stderr is not None
        try:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    say(f"{prefix}: {line}")
        except (OSError, ValueError):
            pass

    threading.Thread(target=run, daemon=True).start()


def _v4l2_caps(device: str) -> tuple[bool, bool]:
    """Return (has_capture, has_output) for a V4L2 node."""
    import array

    try:
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False, False
    try:
        buf = array.array("B", [0] * 104)
        # VIDIOC_QUERYCAP
        fcntl.ioctl(fd, 0x80685600, buf, True)
        caps = int.from_bytes(buf[84:88], "little")
        device_caps = int.from_bytes(buf[88:92], "little")
        flags = device_caps or caps
        return bool(flags & 0x1), bool(flags & 0x2)
    except OSError:
        return False, False
    finally:
        os.close(fd)


def find_v4l2_device() -> str | None:
    """Find the PhoneCam (or any) v4l2loopback device."""
    base = Path("/sys/class/video4linux")
    if not base.is_dir():
        return None
    candidates: list[tuple[str, str]] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        name_file = entry / "name"
        if not name_file.is_file():
            continue
        try:
            label = name_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        device = f"/dev/{entry.name}"
        if not Path(device).exists():
            continue
        lower = label.lower()
        if "phonecam" not in lower and "loopback" not in lower and "v4l2" not in lower:
            continue
        capture, output = _v4l2_caps(device)
        if capture or output:
            candidates.append((device, lower))
    phone = [device for device, label in candidates if "phonecam" in label]
    pool = phone or [device for device, _label in candidates]
    return pool[0] if pool else None


def camera_size(width: int, height: int) -> tuple[int, int]:
    """PhoneCam size for a phone frame: same shape, long edge capped."""
    scale = min(1.0, MAX_CAMERA_EDGE / max(width, height))
    return max(16, int(width * scale) & ~1), max(16, int(height * scale) & ~1)


def remember_size(size: tuple[int, int]) -> None:
    try:
        LAST_SIZE.write_text(f"{size[0]}x{size[1]}\n", encoding="utf-8")
    except OSError:
        pass


def remembered_size() -> tuple[int, int] | None:
    try:
        return parse_size(LAST_SIZE.read_text(encoding="utf-8").strip())
    except (OSError, argparse.ArgumentTypeError):
        return None


class Output:
    """The virtual camera. Accepts whole YUY2 frames of width x height."""

    def __init__(self, width: int, height: int) -> None:
        self._lock = threading.Lock()
        self.last_time = 0.0
        self._failed = False
        self._set_size(width, height)

    def _set_size(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.black = bytes((16, 128)) * (width * height)
        self.last: bytes | None = None

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def resize(self, width: int, height: int) -> bool:
        """Try to change the camera shape. False while an app holds the old one."""
        with self._lock:
            if (width, height) == self.size:
                return True
            return self._resize(width, height)

    def _resize(self, width: int, height: int) -> bool:
        return False

    def write(self, frame: bytes, keep: bool = True) -> None:
        with self._lock:
            if len(frame) != self.width * self.height * 2:
                return
            try:
                self._write(frame)
            except OSError as err:
                if not self._failed:
                    say(f"Virtual camera write failed: {err}")
                    self._failed = True
                return
            self._failed = False
            self.last_time = time.monotonic()
            self.last = frame if keep else None

    def _write(self, frame: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class V4L2Output(Output):
    def __init__(self, device: str, width: int, height: int, fps: int) -> None:
        self.device = device
        self.fps = fps
        self.fd = -1
        super().__init__(*self._open(width, height))

    def _open(self, width: int, height: int) -> tuple[int, int]:
        """Open the device asking for width x height; returns the size it really has.

        While an app is reading, v4l2loopback keeps the old size."""
        fd = os.open(self.device, os.O_RDWR)
        try:
            fmt = bytearray(208)
            struct.pack_into("<I", fmt, 0, V4L2_BUF_TYPE_VIDEO_OUTPUT)
            struct.pack_into(
                "<8I", fmt, 8,
                width, height, V4L2_PIX_FMT_YUYV, V4L2_FIELD_NONE,
                width * 2, width * height * 2, V4L2_COLORSPACE_SRGB, 0,
            )
            fcntl.ioctl(fd, VIDIOC_S_FMT, fmt)
            got = struct.unpack_from("<2I", fmt, 8)
            parm = bytearray(204)
            struct.pack_into("<I", parm, 0, V4L2_BUF_TYPE_VIDEO_OUTPUT)
            struct.pack_into("<4I", parm, 4, V4L2_CAP_TIMEPERFRAME, 0, 1, self.fps)
            try:
                fcntl.ioctl(fd, VIDIOC_S_PARM, parm)
            except OSError:
                pass
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        return got

    def in_use(self) -> bool:
        """True if any other process (Discord, OBS, …) has the device open."""
        me = str(os.getpid())
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or pid == me:
                continue
            fd_dir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    if os.readlink(f"{fd_dir}/{fd}") == self.device:
                        return True
                except OSError:
                    continue
        return False

    def _resize(self, width: int, height: int) -> bool:
        # Closing our end while an app is reading would make it drop the camera.
        if self.in_use():
            return False
        old = self.size
        os.close(self.fd)
        self.fd = -1
        try:
            got = self._open(width, height)
        except OSError:
            got = self._open(*old)
        self._set_size(*got)
        try:
            os.write(self.fd, self.black)
        except OSError:
            pass
        return got == (width, height)

    def _write(self, frame: bytes) -> None:
        os.write(self.fd, frame)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class PipeWireOutput(Output):
    def __init__(self, width: int, height: int, fps: int) -> None:
        super().__init__(width, height)
        props = ",".join(
            [
                "props",
                "media.class=Video/Source",
                "media.role=Camera",
                "node.name=phonecam",
                "node.description=PhoneCam",
                "node.nick=PhoneCam",
            ]
        )
        self.proc = spawn(
            [
                "gst-launch-1.0", "-q",
                "fdsrc", "fd=0", f"blocksize={width * height * 2}",
                "!", "rawvideoparse", "format=yuy2",
                f"width={width}", f"height={height}", f"framerate={fps}/1",
                "!", "pipewiresink", "mode=provide", "sync=false", "async=false",
                "client-name=PhoneCam", f"stream-properties={props}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        drain_stderr(self.proc, "camera")
        time.sleep(0.5)
        if self.proc.poll() is not None:
            raise OSError("the PipeWire camera exited")

    def _write(self, frame: bytes) -> None:
        if self.proc.poll() is not None or self.proc.stdin is None:
            raise OSError("the PipeWire camera exited")
        write_all(self.proc.stdin.fileno(), frame)

    def close(self) -> None:
        kill_proc(self.proc)


class Decoder:
    """One gst process: phone frames in, scaled YUY2 frames out."""

    def __init__(self, codec: str, width: int, height: int, on_frame) -> None:
        self.codec = codec
        self.size = (width, height)
        self.frame_size = width * height * 2
        self.on_frame = on_frame
        self.proc = spawn(
            [
                "gst-launch-1.0", "-q",
                "fdsrc", "fd=0", "blocksize=65536",
                "!", *DECODERS[codec],
                # Never letterbox: if the camera shape is stuck, crop to it instead.
                "!", "aspectratiocrop", f"aspect-ratio={width}/{height}",
                "!", "videoscale",
                "!", "videoconvert",
                "!", f"video/x-raw,format=YUY2,width={width},height={height},pixel-aspect-ratio=1/1",
                "!", "fdsink", "fd=1", "sync=false",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        assert self.proc.stdin is not None
        try:
            fcntl.fcntl(self.proc.stdin.fileno(), F_SETPIPE_SZ, 16384)
        except OSError:
            pass
        self._cond = threading.Condition()
        self._queue: collections.deque[tuple[bytes, float]] = collections.deque()
        self._closed = False
        self._need_key = codec == "h264"
        self.dropped = 0
        drain_stderr(self.proc, "decoder")
        threading.Thread(target=self._feed, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()

    @property
    def dead(self) -> bool:
        return not self._closed and self.proc.poll() is not None

    def push(self, data: bytes, key: bool) -> bool:
        """Queue one phone frame. False means the decoder needs a keyframe."""
        now = time.monotonic()
        with self._cond:
            if self._closed:
                return True
            if self.codec != "h264" or key:
                # Every image, and every H.264 keyframe, stands alone, so
                # anything still queued behind it is stale.
                self.dropped += len(self._queue)
                self._queue.clear()
                self._need_key = False
            elif self._need_key:
                self.dropped += 1
                return False
            self._queue.append((data, now))
            self._cond.notify()
            stale = now - self._queue[0][1] > MAX_QUEUE_AGE
            return not (self.codec == "h264" and stale)

    def take_dropped(self) -> int:
        with self._cond:
            count, self.dropped = self.dropped, 0
            return count

    def _feed(self) -> None:
        assert self.proc.stdin is not None
        fd = self.proc.stdin.fileno()
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                data, _arrived = self._queue.popleft()
            try:
                write_all(fd, data)
            except (OSError, ValueError):
                return

    def _read(self) -> None:
        assert self.proc.stdout is not None
        size = self.frame_size
        buf = bytearray(size)
        view = memoryview(buf)
        while True:
            got = 0
            try:
                while got < size:
                    count = self.proc.stdout.readinto(view[got:])
                    if not count:
                        return
                    got += count
            except (OSError, ValueError):
                return
            self.on_frame(self, bytes(buf))

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._cond.notify_all()
        kill_proc(self.proc)


class Session:
    """One connected phone."""

    def __init__(self, output: Output, send) -> None:
        self.output = output
        self.send = send
        self._lock = threading.Lock()
        self.decoder: Decoder | None = None
        self.config: tuple[str, int, int, int] | None = None
        self.wanted: tuple[int, int] | None = None
        self.closed = False
        self.live = False
        self.rx_frames = 0
        self.rx_bytes = 0
        self.out_frames = 0
        self._key_asked = 0.0
        self._key_wanted = False
        self._told_cropped = False
        self._reshape_lock = threading.RLock()

    def configure(self, codec: str, width: int, height: int, fps: int) -> None:
        if codec not in CODECS:
            raise ValueError("unknown codec")
        if not (16 <= width <= 8192 and 16 <= height <= 8192 and 1 <= fps <= 120):
            raise ValueError("bad stream size")
        with self._lock:
            self.config = (codec, width, height, fps)
            self.wanted = camera_size(width, height)
        say(f"Phone stream: {CODECS[codec]} {width}x{height} at {fps} fps")
        with self._reshape_lock:
            self.reshape(restart=False)
            self._start_decoder()

    def _start_decoder(self) -> None:
        with self._lock:
            config = self.config
        if config is None:
            return
        decoder = Decoder(config[0], self.output.width, self.output.height, self._frame)
        with self._lock:
            if self.closed:
                old = decoder
            else:
                old, self.decoder = self.decoder, decoder
                self._key_wanted = True
        if old is not None:
            old.close()

    def reshape(self, restart: bool = True) -> None:
        """Match the PC camera to the phone's frame shape when no app is holding it."""
        with self._reshape_lock:
            self._reshape(restart)

    def _reshape(self, restart: bool) -> None:
        wanted = self.wanted
        if wanted is None or self.closed:
            return
        before = self.output.size
        if self.output.resize(*wanted):
            if self.output.size != before:
                say(f"PhoneCam is now {wanted[0]}x{wanted[1]} to match the phone.")
                remember_size(self.output.size)
            self._told_cropped = False
        elif not self._told_cropped:
            self._told_cropped = True
            width, height = self.output.size
            say(
                f"An app has PhoneCam open at {width}x{height}, so the picture is cropped to fit. "
                "Turn the camera off and on in that app to switch shape."
            )
        decoder = self.decoder
        if restart and decoder is not None and decoder.size != self.output.size:
            self._start_decoder()

    def shape(self) -> dict[str, object]:
        width, height = self.output.size
        return {
            "type": "output",
            "width": width,
            "height": height,
            "cropped": self.wanted is not None and self.wanted != (width, height),
        }

    def take_keyframe_request(self) -> bool:
        with self._lock:
            wanted, self._key_wanted = self._key_wanted, False
        return wanted

    def _frame(self, decoder: Decoder, frame: bytes) -> None:
        with self._lock:
            if self.closed or decoder is not self.decoder:
                return
            self.out_frames += 1
            first = not self.live
            self.live = True
        self.output.write(frame)
        if first:
            say("PhoneCam is live. If Vesktop was already open, quit it fully and reopen it.")

    def push(self, payload: bytes) -> float | None:
        """Hand one phone frame to the decoder; returns its phone timestamp."""
        if len(payload) <= FRAME_HEADER.size:
            return None
        flags, sent_at = FRAME_HEADER.unpack_from(payload)
        with self._lock:
            decoder = self.decoder
            self.rx_frames += 1
            self.rx_bytes += len(payload)
        if decoder is not None and not decoder.push(payload[FRAME_HEADER.size :], bool(flags & 1)):
            self.ask_keyframe()
        return sent_at

    def ask_keyframe(self) -> None:
        now = time.monotonic()
        if now - self._key_asked >= 0.5:
            self._key_asked = now
            self.send({"type": "keyframe"})

    def tick(self, seconds: float) -> dict[str, object]:
        with self._lock:
            rx_frames, self.rx_frames = self.rx_frames, 0
            rx_bytes, self.rx_bytes = self.rx_bytes, 0
            out_frames, self.out_frames = self.out_frames, 0
            decoder = self.decoder
            config = self.config
        dropped = decoder.take_dropped() if decoder else 0
        if decoder is not None and decoder.dead and config and not self.closed:
            say("The decoder stopped. Restarting it.")
            self._start_decoder()
        seconds = max(seconds, 0.001)
        return {
            "type": "stats",
            "rx_fps": rx_frames / seconds,
            "out_fps": out_frames / seconds,
            "rx_mbps": rx_bytes * 8 / seconds / 1e6,
            "dropped": dropped,
        }

    def close(self) -> None:
        with self._lock:
            self.closed = True
            decoder, self.decoder = self.decoder, None
        if decoder is not None:
            decoder.close()


class Broker:
    """Only one phone drives PhoneCam at a time; the newest one wins."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active: Session | None = None

    def attach(self, session: Session) -> None:
        with self._lock:
            old, self.active = self.active, session
        if old is not None:
            old.close()

    def detach(self, session: Session) -> None:
        with self._lock:
            if self.active is session:
                self.active = None
        session.close()

    def close(self) -> None:
        with self._lock:
            old, self.active = self.active, None
        if old is not None:
            old.close()


def keep_alive(output: Output, broker: Broker, stop: threading.Event) -> None:
    """Keep frames flowing so viewers never stall: black when idle, else repeat the last frame.

    Also retries a pending camera reshape once the app holding PhoneCam lets go."""
    last_reshape = 0.0
    while not stop.wait(0.1):
        now = time.monotonic()
        session = broker.active
        if session is not None and now - last_reshape >= 1.0:
            last_reshape = now
            if session.wanted is not None and session.wanted != output.size:
                session.reshape()
        if session is None or not session.live:
            if now - output.last_time >= 0.2:
                output.write(output.black, keep=False)
        elif output.last is not None and now - output.last_time >= 1.0:
            output.write(output.last)


class Reader:
    def __init__(self, conn: ssl.SSLSocket, initial: bytes = b"") -> None:
        self.conn = conn
        self.buf = bytearray(initial)

    def read(self, size: int) -> bytes:
        while len(self.buf) < size:
            chunk = self.conn.recv(max(65536, size - len(self.buf)))
            if not chunk:
                raise EOFError
            self.buf += chunk
        out = bytes(self.buf[:size])
        del self.buf[:size]
        return out


def unmask(payload: bytes, mask: bytes) -> bytes:
    size = len(payload)
    if not size:
        return payload
    key = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(key, "little")).to_bytes(size, "little")


def read_frame(reader: Reader) -> tuple[int, bytes]:
    chunks: list[bytes] = []
    opcode = None
    total = 0
    while True:
        first, second = reader.read(2)
        op = first & 0x0F
        masked = second & 0x80
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", reader.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", reader.read(8))[0]
        total += length
        if total > MAX_FRAME:
            raise ValueError("frame too large")
        mask = reader.read(4) if masked else b""
        payload = reader.read(length) if length else b""
        if mask:
            payload = unmask(payload, mask)
        if op >= 0x8:
            return op, payload
        if opcode is None:
            opcode = op
        chunks.append(payload)
        if first & 0x80:
            return opcode, b"".join(chunks)


def write_frame(conn: ssl.SSLSocket, opcode: int, payload: bytes = b"") -> None:
    first = 0x80 | opcode
    size = len(payload)
    if size < 126:
        header = bytes((first, size))
    elif size < 65536:
        header = bytes((first, 126)) + struct.pack("!H", size)
    else:
        header = bytes((first, 127)) + struct.pack("!Q", size)
    conn.sendall(header + payload)


def send_http(conn: ssl.SSLSocket, status: int, reason: str, body: bytes, content_type: str) -> None:
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    conn.sendall(head + body)


def handle_ws(
    conn: ssl.SSLSocket,
    headers: dict[str, str],
    rest: bytes,
    broker: Broker,
    output: Output,
    code: str,
) -> None:
    key = headers.get("sec-websocket-key", "")
    if headers.get("upgrade", "").lower() != "websocket" or not key:
        send_http(conn, 400, "Bad Request", b"Expected a websocket.\n", "text/plain")
        return
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode()
    )
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(120)
    reader = Reader(conn, rest)

    # TLS sockets must not be written from two threads, so every send
    # happens here on the reading thread.
    def send(message: dict[str, object]) -> None:
        try:
            write_frame(conn, 0x1, json.dumps(message).encode())
        except (OSError, ssl.SSLError):
            pass

    session: Session | None = None
    last_stats = time.monotonic()
    last_shape: dict[str, object] | None = None
    try:
        while True:
            opcode, payload = read_frame(reader)
            if opcode == 0x8:
                return
            if opcode == 0x9:
                write_frame(conn, 0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if session is None:
                got = payload.decode("utf-8", "replace").strip()
                if opcode == 0x1 and len(got) == len(code) and hmac.compare_digest(got, code):
                    session = Session(output, send)
                    broker.attach(session)
                    write_frame(conn, 0x1, b"ok")
                    say("Phone connected.")
                    last_stats = time.monotonic()
                    last_shape = session.shape()
                    send(last_shape)
                else:
                    write_frame(conn, 0x1, b"no")
                    return
                continue
            if opcode == 0x2:
                sent_at = session.push(payload)
                if sent_at is not None:
                    send({"type": "ack", "t": sent_at})
            elif opcode == 0x1:
                try:
                    message = json.loads(payload)
                    kind = message.get("type")
                    if kind == "config":
                        session.configure(
                            str(message["codec"]),
                            int(message["width"]),
                            int(message["height"]),
                            int(message["fps"]),
                        )
                except (ValueError, KeyError, TypeError, AttributeError) as err:
                    say(f"Ignored a bad message from the phone: {err}")
            if session.take_keyframe_request():
                session.ask_keyframe()
            shape = session.shape()
            if shape != last_shape:
                send(shape)
                last_shape = shape
            now = time.monotonic()
            if now - last_stats >= 1.0:
                send(session.tick(now - last_stats))
                last_stats = now
    except (EOFError, TimeoutError, ConnectionError, OSError, ValueError, ssl.SSLError):
        return
    finally:
        if session is not None:
            broker.detach(session)
            say("Phone disconnected. Still watching.")


def handle_client(conn: ssl.SSLSocket, broker: Broker, output: Output, code: str) -> None:
    try:
        conn.settimeout(8)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
            if len(data) > 65536:
                return
        head, rest = data.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1", "replace").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return
        method, target = parts[0], parts[1]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        path = target.split("?", 1)[0]
        if path == "/ws":
            handle_ws(conn, headers, rest, broker, output, code)
            return
        if method == "GET" and path == "/":
            page = PAGE.replace("__OUTPUT__", f"{output.width}x{output.height}")
            send_http(conn, 200, "OK", page.encode(), "text/html; charset=utf-8")
            return
        send_http(conn, 404, "Not Found", b"Not found.\n", "text/plain; charset=utf-8")
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError):
        return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(
    host: str,
    port: int,
    broker: Broker,
    output: Output,
    code: str,
    stop: threading.Event,
    listening: list[socket.socket | None],
) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(CERT, KEY)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as err:
        raise SystemExit(f"Cannot listen on port {port}: {err}") from err
    sock.listen(8)
    sock.settimeout(0.5)
    listening[0] = sock
    try:
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                conn.settimeout(8)
                secure = context.wrap_socket(conn, server_side=True)
            except (OSError, ssl.SSLError):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(
                target=handle_client, args=(secure, broker, output, code), daemon=True
            ).start()
    finally:
        listening[0] = None
        try:
            sock.close()
        except OSError:
            pass


def banner(ip: str, port: int, code: str, others: list[str], output: Output) -> None:
    say()
    say("PhoneCam is watching this PC.")
    say(f"This PC's address:  {ip}")
    if others:
        say("Other addresses:    " + ", ".join(others))
    say()
    say("On the phone, open:")
    say(f"    https://{ip}:{port}/#{code}")
    say()
    say(f"Pairing code:       {code}")
    if isinstance(output, V4L2Output):
        say(f"Virtual camera:     {LABEL} ({output.device}, {output.width}x{output.height})")
    else:
        say(f"Virtual camera:     {LABEL} (PipeWire only, {output.width}x{output.height})")
    say()
    say("The browser will warn about the certificate. Continue anyway,")
    say("allow the camera, then tap Broadcast.")
    if isinstance(output, V4L2Output):
        say("Vesktop/Discord/OBS: select PhoneCam. If Vesktop was open")
        say("before this script started, quit it fully (tray too) and reopen it.")
        say("PhoneCam takes the shape of the phone's picture (upright or sideways).")
    else:
        say("Vesktop/Discord need a V4L2 webcam. PipeWire alone stays blank there.")
        say("Stop this script, then run once:")
        say(f"    sudo {ROOT / 'enable-v4l2-webcam.sh'}")
        say("Then start this script again.")
    say("No phone camera is broadcasting yet.")
    say()


def parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT, for example 1920x1080") from None
    if width < 16 or height < 16 or width % 2 or height % 2:
        raise argparse.ArgumentTypeError("width and height must be even and at least 16")
    return width, height


def main() -> None:
    parser = argparse.ArgumentParser(description="Phone browser camera to a virtual webcam.")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument(
        "--size", type=parse_size, default=None,
        help="starting camera size (default: the last shape used, else "
        f"{WIDTH}x{HEIGHT}); it then follows the phone",
    )
    parser.add_argument("--fps", type=int, default=FPS, help=f"advertised frame rate, default {FPS}")
    args = parser.parse_args()
    if not shutil.which("openssl") or not shutil.which("gst-launch-1.0"):
        raise SystemExit("phonecam needs gstreamer and openssl on PATH.")

    ips = local_ipv4s()
    ip = primary_ip(ips)
    say(f"This PC's local IP: {ip}")
    ensure_cert(ips)
    width, height = args.size or remembered_size() or (WIDTH, HEIGHT)
    device = find_v4l2_device()
    try:
        if device:
            say(f"Using V4L2 webcam device: {device}")
            output: Output = V4L2Output(device, width, height, args.fps)
        else:
            say("No V4L2 loopback device found. Vesktop/Discord will not show a picture.")
            say(f"Fix once with: sudo {ROOT / 'enable-v4l2-webcam.sh'}")
            output = PipeWireOutput(width, height, args.fps)
    except OSError as err:
        hint = (
            " Close other phonecam.py copies and apps using the camera (OBS, Discord), then try again."
            if device else " Is PipeWire running?"
        )
        raise SystemExit(f"Could not open the virtual camera: {err}.{hint}") from None
    output.write(output.black, keep=False)

    broker = Broker()
    stop = threading.Event()
    listening: list[socket.socket | None] = [None]
    presses = [0]

    def request_stop(signum: int, frame: object) -> None:
        presses[0] += 1
        stop.set()
        sock = listening[0]
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if presses[0] >= 2:
            kill_all()
            os._exit(130)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    threading.Thread(target=keep_alive, args=(output, broker, stop), daemon=True).start()

    code = f"{secrets.randbelow(1_000_000):06d}"
    banner(ip, args.port, code, [item for item in ips if item != ip], output)
    try:
        serve("0.0.0.0", args.port, broker, output, code, stop, listening)
    finally:
        stop.set()
        broker.close()
        output.close()
        say("Stopped.")


if __name__ == "__main__":
    main()
