import asyncio
import base64
import re
import time
from dataclasses import dataclass, field
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.all import Image, Plain
from astrbot.core.message.components import Reply
from .utils.ttp import generate_image_vertex
from .utils.file_send_server import send_file
from .utils.security import safe_download_image, validate_model_name


@dataclass
class EditSession:
    """多步骤改图会话状态"""
    images: list = field(default_factory=list)
    description: str = ""
    ratio: str = ""
    resolution: int = 0
    created_at: float = field(default_factory=time.time)


@register("astrbot_plugin_vertex_image_command", "YanL", "使用 Google Vertex AI 生成图片", "1.0.0")
class MyPlugin(Star):
    _DEFAULT_EDIT_SESSION_TIMEOUT_SECONDS = 60
    _DEFAULT_MAX_EDIT_IMAGES = 10

    def __init__(self, context: Context, config: dict):
        super().__init__(context)

        # Vertex AI 配置
        self.vertex_api_keys = self._normalize_api_keys(config.get("vertex_api_key"))

        # 模型配置
        self.model_name = self._resolve_model_name(
            config.get("model_name", "gemini-3-pro-image-preview")
        )

        # 重试配置
        self.max_retry_attempts = int(config.get("max_retry_attempts", 3) or 3)

        # NAP 文件服务器配置
        self.nap_server_address = config.get("nap_server_address")
        self.nap_server_port = config.get("nap_server_port")

        # 群过滤配置（模式 + 名单）
        self.group_filter_mode = str(config.get("group_filter_mode", "none") or "none").strip().lower()
        self.group_filter_list = self._normalize_id_list(config.get("group_filter_list"))

        # 限流配置（按群）
        self.rate_limit_max_calls_per_group = int(config.get("rate_limit_max_calls_per_group", 0) or 0)
        self.rate_limit_period_seconds = int(config.get("rate_limit_period_seconds", 60) or 60)

        # 限流状态：group_id -> (window_start_ts, count)
        self._rate_limit_state: dict[str, tuple[float, int]] = {}
        self._rate_limit_lock = asyncio.Lock()

        # 安全过滤器配置
        self.safety_settings = {
            "hate_speech": config.get("safety_filter_hate_speech", "OFF"),
            "harassment": config.get("safety_filter_harassment", "OFF"),
            "sexually_explicit": config.get("safety_filter_sexually_explicit", "OFF"),
            "dangerous_content": config.get("safety_filter_dangerous_content", "OFF"),
        }

        # 图像生成配置
        self.aspect_ratio = (config.get("aspect_ratio") or "").strip()
        self.default_resolution = int(config.get("default_resolution", 0) or 0)
        self.edit_session_timeout_seconds = self._DEFAULT_EDIT_SESSION_TIMEOUT_SECONDS
        self.max_edit_images = max(
            1,
            int(
                config.get(
                    "max_edit_images", self._DEFAULT_MAX_EDIT_IMAGES
                )
                or self._DEFAULT_MAX_EDIT_IMAGES
            ),
        )

        # C-01: 全局并发限制 (例如 10)
        self._concurrency_limit = asyncio.Semaphore(10)

        # 改图多步骤会话状态：session_key -> EditSession
        self._edit_sessions: dict[str, EditSession] = {}

    # ── 会话管理工具方法 ──────────────────────────────────────────

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        """生成当前用户的会话 key（群+用户 或 私聊用户）"""
        user_id = "unknown"
        if hasattr(event, "get_sender_id"):
            user_id = str(event.get_sender_id() or "unknown")
        group_id = None
        try:
            group_id = event.get_group_id()
        except AttributeError:
            pass
        if group_id:
            return f"{group_id}_{user_id}"
        return f"private_{user_id}"

    def _resolve_model_name(self, raw_name: str | None) -> str:
        try:
            return validate_model_name(raw_name or "")
        except ValueError as e:
            fallback = "gemini-3-pro-image-preview"
            logger.warning(f"模型名配置无效，已回退到默认模型 {fallback}: {e}")
            return fallback

    def _get_active_session(self, key: str) -> tuple[EditSession | None, bool]:
        """获取活跃会话，返回 (session, timed_out)。"""
        session = self._edit_sessions.get(key)
        if session is None:
            return None, False
        if time.time() - session.created_at > self.edit_session_timeout_seconds:
            del self._edit_sessions[key]
            return None, True
        return session, False

    def _get_bot_client(self, event: AstrMessageEvent):
        """尝试从 event 中获取底层 OneBot client 实例"""
        client = None
        if hasattr(event, 'raw_event') and event.raw_event:
            raw = event.raw_event
            if hasattr(raw, 'bot'):
                client = raw.bot
            elif hasattr(raw, '_bot'):
                client = raw._bot
        if not client and hasattr(self, 'context') and self.context:
            if hasattr(self.context, 'get_platform_client'):
                client = self.context.get_platform_client()
            elif hasattr(self.context, 'platform_manager'):
                pm = self.context.platform_manager
                if hasattr(pm, 'get_client'):
                    client = pm.get_client('aiocqhttp')
        return client

    # ── Midjourney 风格标志解析 ─────────────────────────────────────

    _RATIO_FLAGS = {
        "--16:9": "16:9", "-l": "16:9",
        "--9:16": "9:16", "-p": "9:16",
        "--1:1": "1:1", "-s": "1:1",
        "--4:3": "4:3",
        "--3:4": "3:4",
    }
    _RES_FLAGS = {
        "--1k": 1024, "--2k": 2048, "--4k": 4096,
    }

    def _parse_flags(self, text: str) -> tuple[str, int, str]:
        """从文本中解析 midjourney 风格标志（如 --16:9 -l --2k）。
        返回 (ratio, resolution, clean_text)。
        未指定的值使用全局配置的默认值。"""
        ratio = ""
        resolution = 0
        remaining: list[str] = []

        for token in text.split():
            lower = token.lower()
            if lower in self._RATIO_FLAGS:
                ratio = self._RATIO_FLAGS[lower]
            elif lower in self._RES_FLAGS:
                resolution = self._RES_FLAGS[lower]
            else:
                remaining.append(token)

        ratio = ratio or self.aspect_ratio
        resolution = resolution or self.default_resolution
        return ratio, resolution, " ".join(remaining)

    def _get_command_prefixes(self) -> tuple[str, ...]:
        prefixes = ["/", "#", "!"]
        try:
            config = self.context.get_config() if self.context else {}
        except Exception:
            return tuple(prefixes)

        if not isinstance(config, dict):
            return tuple(prefixes)

        for key in ("command_prefixes", "command_prefix", "cmd_prefix", "prefix"):
            value = config.get(key)
            if isinstance(value, list):
                dynamic = [str(item).strip() for item in value if str(item).strip()]
                if dynamic:
                    return tuple(dict.fromkeys(dynamic + prefixes))
            elif isinstance(value, str) and value.strip():
                return tuple(dict.fromkeys([value.strip(), *prefixes]))
        return tuple(prefixes)

    async def _send_ephemeral(self, event: AstrMessageEvent, text: str, delay: float = 5.0) -> bool:
        """发送临时消息，delay 秒后自动撤回。成功返回 True，失败返回 False。"""
        try:
            client = self._get_bot_client(event)
            if not client:
                return False

            group_id = None
            user_id = None
            try:
                group_id = event.get_group_id()
            except AttributeError:
                pass
            try:
                user_id = event.get_sender_id()
            except AttributeError:
                pass

            result = None
            if group_id:
                if hasattr(client, 'call_api'):
                    result = await client.call_api('send_group_msg', group_id=int(group_id), message=text)
                elif hasattr(client, 'send_group_msg'):
                    result = await client.send_group_msg(group_id=int(group_id), message=text)
            elif user_id:
                if hasattr(client, 'call_api'):
                    result = await client.call_api('send_private_msg', user_id=int(user_id), message=text)
                elif hasattr(client, 'send_private_msg'):
                    result = await client.send_private_msg(user_id=int(user_id), message=text)

            if not result:
                return False

            msg_id = None
            if isinstance(result, dict):
                msg_id = result.get('message_id')
            elif hasattr(result, 'message_id'):
                msg_id = result.message_id

            if msg_id:
                async def _delete_later():
                    await asyncio.sleep(delay)
                    try:
                        if hasattr(client, 'call_api'):
                            await client.call_api('delete_msg', message_id=int(msg_id))
                        elif hasattr(client, 'delete_msg'):
                            await client.delete_msg(message_id=int(msg_id))
                    except Exception as e:
                        logger.debug(f"撤回消息失败（可能无权限）: {e}")

                asyncio.create_task(_delete_later())
            return True

        except Exception as e:
            logger.debug(f"发送临时消息失败: {e}")
            return False

    # ── 图片发送 ──────────────────────────────────────────────────

    async def send_image_with_callback_api(self, image_path: str) -> Image:
        """
        优先使用callback_api_base发送图片，失败则退回到本地文件发送
        """
        callback_api_base = self.context.get_config().get("callback_api_base")
        if not callback_api_base:
            logger.info("未配置callback_api_base，使用本地文件发送")
            return Image.fromFileSystem(image_path)

        logger.info(f"检测到配置了callback_api_base: {callback_api_base}")
        try:
            image_component = Image.fromFileSystem(image_path)
            download_url = await image_component.convert_to_web_link()
            logger.info(f"成功生成下载链接: {download_url}")
            return Image.fromURL(download_url)
        except (IOError, OSError) as e:
            logger.warning(f"文件操作失败: {e}，将退回到本地文件发送")
            return Image.fromFileSystem(image_path)
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"网络连接失败: {e}，将退回到本地文件发送")
            return Image.fromFileSystem(image_path)
        except Exception as e:
            logger.error(f"发送图片时出现未预期的错误: {e}，将退回到本地文件发送")
            return Image.fromFileSystem(image_path)

    # ── Vertex AI 生成 ────────────────────────────────────────────

    async def _generate_image_via_provider(self, prompt: str, input_images: list | None, aspect_ratio: str = "", resolution: int = 0):
        """
        调用 Vertex AI 图像生成。

        Returns:
            tuple[str | None, str | None, str | None]: (image_url, image_path, error_reason)
        """
        if not self.vertex_api_keys:
            logger.error("未配置 vertex_api_key，无法生成图像")
            return None, None, "NO_API_KEY"

        logger.info(
            f"使用 Vertex AI 生成图像，model={self.model_name}"
        )

        data_dir = StarTools.get_data_dir("vertex_image-command")

        return await generate_image_vertex(
            prompt,
            api_key=self.vertex_api_keys,
            model=self.model_name,
            input_images=input_images,
            max_retry_attempts=self.max_retry_attempts,
            data_dir=data_dir,
            safety_settings=self.safety_settings,
            aspect_ratio=aspect_ratio or self.aspect_ratio,
            resolution=resolution or self.default_resolution,
        )

    def _format_timeout_message(self) -> str:
        return "编辑会话已超时，已自动取消。请重新发送 /edit 开始。"

    def _format_image_limit_message(
        self,
        accepted_count: int,
        total_count: int,
        ignored_count: int,
    ) -> str:
        if accepted_count > 0:
            return (
                f"已收到 {accepted_count} 张图片（共 {total_count} 张）。\n"
                f"当前会话最多保留 {self.max_edit_images} 张图片，多余 {ignored_count} 张已忽略。\n"
                "继续发送描述，或发送 /ok 开始处理。"
            )
        return (
            f"当前会话最多保留 {self.max_edit_images} 张图片，"
            f"刚发送的 {ignored_count} 张已忽略。\n"
            "请直接发送描述，或发送 /ok 开始处理。"
        )

    # ── 配置规范化 ────────────────────────────────────────────────

    @staticmethod
    def _normalize_api_keys(value) -> list[str]:
        if value is None:
            return []
        keys: list[str] = []
        if isinstance(value, str):
            for part in value.split(","):
                k = part.strip()
                if k:
                    keys.append(k)
            return keys
        if isinstance(value, list):
            for item in value:
                if item is None:
                    continue
                k = str(item).strip()
                if k:
                    keys.append(k)
        return keys

    @staticmethod
    def _normalize_id_list(value) -> list[str]:
        if value is None:
            return []
        ids: list[str] = []
        if isinstance(value, str):
            for part in value.split(","):
                gid = part.strip()
                if gid:
                    ids.append(gid)
            return ids
        if isinstance(value, list):
            for item in value:
                if item is None:
                    continue
                gid = str(item).strip()
                if gid:
                    ids.append(gid)
        return ids

    # ── 群过滤 & 限流 ────────────────────────────────────────────

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = None
        try:
            group_id = event.get_group_id()
        except AttributeError:
            group_id = None
        if not group_id:
            return True
        gid = str(group_id)
        mode = self.group_filter_mode or "none"
        if mode == "whitelist":
            allowed = gid in self.group_filter_list
            if not allowed:
                logger.info(f"群 {gid} 不在白名单中，忽略指令")
            return allowed
        if mode == "blacklist":
            if gid in self.group_filter_list:
                logger.info(f"群 {gid} 命中黑名单，忽略指令")
                return False
            return True
        if mode not in {"none", "whitelist", "blacklist"}:
            logger.warning(f"未知的 group_filter_mode={mode}，按 none 处理")
        return True

    async def _check_and_consume_rate_limit(self, event: AstrMessageEvent) -> bool:
        if self.rate_limit_max_calls_per_group <= 0 or self.rate_limit_period_seconds <= 0:
            return True
        group_id = None
        try:
            group_id = event.get_group_id()
        except AttributeError:
            group_id = None
        if not group_id:
            return True
        gid = str(group_id)
        now = time.time()
        async with self._rate_limit_lock:
            window_start, count = self._rate_limit_state.get(gid, (now, 0))
            if now - window_start >= self.rate_limit_period_seconds:
                window_start = now
                count = 0
            if count >= self.rate_limit_max_calls_per_group:
                logger.info(f"群 {gid} 已达到限流上限 ({count}/{self.rate_limit_max_calls_per_group})")
                return False
            self._rate_limit_state[gid] = (window_start, count + 1)
            return True

    # ── 安全检查 ──────────────────────────────────────────────────

    # ── 错误消息 ──────────────────────────────────────────────────

    @staticmethod
    def _get_error_message(error_reason: str | None, command_name: str = "图像生成") -> str:
        if error_reason == "SAFETY_BLOCKED":
            return f"⚠️ {command_name}被安全策略阻止，请尝试调整提示词或更换图片后重试。"
        if error_reason == "NO_API_KEY":
            return f"{command_name}失败：未配置 Vertex AI API 密钥。"
        if error_reason == "RATE_LIMITED":
            return f"⏳ {command_name}失败：API 请求频率超限，请稍后再试。"
        if error_reason == "BAD_REQUEST":
            return (
                f"{command_name}失败：请求参数不被当前模型接受。"
                "请检查模型名、分辨率和参考图数量后重试。"
            )
        return f"{command_name}失败，请检查 Vertex AI API 配置和网络连接。"

    # ── 图片收集 ──────────────────────────────────────────────────

    async def _collect_input_images(self, event: AstrMessageEvent) -> list[str]:
        """从当前事件中收集图片，返回 base64 字符串列表。"""
        images: list[str] = []
        reply_id: str | None = None
        reply_images_found = False

        if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    try:
                        base64_data = await comp.convert_to_base64()
                        images.append(base64_data)
                        logger.info("从消息中获取到图片")
                    except (IOError, ValueError, OSError) as e:
                        logger.warning(f"转换图片到base64失败: {e}")
                    except Exception as e:
                        logger.error(f"处理图片时出现未预期的错误: {e}")
                elif isinstance(comp, Reply):
                    if hasattr(comp, 'id') and comp.id:
                        reply_id = str(comp.id)
                    elif hasattr(comp, 'message_id') and comp.message_id:
                        reply_id = str(comp.message_id)
                    elif hasattr(comp, 'data') and isinstance(comp.data, dict):
                        reply_id = str(comp.data.get('id', ''))

                    if hasattr(comp, 'chain') and comp.chain:
                        for reply_comp in comp.chain:
                            if isinstance(reply_comp, Image):
                                try:
                                    base64_data = await reply_comp.convert_to_base64()
                                    images.append(base64_data)
                                    logger.info("从引用消息中获取到图片")
                                    reply_images_found = True
                                except (IOError, ValueError, OSError) as e:
                                    logger.warning(f"转换引用消息中的图片到base64失败: {e}")
                                except Exception as e:
                                    logger.error(f"处理引用消息中的图片时出现未预期的错误: {e}")

                    if not reply_images_found and hasattr(comp, 'message') and comp.message:
                        for reply_comp in comp.message:
                            if isinstance(reply_comp, Image):
                                try:
                                    base64_data = await reply_comp.convert_to_base64()
                                    images.append(base64_data)
                                    logger.info("从引用消息(message属性)中获取到图片")
                                    reply_images_found = True
                                except Exception as e:
                                    logger.warning(f"处理引用消息图片失败: {e}")

        if reply_id and not reply_images_found and not images:
            logger.info(f"Reply.chain 为空，尝试通过 API 获取被引用消息 (id={reply_id})")
            fetched_images = await self._fetch_reply_images_via_api(event, reply_id)
            images.extend(fetched_images)

        return images

    async def _fetch_reply_images_via_api(self, event: AstrMessageEvent, reply_id: str) -> list[str]:
        """通过 OneBot API 获取被引用消息中的图片。"""
        images: list[str] = []
        try:
            client = self._get_bot_client(event)
            if not client:
                logger.debug("无法获取底层 client，跳过 API 获取")
                return images
            if hasattr(client, 'call_api'):
                result = await client.call_api('get_msg', message_id=int(reply_id))
            elif hasattr(client, 'get_msg'):
                result = await client.get_msg(message_id=int(reply_id))
            else:
                logger.debug("client 没有 call_api 或 get_msg 方法")
                return images
            if not result:
                logger.debug("get_msg 返回空结果")
                return images
            logger.info(f"成功获取被引用消息: {type(result)}")
            message_content = None
            if isinstance(result, dict):
                message_content = result.get('message', [])
            elif hasattr(result, 'message'):
                message_content = result.message
            if not message_content:
                return images
            for seg in message_content:
                seg_type = None
                seg_data = None
                if isinstance(seg, dict):
                    seg_type = seg.get('type')
                    seg_data = seg.get('data', {})
                elif hasattr(seg, 'type'):
                    seg_type = seg.type
                    seg_data = getattr(seg, 'data', {})
                if seg_type == 'image':
                    img_url = None
                    if isinstance(seg_data, dict):
                        img_url = seg_data.get('url') or seg_data.get('file')
                    elif hasattr(seg_data, 'url'):
                        img_url = seg_data.url
                    if img_url:
                        logger.info(f"从被引用消息获取到图片 URL: {img_url[:50]}...")
                        img_bytes = await safe_download_image(img_url)
                        if not img_bytes:
                            logger.warning(f"安全下载引用图片失败: {img_url}")
                            continue
                        base64_data = base64.b64encode(img_bytes).decode('utf-8')
                        images.append(base64_data)
                        logger.info("成功通过 API 获取被引用消息中的图片")
        except Exception as e:
            logger.warning(f"通过 API 获取被引用消息失败: {e}")
        return images

    def _extract_text_from_message(self, event: AstrMessageEvent, command: str) -> str:
        """从消息中提取文本内容（排除指令本身）"""
        text_parts: list[str] = []
        if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, Plain):
                    try:
                        if hasattr(comp, 'text') and comp.text:
                            text = str(comp.text)
                        elif hasattr(comp, 'toString'):
                            text = comp.toString()
                        else:
                            text = str(comp)
                        if text:
                            text_parts.append(text)
                    except Exception as e:
                        logger.debug(f"提取Plain组件文本失败: {e}")
        if not text_parts:
            raw = getattr(event, "message_str", "") or ""
            if raw:
                text_parts.append(raw)
        full_text = " ".join(text_parts)
        pattern = rf'/?{re.escape(command)}[\s]*'
        full_text = re.sub(pattern, '', full_text, count=1)
        full_text = re.sub(r'[ \t]+', ' ', full_text)
        full_text = re.sub(r'\n\s*\n', '\n', full_text)
        full_text = full_text.strip()
        return full_text

    # ══════════════════════════════════════════════════════════════
    # 指令处理
    # ══════════════════════════════════════════════════════════════

    @filter.command("nano")
    async def generate_image_command(
        self,
        event: AstrMessageEvent,
        image_description: str = "",
    ):
        """纯文本生图指令 /nano，根据文字描述生成图片。"""
        if not self._is_group_allowed(event):
            return

        if not await self._check_and_consume_rate_limit(event):
            yield event.plain_result("本群本周期内的插件调用次数已达上限，请稍后再试。")
            return

        nap_server_address = self.nap_server_address
        nap_server_port = self.nap_server_port

        if not image_description:
            raw = getattr(event, "message_str", "") or ""
            parts = raw.strip().split(" ", 1)
            if len(parts) == 2:
                image_description = parts[1].strip()
            else:
                image_description = ""

        if not image_description:
            yield event.plain_result(
                "请提供要生成图像的文字描述，例如：\n"
                "/nano 一只坐在键盘上的橙色猫\n"
                "/nano 赛博朋克风格的城市夜景 --16:9 --2k\n"
                "详见 /imghelp"
            )
            return

        # 解析 midjourney 风格标志（如 --16:9 -l --2k）
        ratio, resolution, image_description = self._parse_flags(image_description)

        if not image_description:
            yield event.plain_result("请在标志后面提供图像描述。")
            return

        input_images: list = []

        try:
            async with self._concurrency_limit:
                image_url, image_path, error_reason = await self._generate_image_via_provider(
                    image_description,
                    input_images=input_images,
                    aspect_ratio=ratio,
                    resolution=resolution,
                )

            if not image_url or not image_path:
                error_msg = self._get_error_message(error_reason, "图像生成")
                yield event.chain_result([Plain(error_msg)])
                return

            if self.nap_server_address and self.nap_server_address != "localhost":
                image_path = await send_file(image_path, HOST=nap_server_address, PORT=nap_server_port)

            image_component = await self.send_image_with_callback_api(image_path)
            yield event.chain_result([image_component])

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"网络连接错误导致图像生成失败: {e}")
            yield event.chain_result([Plain(f"网络连接错误，图像生成失败: {str(e)}")])
        except ValueError as e:
            logger.error(f"参数错误导致图像生成失败: {e}")
            yield event.chain_result([Plain(f"参数错误，图像生成失败: {str(e)}")])
        except Exception as e:
            logger.error(f"图像生成过程出现未预期的错误: {e}")
            yield event.chain_result([Plain(f"图像生成失败: {str(e)}")])

    # ── /edit：开启改图会话 ───────────────────────────────────────

    @filter.command("edit")
    async def edit_start(self, event: AstrMessageEvent):
        """开启多步骤改图会话。发送 /edit 后逐条发送图片和描述文字，最后发送 /ok 确认。"""
        if not self._is_group_allowed(event):
            return

        if not await self._check_and_consume_rate_limit(event):
            yield event.plain_result("本群本周期内的插件调用次数已达上限，请稍后再试。")
            return

        key = self._get_session_key(event)

        # 收集本条消息中可能附带的图片
        initial_images = await self._collect_input_images(event)
        ignored_initial_images = 0
        if len(initial_images) > self.max_edit_images:
            ignored_initial_images = len(initial_images) - self.max_edit_images
            initial_images = initial_images[:self.max_edit_images]

        self._edit_sessions[key] = EditSession(
            images=initial_images,
            created_at=time.time(),
        )

        img_hint = f"已收到 {len(initial_images)} 张图片。" if initial_images else ""
        hint_msg = (
            f"编辑会话已开始！{img_hint}\n"
            "请逐条发送图片，然后发送文字描述。\n"
            "完成后发送 /ok 开始处理，发送 /cancel 取消。\n"
            f"超过 {self.edit_session_timeout_seconds} 秒未操作将自动取消。"
        )
        if ignored_initial_images > 0:
            hint_msg += (
                f"\n当前会话最多保留 {self.max_edit_images} 张图片，"
                f"多余 {ignored_initial_images} 张已忽略。"
            )
        if not await self._send_ephemeral(event, hint_msg):
            yield event.plain_result(hint_msg)

    # ── /ok：确认并执行改图 ───────────────────────────────────────

    @filter.command("ok")
    async def edit_confirm(self, event: AstrMessageEvent):
        """确认改图会话，开始处理。"""
        key = self._get_session_key(event)
        session, timed_out = self._get_active_session(key)

        if session is None and timed_out:
            timeout_msg = self._format_timeout_message()
            if not await self._send_ephemeral(event, timeout_msg):
                yield event.plain_result(timeout_msg)
            return

        if session is None:
            # 没有活跃会话，忽略
            return

        # 弹出会话
        del self._edit_sessions[key]

        if not session.images:
            no_img_msg = "未收到任何图片，编辑已取消。"
            if not await self._send_ephemeral(event, no_img_msg):
                yield event.plain_result(no_img_msg)
            return

        description = session.description or "请在保持主体内容不变的前提下，对这张图片进行美化。"

        logger.info(f"改图确认，共 {len(session.images)} 张图片，描述: {description[:50]}")

        try:
            async with self._concurrency_limit:
                image_url, image_path, error_reason = await self._generate_image_via_provider(
                    description,
                    input_images=session.images,
                    aspect_ratio=session.ratio,
                    resolution=session.resolution,
                )

            if not image_url or not image_path:
                error_msg = self._get_error_message(error_reason, "改图")
                yield event.chain_result([Plain(error_msg)])
                return

            if self.nap_server_address and self.nap_server_address != "localhost":
                image_path = await send_file(image_path, HOST=self.nap_server_address, PORT=self.nap_server_port)

            image_component = await self.send_image_with_callback_api(image_path)
            yield event.chain_result([Plain("✨ 改图完成！"), image_component])

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"网络连接错误导致改图失败: {e}")
            yield event.chain_result([Plain(f"网络连接错误，改图失败: {str(e)}")])
        except ValueError as e:
            logger.error(f"参数错误导致改图失败: {e}")
            yield event.chain_result([Plain(f"参数错误，改图失败: {str(e)}")])
        except Exception as e:
            logger.error(f"改图过程出现未预期的错误: {e}")
            yield event.chain_result([Plain(f"改图失败: {str(e)}")])

    # ── /cancel：取消改图会话 ─────────────────────────────────────

    @filter.command("cancel")
    async def edit_cancel(self, event: AstrMessageEvent):
        """取消当前改图会话。"""
        key = self._get_session_key(event)
        if key in self._edit_sessions:
            del self._edit_sessions[key]
            cancel_msg = "编辑会话已取消。"
            if not await self._send_ephemeral(event, cancel_msg):
                yield event.plain_result(cancel_msg)

    # ── 会话输入：捕获图片和文字描述 ─────────────────────────────

    @filter.regex(r"[\s\S]*")
    async def handle_edit_session_input(self, event: AstrMessageEvent):
        """在改图会话期间，捕获用户发送的图片和文字描述。"""
        # 跳过指令消息（由对应的 command handler 处理）
        msg = getattr(event, "message_str", "") or ""
        msg_stripped = msg.strip()
        # 检查是否以指令前缀开头，避免把命令误当作描述写入会话
        if msg_stripped and msg_stripped.startswith(self._get_command_prefixes()):
            return

        key = self._get_session_key(event)
        session, timed_out = self._get_active_session(key)
        if session is None:
            if timed_out:
                timeout_msg = self._format_timeout_message()
                if not await self._send_ephemeral(event, timeout_msg):
                    yield event.plain_result(timeout_msg)
            return

        # 刷新会话时间
        session.created_at = time.time()

        # 收集图片
        new_images = await self._collect_input_images(event)
        if new_images:
            available_slots = max(self.max_edit_images - len(session.images), 0)
            accepted_images = new_images[:available_slots]
            ignored_images = len(new_images) - len(accepted_images)
            if accepted_images:
                session.images.extend(accepted_images)
            if ignored_images > 0:
                img_msg = self._format_image_limit_message(
                    accepted_count=len(accepted_images),
                    total_count=len(session.images),
                    ignored_count=ignored_images,
                )
            else:
                img_msg = (
                    f"已收到 {len(accepted_images)} 张图片（共 {len(session.images)} 张）。\n"
                    "继续发送图片/描述，或发送 /ok 开始处理。"
                )
            if not await self._send_ephemeral(event, img_msg):
                yield event.plain_result(img_msg)
            return

        # 收集文字描述（只有非空文本才处理）
        text = msg_stripped
        if text:
            # 解析 midjourney 风格标志
            ratio, resolution, desc_text = self._parse_flags(text)
            if ratio:
                session.ratio = ratio
            if resolution:
                session.resolution = resolution
            session.description = desc_text or text
            hints: list[str] = []
            if session.ratio:
                hints.append(f"比例:{session.ratio}")
            if session.resolution:
                hints.append(f"{session.resolution // 1024}K")
            hint_str = f"（{'，'.join(hints)}）" if hints else ""
            desc_msg = (
                f"已收到描述文字{hint_str}：「{session.description[:50]}{'…' if len(session.description) > 50 else ''}」\n"
                "发送 /ok 开始处理，或继续发送图片/修改描述。"
            )
            if not await self._send_ephemeral(event, desc_msg):
                yield event.plain_result(desc_msg)

    # ── /imghelp：帮助信息 ────────────────────────────────────────

    @filter.command("imghelp")
    async def img_help(self, event: AstrMessageEvent):
        """列出本插件支持的图像相关指令。"""
        if not self._is_group_allowed(event):
            return

        lines = [
            "本插件支持的图像相关指令：",
            "",
            "/nano 描述 [标志] —— 根据文字描述生成图片",
            "  例：/nano 赛博朋克城市夜景 --16:9 --2k",
            "",
            "/edit —— 开启改图/参考图会话（多步骤）",
            "  1. 发送 /edit 开始",
            "  2. 逐条发送图片（支持多张参考图）",
            "  3. 发送文字描述（可附带标志）",
            "  4. 发送 /ok 开始处理",
            "  * 发送 /cancel 取消",
            f"  * 超过 {self.edit_session_timeout_seconds} 秒未操作自动取消",
            f"  * 最多保留 {self.max_edit_images} 张图片",
            "",
            "宽高比标志：",
            "  --16:9 或 -l  横屏",
            "  --9:16 或 -p  竖屏",
            "  --1:1  或 -s  方形",
            "  --4:3         4:3",
            "  --3:4         3:4",
            "",
            "分辨率标志：",
            "  --1k  1024px",
            "  --2k  2048px",
            "  --4k  4096px",
            "",
            "/imghelp —— 显示此帮助信息",
        ]
        yield event.plain_result("\n".join(lines))
