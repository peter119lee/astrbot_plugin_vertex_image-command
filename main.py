import asyncio
import base64
import re
import time
import ipaddress
import socket
from urllib.parse import urlparse

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.all import Image, Plain
from astrbot.core.message.components import Reply
from .utils.ttp import generate_image_vertex
from .utils.file_send_server import send_file


@register("astrbot_plugin_vertex_image_command", "YanL", "使用 Google Vertex AI 生成图片", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)

        # Vertex AI 配置
        self.vertex_api_keys = self._normalize_api_keys(config.get("vertex_api_key"))

        # 模型配置
        self.model_name = config.get("model_name", "gemini-3-pro-image-preview").strip()

        # 重试配置
        self.max_retry_attempts = config.get("max_retry_attempts", 3)

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
        # 可选值: "OFF", "BLOCK_NONE", "BLOCK_ONLY_HIGH", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE"
        self.safety_settings = {
            "hate_speech": config.get("safety_filter_hate_speech", "OFF"),
            "harassment": config.get("safety_filter_harassment", "OFF"),
            "sexually_explicit": config.get("safety_filter_sexually_explicit", "OFF"),
            "dangerous_content": config.get("safety_filter_dangerous_content", "OFF"),
        }

        # C-01: 全局并发限制 (例如 10)
        self._concurrency_limit = asyncio.Semaphore(10)

    async def send_image_with_callback_api(self, image_path: str) -> Image:
        """
        优先使用callback_api_base发送图片，失败则退回到本地文件发送

        Args:
            image_path (str): 图片文件路径

        Returns:
            Image: 图片组件
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

    async def _generate_image_via_provider(self, prompt: str, input_images: list | None):
        """
        调用 Vertex AI 图像生成。

        Returns:
            tuple[str | None, str | None, str | None]: (image_url, image_path, error_reason)
            error_reason 可能的值：
            - None: 成功
            - "SAFETY_BLOCKED": 被安全策略阻止
            - "API_ERROR": API 配置或网络错误
            - "NO_API_KEY": 未配置 API 密钥
        """
        if not self.vertex_api_keys:
            logger.error("未配置 vertex_api_key，无法生成图像")
            return None, None, "NO_API_KEY"

        logger.info(
            f"使用 Vertex AI 生成图像，model={self.model_name}"
        )

        # 使用 StarTools 获取标准数据目录，避免污染源码目录
        data_dir = StarTools.get_data_dir("vertex_image-command")

        return await generate_image_vertex(
            prompt,
            api_key=self.vertex_api_keys,
            model=self.model_name,
            input_images=input_images,
            max_retry_attempts=self.max_retry_attempts,
            data_dir=data_dir,
            safety_settings=self.safety_settings,
        )

    @staticmethod
    def _normalize_api_keys(value) -> list[str]:
        """
        将配置中的 vertex_api_key 规范化为字符串列表。

        支持以下输入形式：
        - 单个字符串（可能包含逗号分隔的多个 key）
        - 字符串列表
        - 其他可转为字符串的元素列表
        """
        if value is None:
            return []

        keys: list[str] = []

        # 字符串形式：支持用逗号分隔多个 key，兼容旧配置
        if isinstance(value, str):
            for part in value.split(","):
                k = part.strip()
                if k:
                    keys.append(k)
            return keys

        # 列表形式：逐项转为字符串并去空白
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
        """
        将配置中的群号列表规范化为字符串列表。

        支持：
        - 单个字符串（可用逗号分隔多个群号）
        - 列表（元素会被转为字符串）
        """
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

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        """
        判断当前事件所在群是否允许使用插件指令。

        通过 group_filter_mode + group_filter_list 控制：
        - mode = whitelist: 仅名单内群允许；
        - mode = blacklist: 名单内群禁止；
        - mode = none 或其他: 不做群过滤。
        """
        group_id = None
        try:
            group_id = event.get_group_id()
        except AttributeError:
            group_id = None

        # 私聊或无法获取群号时不做限制
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

        # none 或未知值：不做过滤
        if mode not in {"none", "whitelist", "blacklist"}:
            logger.warning(f"未知的 group_filter_mode={mode}，按 none 处理")
        return True

    async def _check_and_consume_rate_limit(self, event: AstrMessageEvent) -> bool:
        """
        检查并消耗当前群的限流配额。

        返回 True 表示允许本次调用；False 表示已达到上限。
        """
        if self.rate_limit_max_calls_per_group <= 0 or self.rate_limit_period_seconds <= 0:
            return True

        group_id = None
        try:
            group_id = event.get_group_id()
        except AttributeError:
            group_id = None

        # 仅对群聊做限流，私聊不受限
        if not group_id:
            return True

        gid = str(group_id)
        now = time.time()

        async with self._rate_limit_lock:
            window_start, count = self._rate_limit_state.get(gid, (now, 0))

            # 如果已经超过周期，重置窗口
            if now - window_start >= self.rate_limit_period_seconds:
                window_start = now
                count = 0

            if count >= self.rate_limit_max_calls_per_group:
                logger.info(f"群 {gid} 已达到限流上限 ({count}/{self.rate_limit_max_calls_per_group})")
                return False

            # 消耗一次配额
            self._rate_limit_state[gid] = (window_start, count + 1)
            return True

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        """
        检查 URL 是否安全 (防止 SSRF)
        禁止访问私有 IP 和循环地址
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                return False
            
            hostname = parsed.hostname
            if not hostname:
                return False
                
            # 获取 IP 地址
            try:
                addr_info = socket.getaddrinfo(hostname, None)
            except socket.gaierror:
                return False
                
            for family, socktype, proto, canonname, sockaddr in addr_info:
                ip_str = sockaddr[0]
                ip = ipaddress.ip_address(ip_str)
                # 禁止以下类型的 IP
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                    logger.warning(f"检测到不安全的 IP 地址: {ip_str} ({hostname})")
                    return False
            
            return True
        except Exception as e:
            logger.warning(f"URL 安全检查失败: {e}")
            return False

    @staticmethod
    def _get_error_message(error_reason: str | None, command_name: str = "图像生成") -> str:
        """
        根据错误原因返回用户友好的错误消息。

        Args:
            error_reason: 错误原因标识符
            command_name: 命令名称，用于生成错误消息

        Returns:
            用户友好的错误消息字符串
        """
        if error_reason == "SAFETY_BLOCKED":
            return f"⚠️ {command_name}被安全策略阻止，请尝试调整提示词或更换图片后重试。"
        if error_reason == "NO_API_KEY":
            return f"{command_name}失败：未配置 Vertex AI API 密钥。"
        if error_reason == "RATE_LIMITED":
            return f"⏳ {command_name}失败：API 请求频率超限，请稍后再试。"
        # API_ERROR 或其他未知错误
        return f"{command_name}失败，请检查 Vertex AI API 配置和网络连接。"

    async def _collect_input_images(self, event: AstrMessageEvent) -> list[str]:
        """
        从当前事件中收集图片（包含直接发送的图片和引用消息中的图片）。
        返回 base64 字符串列表。
        """
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
                    # 尝试多种方式获取 reply_id
                    if hasattr(comp, 'id') and comp.id:
                        reply_id = str(comp.id)
                    elif hasattr(comp, 'message_id') and comp.message_id:
                        reply_id = str(comp.message_id)
                    elif hasattr(comp, 'data') and isinstance(comp.data, dict):
                        reply_id = str(comp.data.get('id', ''))
                    
                    # 方式1：从 comp.chain 获取（AstrBot 已解析的引用消息内容）
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
                    
                    # 方式2：从 comp.message 获取
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

        # 方式3：如果有 reply_id 但没获取到图片，尝试通过 API 获取被引用消息
        if reply_id and not reply_images_found and not images:
            logger.info(f"Reply.chain 为空，尝试通过 API 获取被引用消息 (id={reply_id})")
            fetched_images = await self._fetch_reply_images_via_api(event, reply_id)
            images.extend(fetched_images)

        return images

    async def _fetch_reply_images_via_api(self, event: AstrMessageEvent, reply_id: str) -> list[str]:
        """
        通过 OneBot API 获取被引用消息中的图片。
        
        Args:
            event: 当前消息事件
            reply_id: 被引用消息的 ID
        
        Returns:
            图片的 base64 字符串列表
        """
        images: list[str] = []
        
        try:
            # 尝试获取底层 client 并调用 get_msg API
            client = None
            
            # 方式1：从 event.raw_event 获取 bot 实例
            if hasattr(event, 'raw_event') and event.raw_event:
                raw = event.raw_event
                if hasattr(raw, 'bot'):
                    client = raw.bot
                elif hasattr(raw, '_bot'):
                    client = raw._bot
            
            # 方式2：从 context 获取
            if not client and hasattr(self, 'context') and self.context:
                # 尝试多种路径获取 client
                if hasattr(self.context, 'get_platform_client'):
                    client = self.context.get_platform_client()
                elif hasattr(self.context, 'platform_manager'):
                    pm = self.context.platform_manager
                    if hasattr(pm, 'get_client'):
                        client = pm.get_client('aiocqhttp')
            
            if not client:
                logger.debug("无法获取底层 client，跳过 API 获取")
                return images
            
            # 调用 get_msg API
            if hasattr(client, 'call_api'):
                result = await client.call_api('get_msg', message_id=int(reply_id))
            elif hasattr(client, 'get_msg'):
                result = await client.get_msg(message_id=int(reply_id))
            else:
                logger.debug("client 没有 call_api 或 get_msg 方法")
                return images
            
            if not result:
                logger.debug(f"get_msg 返回空结果")
                return images
            
            logger.info(f"成功获取被引用消息: {type(result)}")
            
            # 解析返回的消息，提取图片
            message_content = None
            if isinstance(result, dict):
                message_content = result.get('message', [])
            elif hasattr(result, 'message'):
                message_content = result.message
            
            if not message_content:
                return images
            
            # 遍历消息段，找到图片
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
                    # 获取图片 URL
                    img_url = None
                    if isinstance(seg_data, dict):
                        img_url = seg_data.get('url') or seg_data.get('file')
                    elif hasattr(seg_data, 'url'):
                        img_url = seg_data.url
                    
                    if img_url:
                        logger.info(f"从被引用消息获取到图片 URL: {img_url[:50]}...")
                        
                        # D-01: SSRF 安全检查
                        if not self._is_safe_url(img_url):
                            logger.warning(f"拦截到不安全的图片 URL: {img_url}")
                            continue

                        # 下载图片并转为 base64
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                                    if resp.status == 200:
                                        img_bytes = await resp.read()
                                        base64_data = base64.b64encode(img_bytes).decode('utf-8')
                                        images.append(base64_data)
                                        logger.info("成功通过 API 获取被引用消息中的图片")
                        except Exception as e:
                            logger.warning(f"下载图片失败: {e}")
        
        except Exception as e:
            logger.warning(f"通过 API 获取被引用消息失败: {e}")
        
        return images

    def _extract_text_from_message(self, event: AstrMessageEvent, command: str) -> str:
        """
        从消息中提取文本内容（排除指令本身），支持图片在文字前后的情况。
        支持处理包含换行符的多行提示词。
        
        Args:
            event: 消息事件
            command: 指令名称（如 "改图"）
        
        Returns:
            提取到的文本描述
        """
        text_parts: list[str] = []
        
        # 方法1：从 message_obj.message 组件列表中提取
        if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                # 处理 Plain 文本组件
                if isinstance(comp, Plain):
                    try:
                        # Plain 组件可能有 text 属性或 toString 方法
                        # 注意：不要 strip()，保留原始文本（包括换行）
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
        
        # 方法2：如果组件提取失败，尝试从 message_str 提取
        if not text_parts:
            raw = getattr(event, "message_str", "") or ""
            if raw:
                text_parts.append(raw)
        
        # 合并所有文本（用空格连接不同组件，但保留组件内部的换行）
        full_text = " ".join(text_parts)
        
        # 移除指令部分（如 "/改图" 或 "改图"），使用 DOTALL 模式处理换行
        # 匹配指令及其后面可能的空白字符（包括换行）
        pattern = rf'/?{re.escape(command)}[\s]*'
        full_text = re.sub(pattern, '', full_text, count=1)
        
        # 清理：将多个连续空白字符（空格、制表符）替换为单个空格，但保留换行的语义
        # 先将换行符替换为特殊标记，清理空格后再换回来（或者直接保留为空格）
        full_text = re.sub(r'[ \t]+', ' ', full_text)  # 只清理空格和制表符
        full_text = re.sub(r'\n\s*\n', '\n', full_text)  # 多个换行合并为一个
        full_text = full_text.strip()
        
        return full_text

    @filter.command("nano")
    async def generate_image_command(
        self,
        event: AstrMessageEvent,
        image_description: str = "",
    ):
        """Text-to-image command `/nano`. Generates images from text descriptions."""
        if not self._is_group_allowed(event):
            return

        if not await self._check_and_consume_rate_limit(event):
            yield event.plain_result("本群本周期内的插件调用次数已达上限，请稍后再试。")
            return

        # NAP 文件转发配置
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
                "请提供要生成图像的文字描述，例如：/nano 一只坐在键盘上的橙色猫，赛博朋克风格。"
            )
            return

        input_images: list = []

        try:
            # C-01: 并发限制
            async with self._concurrency_limit:
                image_url, image_path, error_reason = await self._generate_image_via_provider(
                    image_description,
                    input_images=input_images,
                )

            if not image_url or not image_path:
                error_msg = self._get_error_message(error_reason, "图像生成")
                yield event.chain_result([Plain(error_msg)])
                return

            if self.nap_server_address and self.nap_server_address != "localhost":
                image_path = await send_file(image_path, HOST=nap_server_address, PORT=nap_server_port)

            image_component = await self.send_image_with_callback_api(image_path)
            chain = [image_component]
            yield event.chain_result(chain)

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"网络连接错误导致图像生成失败: {e}")
            error_chain = [Plain(f"网络连接错误，图像生成失败: {str(e)}")]
            yield event.chain_result(error_chain)
        except ValueError as e:
            logger.error(f"参数错误导致图像生成失败: {e}")
            error_chain = [Plain(f"参数错误，图像生成失败: {str(e)}")]
            yield event.chain_result(error_chain)
        except Exception as e:
            logger.error(f"图像生成过程出现未预期的错误: {e}")
            error_chain = [Plain(f"图像生成失败: {str(e)}")]
            yield event.chain_result(error_chain)



    @filter.command("edit")
    async def edit_image_command(
        self,
        event: AstrMessageEvent,
        edit_description: str = "",
    ):
        """改图指令 `/edit`，专注于基于用户提供或引用的图片进行修改。

        使用示例：
        - `/edit 把这张图改成赛博朋克风格` + 图片
        - `/edit` + 图片 + `把这张图改成赛博朋克风格`
        - 回复一条包含图片的消息并输入：`/edit 给这张图加上蓝色霓虹背景`
        """
        if not self._is_group_allowed(event):
            return

        if not await self._check_and_consume_rate_limit(event):
            yield event.plain_result("本群本周期内的插件调用次数已达上限，请稍后再试。")
            return

        # NAP 文件转发配置
        nap_server_address = self.nap_server_address
        nap_server_port = self.nap_server_port

        # 从消息中提取文本描述，支持图片在文字前后的情况
        extracted_description = self._extract_text_from_message(event, "edit")
        if extracted_description:
            edit_description = extracted_description

        input_images: list[str] = await self._collect_input_images(event)

        if not input_images:
            yield event.plain_result("请先发送一张图片，或回复包含图片的消息后再使用 /edit 指令。")
            return

        if not edit_description:
            edit_description = "请在保持主体内容不变的前提下，对这张图片进行美化。"

        logger.info(f"改图指令使用了 {len(input_images)} 张图片")

        try:
            # C-01: 并发限制
            async with self._concurrency_limit:
                image_url, image_path, error_reason = await self._generate_image_via_provider(
                    edit_description,
                    input_images=input_images,
                )

                if not image_url or not image_path:
                    error_msg = self._get_error_message(error_reason, "改图")
                    yield event.chain_result([Plain(error_msg)])
                    return

                if self.nap_server_address and self.nap_server_address != "localhost":
                    image_path = await send_file(image_path, HOST=self.nap_server_address, PORT=self.nap_server_port)

                image_component = await self.send_image_with_callback_api(image_path)
                result_chain = [Plain("✨ 改图完成！"), image_component]
                yield event.chain_result(result_chain)

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"网络连接错误导致改图失败: {e}")
            error_chain = [Plain(f"网络连接错误，改图失败: {str(e)}")]
            yield event.chain_result(error_chain)
        except Exception as e:
            logger.error(f"改图过程出现未预期的错误: {e}")
            error_chain = [Plain(f"改图失败: {str(e)}")]
            yield event.chain_result(error_chain)

    @filter.command("imghelp")
    async def img_help(self, event: AstrMessageEvent):
        """列出本插件支持的图像相关指令。"""
        if not self._is_group_allowed(event):
            return

        lines = [
            "本插件支持的图像相关指令：",
            "/nano 文本 —— 根据文字描述生成图片",
            "/edit + 图片 —— 基于已有图片进行改图",
            "/imghelp —— 显示此帮助信息",
        ]
        yield event.plain_result("\n".join(lines))
