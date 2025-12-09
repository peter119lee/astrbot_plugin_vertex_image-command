# AstrBot Vertex AI 图像生成插件

基于 Google Vertex AI Gemini 模型的 AstrBot 图像生成插件，使用 Gemini 图像生成模型生成高质量图像。

## 功能特点

- 🖼️ **Vertex AI Gemini 图像生成**: 使用 Google Vertex AI Gemini 模型生成图片
- 🎨 **参考图片支持**: 支持基于参考图片进行图像生成和编辑
- ♻️ **智能重试机制**: 支持可配置的自动重试，提高请求成功率和稳定性
- � **异步处理**: 基于 asyncio 的高性能异步图像生成
- 🔗 **智能文件传输**: 支持本地和远程服务器的文件传输
- 🧹 **自动清理**: 自动清理超过15分钟的历史图像文件
- 🔒 **群过滤**: 支持白名单/黑名单模式的群过滤
- ⏱️ **限流控制**: 支持按群配置调用频率限制

## 安装配置

### 1. 获取 API Key

前往 [Google Cloud Console](https://console.cloud.google.com/) 创建 Vertex AI API Key。

> ⚠️ **注意**：本插件**仅支持 Vertex AI**，不支持 AI Studio 的 Key。
>
> 💰 新用户可获得 **$300 免费额度**，[详见说明](https://cloud.google.com/free/docs/free-cloud-features)

### 2. 安装插件

在 AstrBot 插件市场搜索 `vertex` 或 `Vertex AI` 安装本插件。

> ⚠️ **注意**：如果之前安装过其他使用相同指令的图像生成插件（如 `astrbot_plugin_openai_image-command`），请先**停用该插件并重启 AstrBot**，否则两个插件会同时响应同一条指令。好消息是 AstrBot 的缓存回收机制正常工作，停用后立即生效，无需重启。

### 3. 配置参数

#### 通过Web界面配置

1. 访问AstrBot的Web管理界面
2. 进入"插件管理"页面
3. 找到插件
4. 点击"配置"按钮进行可视化配置

#### 配置参数说明

- **vertex_api_key**: Vertex AI API 密钥列表（⚠️ 仅支持 Vertex AI，AI Studio 的 Key 不可用）
- **model_name**: 使用的 Gemini 图像生成模型名称（直接填写模型名，无需 `google/` 前缀）
  - 默认值：`gemini-3-pro-image-preview`
  - 可用模型参考：[Vertex AI 图像生成模型](https://cloud.google.com/vertex-ai/generative-ai/docs/image/generate-images#img-gen-models)

- **max_retry_attempts**: 每个请求的最大重试次数（默认：3 次，推荐 2-5 次）
- **nap_server_address**: NAP cat 服务地址（同服务器填写 `localhost`）
- **nap_server_port**: 文件传输端口（默认 3658）
- **group_filter_mode**: 群过滤模式，可选 `none` / `whitelist` / `blacklist`：
  - `none`: 不做群过滤（默认）；
  - `whitelist`: 仅名单内群允许使用插件指令；
  - `blacklist`: 名单内群不会响应插件指令。
- **group_filter_list**: 群过滤名单（字符串列表）。结合 `group_filter_mode` 使用：
  - 当为 `whitelist` 时，列表作为白名单；
  - 当为 `blacklist` 时，列表作为黑名单；
  - `none` 时此列表不生效。
- **rate_limit_max_calls_per_group**: 单群在一个限流周期内允许调用插件指令的最大次数。设置为 `0` 表示不启用限流。
- **rate_limit_period_seconds**: 限流周期长度（秒），与 `rate_limit_max_calls_per_group` 搭配使用，默认 `60` 秒。

## 技术实现

### 核心组件

- **main.py**: 插件主要逻辑，继承自 AstrBot 的 Star 类
- **utils/ttp.py**: Vertex AI Gemini API 调用和图像处理逻辑
- **utils/file_send_server.py**: 文件传输工具

### 支持的指令

| 指令 | 说明 | 用法示例 |
|------|------|----------|
| `/生图` | 根据文字描述生成图片 | `/生图 一只橙色猫咪` |
| `/改图` | 基于已有图片进行改图 | 发送图片后 `/改图 变成水彩风格` |
| `/img帮助` | 列出本插件支持的指令 | `/img帮助` |

### `/改图` 指令用法

支持以下格式（群聊中需要先 @Bot 唤醒）：

```
✅ 图片 + /改图 描述       （推荐）
✅ /改图 描述 + 图片
✅ 回复图片消息 + /改图 描述
❌ /改图 + 图片 + 描述     （不支持，图片在指令和描述中间）
```

> ⚠️ **已知限制**：使用「回复图片消息」方式时，AstrBot 框架可能无法获取被引用消息中的图片（aiocqhttp 适配器仅传递引用消息 ID，不一定包含图片内容）。如遇此问题，建议使用「图片 + /改图 描述」的方式。

**多图支持**：所有图片相关指令都支持同时发送多张参考图片（最多 9 张）

### 支持的模型

- `gemini-3-pro-image-preview` (推荐，最高支持 4096px)
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
- **详细的错误日志输出**: 便于调试和问题定位

## 致谢

本插件基于 [AstrBot_plugin_openai_image-command](https://github.com/exynos967/AstrBot_plugin_openai_image-command) 修改而来，感谢原作者 **薄暝 (exynos967)** 

主要改动：将后端 API 从 OpenAI 兼容格式更换为 Google Vertex AI Gemini API，以便使用 Google Cloud 的图像生成服务。

本插件的开发过程中使用了 **Claude** 辅助编程。
