import aiohttp
import asyncio
import aiofiles
import base64
import json
import uuid
import re
import random
from datetime import datetime, timedelta
from pathlib import Path
from astrbot.api import logger

from .security import safe_download_image, validate_model_name


class ImageGeneratorState:
    """图像生成器状态管理类，用于处理并发安全"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.api_key_index = 0
        self.last_saved_image = {"url": None, "path": None}

    async def get_current_api_key(self, api_keys):
        """获取当前使用的API密钥"""
        async with self._lock:
            if api_keys and isinstance(api_keys, list) and len(api_keys) > 0:
                return api_keys[self.api_key_index % len(api_keys)]
            return None

    async def rotate_to_next_api_key(self, api_keys):
        """轮换到下一个API密钥"""
        async with self._lock:
            if api_keys and isinstance(api_keys, list) and len(api_keys) > 1:
                self.api_key_index = (self.api_key_index + 1) % len(api_keys)
                logger.info(f"已轮换到下一个API密钥，当前索引: {self.api_key_index}")

    async def update_saved_image(self, url, path):
        """更新保存的图像信息"""
        async with self._lock:
            self.last_saved_image = {"url": url, "path": path}

    async def get_saved_image_info(self):
        """获取最后保存的图像信息"""
        async with self._lock:
            return self.last_saved_image["url"], self.last_saved_image["path"]


# 全局状态管理实例
_state = ImageGeneratorState()

# 响应文本中图片信息的匹配模式
_DATA_URL_PATTERN = re.compile(
    r"(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)"
)
_HTTP_URL_PATTERN = re.compile(r"(https?://[^\s)]+)")
_SUPPORTED_ASPECT_RATIOS = {
    "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
}
_IMAGE_SIZE_MAP = {
    1024: "1K",
    2048: "2K",
    4096: "4K",
}


def _normalize_aspect_ratio(value: str | None) -> str:
    """标准化宽高比；无效值返回空字符串，避免请求 400。"""
    ratio = (value or "").strip()
    if not ratio:
        return ""
    if ratio not in _SUPPORTED_ASPECT_RATIOS:
        logger.warning(f"忽略不支持的宽高比参数: {ratio}")
        return ""
    return ratio


def _normalize_image_size(value: int | str | None) -> str:
    """将分辨率值映射为 Vertex API 接受的 imageSize。"""
    if value in (None, "", 0, "0"):
        return ""

    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in {"1K", "2K", "4K"}:
            return normalized
        try:
            value = int(normalized)
        except ValueError:
            logger.warning(f"忽略无法识别的分辨率参数: {value}")
            return ""

    if isinstance(value, int):
        image_size = _IMAGE_SIZE_MAP.get(value, "")
        if not image_size:
            logger.warning(f"忽略不支持的分辨率参数: {value}")
        return image_size

    logger.warning(f"忽略无法识别的分辨率参数类型: {type(value)}")
    return ""


async def cleanup_old_images(data_dir=None):
    """
    清理超过15分钟的图像文件

    Args:
        data_dir (Path): 数据目录路径，必须提供
    """
    if data_dir is None:
        logger.warning("cleanup_old_images: 未提供 data_dir，跳过清理")
        return

    try:
        images_dir = data_dir / "images"

        if not images_dir.exists():
            return

        current_time = datetime.now()
        cutoff_time = current_time - timedelta(minutes=15)

        # 查找images目录下的所有图像文件
        # B-01: 优化为一次遍历，减少 IO
        for file_path in images_dir.iterdir():
            if file_path.is_file() and file_path.name.startswith(("vertex_image_", "gemini_image_")):
                try:
                    file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if file_mtime < cutoff_time:
                        file_path.unlink()
                        logger.debug(f"已清理过期图像: {file_path.name}")
                except OSError as e:
                    logger.warning(f"清理文件 {file_path} 时出错: {e}")

    except Exception as e:
        logger.error(f"清理过期图像时发生错误: {e}")


async def save_base64_image(base64_string, image_format="png", data_dir=None):
    """
    将base64编码的图像保存到文件

    Args:
        base64_string (str): base64编码的图像数据
        image_format (str): 图像格式
        data_dir (Path): 数据目录路径，必须提供

    Returns:
        tuple: (image_url, image_path)
    """
    if data_dir is None:
        logger.error("save_base64_image: 未提供 data_dir，无法保存图像")
        return None, None

    try:
        images_dir = data_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        # B-01: 概率触发清理 (5%)，且不阻塞当前请求
        if random.random() < 0.05:
            asyncio.create_task(cleanup_old_images(data_dir))

        # 生成唯一文件名
        unique_id = uuid.uuid4().hex[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"vertex_image_{timestamp}_{unique_id}.{image_format}"
        file_path = images_dir / filename

        # 解码并保存图像
        image_data = base64.b64decode(base64_string)
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(image_data)

        image_url = f"file://{file_path}"
        image_path = str(file_path)

        await _state.update_saved_image(image_url, image_path)

        logger.info(f"图像已保存: {file_path}")
        return image_url, image_path

    except Exception as e:
        logger.error(f"保存图像时发生错误: {e}")
        return None, None


async def get_next_api_key(api_keys):
    """获取当前API密钥"""
    return await _state.get_current_api_key(api_keys)


async def rotate_to_next_api_key(api_keys):
    """轮换到下一个API密钥"""
    await _state.rotate_to_next_api_key(api_keys)


async def get_saved_image_info():
    """获取最后保存的图像信息"""
    return await _state.get_saved_image_info()


async def generate_image_vertex(
    prompt,
    api_key,
    model: str = "gemini-3-pro-image-preview",
    input_images=None,
    max_retry_attempts: int = 3,
    data_dir=None,
    safety_settings: dict | None = None,
    aspect_ratio: str = "",
    resolution: int = 0,
):
    """
    使用 Google Vertex AI Gemini 模型生成图像

    Args:
        prompt (str): 图像生成提示词
        api_key: API密钥，可以是字符串或字符串列表
        model (str): 使用的模型名称
        input_images: 输入图像列表（base64编码）
        max_retry_attempts (int): 最大重试次数
        data_dir: 数据存储目录，用于保存生成的图像
        safety_settings: 安全过滤器配置，包含以下可选键：
            - hate_speech: 仇恨言论过滤阈值
            - harassment: 骚扰内容过滤阈值
            - sexually_explicit: 色情内容过滤阈值
            - dangerous_content: 危险内容过滤阈值
            阈值可选值: "OFF", "BLOCK_NONE", "BLOCK_ONLY_HIGH", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE"

    Returns:
        tuple: (image_url, image_path, error_reason) 
               成功时 error_reason 为 None
               失败时 image_url 和 image_path 为 None，error_reason 包含错误类型：
               - "SAFETY_BLOCKED": 被安全策略阻止
               - "API_ERROR": API 配置或网络错误
               - "NO_API_KEY": 未配置 API 密钥
               - "RATE_LIMITED": API 请求频率限制
    """
    # 标准化 API 密钥列表
    if isinstance(api_key, list):
        api_keys = [k for k in (str(k).strip() for k in api_key or []) if k]
    elif api_key:
        api_keys = [str(api_key).strip()]
    else:
        api_keys = []

    if not api_keys:
        logger.error("generate_image_vertex: 未提供 Vertex AI API 密钥")
        return None, None, "NO_API_KEY"

    model = validate_model_name(model)

    # 构建 Vertex AI API URL
    base_url = "https://aiplatform.googleapis.com/v1/publishers/google/models"
    
    normalized_aspect_ratio = _normalize_aspect_ratio(aspect_ratio)
    normalized_image_size = _normalize_image_size(resolution)
    logger.info(
        "Vertex 图像参数: "
        f"model={model}, aspect_ratio={normalized_aspect_ratio or 'auto'}, "
        f"image_size={normalized_image_size or 'auto'}, input_images={len(input_images or [])}"
    )

    # 构建请求内容
    parts = []
    
    # 添加文本提示
    full_prompt = (
        "Generate an image based on the following description. "
        "Output only the image, no text explanation needed.\n\n"
        f"{prompt}"
    )
    parts.append({"text": full_prompt})
    
    # 添加输入图像（如果有）
    if input_images:
        for img_base64 in input_images:
            if img_base64:
                # 清理 base64 字符串
                clean_base64 = img_base64
                if clean_base64.startswith("data:"):
                    # 提取纯 base64 部分
                    try:
                        clean_base64 = clean_base64.split(",", 1)[1]
                    except IndexError:
                        pass
                
                parts.append({
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": clean_base64
                    }
                })
        logger.info(f"已添加 {len(input_images)} 张参考图片")

    # 构建安全过滤器配置
    # 默认全部关闭 (OFF)，用户可通过配置自定义
    # 注意：PROHIBITED_CONTENT (CSAM) 是不可配置的，无法关闭
    default_thresholds = {
        "hate_speech": "OFF",
        "harassment": "OFF",
        "sexually_explicit": "OFF",
        "dangerous_content": "OFF",
    }
    
    if safety_settings:
        for key in default_thresholds:
            if key in safety_settings and safety_settings[key]:
                default_thresholds[key] = safety_settings[key]
    
    safety_settings_payload = [
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": default_thresholds["hate_speech"]},
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": default_thresholds["harassment"]},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": default_thresholds["sexually_explicit"]},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": default_thresholds["dangerous_content"]},
    ]

    generation_config = {
        "temperature": 1.0,
        "topP": 0.95,
        "maxOutputTokens": 8192,
    }

    image_config = {}
    if normalized_aspect_ratio:
        image_config["aspectRatio"] = normalized_aspect_ratio
    if normalized_image_size:
        image_config["imageSize"] = normalized_image_size
    if image_config:
        generation_config["imageConfig"] = image_config

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": parts
            }
        ],
        "generationConfig": generation_config,
        "safetySettings": safety_settings_payload
    }

    timeout = aiohttp.ClientTimeout(total=300)

    # 记录连续 429 错误次数，用于判断是否全部因限流失败
    rate_limit_count = 0
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for retry_attempt in range(max_retry_attempts):
            try:
                current_key = await get_next_api_key(api_keys)
                if not current_key:
                    logger.error("无可用的 API 密钥")
                    return None, None, "NO_API_KEY"

                url = f"{base_url}/{model}:generateContent?key={current_key}"
                
                headers = {
                    "Content-Type": "application/json"
                }

                if retry_attempt > 0:
                    delay = min(2 ** retry_attempt, 10)
                    logger.info(
                        f"第 {retry_attempt + 1} 次重试，等待 {delay} 秒..."
                    )
                    await asyncio.sleep(delay)

                async with session.post(url, json=payload, headers=headers) as response:
                    response_text = await response.text()
                    
                    if retry_attempt == 0:
                        logger.debug(f"Vertex AI API 响应状态: {response.status}")

                    if response.status == 200:
                        try:
                            data = json.loads(response_text)
                        except Exception as e:
                            logger.error(f"解析响应 JSON 失败: {e}")
                            await rotate_to_next_api_key(api_keys)
                            continue

                        # 解析响应，查找生成的图像
                        image_url = None
                        image_path = None
                        image_format = "png"
                        base64_string = None

                        if "candidates" in data and data["candidates"]:
                            candidate = data["candidates"][0]
                            content = candidate.get("content", {})
                            parts = content.get("parts", [])

                            for part in parts:
                                # 检查 inlineData（图像数据）
                                if "inlineData" in part:
                                    inline_data = part["inlineData"]
                                    mime_type = inline_data.get("mimeType", "image/png")
                                    base64_string = inline_data.get("data")
                                    
                                    # 从 MIME 类型提取格式
                                    if "/" in mime_type:
                                        image_format = mime_type.split("/")[1].split(";")[0]
                                    
                                    if base64_string:
                                        logger.info(f"从响应中获取到图像数据，格式: {image_format}")
                                        break
                                
                                # 检查文本中是否包含 base64 图像
                                elif "text" in part:
                                    text = part["text"]
                                    # 尝试匹配 data URL
                                    data_match = _DATA_URL_PATTERN.search(text)
                                    if data_match:
                                        candidate_url = data_match.group(1)
                                        try:
                                            header, base64_part = candidate_url.split(",", 1)
                                            image_format = header.split("/")[1].split(";")[0]
                                            base64_string = base64_part
                                            logger.info("从文本响应中提取到 base64 图像")
                                            break
                                        except Exception as e:
                                            logger.warning(f"解析文本中的 data URL 失败: {e}")
                                    
                                    # 尝试匹配 HTTP URL
                                    url_match = _HTTP_URL_PATTERN.search(text)
                                    if url_match:
                                        image_url = url_match.group(1)
                                        logger.info(f"从文本响应中提取到图像 URL: {image_url}")

                        # 如果获取到 base64 图像数据，保存到文件
                        if base64_string:
                            image_url, image_path = await save_base64_image(
                                base64_string, image_format, data_dir
                            )
                            if image_url and image_path:
                                return image_url, image_path, None

                        # 如果获取到 URL，尝试下载图像
                        if image_url and image_url.startswith("http"):
                            try:
                                img_data = await safe_download_image(image_url)
                                if img_data:
                                    base64_string = base64.b64encode(img_data).decode("utf-8")
                                    image_url, image_path = await save_base64_image(
                                        base64_string, image_format, data_dir
                                    )
                                    if image_url and image_path:
                                        return image_url, image_path, None
                            except Exception as e:
                                logger.warning(f"下载图像失败: {e}")

                        # 检查是否有安全过滤导致的阻止
                        if "promptFeedback" in data:
                            feedback = data["promptFeedback"]
                            if "blockReason" in feedback:
                                logger.warning(f"请求被安全过滤阻止: {feedback['blockReason']}")
                                return None, None, "SAFETY_BLOCKED"

                        # 检查 finishReason
                        if "candidates" in data and data["candidates"]:
                            finish_reason = data["candidates"][0].get("finishReason", "")
                            if finish_reason in ["IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "SAFETY"]:
                                logger.warning(f"图像生成被安全策略阻止: {finish_reason}")
                                return None, None, "SAFETY_BLOCKED"

                        logger.warning(f"响应中未找到图像数据，第 {retry_attempt + 1} 次尝试")
                        await rotate_to_next_api_key(api_keys)

                    elif response.status == 429:
                        rate_limit_count += 1
                        logger.warning("API 请求频率限制，尝试轮换密钥")
                        await rotate_to_next_api_key(api_keys)

                    elif response.status == 400:
                        logger.error(
                            f"请求参数错误: aspect_ratio={normalized_aspect_ratio or 'auto'}, "
                            f"image_size={normalized_image_size or 'auto'}, "
                            f"response={response_text[:500]}"
                        )
                        # 400 错误通常是请求格式问题，不需要轮换密钥
                        return None, None, "BAD_REQUEST"

                    elif response.status == 401 or response.status == 403:
                        logger.error(f"API 认证失败 (状态码 {response.status})，尝试轮换密钥")
                        await rotate_to_next_api_key(api_keys)

                    else:
                        logger.warning(
                            f"API 请求失败，状态码: {response.status}，响应: {response_text[:500]}"
                        )
                        await rotate_to_next_api_key(api_keys)

            except asyncio.TimeoutError:
                logger.warning(f"API 请求超时，第 {retry_attempt + 1} 次尝试")
                await rotate_to_next_api_key(api_keys)

            except aiohttp.ClientError as e:
                logger.warning(f"网络请求错误: {e}，第 {retry_attempt + 1} 次尝试")
                await rotate_to_next_api_key(api_keys)

            except Exception as e:
                logger.error(f"未预期的错误: {e}")
                await rotate_to_next_api_key(api_keys)

    # 判断是否全部因限流失败
    if rate_limit_count >= max_retry_attempts:
        logger.error("Vertex AI API 调用失败：所有重试均因频率限制 (429) 被拒绝")
        return None, None, "RATE_LIMITED"
    
    logger.error("Vertex AI API 调用失败，已达到最大重试次数")
    return None, None, "API_ERROR"


# 保留旧函数名作为别名，方便兼容
async def generate_image_openai(*args, **kwargs):
    """
    已废弃：请使用 generate_image_vertex
    """
    logger.warning(
        "generate_image_openai 已废弃，请使用 generate_image_vertex"
    )
    return await generate_image_vertex(*args, **kwargs)
