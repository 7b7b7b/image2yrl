# Image API Client

一个纯 Python 标准库写的图片生成客户端，用来调用 OpenAI 兼容的图片生成接口。

## 配置

不要把真实 key 写进代码。推荐用环境变量：

```bash
export IMAGE_API_KEY="replace_with_your_api_key"
export IMAGE_API_BASE="http://45.59.101.161:8083/v1"
```

也可以复制 `.env.example` 为 `.env`，再把 `IMAGE_API_KEY` 改成你的真实 key。本地 `.env` 已经被 `.gitignore` 忽略。

## 查看模型

```bash
python3 image_client.py models
```

如果返回的模型名不是 `gpt-image-2`，生成时用 `--model` 指定实际模型名。

## 生成图片

```bash
python3 image_client.py generate "一只穿宇航服的橘猫，电影感，高清"
```

默认会保存到 `outputs/` 目录。

常用参数：

```bash
python3 image_client.py generate "赛博朋克风格的上海雨夜" \
  --model gpt-image-2 \
  --size 1024x1024 \
  --n 1 \
  --response-format b64_json
```

如果服务商有额外参数，可以用 `--extra` 传 JSON：

```bash
python3 image_client.py generate "极简产品海报" \
  --extra '{"seed":1234,"steps":30}'
```

## 图形界面

macOS/Linux：

```bash
python3 image_gui.py
```

Windows：

```bat
run_gui_windows.bat
```

Windows 上也可以双击 `run_gui_windows.bat`。第一次打开后确认 API Key、Base URL 和 Model，点 `Save` 会写入同目录下的 `.env`。

## 打包 Windows exe

在 Windows 电脑上运行：

```bat
build_windows_exe.bat
```

打包完成后，程序会在 `dist\ImageApiClient.exe`。

## 部署到子域名

推荐用 Cloudflare Workers 部署到 `image.wormforce.net`，不需要绑卡。
部署说明见 [DEPLOY_CLOUDFLARE.md](DEPLOY_CLOUDFLARE.md)。

Render 方案也保留在 [DEPLOY_RENDER.md](DEPLOY_RENDER.md)，但 Render 可能要求添加银行卡验证。
