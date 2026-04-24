const DEFAULT_BASE_URL = "http://image-api.wormforce.net:8083/v1";
const DEFAULT_MODEL = "gpt-image-2";
const DEFAULT_USERNAME = "admin";
const MAX_COUNT = 4;
const MAX_REFERENCES = 6;
const MAX_REFERENCE_BYTES = 10 * 1024 * 1024;
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
const SIZES = [
  "auto",
  "1024x1024",
  "1536x1024",
  "1024x1536",
  "2048x2048",
  "2048x1152",
  "3840x2160",
  "2160x3840",
];
const QUALITIES = ["high", "medium", "low", "auto"];

export default {
  async fetch(request, env) {
    try {
      const access = ensureAccess(request, env);
      if (access) return access;

      const url = new URL(request.url);

      if (request.method === "GET" && url.pathname === "/") {
        return htmlResponse(INDEX_HTML);
      }
      if (request.method === "GET" && url.pathname === "/api/config") {
        return jsonResponse({
          model: env.IMAGE_MODEL || DEFAULT_MODEL,
          sizes: SIZES,
          qualities: QUALITIES,
          defaultSize: "auto",
          defaultQuality: "high",
        });
      }
      if (request.method === "GET" && url.pathname === "/api/models") {
        return handleModels(env);
      }
      if (request.method === "GET" && url.pathname === "/api/image") {
        return handleImageProxy(request, env);
      }
      if (request.method === "POST" && url.pathname === "/api/generate") {
        return handleGenerate(request, env);
      }

      return new Response("Not Found", { status: 404, headers: commonHeaders() });
    } catch (error) {
      return jsonResponse({ error: friendlyError(error) }, 500);
    }
  },
};

function commonHeaders() {
  return {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
  };
}

function htmlResponse(html) {
  return new Response(html, {
    headers: {
      ...commonHeaders(),
      "Content-Type": "text/html; charset=utf-8",
    },
  });
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      ...commonHeaders(),
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

function friendlyError(error) {
  const message = error?.message || String(error);
  if (/string did not match the expected pattern|load failed|fetch failed|networkerror/i.test(message)) {
    return [
      "生成请求的网络链路临时失败。",
      "这通常发生在 Cloudflare Worker 连接上游图片 API、等待长时间生成、或加载远程图片 URL 时。",
      "请先重试一次；如果频繁出现，建议把上游 API 放到 HTTPS/443 或更稳定的服务上。",
      `原始错误：${message}`,
    ].join("\n");
  }
  return message;
}

function normalizeBaseUrl(baseUrl) {
  return (baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
}

function assertFetchableApiBase(baseUrl) {
  let parsed;
  try {
    parsed = new URL(normalizeBaseUrl(baseUrl));
  } catch {
    throw new Error(`IMAGE_API_BASE is not a valid URL: ${baseUrl}`);
  }

  const hostname = parsed.hostname;
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostname) || hostname.includes(":")) {
    throw new Error(
      "Cloudflare Workers cannot call the image API through a raw IP address. " +
        "Create a DNS-only A record such as image-api.wormforce.net -> 45.59.101.161, " +
        "then set IMAGE_API_BASE to http://image-api.wormforce.net:8083/v1.",
    );
  }
}

function hostWithoutPort(host) {
  const firstHost = (host || "").split(",", 1)[0].trim().toLowerCase();
  if (!firstHost) return "";
  if (firstHost.startsWith("[")) return firstHost.split("]", 1)[0] + "]";
  if (firstHost.split(":").length > 2) return firstHost;
  return firstHost.split(":", 1)[0];
}

function splitHosts(raw) {
  return new Set(
    String(raw || "")
      .split(",")
      .map((host) => host.trim().toLowerCase())
      .filter(Boolean),
  );
}

function ensureAccess(request, env) {
  const url = new URL(request.url);
  const requestHost = hostWithoutPort(request.headers.get("Host") || url.host);
  const allowedHosts = splitHosts(env.ALLOWED_HOSTS || "");

  if (allowedHosts.size && !allowedHosts.has(requestHost) && !LOOPBACK_HOSTS.has(requestHost)) {
    return new Response("Not Found", { status: 404, headers: commonHeaders() });
  }

  if (!env.APP_PASSWORD) {
    return jsonResponse(
      {
        error:
          "APP_PASSWORD is not configured. Set it with `npx wrangler secret put APP_PASSWORD`.",
      },
      503,
    );
  }

  const header = request.headers.get("Authorization") || "";
  const prefix = "Basic ";
  if (header.startsWith(prefix)) {
    const decoded = safeBase64Decode(header.slice(prefix.length));
    if (decoded) {
      const separator = decoded.indexOf(":");
      const suppliedUsername = decoded.slice(0, separator);
      const suppliedPassword = decoded.slice(separator + 1);
      const expectedUsername = env.APP_USERNAME || DEFAULT_USERNAME;
      if (
        separator >= 0 &&
        timingSafeEqual(suppliedUsername, expectedUsername) &&
        timingSafeEqual(suppliedPassword, env.APP_PASSWORD)
      ) {
        return null;
      }
    }
  }

  return new Response("Authentication required.", {
    status: 401,
    headers: {
      ...commonHeaders(),
      "Content-Type": "text/plain; charset=utf-8",
      "WWW-Authenticate": 'Basic realm="Image2YRL", charset="UTF-8"',
    },
  });
}

function safeBase64Decode(value) {
  try {
    return new TextDecoder().decode(base64ToBytes(value));
  } catch {
    return "";
  }
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i += 1) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

function validateApiKey(apiKey) {
  if (!apiKey) throw new Error("IMAGE_API_KEY is not configured.");
  if (/[^\x00-\x7F]/.test(apiKey)) {
    throw new Error("IMAGE_API_KEY contains non-ASCII characters.");
  }
  if (/\s/.test(apiKey)) throw new Error("IMAGE_API_KEY contains whitespace.");
  if (["your_api_key", "replace_with_your_api_key", "your_api_key_here"].includes(apiKey.toLowerCase())) {
    throw new Error("IMAGE_API_KEY is still a placeholder.");
  }
}

async function apiJson(env, path, options = {}) {
  validateApiKey(env.IMAGE_API_KEY || "");
  assertFetchableApiBase(env.IMAGE_API_BASE || DEFAULT_BASE_URL);
  const apiUrl = `${normalizeBaseUrl(env.IMAGE_API_BASE)}/${path.replace(/^\/+/, "")}`;
  let response;
  try {
    response = await fetch(apiUrl, {
      ...options,
      headers: {
        Authorization: `Bearer ${env.IMAGE_API_KEY}`,
        Accept: "application/json",
        ...(options.headers || {}),
      },
    });
  } catch (error) {
    throw new Error(`Could not call image API at ${apiUrl}: ${error.message || String(error)}`);
  }
  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error(`API response is not JSON: ${text.slice(0, 500)}`);
  }
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${JSON.stringify(payload)}`);
  }
  return payload;
}

async function handleModels(env) {
  const payload = await apiJson(env, "/models");
  const models = Array.isArray(payload.data)
    ? payload.data
        .map((item) => (item && typeof item === "object" ? item.id : item))
        .filter((id) => typeof id === "string")
    : [];
  const imageModels = models.filter((model) => model.includes("image"));
  return jsonResponse({ models: imageModels.length ? imageModels : models });
}

async function handleImageProxy(request, env) {
  validateApiKey(env.IMAGE_API_KEY || "");
  assertFetchableApiBase(env.IMAGE_API_BASE || DEFAULT_BASE_URL);

  const requestUrl = new URL(request.url);
  const rawUrl = requestUrl.searchParams.get("u") || "";
  let imageUrl;
  try {
    imageUrl = new URL(rawUrl);
  } catch {
    return new Response("Invalid image URL.", { status: 400, headers: commonHeaders() });
  }

  if (!["http:", "https:"].includes(imageUrl.protocol)) {
    return new Response("Unsupported image URL protocol.", { status: 400, headers: commonHeaders() });
  }

  const upstreamBase = new URL(normalizeBaseUrl(env.IMAGE_API_BASE || DEFAULT_BASE_URL));
  const allowedHosts = new Set([upstreamBase.hostname, "45.59.101.161"]);
  if (!allowedHosts.has(imageUrl.hostname)) {
    return new Response("Image URL host is not allowed.", { status: 403, headers: commonHeaders() });
  }

  if (imageUrl.hostname === "45.59.101.161") {
    imageUrl.hostname = upstreamBase.hostname;
  }

  let response;
  try {
    response = await fetch(imageUrl.toString(), {
      headers: {
        Authorization: `Bearer ${env.IMAGE_API_KEY}`,
        Accept: "image/*,*/*",
      },
    });
  } catch (error) {
    return new Response(`Image proxy failed: ${error.message || String(error)}`, {
      status: 502,
      headers: commonHeaders(),
    });
  }

  if (!response.ok) {
    return new Response(`Image proxy failed with HTTP ${response.status}.`, {
      status: 502,
      headers: commonHeaders(),
    });
  }

  const headers = {
    ...commonHeaders(),
    "Cache-Control": "private, max-age=3600",
    "Content-Type": response.headers.get("Content-Type") || "image/png",
  };
  return new Response(response.body, { status: 200, headers });
}

async function handleGenerate(request, env) {
  const body = await request.json();
  if (!body || typeof body !== "object") throw new Error("Request body must be a JSON object.");

  const prompt = String(body.prompt || "").trim();
  if (!prompt) throw new Error("Prompt is empty.");

  const model = String(body.model || env.IMAGE_MODEL || DEFAULT_MODEL).trim();
  const size = String(body.size || "auto").trim();
  const quality = String(body.quality || "high").trim();
  const count = Math.max(1, Math.min(Number.parseInt(body.count || "1", 10) || 1, MAX_COUNT));
  const name = String(body.name || "").trim();
  const extra = parseExtra(body.extra);
  const references = Array.isArray(body.referenceImages)
    ? body.referenceImages.filter((item) => item && typeof item === "object" && item.data).slice(0, MAX_REFERENCES)
    : [];

  const paths = references.length
    ? await generateEdits({ env, model, prompt, size, quality, count, name, extra, references })
    : await generateImages({ env, model, prompt, size, quality, count, name, extra });

  return jsonResponse({
    images: paths.map((image) => ({
      ...image,
      prompt,
      model,
      size,
      quality,
      createdAt: new Date().toISOString(),
      mode: references.length ? "edit" : "generate",
    })),
  });
}

function parseExtra(raw) {
  const value = String(raw || "").trim();
  if (!value) return {};
  const parsed = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Extra must be a JSON object.");
  }
  return parsed;
}

async function generateImages({ env, model, prompt, size, quality, count, name, extra }) {
  const payload = {
    model,
    prompt,
    n: count,
    ...extra,
  };
  if (size) payload.size = size;
  if (quality) payload.quality = quality;

  const result = await apiJson(env, "/images/generations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return extractImages(result, prompt, name);
}

async function generateEdits({ env, model, prompt, size, quality, count, name, extra, references }) {
  validateApiKey(env.IMAGE_API_KEY || "");
  assertFetchableApiBase(env.IMAGE_API_BASE || DEFAULT_BASE_URL);
  const form = new FormData();
  form.append("model", model);
  form.append("prompt", prompt);
  form.append("n", String(count));
  if (size) form.append("size", size);
  if (quality) form.append("quality", quality);
  for (const [key, value] of Object.entries(extra)) {
    form.append(key, typeof value === "object" ? JSON.stringify(value) : String(value));
  }

  const fieldName = references.length === 1 ? "image" : "image[]";
  for (const reference of references) {
    const decoded = decodeDataUrl(reference);
    if (decoded.bytes.byteLength > MAX_REFERENCE_BYTES) {
      throw new Error(`Reference image ${decoded.name} is larger than 10MB.`);
    }
    form.append(fieldName, new Blob([decoded.bytes], { type: decoded.contentType }), decoded.name);
  }

  const apiUrl = `${normalizeBaseUrl(env.IMAGE_API_BASE)}/images/edits`;
  let response;
  try {
    response = await fetch(apiUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.IMAGE_API_KEY}`,
        Accept: "application/json",
      },
      body: form,
    });
  } catch (error) {
    throw new Error(`Could not call image API at ${apiUrl}: ${error.message || String(error)}`);
  }
  const text = await response.text();
  let result;
  try {
    result = JSON.parse(text);
  } catch {
    throw new Error(`API response is not JSON: ${text.slice(0, 500)}`);
  }
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${JSON.stringify(result)}`);
  }
  return extractImages(result, prompt, name);
}

function decodeDataUrl(reference) {
  const fallbackName = safeFilename(reference.name || "reference");
  const dataUrl = String(reference.data || "");
  const marker = ";base64,";
  if (!dataUrl.startsWith("data:") || !dataUrl.includes(marker)) {
    throw new Error(`Reference image ${fallbackName} is not a base64 data URL.`);
  }
  const [header, encoded] = dataUrl.split(marker, 2);
  const contentType = header.replace(/^data:/, "") || reference.type || "application/octet-stream";
  return {
    name: fallbackName,
    contentType,
    bytes: base64ToBytes(encoded),
  };
}

async function extractImages(result, prompt, name) {
  if (!Array.isArray(result.data) || !result.data.length) {
    throw new Error("Response does not contain a non-empty data array.");
  }

  const prefix = safeFilename(name || prompt || "image");
  const timestamp = timestampSlug();
  const images = [];
  for (let index = 0; index < result.data.length; index += 1) {
    const item = result.data[index];
    const extracted = await extractImageItem(item);
    const extension = extensionFromDataUrl(extracted.url);
    images.push({
      name: `${prefix}-${timestamp}-${index + 1}${extension}`,
      url: extracted.url,
    });
  }
  return images;
}

async function extractImageItem(item) {
  if (!item || typeof item !== "object") {
    throw new Error("Expected image item to be an object.");
  }
  if (typeof item.b64_json === "string") {
    return { url: `data:image/png;base64,${item.b64_json}` };
  }
  if (typeof item.image === "string") {
    if (item.image.startsWith("data:")) return { url: item.image };
    if (item.image.startsWith("http://") || item.image.startsWith("https://")) {
      return { url: proxiedImageUrl(item.image) };
    }
    return { url: `data:image/png;base64,${item.image}` };
  }
  if (typeof item.url === "string") {
    if (item.url.startsWith("data:")) return { url: item.url };
    if (item.url.startsWith("http://") || item.url.startsWith("https://")) {
      return { url: proxiedImageUrl(item.url) };
    }
  }
  throw new Error(`Cannot find image data in item: ${JSON.stringify(item).slice(0, 500)}`);
}

function proxiedImageUrl(url) {
  return `/api/image?u=${encodeURIComponent(url)}`;
}

function safeFilename(value) {
  const cleaned = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9._-]+/g, "")
    .replace(/^[._-]+|[._-]+$/g, "");
  return cleaned.slice(0, 80) || "image";
}

function timestampSlug() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return [
    now.getUTCFullYear(),
    pad(now.getUTCMonth() + 1),
    pad(now.getUTCDate()),
    "-",
    pad(now.getUTCHours()),
    pad(now.getUTCMinutes()),
    pad(now.getUTCSeconds()),
  ].join("");
}

function extensionFromDataUrl(dataUrl) {
  if (dataUrl.startsWith("/")) return ".png";
  if (dataUrl.startsWith("http://") || dataUrl.startsWith("https://")) {
    const pathname = new URL(dataUrl).pathname.toLowerCase();
    if (pathname.endsWith(".jpg") || pathname.endsWith(".jpeg")) return ".jpg";
    if (pathname.endsWith(".webp")) return ".webp";
    if (pathname.endsWith(".gif")) return ".gif";
    return ".png";
  }
  const contentType = /^data:([^;,]+)/.exec(dataUrl)?.[1]?.toLowerCase() || "image/png";
  if (contentType.includes("jpeg") || contentType.includes("jpg")) return ".jpg";
  if (contentType.includes("webp")) return ".webp";
  if (contentType.includes("gif")) return ".gif";
  return ".png";
}

function base64ToBytes(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}

const INDEX_HTML = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Image2YRL</title>
  <style>
    :root { color-scheme: light; --bg:#f3f5f8; --panel:#fff; --line:#d8dee7; --text:#111827; --muted:#667085; --accent:#2563eb; --accent-dark:#1d4ed8; --stage:#0f172a; --danger:#b91c1c; }
    * { box-sizing:border-box; }
    html, body { margin:0; min-height:100%; }
    body { min-height:100vh; min-height:100dvh; background:var(--bg); color:var(--text); font:14px/1.45 "Segoe UI", Arial, sans-serif; }
    .app { display:grid; grid-template-columns:400px minmax(0,1fr); min-height:100vh; min-height:100dvh; }
    aside { background:var(--panel); border-right:1px solid var(--line); padding:18px; overflow:auto; }
    main { display:grid; grid-template-rows:minmax(0,1fr) auto; min-width:0; min-height:0; }
    h1 { margin:0 0 14px; font-size:21px; font-weight:700; letter-spacing:0; }
    label { display:block; margin:12px 0 6px; font-weight:650; color:#243041; }
    textarea, input, select { width:100%; border:1px solid var(--line); border-radius:7px; background:#fff; color:var(--text); padding:10px 11px; font:inherit; }
    textarea { min-height:170px; resize:vertical; }
    button { min-height:40px; border:1px solid var(--line); border-radius:7px; background:#fff; color:#172033; padding:9px 12px; font:inherit; cursor:pointer; }
    button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:700; }
    button.primary:hover { background:var(--accent-dark); }
    button:disabled { opacity:.62; cursor:default; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .actions { display:grid; grid-template-columns:1fr auto; gap:10px; margin-top:16px; }
    .meta { margin-top:12px; color:var(--muted); font-size:12px; word-break:break-word; }
    .error { color:var(--danger); margin-top:10px; white-space:pre-wrap; }
    .refs { display:flex; gap:8px; overflow-x:auto; padding:8px; border:1px solid var(--line); border-radius:7px; min-height:72px; background:#f8fafc; }
    .refs:empty::before { content:"No reference images"; color:var(--muted); font-size:12px; align-self:center; }
    .ref { position:relative; flex:0 0 auto; width:76px; height:56px; }
    .ref img { width:100%; height:100%; object-fit:cover; border-radius:6px; border:1px solid var(--line); }
    .ref button { position:absolute; right:3px; top:3px; width:22px; min-height:22px; height:22px; padding:0; border-radius:999px; line-height:18px; background:rgba(15,23,42,.82); color:#fff; border:0; }
    .stage { position:relative; min-width:0; min-height:0; display:grid; place-items:center; padding:18px; background:var(--stage); overflow:hidden; }
    .image-host { width:100%; height:100%; min-width:0; min-height:0; display:grid; place-items:center; }
    .empty { color:#cbd5e1; text-align:center; }
    .image-wrap { display:grid; gap:10px; place-items:center; max-width:100%; max-height:100%; }
    .image { max-width:100%; max-height:calc(100vh - 160px); width:auto; height:auto; object-fit:contain; display:block; box-shadow:0 12px 40px rgba(0,0,0,.35); }
    .download { display:inline-flex; align-items:center; justify-content:center; min-height:38px; border-radius:7px; background:#fff; color:#172033; padding:8px 12px; text-decoration:none; font-weight:650; }
    .overlay { position:absolute; inset:0; display:none; place-items:center; background:rgba(15,23,42,.65); backdrop-filter: blur(2px); }
    .overlay.active { display:grid; }
    .loader { display:grid; gap:14px; place-items:center; color:#f8fafc; font-weight:700; }
    .spinner { width:42px; height:42px; border-radius:999px; border:4px solid rgba(255,255,255,.26); border-top-color:#fff; animation:spin .8s linear infinite; }
    @keyframes spin { to { transform:rotate(360deg); } }
    .strip { background:var(--panel); border-top:1px solid var(--line); padding:10px 14px; overflow-x:auto; white-space:nowrap; }
    .strip:empty::before { content:"No session history yet"; color:var(--muted); font-size:12px; }
    .thumb-item { position:relative; display:inline-block; width:112px; height:74px; margin-right:8px; vertical-align:middle; }
    .thumb { width:112px; height:74px; object-fit:cover; border:2px solid transparent; border-radius:7px; vertical-align:middle; cursor:pointer; background:#e5e7eb; }
    .thumb.active { border-color:var(--accent); }
    .thumb-delete { position:absolute; right:4px; top:4px; width:24px; min-height:24px; height:24px; padding:0; border:0; border-radius:999px; background:rgba(15,23,42,.82); color:#fff; font-size:16px; line-height:22px; display:grid; place-items:center; }
    .thumb-delete:hover { background:rgba(185,28,28,.92); }
    @media (max-width:860px) {
      body { font-size:15px; }
      .app { display:block; min-height:100vh; min-height:100dvh; }
      aside { border-right:0; border-bottom:1px solid var(--line); padding:14px; overflow:visible; }
      main { min-height:60vh; min-height:60dvh; grid-template-rows:minmax(52vh,1fr) auto; grid-template-rows:minmax(52dvh,1fr) auto; }
      h1 { margin-bottom:12px; font-size:19px; }
      label { margin:10px 0 5px; }
      textarea { min-height:128px; }
      textarea, input, select, button { min-height:42px; font-size:16px; }
      .row { grid-template-columns:1fr; gap:0; }
      .actions { grid-template-columns:1fr; }
      .stage { min-height:52vh; min-height:52dvh; padding:10px; }
      .image { max-height:48vh; max-height:48dvh; }
      .strip { padding:8px 10px; }
      .thumb-item { width:96px; height:64px; margin-right:6px; }
      .thumb { width:96px; height:64px; }
      .refs { min-height:68px; }
    }
    @media (max-width:480px) {
      aside { padding:12px; }
      textarea { min-height:112px; }
      .stage { min-height:48vh; min-height:48dvh; }
      main { grid-template-rows:minmax(48vh,1fr) auto; grid-template-rows:minmax(48dvh,1fr) auto; }
      .image { max-height:44vh; max-height:44dvh; }
      .thumb-item { width:84px; height:58px; }
      .thumb { width:84px; height:58px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <h1>Image2YRL</h1>
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
      <section class="stage">
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
    const HISTORY_KEY = "image2yrl.history.v1";
    const MAX_HISTORY = 24;
    const MAX_STORED_DATA_URLS = 6;
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

    function imageIsDataUrl(image) {
      return typeof image.url === "string" && image.url.startsWith("data:");
    }

    function trimForStorage(items) {
      let storedDataUrls = 0;
      return items.slice(0, MAX_HISTORY).filter(image => {
        if (!imageIsDataUrl(image)) return true;
        storedDataUrls += 1;
        return storedDataUrls <= MAX_STORED_DATA_URLS;
      });
    }

    function saveHistory() {
      try {
        const stored = trimForStorage(images);
        localStorage.setItem(HISTORY_KEY, JSON.stringify(stored));
      } catch (err) {
        try {
          const lightweight = images.filter(image => !imageIsDataUrl(image)).slice(0, MAX_HISTORY);
          localStorage.setItem(HISTORY_KEY, JSON.stringify(lightweight));
        } catch {
          localStorage.removeItem(HISTORY_KEY);
        }
      }
    }

    function loadHistory() {
      try {
        const raw = localStorage.getItem(HISTORY_KEY);
        if (!raw) return;
        const stored = JSON.parse(raw);
        if (!Array.isArray(stored)) return;
        images = stored.filter(image => image && typeof image === "object" && image.url && image.name);
        renderStrip();
        if (images.length) showImage(0);
      } catch {
        localStorage.removeItem(HISTORY_KEY);
      }
    }

    function renderStrip() {
      $("strip").innerHTML = "";
      images.forEach((image, index) => {
        const item = document.createElement("div");
        item.className = "thumb-item";
        const thumb = document.createElement("img");
        thumb.className = "thumb";
        thumb.src = image.url;
        thumb.title = image.name;
        thumb.addEventListener("click", () => showImage(index));
        const deleteButton = document.createElement("button");
        deleteButton.className = "thumb-delete";
        deleteButton.type = "button";
        deleteButton.title = "Delete from history";
        deleteButton.textContent = "x";
        deleteButton.addEventListener("click", event => {
          event.stopPropagation();
          deleteImage(index);
        });
        item.append(thumb, deleteButton);
        $("strip").appendChild(item);
      });
    }

    function showImage(index) {
      active = index;
      const image = images[index];
      $("imageHost").innerHTML = "";
      const wrap = document.createElement("div");
      wrap.className = "image-wrap";
      const img = document.createElement("img");
      img.className = "image";
      img.src = image.url;
      img.alt = image.name;
      const download = document.createElement("a");
      download.className = "download";
      download.href = image.url;
      download.download = image.name;
      download.textContent = "Download";
      wrap.append(img, download);
      $("imageHost").appendChild(wrap);
      [...$("strip").querySelectorAll(".thumb")].forEach((imgNode, i) => imgNode.classList.toggle("active", i === index));
      if (image.prompt) $("prompt").value = image.prompt;
      if (image.model) $("model").value = image.model;
      if (image.size) $("size").value = image.size;
      if (image.quality) $("quality").value = image.quality;
      setStatus(image.name);
    }

    function prependImages(newImages) {
      images = newImages.concat(images).slice(0, MAX_HISTORY);
      renderStrip();
      saveHistory();
      if (images.length) showImage(0);
    }

    function deleteImage(index) {
      images.splice(index, 1);
      saveHistory();
      renderStrip();
      if (!images.length) {
        active = -1;
        $("imageHost").innerHTML = '<div class="empty">Generated images will appear here</div>';
        setStatus("Ready");
        return;
      }
      showImage(Math.min(index, images.length - 1));
    }

    async function loadConfig() {
      const res = await fetch("/api/config");
      const config = await res.json();
      if (!res.ok) throw new Error(config.error || "Failed to load config");
      $("model").value = config.model;
      fillSelect($("size"), config.sizes, config.defaultSize);
      fillSelect($("quality"), config.qualities, config.defaultQuality);
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
        prependImages(data.images || []);
        setStatus("Done");
      } catch (err) {
        setError(err.message || String(err));
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
        alert((data.models || []).join("\\n") || "No models returned");
        setStatus("Ready");
      } catch (err) {
        setError(err.message || String(err));
        setStatus("Error");
      }
    }

    $("generate").addEventListener("click", generate);
    $("models").addEventListener("click", loadModels);
    $("refsInput").addEventListener("change", event => {
      addReferenceFiles(event.target.files).catch(err => setError(err.message || String(err)));
      event.target.value = "";
    });
    loadHistory();
    loadConfig().catch(err => setError(err.message || String(err)));
  </script>
</body>
</html>`;
