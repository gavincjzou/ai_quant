"""
Alerts - 告警系统（四通道：WeCom / Telegram / Email / 本地日志）

阶段7 升级：
- 新增企业微信 (WeCom) Webhook 通道（国内友好，推荐首选）
- 支持从 config/monitor.yaml + config/monitor.local.yaml 加载配置
- 环境变量继续生效（优先级最高）

加载优先级（由高到低）：
    环境变量 > config/monitor.local.yaml > config/monitor.yaml > 代码默认

环境变量：
- AIQUANT_WECOM_WEBHOOK   企业微信 Webhook URL
- TELEGRAM_BOT_TOKEN      Telegram Bot Token
- TELEGRAM_CHAT_ID        Telegram chat_id
- ALERT_SMTP_HOST         SMTP 服务器
- ALERT_SMTP_PORT         SMTP 端口（默认 465 SSL）
- ALERT_SMTP_USER         发件邮箱
- ALERT_SMTP_PASS         邮箱密码/授权码
- ALERT_EMAIL_TO          收件邮箱（逗号分隔）
"""

import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import List, Optional

from loguru import logger

try:
    import requests

    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    import yaml

    _YAML_OK = True
except ImportError:
    _YAML_OK = False

from src.monitor.wecom_channel import WeComChannel


class AlertLevel(str, Enum):
    INFO = "info"          # 普通通知（订单成交、TP1 等）
    WARNING = "warning"    # 警告（接近止损、日亏损接近上限）
    CRITICAL = "critical"  # 严重（熔断、风控拒单、连接失败）


_LEVEL_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🚨",
}

_LEVEL_ORDER = [AlertLevel.INFO, AlertLevel.WARNING, AlertLevel.CRITICAL]


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个 dict，override 优先。"""
    if not isinstance(base, dict):
        return override
    if not isinstance(override, dict):
        return base
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_monitor_yaml(project_root: Optional[str] = None) -> dict:
    """加载 config/monitor.yaml + monitor.local.yaml（local 覆盖）。"""
    if not _YAML_OK:
        return {}

    if project_root is None:
        # 猜测项目根：alerts.py 所在 src/monitor 向上两级
        here = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(here))

    config_dir = os.path.join(project_root, "config")
    cfg = {}
    for fname in ("monitor.yaml", "monitor.local.yaml"):
        p = os.path.join(config_dir, fname)
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg = _deep_merge(cfg, data)
        except Exception as e:
            logger.warning(f"AlertManager load {fname} failed: {e}")
    return cfg


class AlertManager:
    """多通道告警管理器（阶段7 加入 WeCom）。"""

    def __init__(
        self,
        log_file: Optional[str] = None,
        min_level_for_telegram: AlertLevel = AlertLevel.INFO,
        min_level_for_email: AlertLevel = AlertLevel.WARNING,
        config: Optional[dict] = None,
    ):
        """
        Args:
            log_file: 告警文件落盘路径（默认 output/alerts.log）
            min_level_for_telegram: Telegram 最低推送等级
            min_level_for_email: 邮件最低推送等级
            config: 直接传配置 dict（测试用），None 时自动加载 yaml
        """
        # 1) 日志文件
        self.log_file = log_file or os.path.join(
            os.getcwd(), "output", "alerts.log"
        )
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

        # 2) 加载 YAML 配置
        self._cfg = config if config is not None else _load_monitor_yaml()
        alerts_cfg = (self._cfg or {}).get("alerts", {})

        # 3) WeCom 通道
        wecom_cfg = alerts_cfg.get("wecom", {}) or {}
        wecom_webhook = (
            os.environ.get("AIQUANT_WECOM_WEBHOOK", "").strip()
            or (wecom_cfg.get("webhook_url") or "").strip()
        )
        self._wecom_enabled = bool(wecom_cfg.get("enabled", True)) and bool(wecom_webhook)
        self._wecom_min_level = AlertLevel(
            (wecom_cfg.get("min_level") or "info").lower()
        )
        self._wecom_mention_on = {
            AlertLevel(s.lower()) for s in (wecom_cfg.get("mention_on") or [])
        }
        self._wecom_mention_users = list(wecom_cfg.get("mention_users") or [])
        self._wecom_mention_mobiles = list(wecom_cfg.get("mention_mobiles") or [])
        self._wecom_markdown_levels = {
            AlertLevel(s.lower()) for s in (wecom_cfg.get("markdown_for_levels") or [])
        }

        self._wecom = (
            WeComChannel(
                webhook_url=wecom_webhook,
                mention_users=self._wecom_mention_users,
                mention_mobiles=self._wecom_mention_mobiles,
            )
            if self._wecom_enabled
            else None
        )

        # 4) Telegram（沿用旧逻辑）
        self._tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self._tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self._min_tg = min_level_for_telegram

        # 5) Email
        self._smtp_host = os.environ.get("ALERT_SMTP_HOST", "").strip()
        self._smtp_port = int(os.environ.get("ALERT_SMTP_PORT", "465"))
        self._smtp_user = os.environ.get("ALERT_SMTP_USER", "").strip()
        self._smtp_pass = os.environ.get("ALERT_SMTP_PASS", "").strip()
        _email_to = os.environ.get("ALERT_EMAIL_TO", "").strip()
        self._email_to = [x.strip() for x in _email_to.split(",") if x.strip()]
        self._min_email = min_level_for_email

        # 6) 打印通道状态
        channels = ["LOG(always)"]
        if self.wecom_ready:
            channels.append("WECOM")
        if self.telegram_ready:
            channels.append("TELEGRAM")
        if self.email_ready:
            channels.append("EMAIL")
        logger.info(f"AlertManager initialized, active channels: {channels}")

    # ----------------- Channel Readiness -----------------

    @property
    def wecom_ready(self) -> bool:
        return bool(self._wecom and self._wecom.ready)

    @property
    def telegram_ready(self) -> bool:
        return bool(self._tg_token and self._tg_chat and _REQUESTS_OK)

    @property
    def email_ready(self) -> bool:
        return bool(
            self._smtp_host and self._smtp_user
            and self._smtp_pass and self._email_to
        )

    # ----------------- Main Entry -----------------

    def send(
        self,
        message: str,
        level: AlertLevel = AlertLevel.INFO,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        markdown: Optional[str] = None,
    ) -> dict:
        """
        发送告警。所有通道失败也不抛异常，返回各通道结果。

        Args:
            message: 纯文本消息体（所有通道都会用这个）
            level: 告警级别
            title: 标题（可选）
            tags: 标签列表（用于日志）
            markdown: 可选 Markdown 富文本正文。传入后 WeCom 通道优先使用
                      Markdown 格式（其他通道仍用 message 纯文本）
        """
        if isinstance(level, str):
            level = AlertLevel(level)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        emoji = _LEVEL_EMOJI.get(level, "•")
        tag_str = " ".join(f"#{t}" for t in (tags or []))
        head = f"{emoji} [{level.value.upper()}] {title or 'Alert'}"
        body = f"{head}\n{now}\n{message}"
        if tag_str:
            body += f"\n{tag_str}"

        results = {"log": False, "wecom": None, "telegram": None, "email": None}

        # 1) 日志通道（必开）
        self._write_log(body, level)
        results["log"] = True

        # 2) WeCom（按阈值）
        if self.wecom_ready and self._should_send(level, self._wecom_min_level):
            try:
                ok = self._send_wecom(
                    level=level,
                    title=title or "Alert",
                    now=now,
                    text_body=body,
                    markdown_body=markdown,
                    tags=tags,
                )
                results["wecom"] = ok
            except Exception as e:
                logger.warning(f"WeCom alert failed: {e}")
                results["wecom"] = False

        # 3) Telegram
        if self.telegram_ready and self._should_send(level, self._min_tg):
            try:
                ok = self._send_telegram(body)
                results["telegram"] = ok
            except Exception as e:
                logger.warning(f"Telegram alert failed: {e}")
                results["telegram"] = False

        # 4) Email
        if self.email_ready and self._should_send(level, self._min_email):
            try:
                subject = f"[AI-Quant {level.value.upper()}] {title or 'Alert'}"
                ok = self._send_email(subject, body)
                results["email"] = ok
            except Exception as e:
                logger.warning(f"Email alert failed: {e}")
                results["email"] = False

        return results

    # ----------------- Shortcuts -----------------

    def info(self, msg: str, title: str = "Info", tags=None, markdown: Optional[str] = None):
        return self.send(msg, AlertLevel.INFO, title=title, tags=tags, markdown=markdown)

    def warning(self, msg: str, title: str = "Warning", tags=None, markdown: Optional[str] = None):
        return self.send(msg, AlertLevel.WARNING, title=title, tags=tags, markdown=markdown)

    def critical(self, msg: str, title: str = "Critical", tags=None, markdown: Optional[str] = None):
        return self.send(msg, AlertLevel.CRITICAL, title=title, tags=tags, markdown=markdown)

    # ----------------- Internal -----------------

    @staticmethod
    def _should_send(level: AlertLevel, min_level: AlertLevel) -> bool:
        return _LEVEL_ORDER.index(level) >= _LEVEL_ORDER.index(min_level)

    def _write_log(self, body: str, level: AlertLevel):
        """落盘到 alerts.log 并同步到 loguru（按等级映射）。"""
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(body + "\n\n")
        except Exception as e:
            logger.warning(f"AlertManager write log failed: {e}")

        if level == AlertLevel.CRITICAL:
            logger.critical(body)
        elif level == AlertLevel.WARNING:
            logger.warning(body)
        else:
            logger.info(body)

    def _send_wecom(
        self,
        level: AlertLevel,
        title: str,
        now: str,
        text_body: str,
        markdown_body: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> bool:
        """企业微信推送路由：markdown 优先（如果配置 + 提供了 markdown_body）。"""
        use_markdown = (
            markdown_body is not None
            and level in self._wecom_markdown_levels
        )

        # 根据 mention_on 决定是否 @
        should_mention = level in self._wecom_mention_on
        users = self._wecom_mention_users if should_mention else []
        mobiles = self._wecom_mention_mobiles if should_mention else []

        if use_markdown:
            # Markdown 不支持 @mention，如果需要 @ 就在 markdown 前加一条 text
            md_ok = self._wecom.send_markdown(markdown_body)
            if should_mention and (users or mobiles):
                # 再追发一条 text 承载 @（短消息）
                mention_txt = (
                    f"{_LEVEL_EMOJI.get(level, '•')} {title}（详情见上条报告）"
                )
                self._wecom.send_text(
                    mention_txt, mention_users=users, mention_mobiles=mobiles
                )
            return md_ok
        else:
            return self._wecom.send_text(
                text_body, mention_users=users, mention_mobiles=mobiles
            )

    def _send_telegram(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        payload = {
            "chat_id": self._tg_chat,
            "text": text,
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=8)
        return r.status_code == 200

    def _send_email(self, subject: str, body: str) -> bool:
        msg = MIMEMultipart()
        msg["From"] = self._smtp_user
        msg["To"] = ", ".join(self._email_to)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if self._smtp_port == 465:
            with smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, timeout=15) as s:
                s.login(self._smtp_user, self._smtp_pass)
                s.sendmail(self._smtp_user, self._email_to, msg.as_string())
        else:
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=15) as s:
                s.starttls()
                s.login(self._smtp_user, self._smtp_pass)
                s.sendmail(self._smtp_user, self._email_to, msg.as_string())
        return True


# ----------------- 单例 -----------------

_default_alerter: Optional[AlertManager] = None


def get_alerter() -> AlertManager:
    """全局单例入口，外部可 from src.monitor.alerts import get_alerter"""
    global _default_alerter
    if _default_alerter is None:
        _default_alerter = AlertManager()
    return _default_alerter


def reset_alerter():
    """测试/运行时重置单例（比如重新加载配置）。"""
    global _default_alerter
    _default_alerter = None
