#!/usr/bin/env python3
"""Local browser UI for the image generation API."""

from __future__ import annotations

import json
import mimetypes
import os
import base64
import argparse
import hmac
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
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def load_config() -> None:
    load_env_file(ENV_PATH)
    if not ENV_PATH.exists():
        load_env_file(ENV_EXAMPLE_PATH)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def split_hosts(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def host_without_port(value: str) -> str:
    value = value.split(",", 1)[0].strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    if value.count(":") > 1:
        return value
    return value.split(":", 1)[0]


def is_loopback_host(host: str) -> bool:
    return host_without_port(host) in LOOPBACK_HOSTS


def allowed_hosts() -> set[str]:
    return split_hosts(os.environ.get("ALLOWED_HOSTS"))


def public_request_host(host: str) -> bool:
    return not is_loopback_host(host)


def auth_config() -> tuple[str, str]:
    username = os.environ.get("APP_USERNAME", "admin").strip() or "admin"
    password = os.environ.get("APP_PASSWORD", "").strip()
    return username, password


def auth_required(host: str) -> bool:
    _username, password = auth_config()
    return bool(password) or public_request_host(host)


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
    send_common_headers(handler)
    handler.end_headers()
    handler.wfile.write(data)


def text_response(handler: BaseHTTPRequestHandler, content: str, content_type: str = "text/html; charset=utf-8") -> None:
    data = content.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    send_common_headers(handler)
    handler.end_headers()
    handler.wfile.write(data)


def send_common_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "same-origin")
    handler.send_header("Cache-Control", "no-store")


class ImageWebHandler(BaseHTTPRequestHandler):
    server_version = "ImageApiWeb/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def request_host(self) -> str:
        forwarded_host = self.headers.get("X-Forwarded-Host")
        return forwarded_host or self.headers.get("Host", "")

    def reject_forbidden_host(self) -> bool:
        hosts = allowed_hosts()
        if not hosts:
            return False

        host = host_without_port(self.request_host())
        if host in hosts or host in LOOPBACK_HOSTS:
            return False

        self.send_error(404)
        return True

    def reject_missing_password(self) -> bool:
        host = self.request_host()
        if not public_request_host(host):
            return False

        _username, password = auth_config()
        if password:
            return False

        json_response(
            self,
            {
                "error": (
                    "APP_PASSWORD is not configured. Set APP_USERNAME and "
                    "APP_PASSWORD before exposing this service publicly."
                )
            },
            status=503,
        )
        return True

    def reject_unauthorized(self) -> bool:
        host = self.request_host()
        if not auth_required(host):
            return False

        username, password = auth_config()
        if not password:
            return False

        header = self.headers.get("Authorization", "")
        prefix = "Basic "
        if header.startswith(prefix):
            try:
                decoded = base64.b64decode(header[len(prefix) :], validate=True).decode("utf-8")
                supplied_username, supplied_password = decoded.split(":", 1)
                if hmac.compare_digest(supplied_username, username) and hmac.compare_digest(
                    supplied_password,
                    password,
                ):
                    return False
            except (ValueError, UnicodeDecodeError):
                pass

        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Image2YRL", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"Authentication required.")
        return True

    def ensure_access(self) -> bool:
        if self.reject_forbidden_host():
            return False
        if self.reject_missing_password():
            return False
        if self.reject_unauthorized():
            return False
        return True

    def do_GET(self) -> None:
        if not self.ensure_access():
            return

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
        if not self.ensure_access():
            return

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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=3600")
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
    html { min-height:100%; }
    body { margin:0; min-height:100vh; min-height:100dvh; background:var(--bg); color:var(--text); font:14px/1.45 "Segoe UI", Arial, sans-serif; }
    .app { display:grid; grid-template-columns: 390px minmax(0, 1fr); min-height:100vh; min-height:100dvh; }
    aside { background:var(--panel); border-right:1px solid var(--line); padding:18px; overflow:auto; }
    main { display:grid; grid-template-rows:minmax(0, 1fr) auto; min-width:0; min-height:0; }
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
    @media (max-width: 860px) {
      body { font-size:15px; }
      .app { display:block; min-height:100vh; min-height:100dvh; }
      aside { border-right:0; border-bottom:1px solid var(--line); padding:14px; overflow:visible; }
      main { min-height:60vh; min-height:60dvh; grid-template-rows:minmax(52vh, 1fr) auto; grid-template-rows:minmax(52dvh, 1fr) auto; }
      h1 { margin-bottom:12px; font-size:18px; }
      label { margin:10px 0 5px; }
      textarea { min-height:128px; }
      textarea, input, select, button { min-height:42px; font-size:16px; }
      .row { grid-template-columns:1fr; gap:0; }
      .actions { grid-template-columns:1fr; }
      .stage { min-height:52vh; min-height:52dvh; padding:10px; }
      .strip { padding:8px 10px; }
      .thumb { width:96px; height:64px; margin-right:6px; }
      .refs { min-height:68px; }
    }
    @media (max-width: 480px) {
      aside { padding:12px; }
      textarea { min-height:112px; }
      .stage { min-height:48vh; min-height:48dvh; }
      main { grid-template-rows:minmax(48vh, 1fr) auto; grid-template-rows:minmax(48dvh, 1fr) auto; }
      .thumb { width:84px; height:58px; }
    }
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
        const img = document.createElement("img");
        img.src = image.data;
        img.title = image.name;
        const button = document.createElement("button");
        button.type = "button";
        button.title = "Remove";
        button.textContent = "x";
        button.addEventListener("click", () => {
          referenceImages.splice(index, 1);
          renderReferences();
        });
        item.append(img, button);
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
      $("imageHost").innerHTML = "";
      const img = document.createElement("img");
      img.className = "image";
      img.src = image.url;
      img.alt = image.name;
      $("imageHost").appendChild(img);
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Image2YRL browser web app.")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Host interface to bind. Use 0.0.0.0 for deployment.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ["PORT"]) if os.environ.get("PORT") else None,
        help="Port to listen on. Defaults to PORT env var or a free local port.",
    )
    return parser


def should_open_browser(host: str) -> bool:
    if "APP_OPEN_BROWSER" in os.environ:
        return env_bool("APP_OPEN_BROWSER")
    return is_loopback_host(host)


def main() -> None:
    args = build_parser().parse_args()
    load_config()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    host = args.host
    port = args.port or find_free_port()
    server = ThreadingHTTPServer((host, port), ImageWebHandler)
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{port}/"
    print(f"Serving Image2YRL on {url}", flush=True)
    if should_open_browser(host):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
