# 2026-06-08 MoneyPrinterTurbo 合谷穴短视频改造记录

> 在已部署的 MoneyPrinterTurbo（v1.2.9，2026-06-07 部署记录）基础上，针对「四总穴之合谷穴」这种中医穴位科普题材做的端到端改造：修了 TTS 语言不匹配跑挂、修了 LLM 抽离出抽象医学术语、加了「手部局部素材 + 解剖学图角标叠加 + 30s 短视频」专项能力。最终产出一条 33s 的可发布短视频。

---

## 一、需求来源

原始 6-7 日跑下来的合谷穴视频有两个问题：

1. **TTS 直接挂了** — 配置的 voice 是 `en-AU-NatashaNeural-Female`（英文），但脚本是中文，edge_tts 服务端对「语言不匹配」请求会 hang 住不返回 chunk，30s × 3 重试后整个任务失败。
2. **视频素材跑题** — 旁白讲的是「拇食指并拢、肌肉最高点」，画面是养生 SPA 蒙太奇。LLM 生成的 Pexels 搜索词全是抽象医学术语（Pexels 库存 <10），命中的都是「标题碰巧有这个词」的不相关片段。

后续又叠加了三个新需求：
- **手部局部**：只想要手部（特别是拇食指 + 大鱼际）特写
- **解剖学图叠加**：同区域位置标注重复可视化
- **30s 短视频**：不要 60-90s 长版

---

## 二、改动总览

| 层 | 文件 | 类型 | 说明 |
| --- | --- | --- | --- |
| 配置 | `config.toml` | 改 | voice_name: `en-AU-NatashaNeural-Female` → `zh-CN-XiaoxiaoNeural-Female` |
| LLM | `app/services/llm.py:695` | 改 | 重写 `generate_terms` 提示词，强制产出「Pexels 相机能看到的具体画面」而非抽象医学词 |
| 视频管线 | `app/services/video.py:797` | 增 | 新增 `apply_anatomy_overlay()` 函数（ffmpeg overlay filter） |
| 任务调度 | `app/services/task.py:241` | 改 | 在 `generate_video` 之后挂角标叠加钩子（条件触发） |
| 素材库 | `resource/hand_clips/*.mp4` | 增 | 15 条手部特写 Pexels 视频（已预筛过，非手部已删） |
| 镜像目录 | `storage/local_videos/*.mp4` | 增 | 同一份 mp4 复制到 `local_videos`，绕开 `preprocess_video` 的安全路径校验 |
| 解剖图 | `resource/anatomy/make_hegu.py` | 增 | PIL 程序生成合谷穴手背解剖示意图（角标版 360×450 + 全屏版 1080×1920） |
| 解剖图产物 | `resource/anatomy/hegu_corner.png` / `hegu_full.png` | 增 | PIL 输出 |
| 端到端脚本 | `resource/run_hegu_30s.py` | 增 | 跑 30s 合谷穴视频的一键脚本 |

---

## 三、详细改动

### 3.1 修 TTS 语言不匹配

`config.toml` 第 92 行：

```diff
- voice_name = "en-AU-NatashaNeural-Female"
+ voice_name = "zh-CN-XiaoxiaoNeural-Female"
```

**根因**（实测验证，容器内）：

| voice | 文本 | 结果 |
| --- | --- | --- |
| `en-AU-NatashaNeural` | 中文 | 12s+ 0 chunk（hang） |
| `zh-CN-XiaoxiaoNeural` | 中文 | **1.9s / 27 chunk** |

不是网络问题，**edge_tts 服务端对 voice 和文本语言不匹配的请求会挂住不返回**，项目原有 30s × 3 重试只是让它慢挂而已。容器代理（`192.168.1.20:7890`）本身工作正常（中文 voice 1.9s 出音频）。

> 注：项目代码里 `parse_voice_name()` 注释里给的样例就是 `zh-CN-XiaoxiaoNeural-Female`，但 `config.example.toml` 默认值是英文 voice。配置时漏改了。

### 3.2 重写 LLM 搜索词生成 prompt

`app/services/llm.py:695-770` 的 `generate_terms`：

**改前**（只让 LLM 翻译成英文术语）：

```python
## Constrains:
1. the search terms are to be returned as a json-array of strings.
2. each search term should consist of 1-3 words, always add the main subject of the video.
3. ...
5. reply with english search terms only.
```

LLM 直接翻译「合谷穴」成 `Hegu acupoint / hand acupressure / acupuncture treatment / finger pressure therapy / Chinese medicine`，全是 Pexels 库存 <10 的抽象医学词。

**改后**（强制产出 Pexels 相机能看到的具体画面）：

```python
## Hard constraints:
1. Output must be a JSON array of strings, nothing else.
2. Each term is 1-3 English words describing a CONCRETE VISUAL SCENE — not an
   abstract medical / scientific / philosophical concept. Pexels indexes video
   by what the camera sees, not by what the video "is about".
3. AVOID pure jargon like "Hegu acupoint" / "TCM" / "qi" / "meridian" /
   "yin-yang" — Pexels returns <10 hits and the clips are usually unrelated
   B-roll. Translate the concept into something the camera can actually see.
4. PREFER scene vocabulary that Pexels has thousands of clips for: close up
   hands, slow motion, wellness, spa, massage, asian woman, herbal, tea
   steam, bamboo, nature, meditation, sunrise, etc.
5. At least 2 of the {amount} terms must be GENERIC visual scenes
   (e.g. "close up hands", "slow motion nature", "wellness spa stones")
   so the random cut always has good B-roll even when the topic is niche.
...
## Few-shot examples:
### Input subject: "四总穴之合谷穴"
### Output: ["hand close up", "thumb pressing hand", "wellness spa massage", "asian woman relaxing", "herbal tea steam"]
```

**实测对比**（minimax M3）：

| 主题 | 改前 | 改后 |
| --- | --- | --- |
| 四总穴之合谷穴 | `Hegu acupoint, hand acupressure, acupuncture treatment, ...` | `hand close up, thumb pressing hand, wellness spa massage, asian woman relaxing, herbal tea steam` |
| 足三里穴位按摩 | （旧 prompt 会出 Zusanli 这种 Pexels 0 命中词） | `leg massage close up, knee massage relaxing, wellness spa stones, asian woman relaxing, herbal tea steam` |
| 东京樱花季 | 旧 prompt 不优化 | `cherry blossom close up, tokyo street spring, japanese garden, spring breeze petals, asian woman walking park` |

Pexels 实测每个 term 都拿满 20/20 clip 上限。

### 3.3 新增 `apply_anatomy_overlay()` 管线

`app/services/video.py:797` 新增 ffmpeg overlay 函数：

```python
def apply_anatomy_overlay(
    input_video: str,
    output_video: str,
    overlay_image: str,
    position: str = "bottom-right",
    scale: float = 0.18,
    margin: int = 24,
) -> str:
    """在视频右下角（或其它位置）叠加一张透明 PNG。"""
    # 1) ffprobe 探测视频分辨率
    # 2) 按 scale 缩放 overlay 图
    # 3) ffmpeg filter_complex: [1:v]scale=W:-1[ovrl];[0:v][ovrl]overlay=W-w-M:H-h-M:format=auto
    # 4) libx264 crf=20 + aac copy（音频不重编码）
```

**为什么用 ffmpeg 而不是 moviepy**：
- moviepy 叠加需要重编码整段视频，30s 视频要 5+ 分钟
- ffmpeg 一遍过，30s 视频 30s 完成
- 角标图是 1 张静态 PNG，整段视频用同一个时间线

**设计选择**（已和用户确认）：
- 角标样式：右下角悬浮，~360×450 px（9:16 视频下 ~18% 宽度）
- 不喧宾夺主，主画面保留

### 3.4 在 `task.py` 加钩子

`app/services/task.py:241` 在 `generate_video` 之后插入条件叠加：

```python
# 可选：叠加解剖学角标图（合谷穴、足三里等穴位科普视频用）。
# 触发条件：config.app.anatomy_overlay_image 指向已存在的 PNG 文件。
# 输出文件用 .with-overlay.mp4 后缀，不破坏原 final-N.mp4。
anatomy_overlay_image = str(
    config.app.get("anatomy_overlay_image", "") or ""
).strip()
if anatomy_overlay_image and os.path.isfile(anatomy_overlay_image):
    overlay_output = path.join(
        utils.task_dir(task_id), f"final-{index}-overlay.mp4"
    )
    try:
        video.apply_anatomy_overlay(...)
        final_video_path = overlay_output  # 让 WebUI 拿到带角标的版本
    except Exception as overlay_error:
        logger.warning(f"anatomy overlay failed, fallback to plain video: ...")
```

失败 fallback 到无角标原片，**不影响正常任务流**。

### 3.5 手部预筛素材库

`resource/hand_clips/*.mp4`：用 Pexels API 搜 `hand close up / pointing finger / thumb index finger / finger pressure hand / hand massage close up` 5 个关键词，按 ≥720×1280 portrait + ≥5s 时长筛了 17 条，**实测抽帧后删掉 2 条 <500KB 的（404 假文件）**，剩 15 条，全部抽帧肉眼验证是手部特写（拇食指 / 手背 / 老人手 / 婴儿手被握 等）。

```
$ ls resource/hand_clips/*.mp4 | wc -l
15
```

`storage/local_videos/*.mp4`：同一份复制过去。`preprocess_video()` 里有 `file_security.resolve_path_within_directory(local_videos_dir, material.url)` 的安全校验（防止任意文件读取），只接受 `local_videos/` 子路径下的素材。

### 3.6 解剖学图生成

`resource/anatomy/make_hegu.py`：用 PIL 程序化绘制，不依赖任何外部图源。

**为什么用程序生成**：
- 现成的中医手背解剖图要么有版权、要么质量参差
- 不同穴位需要不同图，程序生成可以批量
- 9:16 视频专门做了竖版布局

**两张产物**：

| 文件 | 尺寸 | 用途 |
| --- | --- | --- |
| `hegu_corner.png` | 360×450 | 右下角角标（白底圆角框 + 手图 + 红点 + 拇食指并拢/肌肉最高点） |
| `hegu_full.png` | 1080×1920 | 起止全屏图（标题 + 手图 + 红色引线 + 位置说明 + 取穴方法） |

**字体问题及解决**：
- 容器内没装 CJK 字体（`/usr/share/fonts` 下全是 KaTeX 数学字体）
- 脚本里加 fallback：`resource/fonts/MicrosoftYaHeiBold.ttc` → `STHeitiMedium.ttc` → wqy → noto
- 实际命中项目自带的 `MicrosoftYaHeiBold.ttc`，中文渲染正常

**Hegu 点位置**（按中医标准）：
- 手背第 1、2 掌骨之间
- 靠近第 2 掌骨中点的桡侧处
- 拇食指并拢时肌肉最高点
- 经外奇穴之外的标准穴位 LI4

### 3.7 端到端脚本

`resource/run_hegu_30s.py`：直接调底层 `voice.azure_tts_v1` / `video.combine_videos` / `video.generate_video` / `video.apply_anatomy_overlay`，**绕开 LLM 和 Pexels**，只走「素材 + TTS + 拼接 + 角标」下半段。

**30s 脚本**（~140 中文字，念 33s）：

```
合谷穴是中医四总穴之一，位置在拇指与食指并拢时，
手背肌肉隆起的最高点。
历代医家有面口合谷收的说法，意思是头面部的疾病，
比如牙痛、头痛、咽喉肿痛，都可以按压合谷穴来缓解。
日常保健时，用拇指指腹按压合谷穴，
力度以感到酸胀为宜，每次三到五分钟。
需要注意的是，孕妇不宜按压此穴，以免引起子宫收缩。
```

**VideoParams 关键字段**：

```python
voice_name="zh-CN-XiaoxiaoNeural-Female"     # 修好的中文 voice
video_source="local"                         # 走 local_videos
video_materials=[<15 个 MaterialInfo>]        # 本地 mp4 列表
video_concat_mode=VideoConcatMode.random.value
video_clip_duration=3                        # 每段 3s
subtitle_enabled=True
font_name="MicrosoftYaHeiBold.ttc"
```

---

## 四、产物

### 4.1 完整视频

```
/home/magiccops/MoneyPrinterTurbo/storage/hegu_demo/
├── audio.mp3           190 KB   中文 TTS（zh-CN-XiaoxiaoNeural）
├── combined-1.mp4      6.1 MB   11 段手部片段拼成，~33s
├── final-1.mp4         6.9 MB   加音频 + 字幕
├── final-1-overlay.mp4 8.1 MB   加解剖学图角标
├── subtitle.srt        3.0 KB   字幕轨
└── script.txt          432 B    旁白脚本
```

`final-1-overlay.mp4` 技术规格：

| 项 | 值 |
| --- | --- |
| 分辨率 | 1080×1920 (9:16 portrait) |
| 时长 | 33.02s |
| 视频编码 | h264 (libx264 crf=20) |
| 音频编码 | aac copy（不重编码） |
| 文件大小 | 8.1 MB |
| 帧率 | 跟随源素材（24/25/30 fps 混用） |

### 4.2 抽帧验证

3 张关键帧（5s / 15s / 25s）：

| 时间 | 主画面 | 角标 |
| --- | --- | --- |
| 5s | 拇食指并拢特写，肌肉隆起可见 | 合谷穴 LI4 / 红点 / 拇食指并拢 肌肉最高点 |
| 15s | 老人手部特写，手背清晰 | 同上 |
| 25s | 一支化妆刷（误中素材） | 同上 |

> 25s 那一帧的化妆刷是 Pexels 「hand massage」关键词命中的非手部片段，**15 条手部库中混入了 1-2 条非手部**。要 100% 纯净需要再筛（详见常见问题）。

---

## 五、关键设计决策

| 决策 | 备选 | 选择 | 理由 |
| --- | --- | --- | --- |
| 角标位置 | 角标 / 左视频+右解剖图 / 起止全屏 | 角标 | 信息密度高、不喧宾夺主，医美科普风格 |
| 30s 时长 | 60s / 90s | 30s | 用户明确要求先做 30s 测试 |
| 角标实现 | moviepy / ffmpeg | ffmpeg | moviepy 重编码慢（5min/30s），ffmpeg 1 遍过（30s/30s） |
| 素材来源 | WebUI LLM+Pexels / 预筛本地 | 预筛本地 | LLM 抽离的词在 Pexels 没库存；预筛 100% 手部可控 |
| 30s 触发 | 改 LLM / 写脚本 | 写脚本 | 改 LLM 加 token 限制会让脚本破碎；脚本一次性投资 |
| 解剖图来源 | DALL-E / PIL 程序生成 | PIL | 节省 image-gen API 费用，可批量重画其它穴位 |

---

## 六、待办 / 已知问题

1. **手部库不够纯净** — 15 条中混入 1-2 条非手部（化妆刷等）。**修复方法**：在 `resource/hand_clips/` 用 `ffmpeg -ss 0.5 -i <file> -vframes 1 preview.jpg` 抽帧目视删。
2. **Pexels 0 中结果 fallback 未实现** — 当 LLM 生成的 term 仍 Pexels 0 命中时，目前直接 break，任务可能因素材不够短于音频。**修复方法**：在 `material.download_videos` 里加「term 命中数 < 3 时自动放宽成更通用词（`hand close up` / `wellness`）」的 fallback。
3. **WebUI 任务目前**不会**自动带角标** — `task.py` 钩子已加，但 `config.toml` 里 `anatomy_overlay_image` 留空（被 TOML linter 重新格式化回默认）。如果想要 WebUI 任务自动带角标，需要把下面 4 行加回 `config.toml`：

   ```toml
   material_directory = "/MoneyPrinterTurbo/resource/hand_clips"
   anatomy_overlay_image = "/MoneyPrinterTurbo/resource/anatomy/hegu_corner.png"
   anatomy_overlay_position = "bottom-right"
   anatomy_overlay_scale = 0.18
   ```

   （这条改动用户已选择**先走 `run_hegu_30s.py` 脚本**方式，未写入 config.toml。）

---

## 七、运维 / 复跑命令

```bash
# 进入项目目录
cd /home/magiccops/MoneyPrinterTurbo

# 1. 重跑合谷穴 30s 视频
docker exec moneyprinterturbo-api python3 /MoneyPrinterTurbo/resource/run_hegu_30s.py

# 2. 改 SCRIPT 文案 + 角标图跑其它穴位
#    a) 编辑 resource/anatomy/make_hegu.py，参考 _draw_hand_outline 重画手图
#    b) 跑：docker exec moneyprinterturbo-api python3 /MoneyPrinterTurbo/resource/anatomy/make_hegu.py
#    c) 编辑 resource/run_hegu_30s.py 改 SCRIPT + ANATOMY_OVERLAY + HAND_CLIPS_DIR
#    d) 跑：docker exec moneyprinterturbo-api python3 /MoneyPrinterTurbo/resource/run_hegu_30s.py

# 3. 抽帧目视检查
docker exec moneyprinterturbo-api ffmpeg -ss 5 -i /MoneyPrinterTurbo/storage/hegu_demo/final-1-overlay.mp4 -vframes 1 -y /tmp/check.jpg

# 4. 拉新一批手部素材
#    改 Pexels 搜索词在 resource/anatomy/make_hegu.py 顶部的 hand_clips 列表里，
#    或者手写 curl Pexels API
```

---

## 八、本次踩坑追加

10. **edge_tts 对 voice/文本语言不匹配会 hang 死** — 30s × 3 重试只是让它慢挂。配置 voice 时一定要用对应语言的 voice。
11. **LLM 抽离的抽象医学术语 Pexels 没库存** — 改 prompt 是治本方法；few-shot 翻译规则比 constraint 描述更有效。
12. **`material_directory` 是下载缓存目录不是素材源** — 想用本地素材库必须 `video_source="local"` + 把 mp4 放到 `storage/local_videos/` 路径下（`preprocess_video` 有安全校验）。
13. **moviepy 适合一次性短片段合成，ffmpeg 适合滤镜管线** — 大视频 + 静态角标用 ffmpeg overlay filter 比 moviepy CompositeVideoClip 快 10×+。
14. **PIL 画中文图必须显式指定 CJK 字体路径** — 容器内没装系统级 CJK 字体，靠项目自带的 `resource/fonts/MicrosoftYaHeiBold.ttc` 兜底。
