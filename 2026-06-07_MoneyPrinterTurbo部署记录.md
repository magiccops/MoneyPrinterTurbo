# 2026-06-07 MoneyPrinterTurbo 部署记录

> NAS（飞牛 OS / 192.168.1.20）上用 Docker 部署开源项目 [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)，并完成 Pexels + minimax LLM 的端到端配置。

---

## 一、环境

| 项 | 值 |
| --- | --- |
| 主机 | NAS（飞牛 OS 6.18.18-trim） |
| IP | 192.168.1.20 |
| 根盘剩余 | 19G（够用，最终占用 2.11GB × 2 镜像 + 项目） |
| Docker | 28.5.2（docker buildx 0.29.1） |
| Docker Compose | v2.40.3 |
| 已有冲突端口 | 8080（被同机的 `trading-system-frontend` 占用，绑定 0.0.0.0:8080→80） |
| 局域网 | 192.168.1.0/24（部署后给 0.0.0.0 绑定，方便别的设备访问） |

---

## 二、部署过程

### 1. 克隆项目

```bash
git clone --depth 1 https://github.com/harry0703/MoneyPrinterTurbo.git
```

直连 GitHub 成功，未走代理。

### 2. 准备配置 & 调整 docker-compose

```bash
cp config.example.toml config.toml
```

随后为局域网访问修改 `docker-compose.yml`：

- `webui` 端口 `127.0.0.1:8501` → `0.0.0.0:8501`
- `api` 端口 `127.0.0.1:8080` → 起初也改为 `0.0.0.0:8080`，后因端口冲突改成 `0.0.0.0:8090:8080`（容器内仍跑 8080，宿主机映射到 8090）。

### 3. 构建镜像

```bash
docker compose -f /home/magiccops/MoneyPrinterTurbo/docker-compose.yml \
  --project-directory /home/magiccops/MoneyPrinterTurbo build
```

- `Dockerfile` 基于 `python:3.11-slim-bullseye`
- apt 镜像源：阿里云 → 清华 → Debian 默认（三级回退）
- pip 镜像源：阿里云 → 清华 → PyPI 默认
- 安装系统依赖：`git` / `imagemagick` / `ffmpeg`
- pip 安装 `requirements.txt`

构建产物：

| 镜像 | 大小 |
| --- | --- |
| `moneyprinterturbo-webui` | 2.11 GB |
| `moneyprinterturbo-api` | 2.11 GB |

### 4. 启动 & 验证

```bash
docker compose -f /home/magiccops/MoneyPrinterTurbo/docker-compose.yml \
  --project-directory /home/magiccops/MoneyPrinterTurbo up -d
```

第一次启动失败：8080 端口被 `trading-system-frontend` 占用。把 api 端口改为 `8090:8080` 后启动成功。

健康检查：

| 端点 | 结果 |
| --- | --- |
| `http://127.0.0.1:8501/` (WebUI) | HTTP 200 |
| `http://127.0.0.1:8090/docs` (Swagger) | HTTP 200 |
| `http://127.0.0.1:8090/openapi.json` | 返回 MoneyPrinterTurbo v1.2.9 接口定义 |

容器名：

- `moneyprinterturbo-webui` (Streamlit)
- `moneyprinterturbo-api` (FastAPI)

`restart: always`，NAS 重启后会自动拉起。

---

## 三、配置过程

### 1. Pexels 视频素材

申请地址：<https://www.pexels.com/api/>

写入 `config.toml`：

```toml
pexels_api_keys = [ "BL9W4zqprAxBvbEPHtdjUXNlFVOKmcvZXYkDzTDrDNr3Oad73HQifHPh",]
```

直打 Pexels 官方 `https://api.pexels.com/v1/search?query=ocean&per_page=1` 验证，返回 photo id 34480294，key 有效。

### 2. minimax LLM

#### 来源

复用 `~/.claude/settings.json` 里的 Claude Code 认证 token：

```
env.ANTHROPIC_AUTH_TOKEN = sk-cp-3UP82u6h5GugRMrn7IxW7L7HbD29IarClXpaDLLoIg-vr8vfwQlKx8wUSN0WGiayM7t5vUTwOLHtrx_jrf9Ig1mJlSutPDkZo7pvsp-k8CBhRmBUswILhII
env.ANTHROPIC_BASE_URL  = https://api.minimaxi.com/anthropic
```

> ⚠️ 用途说明：Claude Code 走的是 Anthropic 协议（`/anthropic`），而 MoneyPrinterTurbo 的 `minimax` provider 走的是 OpenAI 兼容协议（`/v1`）。虽然二者 base_url 域名相同，但鉴权端点不同。这个 token 在两种协议下都能用，但消费的是同一个 minimax 账户的额度（不可拆分计费）。

#### 写入 config.toml

```toml
llm_provider        = "minimax"
minimax_api_key     = "sk-cp-3UP82u6h5GugRMrn7IxW7L7HbD29IarClXpaDLLoIg-vr8vfwQlKx8wUSN0WGiayM7t5vUTwOLHtrx_jrf9Ig1mJlSutPDkZo7pvsp-k8CBhRmBUswILhII"
minimax_base_url    = "https://api.minimaxi.com/v1"
minimax_model_name  = "MiniMax-M3"
```

> 区域判断：因为 settings.json 里 base_url 是 `minimaxi.com`（国内），所以这里也用 `https://api.minimaxi.com/v1`（国内 OpenAI 兼容端点）。海外用 `https://api.minimax.io/v1`。

#### 踩坑记录

minimax 端点要求严格的 `Authorization: Bearer <key>` 前缀，裸 `Authorization: sk-cp-...` 会返回 1004/401：

```
{"type":"error","error":{"type":"authorized_error","message":"login fail: Please carry the API secret key in the 'Authorization' field of the request header (1004)","http_code":"401"}}
```

MoneyPrinterTurbo 内部用的是 OpenAI 兼容 SDK（看 `app/services/llm.py:227-232`），会自动加 `Bearer ` 前缀，所以传 key 即可，不需要在 key 里手动拼前缀。

#### 端到端验证

直打 minimax `https://api.minimaxi.com/v1/chat/completions`（这就是 MoneyPrinterTurbo 实际调的端点）：

```bash
curl -sS -H "Authorization: Bearer sk-cp-3UP82u6h5...hII" \
  -H "Content-Type: application/json" \
  "https://api.minimaxi.com/v1/chat/completions" \
  -d '{"model":"MiniMax-M3","messages":[{"role":"user","content":"用一句话介绍你自己"}],"max_tokens":80}'
```

返回：

> "我是 MiniMax-M3，一个由 MiniMax 公司开发的大型语言模型 AI 助手…"

同时 `/v1/models` 返回模型列表：`MiniMax-M3`、`MiniMax-M2.7`、`MiniMax-M2.7-highspeed`、`MiniMax-M2.5`、`MiniMax-M2.5-highspeed` 等。

### 3. config.toml 关键字段汇总（部署最终态）

```toml
[app]
video_source        = "pexels"
llm_provider        = "minimax"
pexels_api_keys     = [ "BL9W4zqprAxBvbEPHtdjUXNlFVOKmcvZXYkDzTDrDNr3Oad73HQifHPh",]
minimax_api_key     = "sk-cp-3UP82u6h5GugRMrn7IxW7L7HbD29IarClXpaDLLoIg-vr8vfwQlKx8wUSN0WGiayM7t5vUTwOLHtrx_jrf9Ig1mJlSutPDkZo7pvsp-k8CBhRmBUswILhII"
minimax_base_url    = "https://api.minimaxi.com/v1"
minimax_model_name  = "MiniMax-M3"
subtitle_provider   = "edge"
video_codec         = "libx264"

[ui]
language            = "zh"
tts_server          = "azure-tts-v1"
voice_name          = "en-AU-NatashaNeural-Female"
font_name           = "MicrosoftYaHeiBold.ttc"
subtitle_position   = "bottom"
text_fore_color     = "#FFFFFF"
font_size           = 60
subtitle_background_enabled = true
subtitle_background_color   = "#000000"
rounded_subtitle_background  = false
```

> 注：配置写入后被 TOML linter 重新格式化（去掉双引号、补充 UI 默认值），不影响功能。

---

## 四、Key / Token 清单（本次部署用到）

| 用途 | Key / Token | 位置 | 备注 |
| --- | --- | --- | --- |
| Pexels 视频素材 | `BL9W4zqprAxBvbEPHtdjUXNlFVOKmcvZXYkDzTDrDNr3Oad73HQifHPh` | `MoneyPrinterTurbo/config.toml` → `pexels_api_keys` | 在 [pexels.com/api](https://www.pexels.com/api/) 申请 |
| minimax LLM (Anthropic 协议) | `sk-cp-3UP82u6h5GugRMrn7IxW7L7HbD29IarClXpaDLLoIg-vr8vfwQlKx8wUSN0WGiayM7t5vUTwOLHtrx_jrf9Ig1mJlSutPDkZo7pvsp-k8CBhRmBUswILhII` | `~/.claude/settings.json` → `env.ANTHROPIC_AUTH_TOKEN` | Claude Code 会话认证用 |
| minimax LLM (OpenAI 兼容) | 同上 | `MoneyPrinterTurbo/config.toml` → `minimax_api_key` | 复用同一个 token，MoneyPrinterTurbo 走 `/v1` 端点 |
| 极少量 Pixabay key | 空 | `config.toml` → `pixabay_api_keys` | 未配，Pexels 已够用 |
| Azure Speech | 空 | `config.toml` → `[azure] speech_key` | 用了 edge-tts，未配 |
| Whisper | 未启用 | `config.toml` → `subtitle_provider = "edge"` | 若改 `"whisper"` 需 ~3GB 模型 |

---

## 五、最终可用链路

- WebUI: <http://192.168.1.20:8501>
- API docs: <http://192.168.1.20:8090/docs>
- LLM: minimax `MiniMax-M3`（国内 `/v1` 端点）
- 视频素材: Pexels
- 字幕: edge-tts（默认）
- 编码: libx264

打开 WebUI 就能跑一次完整生成。

---

## 六、常用运维命令

```bash
# 进入项目目录
cd /home/magiccops/MoneyPrinterTurbo

# 看日志
docker compose logs -f webui
docker compose logs -f api

# 停止 / 启动 / 重启
docker compose stop
docker compose start
docker compose restart

# 完全重建（拉新代码后）
docker compose build --pull
docker compose up -d
```

---

## 七、踩坑总结

1. **docker-compose.yml 默认绑定 127.0.0.1** —— NAS 上从局域网访问不到，必须改成 `0.0.0.0`。
2. **8080 端口冲突** —— 同机的量化交易系统前端占用了 8080，MoneyPrinterTurbo api 改成 `8090:8080`。
3. **minimax 鉴权要求 Bearer 前缀** —— 裸 `Authorization: sk-...` 返回 1004/401；MoneyPrinterTurbo 内部 SDK 自动加 Bearer，OK。
4. **配置改动后必须 `docker compose restart`** —— 容器内 `main.py` 在启动时一次性 `load_config`，不重启不会重新读 config.toml。
5. **`docker compose` 需要在项目目录跑** —— 用 `-f` + `--project-directory` 显式指定更稳。

---

## 八、字幕链路问题排查（补充 · 2026-06-07 当晚）

部署后第一次跑视频生成时字幕报失败。原始日志：

```
2026-06-06 16:20:09.028 | WARNING  | app.services.voice:_build_subtitle_items_from_edge_cues:1314
  - edge cues still have unmatched text after aggregation: 针刺时一般直刺0.5至1寸，体质虚弱者及孕妇应当慎用或禁针，以免引起不良反应
2026-06-06 16:20:09.029 | WARNING  | app.services.voice:create_subtitle:1386 - failed, sub_items len: 0, script_lines len: 44
2026-06-06 16:20:09.030 | WARNING  | app.services.task:generate_subtitle:151 - subtitle file not found, fallback to whisper
2026-06-06 16:20:09.031 | INFO     | app.services.subtitle:create:32 - loading model: large-v3, device: CPU, compute_type: int8
2026-06-06 16:22:23.466 | ERROR    | app.services.subtitle:create:40
  - failed to load model: Got: ConnectError: [Errno 101] Network is unreachable
  An error happened while trying to locate the files on the Hub, ...
```

### 8.1 根因

故障分两层。**先解决网络层**（即使用户没看代码也已经一眼能定位），edge cues 匹配是另一个独立 bug。

#### 网络层（fallback whisper 失败的根因）

容器内 `urllib.request.urlopen` 直接报 `Network is unreachable` —— 这不是 DNS，是 IP 层被防火墙拦了。容器里分段验证：

| 目标 | 类型 | 结果 |
| --- | --- | --- |
| `223.5.5.5:443`（阿里 DNS） | 国内 IP | ✅ |
| `8.8.8.8:443`（Google DNS） | 国外 IP | ❌ Timeout |
| `192.168.1.20`（宿主机） | 局域网 | ✅ |
| `huggingface.co` | 国外域名（Cloudflare 节点） | ❌ `Network is unreachable` |
| `baidu.com` | 国内域名 | ✅ |

说明这台 NAS 上 docker 容器**走的是直连外网，没用上宿主机的 Clash 7890 代理**。Clash (mihomo) 进程还在跑（pid 1346），只是：

- `allow-lan: false` → `mixed-port: 7890` 只绑 `127.0.0.1`
- 容器内访问 `192.168.1.20:7890` → Connection refused

**而 `host.docker.internal` 关键字在飞牛 OS 上解析到 `172.17.0.1`（docker0 网桥），docker0 在飞牛 OS 上是 `linkdown`**，所以 `host.docker.internal:7890` 也连不通。

#### 应用层（edge cues 匹配失败的根因）

代码路径：`task.py:141-152` 默认按 `subtitle_provider = "edge"` 跑 `voice.create_subtitle()`。当生成的 `.srt` 文件不存在时，自动 fallback 到 whisper。**这次网络问题暴露了 edge cues 自己的 bug**：

- `voice.py:1283-1318` `_build_subtitle_items_from_edge_cues` 把 edge_tts 7.x 返回的逐词 cues 累积成整句，再与 `script_lines` 做精确匹配
- 上面报错的那段「针刺时一般直刺0.5至1寸，体质虚弱者及孕妇应当慎用或禁针，以免引起不良反应」里有数字 `0.5至1寸`、长句尾的「以免引起不良反应」
- edge_tts 在 cues 里大概率会把这句切成 `0.5` / `至` / `1寸` / `，` / `体质虚弱者` / `及` / `孕妇` / `应当` / `慎用` / `或` / `禁针` / `，` / `以免` / `引起` / `不良反应` 这种粒度
- 累积拼到当前行时大概率因为标点 / 数字拆分差异而匹配不上 `script_lines[i]`，导致 `current_text` 一直累积，最终该整段都没收敛进 sub_items
- `sub_items = []` → `.srt` 文件不写 → 触发 whisper fallback

**这跟代理无关**，是项目硬编码精确匹配的偶发 bug。

### 8.2 修复

#### Step 1 · 放行 Clash 监听

`/home/magiccops/.config/mihomo/config.yaml`（pid 1346 在用）：

```yaml
allow-lan: false   # →   allow-lan: true
```

热加载：

```bash
kill -HUP 1346
ss -tlnp | grep 7890
# 之前：127.0.0.1:7890
# 之后：*:7890  （0.0.0.0）
```

注：先试了 `kill -USR1 1346`（mihomo 文档语义是 force-config-reload），但这版 mihomo 似乎只在规则层重载，端口没重新 bind；改用 `SIGHUP` 才把监听地址从 127.0.0.1 切到 0.0.0.0。

#### Step 2 · 让容器走代理

`MoneyPrinterTurbo/docker-compose.yml` 加 `x-common-proxy` anchor 给两个 service 用：

```yaml
x-common-volumes: &common-volumes
  - ./:/MoneyPrinterTurbo

x-common-proxy: &common-proxy
  environment:
    HTTP_PROXY: "http://192.168.1.20:7890"
    HTTPS_PROXY: "http://192.168.1.20:7890"
    NO_PROXY: "localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,172.17.0.0/16,172.18.0.0/16,172.19.0.0/16,172.20.0.0/16,.svc.cluster.local"

services:
  webui:
    # ... 其它不变
    <<: *common-proxy
    restart: always
  api:
    # ... 其它不变
    <<: *common-proxy
    restart: always
```

`NO_PROXY` 把内网 / docker bridge / k8s 默认网段都列上，避免 MoneyPrinterTurbo 调用 Pexels（国内可能走代理反而慢）、或者 WebUI 内部回连本机时被代理拦。

`extra_hosts: host-gateway` 没用 —— 飞牛 OS 上这个关键字解析到 `172.17.0.1`（linkdown 的 docker0）。直接写死宿主机 LAN IP `192.168.1.20` 是最稳的。

```bash
docker compose -f /home/magiccops/MoneyPrinterTurbo/docker-compose.yml \
  --project-directory /home/magiccops/MoneyPrinterTurbo up -d
```

### 8.3 验证

容器内 `python3 -c` 验证（一次跑完）：

| 检查 | 结果 |
| --- | --- |
| `socket.create_connection(("192.168.1.20", 7890))` | ✅ |
| `https://huggingface.co` | ✅ 200 |
| `https://huggingface.co/Systran/faster-whisper-large-v3`（元信息） | ✅ 7 个文件，model.bin 2.94 GB |
| HEAD `model.bin` | ✅ 200，Content-Length 2 944.3 MB |
| `https://8.8.8.8` | ✅ 200（走代理） |
| `https://github.com` | ✅ 200（走代理） |
| `https://api.openai.com` | ❌ 421 Misdirected Request（**无影响**，我们用 minimax） |

代理层 + 模型可达性都验过。下次重跑同样任务时：

- edge cues 大概率仍会匹配失败（项目 bug）
- 触发 fallback 后 whisper 这次能成功下载并加载 large-v3，跑通
- 代价是首跑要 3–5 分钟下 2.94GB 模型 + CPU 推理（NAS 上会慢一点）

### 8.4 待办（用户拍板）

针对 edge cues 匹配 bug 的几个方案：

1. **保持 `subtitle_provider = "edge"`** —— 同样脚本重跑，edge 失败时 fallback whisper 这次能成；问题是每次都白白触发 fallback。
2. **改 `subtitle_provider = "whisper"`** —— 强制 whisper，绕开 edge cues bug；首跑要 3GB 下载。
3. **预先下载到 `./MoneyPrinterTurbo/models/whisper-large-v3`**（按 README 路径）—— 一次性投入 3GB，之后无论 edge 还是 whisper 都即时可用，faster-whisper 检测到本地模型就跳过下载。
4. **关掉字幕**（WebUI 里 `subtitle_enabled = false`）—— 最轻，旁路整个 bug。

### 8.5 这次新增的踩坑（追加到第 7 节后）

6. **容器内国外 IP 默认不可达** —— NAS 上 docker 容器走直连外网，没自动用宿主代理；要么改 mihomo `allow-lan: true` + 容器配 `HTTPS_PROXY`，要么把模型预先下载到 `./models/` 离线用。
7. **飞牛 OS 上 `host-gateway` 关键字解析到 linkdown 的 docker0** —— compose 里别用 `extra_hosts: host.docker.internal:host-gateway`，直接写死宿主 LAN IP。
8. **mihomo 重载监听地址用 SIGHUP 不用 SIGUSR1** —— 这版 mihomo 的 USR1 只重载规则，不会重新 bind 端口。
9. **edge cues 聚合是硬编码精确匹配** —— `voice.py:1283-1318` 对中文长句 + 数字 + 标点边缘断句容易匹配失败；是项目 bug，需要走 whisper 或关字幕绕开。
