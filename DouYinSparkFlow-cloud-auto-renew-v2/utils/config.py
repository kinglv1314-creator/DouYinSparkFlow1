import os
import sys
from enum import Enum
import json
import logging
from utils.logger import setup_logger

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = True
config = None
userData = None


def load_json_env(name, default):
    """Read a JSON environment variable and report configuration errors clearly."""
    raw_value = os.getenv(name, default)
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"环境变量 {name} 不是有效的 JSON：{exc.msg}") from exc


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"  # GitHub Action 运行
    LOCAL = "LOCAL"  # 本地代码运行
    PACKED = "PACKED"  # PyInstaller 打包运行

    def __str__(self):
        return self.value


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    elif os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    else:
        return Environment.LOCAL


def get_config():
    """
    获取配置信息
    :return: 配置字典
    """
    global config

    if config:
        return config

    config = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", ""),
        "messageTemplate": os.getenv("MESSAGE_TEMPLATE", "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]"),
        "hitokotoTypes": load_json_env(
            "HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]'
        ),
        "matchMode": os.getenv("MATCH_MODE", "nickname"),  # 是否使用短 ID 进行好友匹配
        "browserTimeout": int(os.getenv("BROWSER_TIMEOUT", "120000")),  # 浏览器操作超时时间，单位毫秒
        "friendListTimeout": int(os.getenv("FRIEND_LIST_WAIT_TIME", "2000")),  # 好友列表加载超时时间，单位毫秒
        "taskRetryTimes": int(os.getenv("TASK_RETRY_TIMES", "3")),  # 任务重试次数
        "sendConfirmTimeout": int(os.getenv("SEND_CONFIRM_TIMEOUT", "10000")),  # 发送确认等待时间，单位毫秒
        "logLevel": os.getenv("LOG_LEVEL", "DEBUG"),  # 日志级别
    }

    return config

def sanitize_cookies(cookies):
    if not isinstance(cookies, list) or not all(isinstance(cookie, dict) for cookie in cookies):
        raise ValueError("Cookies 必须是 Cookie 对象组成的 JSON 数组")

    allowed_fields = {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
    sanitized = []
    for original in cookies:
        cookie = dict(original)
        if "expirationDate" in cookie and "expires" not in cookie:
            cookie["expires"] = cookie["expirationDate"]

        same_site = str(cookie.get("sameSite", "")).lower()
        same_site_map = {
            "lax": "Lax",
            "strict": "Strict",
            "none": "None",
            "no_restriction": "None",
        }
        if same_site in same_site_map:
            cookie["sameSite"] = same_site_map[same_site]
        else:
            cookie.pop("sameSite", None)

        cookie = {key: value for key, value in cookie.items() if key in allowed_fields}
        if not cookie.get("name") or "value" not in cookie:
            continue
        if not cookie.get("url") and not cookie.get("domain"):
            continue
        if cookie.get("domain") and not cookie.get("path"):
            cookie["path"] = "/"
        sanitized.append(cookie)

    if not sanitized:
        raise ValueError("Cookies 中没有 Playwright 可用的条目")
    return sanitized


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData:
        return userData

    tasks = load_json_env("TASKS", "[]")
    if not isinstance(tasks, list):
        raise ValueError("环境变量 TASKS 必须是任务对象组成的 JSON 数组")

    userData = []

    for task in tasks:
        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        if not unique_id:
            logger.warning(f"{username} 的任务  缺少 unique_id 字段，已跳过")
            continue
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = os.getenv(cookies_key, "")
        if not cookies_str:
            logger.warning(
                f"{username} 的任务 缺少 {cookies_key} 环境变量，已跳过"
            )
            continue
        try:
            cookies = json.loads(cookies_str)
        except json.JSONDecodeError as exc:
            logger.warning(
                f"{username} 的任务 {cookies_key} 格式不正确，已跳过：{exc.msg}"
            )
            continue

        targets = task.get("targets", [])
        if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
            logger.warning(f"{username} 的任务 targets 格式不正确，已跳过")
            continue
        if not targets:
            logger.warning(f"{username} 的任务未配置目标好友，已跳过")
            continue

        try:
            userData.append(
                {
                    "unique_id": str(unique_id),
                    "username": username,
                    "cookies": sanitize_cookies(cookies),
                    "targets": targets,
                }
            )
        except ValueError as exc:
            logger.warning(f"{username} 的任务 {cookies_key} 无效，已跳过：{exc}")

    return userData
