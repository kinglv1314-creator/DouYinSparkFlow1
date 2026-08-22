import traceback
import os
import re
from urllib.parse import urlsplit
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response
import time
import json


complates = {}

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}


def _safe_name(value):
    return re.sub(r"[^0-9A-Za-z._-]+", "_", str(value))[:80] or "account"


def save_page_diagnostics(page, account_name, target_name, reason):
    """Save non-secret page diagnostics for GitHub Actions failures."""
    os.makedirs("logs", exist_ok=True)
    prefix = f"logs/{_safe_name(account_name)}-{_safe_name(target_name)}"
    try:
        page.screenshot(path=f"{prefix}-failure.png", full_page=True)
    except Exception as exc:
        logger.warning(f"保存页面截图失败：{exc}")
    logger.error(
        f"发送诊断：原因={reason}，URL={page.url}，标题={page.title()}"
    )


def _editable_text(locator):
    try:
        return locator.evaluate(
            """(element) => (element.innerText || element.textContent || element.value || '').trim()"""
        )
    except Exception:
        return ""


def confirm_message_send(page, chat_input, before_message_count, response_events):
    """Wait for an accepted send response or a visible UI state change."""
    deadline = time.monotonic() + config["sendConfirmTimeout"] / 1000
    while time.monotonic() < deadline:
        if any(event["ok"] for event in response_events):
            return "send API returned success"

        input_text = _editable_text(chat_input)
        if not input_text:
            return "chat input cleared after send"

        for selector in (
            '[class*="message-item"]',
            '[class*="message-content"]',
            '[class*="chat-message"]',
        ):
            try:
                if page.locator(selector).count() > before_message_count:
                    return f"message list changed ({selector})"
            except Exception:
                continue
        time.sleep(0.25)

    failed_responses = [event for event in response_events if not event["ok"]]
    if failed_responses:
        statuses = ", ".join(str(event["status"]) for event in failed_responses)
        raise RuntimeError(f"抖音发送接口返回失败状态：{statuses}")
    raise RuntimeError("按键后未检测到发送接口成功响应、输入框清空或新消息气泡")

def handle_response(response: Response):
    """
    只监听你要的那个接口响应
    """
    global userIDDict
    # 精准匹配目标接口 URL
    if "aweme/v1/creator/im/user_detail/" in response.url:
        # print(f"URL: {response.url}")
        # print(f"状态码: {response.status}")
        try:
            # 获取接口返回的 JSON 数据（就是你在 Network 里看到的内容）
            json_data = response.json()
            # print("\n📦 响应 JSON 数据：")
            # print(json.dumps(json_data, indent=4, ensure_ascii=False))
            for item in json_data.get("user_list", []):
                short_id = item.get("user", {}).get("ShortId")
                nickname = item.get("user", {}).get("nickname")
                user_id = item.get("user_id", "")
                userIDDict[str(short_id)] = {"nickname": nickname, "user_id": user_id}
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    :param args: 传递给操作的参数
    :param kwargs: 传递给操作的关键字参数
    """
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名"""
    # 定义目标元素和滚动容器的选择器
    friends_tab_selector = 'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]'
    target_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "semi-list-item-body semi-list-item-body-flex-start")]'
    scrollable_friends_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div'
    
    # [修复] 使用模糊匹配 no-more-tip- 前缀，不再依赖精确哈希后缀
    # 同时增加文本匹配作为兜底
    no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-")]'
    loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    logger.debug(f"账号 {username} 点击进入好友标签页")
    # 点击好友标签页
    page.wait_for_selector(friends_tab_selector)
    page.locator(friends_tab_selector).click()

    logger.debug(f"账号 {username} 进入好友列表页面")

    # 确保第一个好友元素加载完成
    first_friend_selector = 'xpath=//*[@id="sub-app"]/div/div/div[2]/div[2]/div/div/div[1]/div/div/div/ul/div/div/div[1]/li/div'
    page.wait_for_selector(first_friend_selector)
    page.locator(first_friend_selector).click()  # 点击第一个好友，确保列表激活

    logger.debug(f"账号 {username} 已激活好友列表，开始滚动查找目标好友")

    time.sleep(config["friendListTimeout"] / 1000)  # 等待好友列表加载

    found_targets = set()
    # [修改] 复制一份目标列表用于追踪进度
    remaining_targets = set(targets)

    # [修复] 新增：连续空滚动计数器（滚动后没有发现新好友的次数）
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10  # 连续10次滚动没有新好友，认为到底了

    while True:
        # 查找所有目标元素
        target_elements = page.locator(target_selector).all()

        # [修复] 记录本轮循环前已发现的好友数，用于判断是否有新发现
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                # 查找子元素 span，模糊匹配 class
                span = element.locator(
                    """xpath=.//span[contains(@class, "item-header-name-")]"""
                )
                targetName = span.inner_text()

                if targetName in found_targets:
                    continue  # 已处理过，跳过
                found_targets.add(targetName)

                logger.debug(f"账号 {username} 找到好友 {targetName}")
                # 检查是否是目标用户名
                if matchMode == "short_id":
                    targetSymbol = next((sid for sid, info in userIDDict.items() if info.get("nickname") == targetName), None)
                else:
                    targetSymbol = targetName

                if targetSymbol in targets:
                    element.click()
                    if matchMode == "short_id":
                        logger.debug(
                            f"账号 {username} 选中目标好友 {targetName} 准备开始交互"
                        )
                    else:
                        logger.debug(
                            f"账号 {username} 选中目标好友 {targetName} (ShortId: {targetSymbol}) 准备开始交互"
                        )
                    yield targetName
                    
                    # [修改] 标记已找到，如果全找到了直接退出
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            # [修复] 检查本轮是否有新好友被发现
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0  # 有新发现，重置计数器
            else:
                empty_scroll_count += 1  # 无新发现，递增计数器

            # [修复] 状态检测逻辑（多重兜底）
            
            # 1. 检查是否到底（"没有更多了" —— 使用模糊类名匹配）
            if page.locator(no_more_selector).count() > 0:
                logger.info(f"账号 {username} 检测到'没有更多了'标志，已到达底部")
                if len(remaining_targets) > 0:
                    logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")
                break

            # 2. [修复] 检查连续空滚动次数，防止死循环
            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部")
                if len(remaining_targets) > 0:
                    logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")
                break

            # 3. 检查是否正在加载
            if page.locator(loading_selector).count() > 0:
                logger.debug(f"账号 {username} 列表正在加载中 (Loading)...")
                time.sleep(1.5) # 给加载留点时间
                # 不 break，继续去滚动以触发后续内容

            # 4. 滚动容器
            scrollable_element = page.locator(
                scrollable_friends_selector
            ).element_handle()
            
            if scrollable_element:
                # [修复] 记录滚动前的 scrollTop，用于检测是否真的滚动了
                scroll_top_before = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )
                
                page.evaluate(
                    "(element) => element.scrollTop += 800", scrollable_element
                )
                
                # [修复] 检测滚动后的 scrollTop
                time.sleep(0.3)
                scroll_top_after = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )
                
                if scroll_top_before == scroll_top_after:
                    # scrollTop 没有变化，说明已经到底了
                    empty_scroll_count += 2  # 加速判定到底
                    logger.debug(f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})")
                else:
                    logger.debug(f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})")
                
                time.sleep(1.5)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break


def do_user_task(browser, account_name, cookies, targets):
    """Run one account and return the friends that received a message."""
    context = browser.new_context()  # 每个任务使用独立的上下文
    sent_targets = []
    try:
        context.set_default_navigation_timeout(config["browserTimeout"])  # 设置导航超时时间为 120 秒
        context.set_default_timeout(config["browserTimeout"])  # 设置所有操作的默认超时时间为 120 秒

        page = context.new_page()
        
        if matchMode == "short_id":  # 使用抖音号进行匹配
            page.on("response", handle_response)
        
        # 打开抖音创作者中心
        retry_operation(
            "打开抖音创作者中心",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://creator.douyin.com/",
        )
        # 注入 Cookie
        context.add_cookies(cookies)

        # 导航到消息页面
        retry_operation(
            "导航到消息页面",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://creator.douyin.com/creator-micro/data/following/chat",
        )

        page.wait_for_load_state("domcontentloaded")
        current_url = page.url.lower()
        if any(marker in current_url for marker in ("login", "passport", "captcha", "verify", "challenge")):
            save_page_diagnostics(page, account_name, "login", "Cookies 未登录或页面触发验证")
            raise RuntimeError("未进入抖音消息页，当前 Cookies 可能失效或云端 IP 触发验证")

        logger.debug(f"账号 {account_name} 开始发送消息")
        # 滚动并选择用户
        for target_name in scroll_and_select_user(page, account_name, targets):
            logger.debug(f"账号 {account_name} 已选中好友 {target_name}，准备发送消息")
            # 等待聊天输入框元素加载完成，使用更稳定的属性选择器
            chat_input_selector = "xpath=//div[contains(@class, 'chat-input-')]"
            page.wait_for_selector(chat_input_selector, timeout=config["browserTimeout"])
            chat_input = page.locator(chat_input_selector)
            chat_input.click()

            message = build_message().replace("\\n", "\n").strip()
            before_message_count = 0
            for selector in (
                '[class*="message-item"]',
                '[class*="message-content"]',
                '[class*="chat-message"]',
            ):
                try:
                    before_message_count = max(before_message_count, page.locator(selector).count())
                except Exception:
                    pass

            for line_index, line in enumerate(message.splitlines() or [message]):
                chat_input.type(line)  # 输入每一行
                if line_index < len(message.splitlines()) - 1:
                    chat_input.press("Shift+Enter")  # 模拟 Shift+Enter 插入换行

            logger.debug(
                f"账号 {account_name} 准备发送消息给好友 {target_name}：\n\t{message}"
            )
            response_events = []

            def on_send_response(response):
                url = response.url.lower()
                if any(marker in url for marker in ("send_message", "send_msg", "message/send", "/im/send")):
                    response_events.append(
                        {
                            "ok": 200 <= response.status < 300,
                            "status": response.status,
                            "path": urlsplit(response.url).path,
                        }
                    )

            page.on("response", on_send_response)
            chat_input.press("Enter")
            try:
                confirmation = confirm_message_send(
                    page, chat_input, before_message_count, response_events
                )
            except Exception as exc:
                save_page_diagnostics(page, account_name, target_name, str(exc))
                raise
            finally:
                page.remove_listener("response", on_send_response)

            sent_targets.append(target_name)
            logger.info(
                f"账号 {account_name} 已向好友 {target_name} 发送续火花消息，确认方式：{confirmation}"
            )
            time.sleep(1)
        if not sent_targets:
            raise RuntimeError("没有找到可发送消息的目标好友，请检查昵称/抖音号和 Cookies")
        return sent_targets
    finally:
        context.close()


def runTasks():
    if not userData:
        raise RuntimeError("没有可执行任务，请检查 TASKS 和对应的 COOKIES_<UNIQUE_ID> 环境变量")

    playwright, browser = get_browser()
    failures = []
    try:
        # 检查是否启用多任务和任务数量
        # 创建信号量以限制并发任务数量
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            complates[user["unique_id"]] = []  # 初始化该用户的已完成列表
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            try:
                sent_targets = do_user_task(browser, username, cookies, targets)
                complates[user["unique_id"]] = sent_targets
                logger.info(f"账号 {username} 任务完成，已发送给：{', '.join(sent_targets)}")
            except Exception as exc:
                failures.append(username)
                logger.exception(f"账号 {username} 任务失败：{exc}")
    finally:
        # 关闭浏览器实例
        browser.close()
        
        playwright.stop()
    if failures:
        raise RuntimeError(f"以下账号续火花失败：{', '.join(failures)}；请查看 logs/app.log")
