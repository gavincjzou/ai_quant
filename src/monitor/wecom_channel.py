"""
WeCom (企业微信) Webhook 告警通道

官方文档：https://developer.work.weixin.qq.com/document/path/91770

支持消息类型：
- text: 纯文本（可 @userid / @手机号 / @all）
- markdown: Markdown 富文本（不支持 @）

使用方式：
    channel = WeComChannel(
        webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
        mention_mobiles=["13800138000"],
    )
    channel.send_text("盘前数据就绪")
    channel.send_markdown("# 每日对账\\n...")

设计原则：
- 失败不抛异常，返回 bool，调用方 fallback 到日志即可
- 超时保护 8s
- 单条 text ≤ 2048 字节，markdown ≤ 4096 字节（超长自动截断 + 提示）
"""

from typing import List, Optional

from loguru import logger

try:
    import requests

    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False


# 企业微信官方限制
MAX_TEXT_BYTES = 2048
MAX_MARKDOWN_BYTES = 4096
HTTP_TIMEOUT = 8


def _safe_truncate(text: str, max_bytes: int) -> str:
    """按字节截断，避免 UTF-8 半字符问题。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "\n…（内容超长已截断）"
    keep = max_bytes - len(suffix.encode("utf-8"))
    # 逐字节回退直到可解码
    while keep > 0:
        try:
            return encoded[:keep].decode("utf-8") + suffix
        except UnicodeDecodeError:
            keep -= 1
    return suffix


class WeComChannel:
    """企业微信 Webhook 告警通道。"""

    def __init__(
        self,
        webhook_url: str,
        mention_users: Optional[List[str]] = None,
        mention_mobiles: Optional[List[str]] = None,
    ):
        """
        Args:
            webhook_url: 完整 Webhook URL（含 key 参数）
            mention_users: 默认 @ 的 userid 列表（仅 text 类型生效）
            mention_mobiles: 默认 @ 的手机号列表（仅 text 类型生效）
        """
        self.webhook_url = (webhook_url or "").strip()
        self.mention_users = list(mention_users or [])
        self.mention_mobiles = list(mention_mobiles or [])

    # ----------------- Readiness -----------------

    @property
    def ready(self) -> bool:
        return bool(self.webhook_url and _REQUESTS_OK)

    # ----------------- Send Text -----------------

    def send_text(
        self,
        content: str,
        mention_users: Optional[List[str]] = None,
        mention_mobiles: Optional[List[str]] = None,
    ) -> bool:
        """
        发送纯文本消息。

        Args:
            content: 消息正文（≤ 2048 字节）
            mention_users: 覆盖默认 @ 用户列表（传 ["@all"] 表示 @所有人）
            mention_mobiles: 覆盖默认 @ 手机号列表

        Returns:
            True 发送成功，False 失败（已吞掉异常）
        """
        if not self.ready:
            logger.debug("[WeCom] channel not ready, skip")
            return False

        body = _safe_truncate(content, MAX_TEXT_BYTES)
        users = mention_users if mention_users is not None else self.mention_users
        mobiles = mention_mobiles if mention_mobiles is not None else self.mention_mobiles

        payload = {
            "msgtype": "text",
            "text": {
                "content": body,
                "mentioned_list": users,
                "mentioned_mobile_list": mobiles,
            },
        }
        return self._post(payload)

    # ----------------- Send Markdown -----------------

    def send_markdown(self, content: str) -> bool:
        """
        发送 Markdown 消息。

        注意：企业微信 Markdown 不支持 @mention，如需 @ 请用 send_text。
        支持语法：标题、列表、粗体/斜体、引用、代码块、颜色标签（<font color="info">）

        Args:
            content: Markdown 正文（≤ 4096 字节）
        """
        if not self.ready:
            return False

        body = _safe_truncate(content, MAX_MARKDOWN_BYTES)
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": body},
        }
        return self._post(payload)

    # ----------------- Internal -----------------

    def _post(self, payload: dict) -> bool:
        """POST Webhook，统一错误处理。"""
        try:
            r = requests.post(
                self.webhook_url,
                json=payload,
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code != 200:
                logger.warning(f"[WeCom] HTTP {r.status_code}: {r.text[:200]}")
                return False
            data = r.json()
            if data.get("errcode") != 0:
                logger.warning(
                    f"[WeCom] errcode={data.get('errcode')} "
                    f"errmsg={data.get('errmsg')}"
                )
                return False
            return True
        except Exception as e:
            logger.warning(f"[WeCom] request failed: {e}")
            return False
