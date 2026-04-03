# AstrBot Vertex AI 图像生成插件

基于 Google Vertex AI Gemini 模型的 AstrBot 图像生成插件，使用 Gemini 图像生成模型生成高质量图像。

## 功能特点

- 🖼️ **Vertex AI Gemini 图像生成**: 使用 Google Vertex AI Gemini 模型生成图片
- 🎨 **多步骤改图/参考图会话**: 逐条发送多张图片 + 文字描述，支持改图与参考图
- 📐 **宽高比控制**: Midjourney 风格标志 `--16:9` `-l` `-p` `-s` 等
- 📏 **分辨率控制**: 支持 `--1k` `--2k` `--4k` 作为分辨率偏好提示
- ♻️ **智能重试机制**: 支持可配置的自动重试，提高请求成功率和稳定性
- 🔇 **临时消息**: 会话状态提示 5 秒后自动撤回，减少群消息刷屏
- 🔗 **智能文件传输**: 支持本地和远程服务器的文件传输
- 🧹 **自动清理**: 自动清理超过15分钟的历史图像文件
- 🔒 **群过滤**: 支持白名单/黑名单模式的群过滤
- ⏱️ **限流控制**: 支持按群配置调用频率限制
- 🛡️ **安全增强**: 内置 SSRF 防护，防止内网探测攻击
- 🚦 **并发控制**: 全局并发请求限制，防止服务器过载

## 安装配置

### 1. 获取 API Key

前往 [Google Cloud Console](https://console.cloud.google.com/) 创建 Vertex AI API Key。

> ⚠️ **注意**：本插件**仅支持 Vertex AI**，不支持 AI Studio 的 Key。
>
> 💰 新用户可获得 **$300 免费额度**，[详见说明](https://cloud.google.com/free/docs/free-cloud-features)

### 2. 安装插件

在 AstrBot 插件市场搜索 `vertex` 或 `Vertex AI` 安装本插件。

> ⚠️ **注意**：如果之前安装过其他使用相同指令的图像生成插件（如 `astrbot_plugin_openai_image-command`），请先**停用该插件并重启 AstrBot**，否则两个插件会同时响应同一条指令。

### 3. 配置指令前缀

本插件的指令使用英文名称（`nano`、`edit`、`ok`、`cancel`、`imghelp`）。使用 AstrBot 默认的 `/` 指令前缀，即 `/nano`、`/edit` 等。

### 4. 配置参数

#### 通过Web界面配置

1. 访问AstrBot的Web管理界面
2. 进入"插件管理"页面
3. 找到插件
4. 点击"配置"按钮进行可视化配置

#### 配置参数说明

- **vertex_api_key**: Vertex AI API 密钥列表（⚠️ 仅支持 Vertex AI，AI Studio 的 Key 不可用）
- **model_name**: 使用的 Gemini 图像生成模型名称（直接填写模型名，无需 `google/` 前缀）
  - 默认值：`gemini-3-pro-image-preview`
  - 推荐：`gemini-3.1-flash-image-preview`
  - 可用模型参考：[Vertex AI 图像生成模型](https://cloud.google.com/vertex-ai/generative-ai/docs/image/generate-images#img-gen-models)

- **aspect_ratio**: 生成图像的默认宽高比，留空则由模型自动决定
  - 可选值：`1:1`、`16:9`、`9:16`、`4:3`、`3:4`
  - 也可在指令中用标志覆盖（见下方指令说明）

- **default_resolution**: 生成图像的默认分辨率（像素），设为 `0` 则由模型自动决定
  - 可选值：`0`（自动）、`1024`（1K）、`2048`（2K）、`4096`（4K）
  - 也可在指令中用 `--1k` / `--2k` / `--4k` 标志覆盖
  - 这是对模型的分辨率偏好提示，实际输出仍受模型能力限制

- **max_retry_attempts**: 每个请求的最大重试次数（默认：3 次，推荐 2-5 次）
- **nap_server_address**: NAPcat 服务地址（同服务器填写 `localhost`）
- **nap_server_port**: 文件传输端口（默认 3658）
- **group_filter_mode**: 群过滤模式，可选 `none` / `whitelist` / `blacklist`
- **group_filter_list**: 群过滤名单（字符串列表）
- **rate_limit_max_calls_per_group**: 单群在一个限流周期内允许调用插件指令的最大次数（`0` 不启用）
- **rate_limit_period_seconds**: 限流周期长度（秒），默认 `60` 秒
- **max_edit_images**: `/edit` 会话最多保留的参考图数量，默认 `10`

### Zeabur / Linux VPS 部署建议

- 推荐使用 **Python 3.10+**
- 如果 AstrBot 与 NAPcat 在同一台 Zeabur Linux VPS 上，`nap_server_address` 填 `localhost`
- 如果你启用了 `callback_api_base`，要确保该地址能被 AstrBot 所在环境正确访问
- 运行时生成的图片和缓存文件不应提交到 GitHub，仓库已通过 `.gitignore` 忽略 `images/` 与 `__pycache__/`
- 建议保留默认的 `max_edit_images=10`，避免参考图过多导致内存占用或 Vertex 请求体过大

## 支持的指令

| 指令 | 说明 | 用法示例 |
|------|------|----------|
| `/nano` | 根据文字描述生成图片 | `/nano 一只橙色猫咪 --16:9 --2k` |
| `/edit` | 开启多步骤改图/参考图会话 | `/edit` |
| `/ok` | 确认改图会话，开始处理 | `/ok` |
| `/cancel` | 取消当前改图会话 | `/cancel` |
| `/imghelp` | 列出本插件支持的指令 | `/imghelp` |

### Midjourney 风格标志

可在描述文字的**任意位置**添加标志（推荐放末尾）：

**宽高比标志：**

| 标志 | 效果 |
|------|------|
| `--16:9` 或 `-l` | 横屏（Landscape） |
| `--9:16` 或 `-p` | 竖屏（Portrait） |
| `--1:1` 或 `-s` | 方形（Square） |
| `--4:3` | 4:3 |
| `--3:4` | 3:4 |

**分辨率标志：**

| 标志 | 效果 |
|------|------|
| `--1k` | 1024px |
| `--2k` | 2048px |
| `--4k` | 4096px |

**使用示例：**

```
/nano 赛博朋克风格的城市夜景 --16:9 --4k
/nano a cute cat sitting on a keyboard -s --2k
```

### `/edit` 改图/参考图流程

改图采用多步骤会话模式，方便逐条发送多张图片：

```
1. 发送 /edit         → 开启改图会话
2. 发送图片1          → 「已收到 1 张图片（共 1 张）」
3. 发送图片2          → 「已收到 1 张图片（共 2 张）」
4. 发送文字描述       → 「已收到描述文字（比例:16:9，2K）：「...」」
5. 发送 /ok           → 开始处理改图
```

**支持的使用场景：**
- **改图**：发送一张图片 + "把这张图改成水彩风格"
- **参考图生成**：发送多张参考图 + "参考这些图片的风格，生成一张新的城市风景"
- **风格迁移**：发送图片A + 图片B + "把图片1改成图片2的风格"

**注意事项：**
- 会话状态提示（收到图片、收到描述等）会在 **5 秒后自动撤回**，不会刷屏
- 超过 **60 秒** 未操作会自动取消会话
- 发送 `/cancel` 可随时取消
- 最终的改图结果不会被撤回
- `/edit` 消息中也可以直接附带图片
- 描述文字中同样支持 midjourney 风格标志（如 `--16:9 --2k`）
- 会话最多保留 **10 张参考图**，超出的图片会被忽略并提示
- 如未发送描述文字，默认使用"保持主体内容不变，进行美化"

> ⚠️ **已知限制**：使用「回复图片消息」方式时，AstrBot 框架可能无法获取被引用消息中的图片（aiocqhttp 适配器仅传递引用消息 ID，不一定包含图片内容）。如遇此问题，建议直接发送图片。

### 支持的模型

- `gemini-3.1-flash-image-preview` (推荐)
- `gemini-3-pro-image-preview` (最高支持 4096px)
- `gemini-2.5-flash-image` (支持 1024px)

## 文件结构

```
astrbot_plugin_vertex_image-command/
├── main.py                 # 插件主文件
├── metadata.yaml          # 插件元数据
├── _conf_schema.json      # 配置模式定义
├── utils/
│   ├── ttp.py            # Vertex AI API 调用
│   └── file_send_server.py # 文件传输工具
├── images/               # 生成的图像存储目录
├── requirements.txt    # 依赖文件
├── LICENSE              # 许可证文件
└── README.md           # 项目说明文档
```

## 错误处理

插件包含完善的错误处理机制：

- **API 调用失败处理**: 记录详细的 Vertex AI API 错误信息
- **Base64 图像解码错误处理**: 自动检测和修复格式问题
- **参考图片处理异常捕获**: 当参考图片转换失败时的回退机制
- **文件传输异常捕获**: 网络传输失败时的错误提示
- **自动清理失败处理**: 清理历史文件时的异常保护
- **安全过滤处理**: 当图像被安全策略阻止时的友好提示
- **临时消息撤回失败处理**: 无权限撤回时静默降级

## 致谢

本插件基于 [AstrBot_plugin_openai_image-command](https://github.com/exynos967/AstrBot_plugin_openai_image-command) 修改而来，感谢原作者 **薄暝 (exynos967)**

主要改动：将后端 API 从 OpenAI 兼容格式更换为 Google Vertex AI Gemini API，以便使用 Google Cloud 的图像生成服务。
