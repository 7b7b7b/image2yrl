#!/usr/bin/env python3
"""Small command line client for an OpenAI-compatible image API."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://45.59.101.161:8083/v1"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_ENV_EXAMPLE_FILE = Path(".env.example")


class ApiError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def validate_api_key(api_key: str) -> None:
    if any(ord(char) > 127 for char in api_key):
        raise ApiError(
            "API key contains non-ASCII characters. "
            "Replace the placeholder in .env with your real key."
        )

    if any(char.isspace() for char in api_key):
        raise ApiError("API key contains whitespace. Check IMAGE_API_KEY in .env.")

    if api_key.lower() in {"your_api_key", "replace_with_your_api_key"}:
        raise ApiError("IMAGE_API_KEY is still a placeholder. Put your real API key in .env.")


def request_json(
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 120,
) -> dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Request failed: {exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApiError(f"Response is not JSON: {body[:500]}") from exc

    if not isinstance(parsed, dict):
        raise ApiError(f"Expected JSON object, got: {type(parsed).__name__}")

    return parsed


def list_models(args: argparse.Namespace) -> int:
    result = request_json(
        "GET",
        f"{args.base_url}/models",
        args.api_key,
        timeout=args.timeout,
    )

    data = result.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                print(item.get("id", json.dumps(item, ensure_ascii=False)))
            else:
                print(item)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    return 0


def parse_extra_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"--extra must be valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ApiError("--extra must be a JSON object")

    return parsed


def safe_filename(text: str, fallback: str = "image") -> str:
    text = re.sub(r"\s+", "-", text.strip().lower())
    text = re.sub(r"[^a-z0-9._-]+", "", text)
    return text[:80].strip(".-_") or fallback


def extension_from_content_type(content_type: str | None) -> str:
    if not content_type:
        return ".png"

    mime = content_type.split(";", 1)[0].strip().lower()
    return mimetypes.guess_extension(mime) or ".png"


def extension_from_bytes(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def write_unique(path: Path, data: bytes) -> Path:
    if not path.exists():
        path.write_bytes(data)
        return path

    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            candidate.write_bytes(data)
            return candidate

    raise ApiError(f"Could not create a unique filename near {path}")


def decode_data_url(value: str) -> tuple[bytes, str]:
    header, encoded = value.split(",", 1)
    content_type = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
    return base64.b64decode(encoded), extension_from_content_type(content_type)


def download_image(url: str, timeout: float) -> tuple[bytes, str]:
    if url.startswith("data:"):
        return decode_data_url(url)

    request = urllib.request.Request(url, headers={"Accept": "image/*,*/*"})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            extension = extension_from_content_type(response.headers.get("Content-Type"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"Image download failed with HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Image download failed: {exc.reason}") from exc

    return data, extension


def extract_image_item(item: dict[str, Any], timeout: float) -> tuple[bytes, str]:
    if isinstance(item.get("b64_json"), str):
        data = base64.b64decode(item["b64_json"])
        return data, extension_from_bytes(data)

    if isinstance(item.get("url"), str):
        return download_image(item["url"], timeout)

    if isinstance(item.get("image"), str):
        value = item["image"]
        if value.startswith("http://") or value.startswith("https://") or value.startswith("data:"):
            return download_image(value, timeout)
        data = base64.b64decode(value)
        return data, extension_from_bytes(data)

    raise ApiError(f"Cannot find image data in item: {json.dumps(item, ensure_ascii=False)[:500]}")


def generate_images(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    size: str | None = "1024x1024",
    n: int = 1,
    quality: str | None = None,
    style: str | None = None,
    response_format: str | None = None,
    extra: dict[str, Any] | None = None,
    output_dir: Path = Path("outputs"),
    name: str | None = None,
    timeout: float = 120,
) -> list[Path]:
    validate_api_key(api_key)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": n,
    }

    if size:
        payload["size"] = size
    if quality:
        payload["quality"] = quality
    if style:
        payload["style"] = style
    if response_format:
        payload["response_format"] = response_format
    if extra:
        payload.update(extra)

    result = request_json(
        "POST",
        f"{normalize_base_url(base_url)}/images/generations",
        api_key,
        payload=payload,
        timeout=timeout,
    )

    data = result.get("data")
    if not isinstance(data, list) or not data:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise ApiError("Response does not contain a non-empty data array")

    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = safe_filename(name or prompt)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    saved_paths: list[Path] = []

    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ApiError(f"Expected image item to be an object, got {type(item).__name__}")

        image_data, extension = extract_image_item(item, timeout)
        filename = f"{prefix}-{timestamp}-{index}{extension}"
        saved_paths.append(write_unique(output_dir / filename, image_data))

    return saved_paths


def generate_image(args: argparse.Namespace) -> int:
    saved_paths = generate_images(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        prompt=args.prompt,
        size=args.size,
        n=args.n,
        quality=args.quality,
        style=args.style,
        response_format=args.response_format,
        extra=parse_extra_json(args.extra),
        output_dir=args.output_dir,
        name=args.name,
        timeout=args.timeout,
    )

    for path in saved_paths:
        print(path)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Command line client for an OpenAI-compatible image API."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Load environment variables from this file if it exists.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("IMAGE_API_BASE", DEFAULT_BASE_URL),
        help=f"API base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("IMAGE_API_KEY"),
        help="API key. Prefer setting IMAGE_API_KEY instead of passing this on the command line.",
    )
    parser.add_argument("--timeout", type=float, default=120, help="Request timeout in seconds.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    models = subparsers.add_parser("models", help="List available models.")
    models.set_defaults(func=list_models)

    generate = subparsers.add_parser("generate", help="Generate images from a prompt.")
    generate.add_argument("prompt", help="Text prompt for image generation.")
    generate.add_argument("--model", default=os.environ.get("IMAGE_MODEL", DEFAULT_MODEL))
    generate.add_argument("--size", default="1024x1024")
    generate.add_argument("--n", type=int, default=1)
    generate.add_argument("--quality", help="Optional quality parameter, if supported by the API.")
    generate.add_argument("--style", help="Optional style parameter, if supported by the API.")
    generate.add_argument(
        "--response-format",
        choices=["url", "b64_json"],
        help="Ask the API to return URL or base64 images, if supported.",
    )
    generate.add_argument(
        "--extra",
        help='Extra JSON object merged into the request body, for provider-specific options.',
    )
    generate.add_argument("--output-dir", type=Path, default=Path("outputs"))
    generate.add_argument("--name", help="Filename prefix. Defaults to a cleaned prompt.")
    generate.set_defaults(func=generate_image)

    return parser


def main() -> int:
    parser = build_parser()
    pre_args, _ = parser.parse_known_args()
    load_env_file(pre_args.env_file)
    if pre_args.env_file == DEFAULT_ENV_FILE and not pre_args.env_file.exists():
        load_env_file(DEFAULT_ENV_EXAMPLE_FILE)

    parser = build_parser()
    args = parser.parse_args()
    args.base_url = normalize_base_url(args.base_url)

    if not args.api_key:
        parser.error("missing API key. Set IMAGE_API_KEY or pass --api-key.")

    try:
        validate_api_key(args.api_key)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        return args.func(args)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
