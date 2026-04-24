# Deploy to Cloudflare Workers

This is the no-card deployment path for `image.wormforce.net`.

The image API key stays in Cloudflare Worker secrets. It is never sent to the
browser. The page uses browser Basic Auth, so your phone and computer can both
open the same URL after entering the username and password.

## Requirements

- A Cloudflare account
- `wormforce.net` added to Cloudflare as a zone
- Node.js 18+ on the computer used for deployment

If `wormforce.net` is not using Cloudflare DNS yet, add the site in Cloudflare
and follow Cloudflare's nameserver instructions first.

## Deploy

Before deploying, create a DNS record for the upstream image API:

```text
Type: A
Name: image-api
IPv4 address: 45.59.101.161
Proxy status: DNS only
TTL: Auto
```

This creates:

```text
image-api.wormforce.net
```

Keep this record **DNS only**. The API listens on port `8083`, and Cloudflare
Workers should call it by hostname instead of raw IP.

From this repo:

```bash
cd cloudflare-worker
npm install
npx wrangler login
```

Add the secrets:

```bash
npx wrangler secret put IMAGE_API_KEY
npx wrangler secret put APP_PASSWORD
```

When prompted, paste your real API key for `IMAGE_API_KEY`, and choose a login
password for `APP_PASSWORD`.

The username defaults to:

```text
admin
```

Deploy:

```bash
npm run deploy
```

After deployment, open:

```text
https://image.wormforce.net
```

Your browser should ask for:

```text
username: admin
password: the APP_PASSWORD you set
```

## Change the Domain

The default config is:

```toml
[[routes]]
pattern = "image.wormforce.net"
custom_domain = true

[vars]
ALLOWED_HOSTS = "image.wormforce.net"
IMAGE_API_BASE = "http://image-api.wormforce.net:8083/v1"
```

If you want another subdomain, update both values in
`cloudflare-worker/wrangler.toml`.

## Local Development

Create a local secret file:

```bash
cd cloudflare-worker
cp .dev.vars.example .dev.vars
```

Edit `.dev.vars`, then run:

```bash
npm run dev
```

`.dev.vars` is ignored by git.

## Notes

- This Workers version does not need a server or credit card.
- Generated images are returned to the current browser session as data URLs.
  Use the `Download` button to keep anything important.
- Session history is in memory only. Reloading the page clears the thumbnails.
- The default `workers_dev = false` setting avoids publishing a separate
  `*.workers.dev` entrance. Access should be through `image.wormforce.net`.
