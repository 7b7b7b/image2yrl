#!/usr/bin/env python3
"""Local browser UI for the image generation API."""

from __future__ import annotations

import json
import mimetypes
import os
import base64
import socket
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from image_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ApiError,
    extract_image_item,
    generate_images,
    load_env_file,
    normalize_base_url,
    parse_extra_json,
    request_json,
    safe_filename,
    validate_api_key,
    write_unique,
)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
RESOURCE_DIR = resource_dir()
ENV_PATH = APP_DIR / ".env"
ENV_EXAMPLE_PATH = RESOURCE_DIR / ".env.example"
OUTPUT_DIR = APP_DIR / "outputs"
HISTORY_PATH = OUTPUT_DIR / "history.json"
HISTORY_LOCK = threading.Lock()
SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536", "2048x2048", "2048x1152", "3840x2160", "2160x3840")
QUALITIES = ("high", "medium", "low", "auto")


def load_config() -> None:
    load_env_file(ENV_PATH)
    if not ENV_PATH.exists():
        load_env_file(ENV_EXAMPLE_PATH)


def image_payload(path: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = metadata or {}
    return {
        "name": path.name,
        "url": f"/outputs/{urllib.parse.quote(path.name)}",
        "path": str(path),
        "prompt": str(metadata.get("prompt") or ""),
        "model": str(metadata.get("model") or ""),
        "size": str(metadata.get("size") or ""),
        "quality": str(metadata.get("quality") or ""),
        "createdAt": str(metadata.get("createdAt") or ""),
    }


def read_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def write_history(items: list[dict[str, Any]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(items[:500], ensure_ascii=False, indent=2), encoding="utf-8")


def history_items() -> list[dict[str, Any]]:
    with HISTORY_LOCK:
        stored = read_history()

    by_name: dict[str, dict[str, Any]] = {}
    for item in stored:
        name = str(item.get("name") or "")
        path = OUTPUT_DIR / name
        if name and path.is_file():
            by_name[name] = image_payload(path, item)

    image_paths = sorted(
        [
            path
            for path in OUTPUT_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if OUTPUT_DIR.exists() else []

    for path in image_paths:
        by_name.setdefault(path.name, image_payload(path))

    return list(by_name.values())


def add_history(paths: list[Path], metadata: dict[str, Any]) -> list[dict[str, Any]]:
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    new_items = [
        {
            **metadata,
            "name": path.name,
            "path": str(path),
            "createdAt": created_at,
        }
        for path in paths
    ]
    with HISTORY_LOCK:
        existing = read_history()
        existing_names = {str(item.get("name") or "") for item in new_items}
        merged = new_items + [item for item in existing if str(item.get("name") or "") not in existing_names]
        write_history(merged)
    return [image_payload(path, item) for path, item in zip(paths, new_items)]


def decode_data_url_image(item: dict[str, Any]) -> tuple[str, str, bytes]:
    name = safe_filename(str(item.get("name") or "reference"), "reference")
    content_type = str(item.get("type") or "application/octet-stream")
    data_url = str(item.get("data") or "")
    marker = ";base64,"
    if not data_url.startswith("data:") or marker not in data_url:
        raise ApiError(f"Reference image {name} is not a base64 data URL.")
    header, encoded = data_url.split(marker, 1)
    if header.startswith("data:"):
        content_type = header.removeprefix("data:") or content_type
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ApiError(f"Reference image {name} is not valid base64.") from exc
    if not data:
        raise ApiError(f"Reference image {name} is empty.")
    return name, content_type, data


def request_multipart(
    url: str,
    api_key: str,
    fields: dict[str, str],
    files: list[tuple[str, str, str, bytes]],
    timeout: float,
) -> dict[str, Any]:
    boundary = f"----ImageApiClient{time.time_ns()}"
    chunks: list[bytes] = []

    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )

    for field, filename, content_type, data in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{field}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                data,
                b"\r\n",
            ]
        )

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Request failed: {exc.reason}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"Response is not JSON: {raw[:500]}") from exc
    if not isinstance(parsed, dict):
        raise ApiError(f"Expected JSON object, got: {type(parsed).__name__}")
    return parsed


def generate_image_edits(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    reference_images: list[dict[str, Any]],
    size: str,
    quality: str,
    n: int,
    extra: dict[str, Any],
    output_dir: Path,
    name: str | None,
    timeout: float,
) -> list[Path]:
    files: list[tuple[str, str, str, bytes]] = []
    field_name = "image" if len(reference_images) == 1 else "image[]"
    for item in reference_images:
        filename, content_type, data = decode_data_url_image(item)
        files.append((field_name, filename, content_type, data))

    fields = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": str(n),
    }
    for key, value in extra.items():
        if isinstance(value, (dict, list)):
            fields[key] = json.dumps(value, ensure_ascii=False)
        else:
            fields[key] = str(value)

    result = request_multipart(
        f"{normalize_base_url(base_url)}/images/edits",
        api_key,
        fields,
        files,
        timeout,
    )
    data = result.get("data")
    if not isinstance(data, list) or not data:
        raise ApiError("Response does not contain a non-empty data array")

    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = safe_filename(name or prompt)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    saved_paths: list[Path] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ApiError(f"Expected image item to be an object, got {type(item).__name__}")
        image_data, extension = extract_image_item(item, timeout)
        saved_paths.append(write_unique(output_dir / f"{prefix}-{timestamp}-{index}{extension}", image_data))
    return saved_paths


def read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ApiError("Request body must be a JSON object.")
    return data


def json_response(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def text_response(handler: BaseHTTPRequestHandler, content: str, content_type: str = "text/html; charset=utf-8") -> None:
    data = content.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class ImageWebHandler(BaseHTTPRequestHandler):
    server_version = "ImageApiWeb/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            text_response(self, INDEX_HTML)
            return
        if parsed.path == "/api/config":
            self.handle_config()
            return
        if parsed.path == "/api/models":
            self.handle_models()
            return
        if parsed.path == "/api/history":
            self.handle_history()
            return
        if parsed.path.startswith("/outputs/"):
            self.handle_output_file(parsed.path)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/generate":
            self.handle_generate()
            return
        self.send_error(404)

    def handle_config(self) -> None:
        json_response(
            self,
            {
                "baseUrl": os.environ.get("IMAGE_API_BASE", DEFAULT_BASE_URL),
                "model": os.environ.get("IMAGE_MODEL", DEFAULT_MODEL),
                "sizes": SIZES,
                "qualities": QUALITIES,
                "defaultSize": "auto",
                "defaultQuality": "high",
            },
        )

    def handle_models(self) -> None:
        try:
            api_key = os.environ.get("IMAGE_API_KEY", "").strip()
            base_url = normalize_base_url(os.environ.get("IMAGE_API_BASE", DEFAULT_BASE_URL))
            validate_api_key(api_key)
            result = request_json("GET", f"{base_url}/models", api_key, timeout=60)
            data = result.get("data")
            models: list[str] = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        models.append(item["id"])
            image_models = [model for model in models if "image" in model]
            json_response(self, {"models": image_models or models})
        except Exception as exc:
            json_response(self, {"error": str(exc)}, status=400)

    def handle_history(self) -> None:
        json_response(self, {"images": history_items()})

    def handle_generate(self) -> None:
        try:
            body = read_body(self)
            prompt = str(body.get("prompt", "")).strip()
            if not prompt:
                raise ApiError("Prompt is empty.")

            api_key = os.environ.get("IMAGE_API_KEY", "").strip()
            base_url = normalize_base_url(os.environ.get("IMAGE_API_BASE", DEFAULT_BASE_URL))
            model = str(body.get("model") or os.environ.get("IMAGE_MODEL", DEFAULT_MODEL)).strip()
            size = str(body.get("size") or "auto").strip()
            quality = str(body.get("quality") or "high").strip()
            count = max(1, min(int(body.get("count") or 1), 4))
            name = str(body.get("name") or "").strip() or None
            extra_raw = str(body.get("extra") or "").strip()
            raw_reference_images = body.get("referenceImages") or []
            if not isinstance(raw_reference_images, list):
                raise ApiError("referenceImages must be an array.")
            reference_images = [
                item for item in raw_reference_images if isinstance(item, dict) and item.get("data")
            ]

            validate_api_key(api_key)
            extra = parse_extra_json(extra_raw) if extra_raw else {}
            if reference_images:
                paths = generate_image_edits(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    prompt=prompt,
                    reference_images=reference_images,
                    size=size,
                    quality=quality,
                    n=count,
                    extra=extra,
                    output_dir=OUTPUT_DIR,
                    name=name,
                    timeout=240,
                )
            else:
                paths = generate_images(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    n=count,
                    extra=extra,
                    output_dir=OUTPUT_DIR,
                    name=name,
                    timeout=240,
                )
            images = add_history(
                paths,
                {
                    "prompt": prompt,
                    "model": model,
                    "size": size,
                    "quality": quality,
                    "extra": extra_raw,
                    "mode": "edit" if reference_images else "generate",
                    "referenceNames": [
                        str(item.get("name") or "reference") for item in reference_images
                    ],
                },
            )
            json_response(
                self,
                {
                    "images": images,
                    "history": history_items(),
                },
            )
        except Exception as exc:
            json_response(self, {"error": str(exc)}, status=400)

    def handle_output_file(self, url_path: str) -> None:
        name = urllib.parse.unquote(url_path.removeprefix("/outputs/"))
        path = (OUTPUT_DIR / name).resolve()
        try:
            path.relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not path.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Image API Client 杨苒琳专用</title>
  <style>
    :root { color-scheme: light; --bg:#f4f6f8; --panel:#ffffff; --line:#d8dee6; --text:#111827; --muted:#64748b; --accent:#2563eb; --accent-dark:#1d4ed8; }
    * { box-sizing: border-box; }
    body { margin:0; min-height:100vh; background:var(--bg); color:var(--text); font:14px/1.45 "Segoe UI", Arial, sans-serif; }
    .app { display:grid; grid-template-columns: 390px minmax(0, 1fr); min-height:100vh; }
    aside { background:var(--panel); border-right:1px solid var(--line); padding:18px; overflow:auto; }
    main { display:grid; grid-template-rows:minmax(0, 1fr) auto; min-width:0; }
    h1 { margin:0 0 16px; font-size:20px; font-weight:650; letter-spacing:0; }
    label { display:block; margin:12px 0 6px; font-weight:600; color:#243041; }
    textarea, input, select { width:100%; border:1px solid var(--line); border-radius:6px; background:#fff; color:var(--text); padding:9px 10px; font:inherit; }
    textarea { min-height:170px; resize:vertical; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .actions { display:grid; grid-template-columns:1fr auto; gap:10px; margin-top:16px; }
    button { border:1px solid var(--line); border-radius:6px; background:#fff; color:#172033; padding:9px 12px; font:inherit; cursor:pointer; }
    button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:650; }
    button.primary:hover { background:var(--accent-dark); }
    button:disabled { opacity:.6; cursor:default; }
    .meta { margin-top:12px; color:var(--muted); font-size:12px; word-break:break-all; }
    .refs { display:flex; gap:8px; overflow-x:auto; padding:8px; border:1px solid var(--line); border-radius:6px; min-height:72px; background:#f8fafc; }
    .refs:empty::before { content:"No reference images"; color:var(--muted); font-size:12px; align-self:center; }
    .ref { position:relative; flex:0 0 auto; width:76px; height:56px; }
    .ref img { width:100%; height:100%; object-fit:cover; border-radius:5px; border:1px solid var(--line); }
    .ref button { position:absolute; right:3px; top:3px; width:20px; height:20px; padding:0; border-radius:999px; line-height:18px; background:rgba(15,23,42,.78); color:#fff; border:0; }
    .stage { position:relative; min-width:0; min-height:0; display:grid; place-items:center; padding:18px; background:#0f172a; overflow:hidden; }
    .image-host { width:100%; height:100%; min-width:0; min-height:0; display:grid; place-items:center; }
    .empty { color:#cbd5e1; text-align:center; }
    .image { max-width:100%; max-height:100%; width:auto; height:auto; object-fit:contain; display:block; box-shadow:0 12px 40px rgba(0,0,0,.35); }
    .overlay { position:absolute; inset:0; display:none; place-items:center; background:rgba(15,23,42,.62); backdrop-filter: blur(2px); }
    .overlay.active { display:grid; }
    .loader { display:grid; gap:14px; place-items:center; color:#f8fafc; font-weight:650; }
    .spinner { width:42px; height:42px; border-radius:999px; border:4px solid rgba(255,255,255,.26); border-top-color:#fff; animation:spin .8s linear infinite; }
    @keyframes spin { to { transform:rotate(360deg); } }
    .strip { background:var(--panel); border-top:1px solid var(--line); padding:10px 14px; overflow-x:auto; white-space:nowrap; }
    .strip:empty::before { content:"No history yet"; color:var(--muted); font-size:12px; }
    .thumb { width:112px; height:74px; object-fit:cover; border:2px solid transparent; border-radius:6px; margin-right:8px; vertical-align:middle; cursor:pointer; background:#e5e7eb; }
    .thumb.active { border-color:var(--accent); }
    .error { color:#b91c1c; margin-top:10px; white-space:pre-wrap; }
    @media (max-width: 860px) { .app { grid-template-columns:1fr; grid-template-rows:auto minmax(60vh, 1fr); } aside { border-right:0; border-bottom:1px solid var(--line); } }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <h1>Image API Client 杨苒琳专用</h1>
      <label for="prompt">Prompt</label>
      <textarea id="prompt" placeholder="输入图片生成提示词"></textarea>
      <label for="refsInput">Reference Images</label>
      <input id="refsInput" type="file" accept="image/*" multiple>
      <div class="refs" id="refs"></div>
      <div class="row">
        <div><label for="size">Size</label><select id="size"></select></div>
        <div><label for="quality">Quality</label><select id="quality"></select></div>
      </div>
      <div class="row">
        <div><label for="count">Count</label><input id="count" type="number" min="1" max="4" value="1"></div>
        <div><label for="name">Name</label><input id="name" placeholder="可选文件名前缀"></div>
      </div>
      <label for="model">Model</label>
      <input id="model">
      <label for="extra">Extra</label>
      <input id="extra" placeholder='例如 {"background":"transparent"}'>
      <div class="actions">
        <button id="generate" class="primary">Generate</button>
        <button id="models">Models</button>
      </div>
      <div id="status" class="meta">Ready</div>
      <div id="error" class="error"></div>
    </aside>
    <main>
      <section class="stage" id="stage">
        <div class="image-host" id="imageHost"><div class="empty">Generated images will appear here</div></div>
        <div class="overlay" id="loadingOverlay">
          <div class="loader"><div class="spinner"></div><div>Generating...</div></div>
        </div>
      </section>
      <section class="strip" id="strip"></section>
    </main>
  </div>
  <script>
    const $ = id => document.getElementById(id);
    let images = [];
    let active = -1;
    let referenceImages = [];

    function setStatus(text) { $("status").textContent = text; }
    function setError(text) { $("error").textContent = text || ""; }
    function setLoading(loading) { $("loadingOverlay").classList.toggle("active", loading); }
    function renderReferences() {
      $("refs").innerHTML = "";
      referenceImages.forEach((image, index) => {
        const item = document.createElement("div");
        item.className = "ref";
        item.innerHTML = `<img src="${image.data}" title="${image.name}"><button type="button" title="Remove">x</button>`;
        item.querySelector("button").addEventListener("click", () => {
          referenceImages.splice(index, 1);
          renderReferences();
        });
        $("refs").appendChild(item);
      });
    }
    async function addReferenceFiles(files) {
      const reads = [...files].map(file => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve({name: file.name, type: file.type || "application/octet-stream", data: reader.result});
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      }));
      referenceImages = referenceImages.concat(await Promise.all(reads)).slice(0, 6);
      renderReferences();
    }
    function fillSelect(select, values, selected) {
      select.innerHTML = "";
      values.forEach(value => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        if (value === selected) option.selected = true;
        select.appendChild(option);
      });
    }
    function showImage(index) {
      active = index;
      const image = images[index];
      $("imageHost").innerHTML = `<img class="image" src="${image.url}" alt="${image.name}">`;
      [...$("strip").querySelectorAll("img")].forEach((img, i) => img.classList.toggle("active", i === index));
      if (image.prompt) $("prompt").value = image.prompt;
      if (image.model) $("model").value = image.model;
      if (image.size) $("size").value = image.size;
      if (image.quality) $("quality").value = image.quality;
      setStatus(`${image.name} | ${image.path}`);
    }
    function renderHistory(newImages, selectedIndex = 0) {
      images = newImages;
      $("strip").innerHTML = "";
      images.forEach((image, index) => {
        const thumb = document.createElement("img");
        thumb.className = "thumb";
        thumb.src = image.url;
        thumb.title = image.path;
        thumb.addEventListener("click", () => showImage(index));
        $("strip").appendChild(thumb);
      });
      if (images.length && selectedIndex >= 0) showImage(Math.min(selectedIndex, images.length - 1));
    }
    async function loadConfig() {
      const res = await fetch("/api/config");
      const config = await res.json();
      $("model").value = config.model;
      fillSelect($("size"), config.sizes, config.defaultSize);
      fillSelect($("quality"), config.qualities, config.defaultQuality);
    }
    async function loadHistory() {
      const res = await fetch("/api/history");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to load history");
      renderHistory(data.images || [], -1);
    }
    async function generate() {
      setError("");
      $("generate").disabled = true;
      setLoading(true);
      setStatus(referenceImages.length ? "Editing from reference image..." : "Generating...");
      try {
        const res = await fetch("/api/generate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            prompt: $("prompt").value,
            size: $("size").value,
            quality: $("quality").value,
            count: $("count").value,
            name: $("name").value,
            model: $("model").value,
            extra: $("extra").value,
            referenceImages
          })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Request failed");
        renderHistory(data.history || data.images || [], 0);
      } catch (err) {
        setError(err.message);
        setStatus("Error");
      } finally {
        $("generate").disabled = false;
        setLoading(false);
      }
    }
    async function loadModels() {
      setError("");
      setStatus("Loading models...");
      try {
        const res = await fetch("/api/models");
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Request failed");
        alert(data.models.join("\n") || "No models returned");
        setStatus("Ready");
      } catch (err) {
        setError(err.message);
        setStatus("Error");
      }
    }
    $("generate").addEventListener("click", generate);
    $("models").addEventListener("click", loadModels);
    $("refsInput").addEventListener("change", event => {
      addReferenceFiles(event.target.files).catch(err => setError(err.message));
      event.target.value = "";
    });
    Promise.all([loadConfig(), loadHistory()]).catch(err => setError(err.message));
  </script>
</body>
</html>
"""


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    load_config()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), ImageWebHandler)
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
