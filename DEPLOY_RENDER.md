# Deploy to Render

This deploys the browser web app at a private subdomain such as:

```text
https://image.wormforce.net
```

The API key stays on the server. The browser never receives it.

## 1. Create the Render service

1. Open Render and choose **New +** -> **Blueprint**.
2. Connect this GitHub repo: `https://github.com/7b7b7b/image2yrl`.
3. Use the included `render.yaml`.
4. Set these secret environment variables when Render asks:

```text
IMAGE_API_KEY=your real API key
APP_PASSWORD=your login password
```

`APP_USERNAME` defaults to `admin`. You can change it in Render.

## 2. Bind the custom domain

In the Render service settings, add:

```text
image.wormforce.net
```

Then add the DNS record Render shows you in the DNS provider for `wormforce.net`.
It is usually a `CNAME` record.

## 3. Block other public entrances

Set:

```text
ALLOWED_HOSTS=image.wormforce.net
```

The app will return 404 for other public Host headers. After the custom domain
works, disable the default Render subdomain in Render's custom domain settings
if your Render plan/settings expose that option.

If you need to test on the temporary Render URL before DNS is ready, temporarily
clear `ALLOWED_HOSTS`, test once, then set it back to `image.wormforce.net`.

## 4. Open the site

Visit:

```text
https://image.wormforce.net
```

Your browser will ask for the username and password:

```text
username: APP_USERNAME, usually admin
password: APP_PASSWORD
```

## Notes

- Free Render services may sleep when idle, so the first visit can be slow.
- Generated images are written to `outputs/`. On free/ephemeral hosting, files
  may disappear after a redeploy or restart. Download important images locally.
- Keep `IMAGE_API_KEY` and `APP_PASSWORD` only in Render environment variables.
