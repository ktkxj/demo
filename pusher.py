# -*- coding: utf-8 -*-
"""
Binance Trenches Token + Twitter Pusher

功能：

1. Binance WebSocket
   - NEW Token
   - UPDATE Token

2. Token 数据采用增量 PATCH Merge
   - UPDATE 缺字段不会覆盖旧数据
   - UPDATE 空字符串不会覆盖旧数据
   - UPDATE None 不会覆盖旧数据

3. Token 创建时间`
   - 优先 createTime
   - 其次 createdAt
   - 其次 created_at
   - 最后使用本地 first_seen

4. Token age
   - 后端计算初始 age
   - 前端可以根据 createTime 每秒自动更新

5. Twitter
   - 从 Token socials 提取 Tweet ID
   - 若没有 Tweet ID，则提取 Twitter Handle
   - 通过 Handle 调用 Profile API 获取用户信息
   - SQLite 缓存
   - 推送占位卡片后立即异步获取完整推文 / Profile
   - 一个 Tweet 或 Profile 可以关联多个 Token
   - 允许对已推送的 ID 再次推送更新
   - 推送失败允许再次尝试
   - API 请求支持自动重试（最多 3 次）

6. 无文件监控

7. 优化：拿到 Twitter ID 后立即创建占位卡片，后台异步补全
   拿到 Handle 后直接异步获取 Profile 并推送

8. 卡片创建规则（关键）：
   - NEW Token 流（w3w@pulse-rank@56_new_tokens）：仅写入 token_map + pending，不创建卡片
   - UPDATE 流（w3w@pulse-rank@56_update_tokens）：
     * NEW Token（在 pending 中）首次从 socials 解析到 tweet_id / handle → 触发卡片创建
     * OLD Token（不在 pending 中）→ 绝不触发卡片创建，仅推送 token_update 增量到已存在卡片
   - OLD Token 的数据保留在 token_map，当未来某个 NEW Token 触发同一 tweet_id 的卡片创建时，
     build_tokens_for_tweet 会把该 tweet_id 关联的所有 contract（含 OLD）一并带上

9. Token 数据实时更新：当某个 Token 发生更新时，自动向 Web 客户端推送
   更新消息，刷新所有关联该 Token 的推文卡片中的 Token 信息。
   现在采用增量推送，只更新 Token 列表，不重新推送完整卡片。
   同时增加 leading + trailing 节流，避免过于频繁的推送导致前端压力，
   且 trailing 保证窗口结束后总有一次推送反映最新状态。
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Set, List, Tuple

import requests
import websockets
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 加载 .env 环境变量（用于 TWITTER_API_KEY）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # 没装 python-dotenv 也能跑，从系统环境变量读

# ============================================================
# HTTP Session 池（分离 API 调用与本地推送，避免互相阻塞）
# ============================================================
# 设计：两类 HTTP 调用走不同的 Session + 连接池：
#   1. Binance API 调用（get_tweet_data / get_profile_info）：可能耗时长（重试 3 次、超时 20s），
#      走较大连接池 + 较长重试，与本地推送隔离避免占用对方连接
#   2. 本地 server 推送（push_tweet / push_token_update）：耗时短（~10ms），重试少，
#      走独立连接池，保证 API 重试占用线程时推送仍能继续
_api_session: Optional[requests.Session] = None
_push_session: Optional[requests.Session] = None
_http_session_lock = threading.Lock()

def _make_session(pool_size: int) -> requests.Session:
    """创建一个新的 requests.Session，配置 HTTPAdapter 连接池。"""
    s = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=Retry(total=0, backoff_factor=0),
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def get_api_session() -> requests.Session:
    """获取用于 Binance API 调用的 Session（大池，支持并发重试）。
    - pool_size=30：爆发式行情时多个 token 同时拉取推文/Profile 不被阻塞
    """
    global _api_session
    if _api_session is not None:
        return _api_session
    with _http_session_lock:
        if _api_session is not None:
            return _api_session
        _api_session = _make_session(30)
        return _api_session

def get_push_session() -> requests.Session:
    """获取用于本地 server 推送的 Session（小池，但与 API 隔离）。
    - pool_size=10：推送 POST 请求短平快，与 API 调用不共享连接池，避免互相饿死
    """
    global _push_session
    if _push_session is not None:
        return _push_session
    with _http_session_lock:
        if _push_session is not None:
            return _push_session
        _push_session = _make_session(10)
        return _push_session

# 兼容旧调用：get_http_session() 现在返回 API session（旧的统一调用都是给 API 用的）
def get_http_session() -> requests.Session:
    """[已废弃] 等同于 get_api_session()，仅为兼容旧调用保留。"""
    return get_api_session()

# ============================================================
# 专用线程池：Twitter API 调用隔离
# ============================================================
# 硬规则：Twitter API 调用（get_tweet_cached / get_tweet_data / get_profile_cached）
# 内部有 time.sleep 重试（最坏 1s+2s+4s = 7s）+ HTTP 超时（20s），
# 单次最坏可达 ~60s。如果走 asyncio.to_thread 默认池（min(32, cpu+4)），
# 爆发式行情时多个 NEW token 同时拉推文会占满默认池，
# 导致 token_update 的 HTTP fallback 也被拖住。
#
# 解决：Twitter API 独占 ThreadPoolExecutor(max_workers=4)，
# 与 token_update / merge 路径完全隔离。
# max_workers=4 的理由：Binance API 有 QPS 限制，并发太高会被限流；
# 4 个并发足以覆盖爆发式行情（同一时刻 4 个 NEW token 同时拉推文已罕见）。
_twitter_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="twitter-api")

async def run_in_twitter_executor(func, *args):
    """在专用 Twitter 线程池中运行阻塞函数，返回 awaitable。
    替代 asyncio.to_thread(func, *args)，与 token_update 路径完全隔离。
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_twitter_executor, lambda: func(*args))

# 专用线程池：GMGN API 查询（限流 5/s，独立线程池避免与 Binance API 竞争）
_gmgn_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gmgn-api")
GMGN_RATE_LIMIT_PER_SEC = 5.0  # IP 限流每秒最多 5 次
_gmgn_last_request_time = 0.0
_gmgn_lock = threading.Lock()

# 专用线程池：Binance Meta API 查询（与 GMGN 和 Twitter API 隔离）
_binance_meta_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-meta")
BINANCE_META_CHAIN_ID = "56"
_binance_meta_lock = threading.Lock()

# ============================================================
# GMGN 客户端（三 API Key 机制）
# ============================================================
# 用途分离：
#   - GMGN_TOKEN_INFO_API_KEY（gmgn_7d80...）：用于 token_info 查询，参与 Binance UPDATE 流竞争
#   - GMGN_TRADE_API_KEY    （gmgn_98c2...）：仅用于 swap 交易（前端"一键买入"/"一键卖出"按钮）
#   - GMGN_HOLDINGS_API_KEY （gmgn_8809...）：用于 wallet_holdings 轮询（前端持仓弹窗）
# 三个 key 各自独立限流配额，互不影响。
GMGN_TOKEN_INFO_API_KEY = "gmgn_7d80f40c92424fb5133adb023f1b7f6b"
GMGN_TRADE_API_KEY     = "gmgn_98c28c4f2bd0e54533be7ecac53eb534"
GMGN_HOLDINGS_API_KEY  = "gmgn_88098790c861356f4a2a89b4fbbddd2d"
# 持仓轮询钱包地址 + 限流
GMGN_HOLDINGS_WALLET   = "0xaced8b129c0eb5ea65d00b92ef3d063e512fd5ff"
GMGN_HOLDINGS_INTERVAL_SEC = 0.5  # 每秒最多 2 次（用户要求）

# 功能开关：是否启用 GMGN get_token_info 查询社交媒体。
# 设为 True：参与 Binance UPDATE 流的 Twitter handle 竞争。
GMGN_TOKEN_INFO_ENABLED = True

# 429 ban 机制：出现 RATE_LIMIT_EXCEEDED / RATE_LIMIT_BANNED 立即停止后续 token_info 请求，
# 直到 _gmgn_ban_until 时刻才恢复。避免被 GMGN 永久 ban。
_gmgn_ban_lock = threading.Lock()
_gmgn_ban_until = 0.0  # 0 = 未 ban；>0 = ban 到这个 time.time() 时刻
GMGN_BAN_DEFAULT_SECONDS = 300  # 默认 ban 5 分钟（无 reset_at 头时使用）
GMGN_BAN_MIN_SECONDS = 60       # 即使 reset_at 很短，也至少冷却 60s 避免反复触发

# 三个独立 client 单例（延迟初始化，互不影响）
_gmgn_token_info_client = None  # 用于 get_token_info 等查询
_gmgn_trade_client = None      # 用于 swap 等交易
_gmgn_holdings_client = None   # 用于 wallet_holdings 轮询

def _get_gmgn_token_info_client():
    """获取 token_info 查询专用客户端（使用 GMGN_TOKEN_INFO_API_KEY）。"""
    global _gmgn_token_info_client
    if _gmgn_token_info_client is not None:
        return _gmgn_token_info_client
    try:
        from gmgn_client import GmGnClient
        _gmgn_token_info_client = GmGnClient(api_key=GMGN_TOKEN_INFO_API_KEY)
        logger.info("✅ GMGN token_info 客户端初始化成功 (key=%s...)",
                    GMGN_TOKEN_INFO_API_KEY[:12])
    except Exception as e:
        logger.warning("⚠️ GMGN token_info 客户端初始化失败: %s", e)
        _gmgn_token_info_client = False  # 标记为不可用
    return _gmgn_token_info_client

def _get_gmgn_trade_client():
    """获取交易专用客户端（使用 GMGN_TRADE_API_KEY + gmgn_keypair.pem）。
    关键：swap 是 signed endpoint，需要与 GMGN_TRADE_API_KEY 匹配的私钥。
    交易用 gmgn_keypair.pem，持仓用 wallet_keypair.pem，两者不同。
    """
    global _gmgn_trade_client
    if _gmgn_trade_client is not None:
        return _gmgn_trade_client
    try:
        import os
        from gmgn_client import GmGnClient
        # gmgn_keypair.pem 与 download/ 同目录（交易专用私钥）
        trade_pem_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "gmgn_keypair.pem"
        )
        if os.path.exists(trade_pem_path):
            # 显式读取并传入，避免依赖 gmgn_client.py 顶部的 PRIVATE_KEY_PEM 默认值
            with open(trade_pem_path, "r") as f:
                trade_pem_content = f.read()
            _gmgn_trade_client = GmGnClient(
                api_key=GMGN_TRADE_API_KEY,
                private_key_pem=trade_pem_content,
            )
        else:
            # 兜底：让 GmGnClient 自己从默认配置加载
            _gmgn_trade_client = GmGnClient(api_key=GMGN_TRADE_API_KEY)
        logger.info("✅ GMGN trade 客户端初始化成功 (key=%s..., pem=gmgn_keypair.pem)",
                    GMGN_TRADE_API_KEY[:12])
    except Exception as e:
        logger.warning("⚠️ GMGN trade 客户端初始化失败: %s", e)
        _gmgn_trade_client = False
    return _gmgn_trade_client

def _get_gmgn_holdings_client():
    """获取持仓轮询专用客户端（使用 GMGN_HOLDINGS_API_KEY + wallet_keypair.pem）。
    关键：wallet_holdings 是 signed endpoint，需要与 GMGN_HOLDINGS_API_KEY 匹配的私钥。
    交易用 gmgn_keypair.pem，持仓用 wallet_keypair.pem，两者不同。
    """
    global _gmgn_holdings_client
    if _gmgn_holdings_client is not None:
        return _gmgn_holdings_client
    try:
        import os
        from gmgn_client import GmGnClient
        # wallet_keypair.pem 与 download/ 同目录
        wallet_pem_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "wallet_keypair.pem"
        )
        if not os.path.exists(wallet_pem_path):
            logger.error("❌ wallet_keypair.pem 未找到: %s", wallet_pem_path)
            _gmgn_holdings_client = False
            return _gmgn_holdings_client
        with open(wallet_pem_path, "r") as f:
            wallet_pem_content = f.read()
        _gmgn_holdings_client = GmGnClient(
            api_key=GMGN_HOLDINGS_API_KEY,
            private_key_pem=wallet_pem_content,  # 显式传入持仓专用私钥
        )
        logger.info("✅ GMGN holdings 客户端初始化成功 (key=%s..., pem=wallet_keypair.pem)",
                    GMGN_HOLDINGS_API_KEY[:12])
    except Exception as e:
        logger.warning("⚠️ GMGN holdings 客户端初始化失败: %s", e)
        _gmgn_holdings_client = False
    return _gmgn_holdings_client

# 兼容旧引用：_get_gmgn_client() 等价于 _get_gmgn_token_info_client()
# （历史代码中可能有其他地方引用，保留入口）
def _get_gmgn_client():
    return _get_gmgn_token_info_client()

# ============================================================
# 持仓轮询：后台线程每 0.5s 拉一次 wallet_holdings，缓存到 _holdings_cache
# 前端 GET /api/holdings 直接读缓存（瞬时返回，不阻塞请求）
# ============================================================
_holdings_cache_lock = threading.Lock()
_holdings_cache = {
    "data": None,           # GMGN 原始返回（list of holding dict）
    "updated_at": 0.0,      # 上次成功更新时间戳
    "error": None,          # 上次错误信息（None = 无错误）
    "poll_total": 0,         # 总轮询次数
    "poll_success": 0,       # 成功次数
    "poll_fail": 0,          # 失败次数
}
_holdings_thread_started = False
_holdings_thread_stop = threading.Event()

def _holdings_poll_loop():
    """持仓轮询主循环：每 GMGN_HOLDINGS_INTERVAL_SEC 拉一次 wallet_holdings。
    使用 GMGN_HOLDINGS_API_KEY（独立限流），不影响 token_info / trade。
    """
    logger.info("🔄 持仓轮询线程启动 (wallet=%s, interval=%.2fs)",
                GMGN_HOLDINGS_WALLET, GMGN_HOLDINGS_INTERVAL_SEC)
    client = _get_gmgn_holdings_client()
    if not client:
        logger.error("❌ 持仓轮询线程退出：GMGN holdings 客户端不可用")
        return
    while not _holdings_thread_stop.is_set():
        try:
            data = client.get_wallet_holdings("bsc", GMGN_HOLDINGS_WALLET)
            with _holdings_cache_lock:
                _holdings_cache["data"] = data
                _holdings_cache["updated_at"] = time.time()
                _holdings_cache["error"] = None
                _holdings_cache["poll_total"] += 1
                _holdings_cache["poll_success"] += 1
        except Exception as e:
            api_error = getattr(e, "api_error", None)
            api_message = getattr(e, "api_message", None) or str(e)
            with _holdings_cache_lock:
                _holdings_cache["error"] = f"{api_error or 'ERROR'}: {api_message}"
                _holdings_cache["poll_total"] += 1
                _holdings_cache["poll_fail"] += 1
            # 429 限流：跳过 5s 避免反复触发
            if api_error in ("RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED"):
                logger.warning("🚫 holdings 查询被限流，休眠 5s: %s", api_message)
                _holdings_thread_stop.wait(5.0)
                continue
            logger.debug("holdings 查询失败: %s", e)
        # 等待 interval（可被打断用于退出）
        _holdings_thread_stop.wait(GMGN_HOLDINGS_INTERVAL_SEC)
    logger.info("🛑 持仓轮询线程退出")

def start_holdings_polling():
    """启动持仓轮询线程（只能启动一次，重复调用安全）。"""
    global _holdings_thread_started
    if _holdings_thread_started:
        return
    _holdings_thread_started = True
    t = threading.Thread(target=_holdings_poll_loop, name="gmgn-holdings-poll", daemon=True)
    t.start()

def get_holdings_snapshot() -> dict:
    """获取持仓快照（前端 /api/holdings 调用）。"""
    with _holdings_cache_lock:
        return {
            "wallet": GMGN_HOLDINGS_WALLET,
            "data": _holdings_cache["data"],
            "updated_at": _holdings_cache["updated_at"],
            "age_seconds": (time.time() - _holdings_cache["updated_at"]) if _holdings_cache["updated_at"] else None,
            "error": _holdings_cache["error"],
            "poll_total": _holdings_cache["poll_total"],
            "poll_success": _holdings_cache["poll_success"],
            "poll_fail": _holdings_cache["poll_fail"],
        }

def _gmgn_is_banned() -> bool:
    """检查 token_info 查询是否处于 ban 期。"""
    with _gmgn_ban_lock:
        return time.time() < _gmgn_ban_until

def _gmgn_set_ban(reset_at_unix: float = None):
    """设置 token_info 查询 ban。
    reset_at_unix：GMGN 返回的 X-RateLimit-Reset（Unix 秒）。
    不传则使用默认 ban 时长。
    """
    global _gmgn_ban_until
    now = time.time()
    if reset_at_unix and reset_at_unix > now:
        # reset_at 是未来时刻，按它设 ban，但至少 GMGN_BAN_MIN_SECONDS
        ban_seconds = max(reset_at_unix - now, GMGN_BAN_MIN_SECONDS)
    else:
        ban_seconds = GMGN_BAN_DEFAULT_SECONDS
    with _gmgn_ban_lock:
        _gmgn_ban_until = now + ban_seconds
    logger.warning("🚫 GMGN token_info 触发 429 限流，停止查询 %d 秒 (至 %s)",
                   ban_seconds,
                   time.strftime("%H:%M:%S", time.localtime(_gmgn_ban_until)))

def _gmgn_query_token_social(contract: str) -> Optional[str]:
    """通过 GMGN API 查询 token 社交媒体信息，提取 twitter username。
    IP 限流：每秒最多 5 次。
    出现 429 立即停止后续请求（设置 ban 期），避免被 GMGN 永久 ban。
    返回 twitter handle / tweet_id:xxx / None。
    """
    # 功能开关
    if not GMGN_TOKEN_INFO_ENABLED:
        return None
    # 429 ban 期内直接返回 None，不发请求
    if _gmgn_is_banned():
        return None
    client = _get_gmgn_token_info_client()
    if not client:
        return None
    # IP 限流：确保距上次请求 >= 200ms (5/s)
    with _gmgn_lock:
        global _gmgn_last_request_time
        now = time.time()
        elapsed = now - _gmgn_last_request_time
        if elapsed < (1.0 / GMGN_RATE_LIMIT_PER_SEC):
            time.sleep((1.0 / GMGN_RATE_LIMIT_PER_SEC) - elapsed)
        _gmgn_last_request_time = time.time()
    try:
        info = client.get_token_info("bsc", contract)
        if info and isinstance(info, dict):
            link = info.get("link") or {}
            twitter = link.get("twitter_username") or ""
            if twitter:
                twitter = twitter.strip()
                # GMGN 的 twitter_username 可能省略 x.com 前缀
                # 如 "Calob2Fly/status/2094631988863050071" 实际是 x.com/Calob2Fly/status/2094631988863050071
                # 先补全成完整 URL 再用 extract_tweet_id / extract_twitter_handle 解析
                if not twitter.startswith("http"):
                    twitter_url = f"https://x.com/{twitter}"
                else:
                    twitter_url = twitter
                # 尝试提取 tweet_id
                tid = extract_tweet_id({"twitter": twitter_url})
                if tid:
                    logger.info("🎯 GMGN 获取到 Tweet ID: %s -> %s", contract, tid)
                    return f"tweet_id:{tid}"
                # 尝试提取 handle
                handle = extract_twitter_handle({"twitter": twitter_url})
                if handle:
                    if handle.isdigit():
                        logger.debug("🎯 GMGN 返回纯数字 twitter_id（非 handle），跳过: %s -> %s", contract, handle)
                        return None
                    logger.info("🎯 GMGN 获取到 Twitter handle: %s -> @%s", contract, handle)
                    return handle
                # 都没匹配到，但原始值非空，检查是否是纯 handle（无路径分隔符）
                if "/" not in twitter and not twitter.isdigit():
                    logger.info("🎯 GMGN 获取到 Twitter handle: %s -> @%s", contract, twitter)
                    return twitter
    except Exception as e:
        # 检测 GMGN 限流错误：RATE_LIMIT_EXCEEDED / RATE_LIMIT_BANNED
        api_error = getattr(e, "api_error", None)
        if api_error in ("RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED"):
            reset_at = getattr(e, "reset_at", None)
            _gmgn_set_ban(reset_at)
            # 同时把这次失败计入 GMGN 统计
            _gmgn_stats_inc("gmgn_fail")
            return None
        logger.debug("GMGN 查询失败 %s: %s", contract, e)
    return None

def _binance_meta_query_social(contract: str) -> Optional[str]:
    """通过 Binance Meta API 查询 token 社交媒体信息，提取 twitter handle。
    返回 twitter handle 或 None。
    """
    import uuid as _uuid
    headers = {
        "sec-ch-ua-platform": "\"macOS\"",
        "referer": "https://web3.binance.com/zh-CN/trenches?chain=bsc",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "sec-ch-ua": "\"Not=A?Brand\";v=\"99\", \"Google Chrome\";v=\"151\", \"Chromium\";v=\"151\"",
        "content-type": "application/json",
        "sec-ch-ua-mobile": "?0",
        "x-trace-id": str(_uuid.uuid4()),
        "x-ui-request-trace": str(_uuid.uuid4()),
        "lang": "zh-CN"
    }
    url = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info"
    params = {"chainId": BINANCE_META_CHAIN_ID, "contractAddress": contract}
    with _binance_meta_lock:
        try:
            resp = get_api_session().get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                d = data.get("data") if isinstance(data, dict) else None
                if d and isinstance(d, dict):
                    links = d.get("links") or []
                    for link in links:
                        if not isinstance(link, dict):
                            continue
                        label = link.get("label", "")
                        link_url = link.get("link", "")
                        if label.lower() in ("x", "twitter") and link_url:
                            handle = link_url.rstrip("/").split("/")[-1]
                            if handle and not handle.isdigit():
                                logger.info("🎯 BinanceMeta 获取到 Twitter handle: %s -> @%s", contract, handle)
                                return handle
        except Exception as e:
            logger.debug("Binance Meta 查询失败 %s: %s", contract, e)
    return None

# GMGN 竞争统计
_gmgn_stats_lock = threading.Lock()
_gmgn_stats = {
    "gmgn_won": 0,            # GMGN 先获取到 twitter id
    "binance_won": 0,         # Binance UPDATE 流先获取到
    "binance_meta_won": 0,    # Binance Meta API 先获取到
    "gmgn_total": 0,          # GMGN 查询总次数
    "gmgn_success": 0,        # GMGN 成功获取到 twitter 的次数
    "gmgn_fail": 0,           # GMGN 查询失败次数
    "binance_meta_total": 0,  # Binance Meta 查询总次数
    "binance_meta_success": 0,
    "binance_meta_fail": 0,
}

def _gmgn_stats_inc(key, delta=1):
    with _gmgn_stats_lock:
        _gmgn_stats[key] = _gmgn_stats.get(key, 0) + delta
    # 同步到全局 stats（持久化到 stats.json）
    stats_inc(key, delta)

def _gmgn_stats_snapshot():
    with _gmgn_stats_lock:
        return dict(_gmgn_stats)

# GMGN 已查询过的 contract 集合（避免重复查询）
_gmgn_queried: Set[str] = set()
_gmgn_queried_lock = threading.Lock()

# 专用线程池：社交流批量写 SQLite（与 Twitter API 隔离，避免互相饿死）
# 社交流是高频写，单次 flush 可能 100-500ms，绝不能阻塞 asyncio 事件循环
_social_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="social-db")

# ============================================================
# 配置
# ============================================================

TARGET_APP_URL = "http://localhost:50000"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tweets.db")
ENABLE_WS = True

WS_URI = "wss://nbstream.binance.com/w3w/stream"
NEW_TOKENS_STREAM = "w3w@pulse-rank@56_new_tokens"
UPDATE_TOKENS_STREAM = "w3w@pulse-rank@56_update_tokens"
# 社交动态流：实时推送 reply/quote/retweet 事件（含推文内容、作者、引用推文）
# 高频流，用于预热 SQLite 缓存，让后续 token 关联的 tweet_id 直接命中本地、跳过 API 调用
SOCIAL_STREAM = "w3w@tracker@social"
# 社交翻译流：推送推文的中文翻译（与社交动态流配套）
SOCIAL_TRANSLATION_STREAM = "w3w@tracker@social@translation"
SUBSCRIBE_ID = "1"
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 10
RETRY_INITIAL = 2
RETRY_MAX = 30
NEW_TOKEN_PENDING_TTL = 10.0
# HTTP 超时：Binance 内部 API 正常响应 <1s，5s 足够覆盖网络抖动；
# 之前 20s 是给"慢响应"留余量，但慢响应在交易场景边际价值极低——
# 5s 内没拿到结果就快速失败重试，比死等更符合交易决策需求。
HTTP_TIMEOUT = 5
PUSH_TIMEOUT = 5  # 本地推送，本应秒级；超时即失败重试
# 重试退避：调保守一点，避免激进退避反而更容易被风控。
# 之前 0.3s→0.6s 过于激进（连发可能触发限流）；1.5s→3s 给 API 足够恢复时间。
API_RETRY_COUNT = 3
API_RETRY_DELAY = 1.5
# TwitterAPI.io hedged request 触发超时：Binance 1s 没返回就并发切到 TwitterAPI.io
# 取先到者。TwitterAPI.io 是付费 API，每 tweet_id 只尝试一次（避免重复扣费）。
BINANCE_HEDGE_TIMEOUT = 1.0

# TwitterAPI.io 配置（从环境变量读取 API Key，在 load_dotenv() 之后取值）
TWITTERAPI_IO_KEY = os.getenv("TWITTER_API_KEY", "")
TWITTERAPI_IO_TIMEOUT = 5

# ============================================================
# 统计指标采集：方便判断订阅是否值得
# ============================================================
# 统计项说明：
# - binance_api: 总调用 / 成功 / 失败 / 平均耗时
# - twitterapi:  总调用 / 成功 / 失败 / 平均耗时
# - hedged:      Binance 抢到次数 / TwitterAPI.io 抢到次数
# - cache:       SQLite 缓存命中（get_tweet_cached 命中本地）
# - social:      接收事件数 / 写入 tweets / 写入 translations / 丢弃 / 缓存命中次数
#
# 写入文件：JSON 格式，每 STATS_WRITE_INTERVAL 秒覆盖式写入
STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")
STATS_WRITE_INTERVAL = 60.0  # 每 60 秒写一次

# 线程安全的统计计数器
_stats_lock = threading.Lock()
_stats = {
    "start_time": time.time(),
    "last_reset": time.time(),
    # Binance API
    "binance_api_total": 0,
    "binance_api_success": 0,
    "binance_api_fail": 0,
    "binance_api_latency_sum": 0.0,  # 秒，用于算平均
    # TwitterAPI.io
    "twitterapi_total": 0,
    "twitterapi_success": 0,
    "twitterapi_fail": 0,
    "twitterapi_latency_sum": 0.0,
    # Hedged
    "hedged_binance_won": 0,
    "hedged_twitterapi_won": 0,
    # Cache（get_tweet_cached 命中本地，按来源细分）
    "cache_hit": 0,
    "cache_miss": 0,
    "social_cache_hits": 0,   # social 流预热的 tweet_id 被命中
    "api_cache_hits": 0,      # API（binance/twitterapi）写入的 tweet_id 被命中
    "unknown_cache_hits": 0,  # 来源未知（旧数据/迁移数据）被命中
    # Social 流
    "social_events_received": 0,
    "social_tweets_written": 0,
    "social_translations_written": 0,
    "social_dropped": 0,
    # GMGN 竞争统计
    "gmgn_total": 0,
    "gmgn_success": 0,
    "gmgn_fail": 0,
    "gmgn_won": 0,
    "binance_won": 0,
    "binance_meta_total": 0,
    "binance_meta_success": 0,
    "binance_meta_fail": 0,
    "binance_meta_won": 0,
}

def stats_inc(key: str, delta: int = 1):
    """原子递增统计计数器（线程安全）。"""
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + delta

def stats_add_latency(key: str, seconds: float):
    """原子累加延迟（用于算平均）。"""
    with _stats_lock:
        _stats[key] = _stats.get(key, 0.0) + seconds

def _snapshot_stats() -> dict:
    """读取统计快照（线程安全），并计算派生指标。"""
    with _stats_lock:
        s = dict(_stats)
    now = time.time()
    uptime = now - s["start_time"]
    window = now - s["last_reset"]
    # 计算平均延迟
    binance_avg = (s["binance_api_latency_sum"] / s["binance_api_success"]) if s["binance_api_success"] else 0
    twitterapi_avg = (s["twitterapi_latency_sum"] / s["twitterapi_success"]) if s["twitterapi_success"] else 0
    # 缓存命中率
    cache_total = s["cache_hit"] + s["cache_miss"]
    cache_hit_rate = (s["cache_hit"] / cache_total * 100) if cache_total else 0
    # API 成功率
    binance_success_rate = (s["binance_api_success"] / s["binance_api_total"] * 100) if s["binance_api_total"] else 0
    twitterapi_success_rate = (s["twitterapi_success"] / s["twitterapi_total"] * 100) if s["twitterapi_total"] else 0
    # 每分钟速率
    window_min = window / 60 if window > 0 else 1
    return {
        "uptime_seconds": int(uptime),
        "window_seconds": int(window),
        "uptime_human": f"{int(uptime // 3600)}h{int((uptime % 3600) // 60)}m{int(uptime % 60)}s",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "binance_api": {
            "total": s["binance_api_total"],
            "success": s["binance_api_success"],
            "fail": s["binance_api_fail"],
            "success_rate_pct": round(binance_success_rate, 1),
            "avg_latency_ms": round(binance_avg * 1000, 1),
            "per_min": round(s["binance_api_total"] / window_min, 1),
        },
        "twitterapi": {
            "total": s["twitterapi_total"],
            "success": s["twitterapi_success"],
            "fail": s["twitterapi_fail"],
            "success_rate_pct": round(twitterapi_success_rate, 1),
            "avg_latency_ms": round(twitterapi_avg * 1000, 1),
            "per_min": round(s["twitterapi_total"] / window_min, 1),
        },
        "hedged": {
            "binance_won": s["hedged_binance_won"],
            "twitterapi_won": s["hedged_twitterapi_won"],
        },
        "cache": {
            "hit": s["cache_hit"],
            "miss": s["cache_miss"],
            "hit_rate_pct": round(cache_hit_rate, 1),
            "social_cache_hits": s["social_cache_hits"],
            "api_cache_hits": s["api_cache_hits"],
            "unknown_cache_hits": s["unknown_cache_hits"],
        },
        "social": {
            "events_received": s["social_events_received"],
            "tweets_written": s["social_tweets_written"],
            "translations_written": s["social_translations_written"],
            "dropped": s["social_dropped"],
            "events_per_min": round(s["social_events_received"] / window_min, 1),
        },
        "gmgn": {
            "total": s.get("gmgn_total", 0),
            "success": s.get("gmgn_success", 0),
            "fail": s.get("gmgn_fail", 0),
            "gmgn_won": s.get("gmgn_won", 0),
            "binance_won": s.get("binance_won", 0),
            "binance_meta_total": s.get("binance_meta_total", 0),
            "binance_meta_success": s.get("binance_meta_success", 0),
            "binance_meta_fail": s.get("binance_meta_fail", 0),
            "binance_meta_won": s.get("binance_meta_won", 0),
        },
    }

def _write_stats_file():
    """把统计快照写入 JSON 文件（覆盖式，便于查看最新状态）。"""
    try:
        snapshot = _snapshot_stats()
        # 先写临时文件再 rename，避免写到一半被读到坏 JSON
        tmp_path = STATS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATS_FILE)
    except Exception as e:
        logger.warning("⚠️ 写 stats 文件失败: %s", e)

async def stats_writer_loop():
    """后台任务：每 STATS_WRITE_INTERVAL 秒把统计写入文件。"""
    while True:
        try:
            await asyncio.sleep(STATS_WRITE_INTERVAL)
            # 在默认线程池执行文件 IO（不阻塞事件循环）
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _write_stats_file)
        except asyncio.CancelledError:
            # 退出前最后写一次
            try:
                _write_stats_file()
            except Exception:
                pass
            break
        except Exception as e:
            logger.error("❌ Stats writer loop error: %s", e)

# ============================================================
# 状态清理 TTL：防止 token_map / token_first_seen / twitter_query_done /
# removed_low_mc_tokens / tweet_tokens / token_tweets 等结构无界增长
# ============================================================
# token_map 等核心结构的清理周期：超过该时长未被任何 tweet 卡片引用且无更新
# 的 token，可视为冷数据，从所有相关结构中移除
STALE_TOKEN_TTL = 3600.0       # 1 小时：未被更新且未被任何卡片引用的 token 视为冷数据
STALE_TOKEN_CLEANUP_INTERVAL = 300.0  # 每 5 分钟执行一次冷数据清理（而非每秒扫描）
# 全量刷新间隔：定期对所有已建卡 tweet_id 重新构建 token 列表并推送
# 确保卡片 token 列表与当前过滤条件一致（兜底机制，防止漏网 token 残留）
FULL_REFRESH_INTERVAL = 120.0  # 每 2 分钟全量刷新一次
# removed_low_mc_tokens 的观察期：被移除的 token 在此期间市值恢复则重新显示
REMOVED_OBSERVATION_TTL = 600.0   # 10 分钟观察期
REMOVED_RECOVERY_MC = 10000.0     # 观察期内市值恢复到 10k 则重新显示
# 超过观察期后从 removed_low_mc_tokens 中移除（不再观察）
REMOVED_TOKEN_TTL = 86400.0       # 24 小时后彻底清理
# twitter_query_done 的清理 TTL：已查询过 Twitter 的 token 记录保留一段时间避免重复查询
TWITTER_QUERY_DONE_TTL = 86400.0  # 24 小时

# 过滤规则：
# - OLD Token（不在 pending 中）marketCap < OLD_TOKEN_MIN_MARKETCAP 时不在卡片中显示
# - NEW Token 创建后 NEW_TOKEN_GRACE_SECONDS 秒，若 marketCap < NEW_TOKEN_MIN_MARKETCAP 则自动移除
# - grace period 内（30s）：marketCap < 4k 移除
# - grace period 后（30s-60s）：marketCap < 5k 移除
# - OLD Token（60s 后）：marketCap < 6k 不显示
OLD_TOKEN_MIN_MARKETCAP = 6000.0      # 6k：OLD Token 显示阈值
NEW_TOKEN_MIN_MARKETCAP = 4000.0      # 4k：grace period 内（30s）移除阈值
NEW_TOKEN_GRACE_SECONDS = 30.0        # 30 秒 grace period

# Token 更新推送：零延迟模式（in-flight 合并，无固定节流间隔）
# 旧值 TOKEN_PUSH_INTERVAL=1.2s 已废弃，改为 push_token_update 内部的 in-flight 合并机制：
#   - 首次推送立即执行
#   - 推送进行中标记 pending，完成后立即再推一次（取最新数据）
#   - 同一 tweet_id 永远只有一个推送在飞行中
TOKEN_PUSH_INTERVAL = 0.0  # 已废弃，保留仅为日志/兼容引用，实际推送无延迟

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("pusher_v4")

# ============================================================
# SQLite
# ============================================================

db_lock = threading.Lock()

# ============================================================
# Thread-local SQLite 连接复用：避免每次操作都 connect+close
# 同一线程多次访问 SQLite 时复用同一连接，省去 connection 建立开销
# 每个连接在第一次使用时自动设置 WAL/busy_timeout/synchronous PRAGMA
# ============================================================
_db_thread_local = threading.local()

def _get_db_conn() -> sqlite3.Connection:
    """获取当前线程的 SQLite 连接（复用，不重复建立）。
    通过 check_same_thread=False 兼容 eventlet 协程切换；
    通过 WAL 模式实现读写并发。
    """
    conn = getattr(_db_thread_local, "conn", None)
    if conn is not None:
        return conn
    # 建立新连接（每线程一个）
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        # autocommit 模式（isolation_level=None）：每次 execute 即时提交，简化事务管理
    except Exception as e:
        logger.warning("⚠️ SQLite PRAGMA 设置失败（不影响功能）: %s", e)
    _db_thread_local.conn = conn
    return conn

def init_db():
    with db_lock:
        conn = _get_db_conn()
        try:
            # 表创建（IF NOT EXISTS 保证幂等）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tweets (
                    tweet_id TEXT PRIMARY KEY,
                    raw_data TEXT NOT NULL,
                    source TEXT DEFAULT 'unknown',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 兼容已有 DB：如果 tweets 表已存在但缺 source 列，自动添加
            try:
                cur = conn.execute("PRAGMA table_info(tweets)")
                columns = [row[1] for row in cur.fetchall()]
                if 'source' not in columns:
                    conn.execute("ALTER TABLE tweets ADD COLUMN source TEXT DEFAULT 'unknown'")
                    logger.info("✅ tweets 表已添加 source 列")
            except Exception as e:
                logger.debug("source 列检查/添加失败（可能已存在）: %s", e)
            # 翻译表：缓存翻译数据，卡片创建时加载并合并
            conn.execute("""
                CREATE TABLE IF NOT EXISTS translations (
                    tweet_id TEXT PRIMARY KEY,
                    translation_data TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # tweet_meta 表：持久化每个 tweet_id 的元数据（如 trigger_count）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tweet_meta (
                    tweet_id TEXT PRIMARY KEY,
                    trigger_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 启动时加载 trigger_count 到内存（避免重启丢失统计）
            _load_trigger_count_from_db(conn)
        except Exception as e:
            logger.warning("⚠️ SQLite 表初始化失败: %s", e)
    logger.info("✅ SQLite 数据库初始化完成: %s (WAL mode, thread-local conn)", DB_PATH)


def _load_trigger_count_from_db(conn):
    """从 tweet_meta 表加载所有 trigger_count 到内存 dict。
    在 init_db 内调用，仅启动时执行一次。
    """
    global tweet_trigger_count
    try:
        cur = conn.execute("SELECT tweet_id, trigger_count FROM tweet_meta")
        loaded = 0
        for row in cur.fetchall():
            tid, count = row[0], row[1]
            if tid and count and count > 0:
                tweet_trigger_count[tid] = count
                loaded += 1
        if loaded:
            logger.info("📊 从 DB 加载 %d 条 trigger_count 记录", loaded)
    except Exception as e:
        logger.warning("⚠️ 加载 trigger_count 失败: %s", e)

def get_tweet_from_db(tweet_id: str) -> Optional[dict]:
    """返回 {"data": {...}, "_source": "api"|"social"|"unknown"} 或 None。"""
    if not tweet_id:
        return None
    with db_lock:
        conn = _get_db_conn()
        try:
            cur = conn.execute("SELECT raw_data, source FROM tweets WHERE tweet_id = ?", (str(tweet_id),))
            row = cur.fetchone()
            if not row:
                return None
            try:
                data = json.loads(row[0])
                # 把 source 附在返回值里，供统计使用
                if isinstance(data, dict):
                    data["_source"] = row[1] or "unknown"
                return data
            except Exception:
                logger.exception("SQLite JSON 解析失败: %s", tweet_id)
                return None
        except Exception as e:
            logger.warning("⚠️ SQLite 读取失败 (tweet_id=%s): %s", tweet_id, e)
            return None

def save_tweet_to_db(tweet_id: str, raw_json: dict, source: str = "binance"):
    """保存推文到 DB。
    source: 'binance' | 'twitterapi' | 'social' | 'unknown'
    用 INSERT OR REPLACE：API 数据是权威的，总是覆盖（含丰富的互动数、翻译等）。
    """
    if not tweet_id:
        return
    with db_lock:
        conn = _get_db_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO tweets (tweet_id, raw_data, source, created_at, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (str(tweet_id), json.dumps(raw_json, ensure_ascii=False), source))
        except Exception as e:
            logger.warning("⚠️ SQLite 写入失败 (tweet_id=%s): %s", tweet_id, e)

# ============================================================
# Translation DB (缓存翻译数据，卡片创建时合并)
# ============================================================

def save_translation_to_db(tweet_id: str, translation_data: dict):
    """保存翻译数据到 DB（合并已存在的字段）"""
    if not tweet_id or not translation_data:
        return
    tweet_id = str(tweet_id)
    # 先读取已存在的翻译数据，合并后写回（避免覆盖之前已缓存的字段）
    existing = get_translation_from_db(tweet_id) or {}
    merged = {**existing, **translation_data}
    with db_lock:
        conn = _get_db_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO translations (tweet_id, translation_data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (tweet_id, json.dumps(merged, ensure_ascii=False)))
        except Exception as e:
            logger.warning("⚠️ SQLite 翻译写入失败 (tweet_id=%s): %s", tweet_id, e)

def get_translation_from_db(tweet_id: str) -> Optional[dict]:
    """从 DB 读取缓存的翻译数据"""
    if not tweet_id:
        return None
    with db_lock:
        conn = _get_db_conn()
        try:
            cur = conn.execute("SELECT translation_data FROM translations WHERE tweet_id = ?", (str(tweet_id),))
            row = cur.fetchone()
            if not row:
                return None
            try:
                return json.loads(row[0])
            except Exception:
                logger.exception("SQLite 翻译 JSON 解析失败: %s", tweet_id)
                return None
        except Exception as e:
            logger.warning("⚠️ SQLite 翻译读取失败 (tweet_id=%s): %s", tweet_id, e)
            return None

# ============================================================
# trigger_count 持久化：内存递增 + 定时批量 UPSERT 到 tweet_meta 表
# ============================================================
# 设计：高频递增时不每条写 DB，用 dirty set 标记变更，后台每 5s 批量 flush
_trigger_dirty: Set[str] = set()
_trigger_dirty_lock = threading.Lock()
_TRIGGER_FLUSH_INTERVAL = 5.0  # 每 5 秒 flush 一次

def _mark_trigger_dirty(tweet_id: str):
    """标记某 tweet_id 的 trigger_count 已变更，待 flush。"""
    with _trigger_dirty_lock:
        _trigger_dirty.add(tweet_id)

def _flush_trigger_count_sync():
    """[同步函数，在线程池执行] 把 dirty 的 trigger_count 批量 UPSERT 到 tweet_meta 表。"""
    with _trigger_dirty_lock:
        if not _trigger_dirty:
            return 0
        dirty = list(_trigger_dirty)
        _trigger_dirty.clear()
    # 快照当前值（值可能在 flush 期间又变了，但下次 flush 会再写，最终一致）
    items = []
    for tid in dirty:
        count = tweet_trigger_count.get(tid, 0)
        if count > 0:
            items.append((count, tid))
    if not items:
        return 0
    written = 0
    with db_lock:
        conn = _get_db_conn()
        try:
            for count, tid in items:
                conn.execute(
                    "INSERT OR REPLACE INTO tweet_meta (tweet_id, trigger_count, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (tid, count)
                )
                written += 1
        except Exception as e:
            logger.warning("⚠️ trigger_count flush 失败: %s", e)
    return written

async def trigger_count_flush_loop():
    """后台任务：每 _TRIGGER_FLUSH_INTERVAL 秒把 dirty 的 trigger_count 写入 DB。"""
    loop = asyncio.get_event_loop()
    while True:
        try:
            await asyncio.sleep(_TRIGGER_FLUSH_INTERVAL)
            written = await loop.run_in_executor(_social_executor, _flush_trigger_count_sync)
            if written:
                logger.info("📊 trigger_count 持久化 %d 条", written)
        except asyncio.CancelledError:
            # 退出前最后 flush 一次
            try:
                _flush_trigger_count_sync()
            except Exception:
                pass
            break
        except Exception as e:
            logger.error("❌ trigger_count flush loop error: %s", e)

# ============================================================
# 迁移 Token 列表推送：定时把排序后的迁移 token 列表推送到前端
# ============================================================
async def migrated_tokens_push_loop():
    """后台任务：每 MIGRATED_PUSH_INTERVAL 秒推送迁移 token 列表到前端。"""
    while True:
        try:
            await asyncio.sleep(MIGRATED_PUSH_INTERVAL)
            tokens_list = get_migrated_tokens_sorted()
            hook = _hooks.get("on_migrated_tokens")
            if hook is not None:
                try:
                    hook(tokens_list)
                except Exception as e:
                    logger.error("❌ 迁移 token hook 推送异常: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("❌ migrated_tokens_push_loop error: %s", e)

# ============================================================
# 全局 Token 状态
# ============================================================

DATA_LOCK = threading.RLock()
token_map: Dict[str, dict] = {}
pending_new_tokens: Dict[str, dict] = {}
twitter_query_done: Dict[str, str] = {}
tweet_tokens: Dict[str, Set[str]] = defaultdict(set)      # tweet_id -> set of contract
token_tweets: Dict[str, Set[str]] = defaultdict(set)      # contract -> set of tweet_id (反向映射)
pushed_tweet_ids: Set[str] = set()
pending_push_tasks: Dict[str, asyncio.Task] = {}
placeholder_sent: Set[str] = set()
# 嵌套推文 tweet_id -> 主推文 tweet_id 的映射
# 当 OLD Token 的 tweet_id 匹配到某个嵌套推文时，通过这个映射找到主推文，
# 把 token 也加到主推文的卡片里
nested_to_parent: Dict[str, Set[str]] = defaultdict(set)
# 每个 tweet_id 触发建卡的次数（统计用，前端显示"第 N 次推送"）
# 每次 schedule_tweet_push 真正进入推送流程时递增（已在队列的不算）
tweet_trigger_count: Dict[str, int] = defaultdict(int)
# 记录每个 token 首次发现的时间（用于判断是否在 grace period 内）
token_first_seen: Dict[str, float] = {}
# 标记已被自动移除的 token（marketCap 过低且超过 grace period），避免重复处理
# value 是被移除的 timestamp，用于 TTL 清理（避免 removed_low_mc_tokens 无界增长）
removed_low_mc_tokens: Dict[str, float] = {}

# ============================================================
# 迁移 Token 列表：从 UPDATE 流提取已迁移 + 市值>20k + 24h 内创建的 token
# 在 header 横向滚动展示，按创建时间排序（最早的在最前）
# ============================================================
MIGRATED_MIN_MARKETCAP = 20000.0    # 市值下限 20k
MIGRATED_MAX_AGE_HOURS = 24.0       # 创建时间上限 24h
MIGRATED_PUSH_INTERVAL = 5.0        # 每 5 秒推送一次迁移 token 列表到前端
MIGRATED_MAX_DISPLAY = 8            # 最多展示 8 个（按迁移时间最近优先）

# 迁移 token 缓存：contract -> snapshot dict（只保留展示所需字段）
migrated_tokens: Dict[str, dict] = {}

last_update_time = 0.0
last_new_token_time = 0.0
last_ws_message_time = 0.0
ws_connected = False
ws_connection_count = 0

# ============================================================
# 进程内推送 hooks：当 pusher 与 server 同进程时，跳过环回 HTTP，直接调用
# server 的 tweet_manager + socketio.emit，省去 JSON 序列化+TCP+反序列化的几～几十 ms
# ============================================================
# hooks 接口（server.py 注册时实现）：
#   on_new_message(message: dict) -> None          新增卡片
#   on_update_message(message: dict) -> None       更新完整卡片（含 tokens）
#   on_token_update(tweet_id: str, tokens: list) -> None  仅更新 token 列表
#   on_migrated_tokens(tokens: list) -> None       迁移 token 列表更新
_hooks = {
    "on_new_message": None,
    "on_update_message": None,
    "on_token_update": None,
    "on_migrated_tokens": None,
}

def register_hooks(on_new_message=None, on_update_message=None, on_token_update=None, on_migrated_tokens=None):
    """注册进程内推送 hooks。
    server.py 在启动时调用此函数注册直接回调，避免环回 HTTP。
    任何一个 hook 为 None 表示该项仍走 HTTP fallback。
    """
    if on_new_message is not None:
        _hooks["on_new_message"] = on_new_message
    if on_update_message is not None:
        _hooks["on_update_message"] = on_update_message
    if on_token_update is not None:
        _hooks["on_token_update"] = on_token_update
    if on_migrated_tokens is not None:
        _hooks["on_migrated_tokens"] = on_migrated_tokens
    logger.info("🔗 进程内推送 hooks 已注册: new=%s update=%s token_update=%s migrated=%s",
                _hooks["on_new_message"] is not None,
                _hooks["on_update_message"] is not None,
                _hooks["on_token_update"] is not None,
                _hooks["on_migrated_tokens"] is not None)

# ============================================================
# Basic
# ============================================================

def safe_str(value) -> str:
    return "" if value is None else str(value)

def normalize_contract(contract: str) -> str:
    return safe_str(contract).strip().lower()

def safe_dict_get(d, key, default=None):
    return d.get(key, default) if isinstance(d, dict) else default

def is_empty_patch_value(value) -> bool:
    # 注意：None 和 0 不再视为"空"——它们是合法的 API 值（None=当前不可用，0=零值）
    # 只有空字符串和空 dict 才跳过（避免覆盖已有数据）
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, dict) and len(value) == 0:
        return True
    return False

def deep_merge_patch(existing, patch):
    """
    深度合并 patch 到 existing，返回 existing（已就地修改）。
    - 空字符串/None/空字典/空列表的 patch 值会被跳过，不覆盖既有值
    - 字典类型的 patch 值会递归合并
    - 其他类型的 patch 值直接覆盖
    返回值：existing（就地修改后的同一个对象引用）
    """
    if not isinstance(patch, dict):
        return existing
    if not isinstance(existing, dict):
        existing = {}
    for key, value in patch.items():
        if is_empty_patch_value(value):
            continue
        if isinstance(value, dict):
            existing[key] = deep_merge_patch(existing.get(key, {}), value)
        else:
            existing[key] = value
    return existing

def deep_merge_patch_changed(existing, patch):
    """
    与 deep_merge_patch 相同的合并语义，但额外返回 bool 表示是否有字段真的被修改。
    避免 update_token_map 用 json.dumps 做整份序列化对比（CPU 浪费，且阻塞事件循环）。
    返回 (existing, changed)。
    """
    if not isinstance(patch, dict):
        return existing, False
    if not isinstance(existing, dict):
        existing = {}
        changed = True  # 从非字典变成空字典也算变化
    else:
        changed = False
    for key, value in patch.items():
        if is_empty_patch_value(value):
            continue
        if isinstance(value, dict):
            sub_existing = existing.get(key, {})
            new_sub, sub_changed = deep_merge_patch_changed(sub_existing, value)
            if sub_changed:
                existing[key] = new_sub
                changed = True
        else:
            if existing.get(key) != value:
                existing[key] = value
                changed = True
    return existing, changed

def parse_timestamp(value) -> float:
    if value is None:
        return 0.0
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
        elif isinstance(value, str):
            value = value.strip()
            if not value:
                return 0.0
            try:
                ts = float(value)
            except ValueError:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return dt.timestamp()
                except Exception:
                    return 0.0
        else:
            return 0.0
        if ts > 10_000_000_000:
            ts /= 1000.0
        return ts
    except Exception:
        return 0.0

def get_token_create_time(token: dict) -> float:
    if not isinstance(token, dict):
        return 0.0
    for key in ("createTime", "createdAt", "created_at"):
        value = token.get(key)
        ts = parse_timestamp(value)
        if ts > 0:
            return ts
    local_first_seen = token.get("_first_seen")
    if local_first_seen:
        try:
            return float(local_first_seen)
        except Exception:
            pass
    return 0.0

def format_age(seconds: float) -> str:
    now = time.time()
    if seconds <= 0 or seconds > now:
        return "—"
    delta = now - seconds
    if delta < 0:
        return "—"
    if delta < 60:
        return str(int(delta)) + "s"
    if delta < 3600:
        return str(int(delta // 60)) + "m"
    if delta < 86400:
        return str(int(delta // 3600)) + "h"
    return str(int(delta // 86400)) + "d"

# ============================================================
# Twitter ID / Handle 提取
# ============================================================

def extract_tweet_id(socials: dict) -> Optional[str]:
    if not isinstance(socials, dict):
        return None
    urls = []
    for key in ("twitter", "x", "twitterUrl", "twitter_url"):
        value = socials.get(key)
        if not value:
            continue
        if isinstance(value, list):
            urls.extend(value)
        else:
            urls.append(value)
    for value in urls:
        if not value:
            continue
        url = safe_str(value).strip()
        # 标准 tweet 链接：x.com/handle/status/123456
        match = re.search(r"(?:x\.com|twitter\.com)/[^/?#]+/(?:status|i/status)/(\d+)", url, re.IGNORECASE)
        if match:
            return match.group(1)
        # x.com/数字 格式：纯数字路径段是 tweet_id（如 x.com/2094489653625651615）
        match = re.search(r"(?:x\.com|twitter\.com)/(\d{15,})(?:[/?#]|$)", url, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"(?:status|i/status)/(\d+)", url, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def extract_tweet_id_from_token(token: dict) -> Optional[str]:
    if not isinstance(token, dict):
        return None
    socials = token.get("socials")
    tweet_id = extract_tweet_id(socials)
    if tweet_id:
        return tweet_id
    for key in ("twitterUrl", "twitter_url", "tweetUrl", "tweet_url"):
        value = token.get(key)
        if not value:
            continue
        tweet_id = extract_tweet_id({"twitter": value})
        if tweet_id:
            return tweet_id
    return None

def extract_twitter_handle(socials: dict) -> Optional[str]:
    """从 socials 中提取 Twitter Handle（个人主页链接）"""
    if not isinstance(socials, dict):
        return None
    urls = []
    for key in ("twitter", "x", "twitterUrl", "twitter_url"):
        value = socials.get(key)
        if not value:
            continue
        if isinstance(value, list):
            urls.extend(value)
        else:
            urls.append(value)
    for value in urls:
        if not value:
            continue
        url = safe_str(value).strip()
        # 检查是否是 broadcast 链接：x.com/i/broadcasts/xxx
        bc_match = re.search(r"(?:x\.com|twitter\.com)/i/broadcasts/([^/?#]+)", url, re.IGNORECASE)
        if bc_match:
            bc_id = bc_match.group(1)
            if bc_id:
                return f"broadcast:{bc_id}"
        match = re.search(r"(?:x\.com|twitter\.com)/([^/?]+)", url, re.IGNORECASE)
        if match:
            handle = match.group(1)
            if handle and handle not in ('status', 'i', 'intent', 'share', 'home', 'explore', 'notifications', 'messages', 'settings', 'broadcasts'):
                # 纯数字的不是 handle，是 twitter user id（如 x.com/2094490132657103214）
                if handle.isdigit():
                    continue
                return handle
    return None

def extract_binance_square_id(socials: dict) -> Optional[str]:
    """从 socials 中提取币安广场 post/article ID。
    匹配同时包含 binance.com 和 square 的链接。
    返回 square_id 或 None。
    """
    if not isinstance(socials, dict):
        return None
    urls = []
    for key in ("twitter", "x", "twitterUrl", "twitter_url", "website", "binance"):
        value = socials.get(key)
        if not value:
            continue
        if isinstance(value, list):
            urls.extend(value)
        else:
            urls.append(value)
    for value in urls:
        if not value:
            continue
        url = safe_str(value).strip()
        # 匹配 binance.com 且包含 square 的链接
        if "binance.com" in url.lower() and "square" in url.lower():
            # post 链接：binance.com/zh-CN/square/post/123456
            match = re.search(r"/square/post/(\d+)", url, re.IGNORECASE)
            if match:
                return f"square:{match.group(1)}"
            # article 链接：binance.com/zh-CN/square/post/slug-123456
            match = re.search(r"/square/post/[^/]*-(\d+)", url, re.IGNORECASE)
            if match:
                return f"square:{match.group(1)}"
            # article 链接：binance.com/zh-CN/square/article/xxx
            match = re.search(r"/square/(?:article|post)/([^/?#]+)", url, re.IGNORECASE)
            if match:
                return f"square:{match.group(1)}"
    return None

def extract_twitter_handle_from_token(token: dict) -> Optional[str]:
    if not isinstance(token, dict):
        return None
    socials = token.get("socials")
    handle = extract_twitter_handle(socials)
    if handle:
        return handle
    for key in ("twitterHandle", "handle"):
        value = token.get(key)
        if value:
            h = safe_str(value).strip()
            if h:
                return h
    return None

def extract_binance_square_from_token(token: dict) -> Optional[str]:
    """从 token 数据中提取币安广场 ID。
    优先从 socials 提取，其次从 website/binanceUrl 等字段提取。
    返回 "square:xxx" 或 None。
    """
    if not isinstance(token, dict):
        return None
    socials = token.get("socials")
    square_id = extract_binance_square_id(socials)
    if square_id:
        return square_id
    for key in ("website", "binanceUrl", "binance_url"):
        value = token.get(key)
        if not value:
            continue
        square_id = extract_binance_square_id({"website": value})
        if square_id:
            return square_id
    return None

def extract_weibo_id(socials: dict) -> Optional[str]:
    """从 socials 中提取微博帖子 ID。
    匹配 weibo.com/detail/数字 格式的链接。
    返回 "weibo:xxx" 或 None。
    """
    if not isinstance(socials, dict):
        return None
    urls = []
    for key in ("twitter", "x", "twitterUrl", "twitter_url", "website", "weibo", "weiboUrl"):
        value = socials.get(key)
        if not value:
            continue
        if isinstance(value, list):
            urls.extend(value)
        else:
            urls.append(value)
    for value in urls:
        if not value:
            continue
        url = safe_str(value).strip()
        # weibo.com/detail/123456
        match = re.search(r"weibo\.com/detail/(\d+)", url, re.IGNORECASE)
        if match:
            return f"weibo:{match.group(1)}"
        # weibo.com/1234567890/Profile
        match = re.search(r"weibo\.com/(\d+)(?:/|$|\?)", url, re.IGNORECASE)
        if match and len(match.group(1)) >= 10:
            return f"weibo:{match.group(1)}"
    return None

def extract_weibo_from_token(token: dict) -> Optional[str]:
    """从 token 数据中提取微博帖子 ID。"""
    if not isinstance(token, dict):
        return None
    socials = token.get("socials")
    weibo_id = extract_weibo_id(socials)
    if weibo_id:
        return weibo_id
    for key in ("website", "weiboUrl", "weibo_url"):
        value = token.get(key)
        if not value:
            continue
        weibo_id = extract_weibo_id({"website": value})
        if weibo_id:
            return weibo_id
    return None

# ============================================================
# Twitter Author & Nested
# ============================================================

def normalize_author(author) -> dict:
    if not author or not isinstance(author, dict):
        return {
            "name": "Unknown",
            "handle": "unknown",
            "profileImgUrl": "",
            "profileBannerUrl": "",
            "isBlueVerified": 0,
            "description": "",
            "location": "",
            "twitterId": "",
            "followersCnt": 0,
            "followingCnt": 0
        }
    return {
        "name": author.get("name", "Unknown"),
        "handle": author.get("handle", "unknown"),
        "profileImgUrl": author.get("profileImgUrl", ""),
        "profileBannerUrl": author.get("profileBannerUrl", ""),
        "isBlueVerified": author.get("isBlueVerified", 0),
        "description": author.get("description", ""),
        "location": author.get("location", ""),
        "twitterId": author.get("twitterId", ""),
        "followersCnt": author.get("followersCnt", 0),
        "followingCnt": author.get("followingCnt", 0)
    }

def normalize_nested(data) -> Optional[dict]:
    if not data or not isinstance(data, dict):
        return None
    author = normalize_author(data.get("author", {}))
    created_at = data.get("createdAt")
    timestamp = parse_timestamp(created_at)
    if timestamp > 0:
        timestamp = int(timestamp * 1000)
    else:
        timestamp = int(time.time() * 1000)
    # 调试：打印嵌套推文的字段
    text_trans = data.get("textTranslation", "")
    if text_trans:
        logger.info("🌐 [DEBUG] 嵌套推文有 textTranslation: tweet_id=%s, trans_len=%d", data.get("tweetId") or data.get("tweet_id"), len(text_trans))
    else:
        # 打印嵌套推文的所有 keys 便于排查
        nested_keys = list(data.keys())
        logger.info("🌐 [DEBUG] 嵌套推文无 textTranslation: tweet_id=%s, keys=%s", data.get("tweetId") or data.get("tweet_id"), nested_keys)
    video_urls = data.get("videoUrls") or []
    if not isinstance(video_urls, list):
        video_urls = [video_urls]
    tweet_id = data.get("tweetId") or data.get("tweet_id") or data.get("id") or ""
    text = data.get("text") or data.get("content") or ""
    tweet_type = data.get("tweetType") or data.get("tweet_type", "original")
    img_urls = data.get("imgUrls") or data.get("img_urls") or []
    if not isinstance(img_urls, list):
        img_urls = [img_urls]
    quoted_tweet_data = data.get("quotedTweet")
    replied_tweet_data = data.get("repliedToTweet")
    retweeted_tweet_data = data.get("retweetedTweet")
    quoted_tweet = normalize_nested(quoted_tweet_data) if quoted_tweet_data else None
    replied_to_tweet = normalize_nested(replied_tweet_data) if replied_tweet_data else None
    retweeted_tweet = normalize_nested(retweeted_tweet_data) if retweeted_tweet_data else None
    return {
        "tweetId": tweet_id,
        "tweet_id": tweet_id,
        "id": tweet_id,
        "text": text,
        "content": text,
        "textTranslation": data.get("textTranslation", ""),
        "lang": data.get("lang", "en"),
        "tweetType": tweet_type,
        "tweet_type": tweet_type,
        "createdAt": timestamp,
        "timestamp": timestamp,
        "author": author,
        "name": author["name"],
        "handle": author["handle"],
        "avatar": author["profileImgUrl"],
        "profileImgUrl": author["profileImgUrl"],
        "profileBannerUrl": author.get("profileBannerUrl", ""),
        "isBlueVerified": author["isBlueVerified"],
        "likeCnt": data.get("likeCnt", data.get("likes", 0)),
        "retweetCnt": data.get("retweetCnt", data.get("retweets", 0)),
        "replyCnt": data.get("replyCnt", data.get("replies", 0)),
        "quoteCnt": data.get("quoteCnt", 0),
        "imgUrls": img_urls,
        "videoUrls": video_urls,
        "article": data.get("article"),
        "quotedTweet": quoted_tweet,
        "repliedToTweet": replied_to_tweet,
        "retweetedTweet": retweeted_tweet
    }

def parse_tweet(data, is_quoted=False) -> Optional[dict]:
    try:
        nested = normalize_nested(data)
        if not nested:
            return None
        nested["is_quoted"] = is_quoted
        nested["has_article"] = bool(nested.get("article"))
        nested["has_video"] = bool(nested.get("videoUrls"))
        return nested
    except Exception as e:
        logger.error("解析推文失败: %s", e)
        return None


# ============================================================
# TwitterAPI.io 适配器：把 TwitterAPI.io 的返回结构转换成 Binance 格式
# 这样后续 parse_tweet / normalize_nested 等逻辑可以无感知地使用
# ============================================================

# TwitterAPI.io createdAt 格式："Sat Aug 29 11:22:25 +0000 2026"
_TWITTERAPI_TIME_FMT = "%a %b %d %H:%M:%S %z %Y"

def _parse_twitterapi_created_at(s: str) -> int:
    """解析 TwitterAPI.io 的 createdAt 字符串为毫秒时间戳，失败返回当前时间。"""
    if not s or not isinstance(s, str):
        return int(time.time() * 1000)
    try:
        dt = time.strptime(s, _TWITTERAPI_TIME_FMT)
        # strptime 不带时区信息，但 TwitterAPI.io 默认 UTC
        return int(time.mktime(dt) * 1000)
    except Exception:
        try:
            # 兜底：尝试 ISO 格式
            from datetime import datetime
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return int(time.time() * 1000)

def _convert_twitterapi_tweet(t: dict) -> Optional[dict]:
    """把 TwitterAPI.io 单条 tweet 转换成 Binance 格式（recursive 处理 quoted/retweeted）。"""
    if not t or not isinstance(t, dict) or not t.get("id"):
        return None
    # 推断 tweetType
    if t.get("isRetweet"):
        tweet_type = "retweeted"
    elif t.get("isReply"):
        tweet_type = "replied_to"
    elif t.get("isQuote"):
        tweet_type = "quoted"
    else:
        tweet_type = "original"
    # author
    a = t.get("author") or {}
    author = {
        "name": a.get("name") or "",
        "handle": a.get("userName") or "",
        "profileImgUrl": a.get("profilePicture") or "",
        "profileBannerUrl": a.get("coverPicture") or "",
        "isBlueVerified": 1 if a.get("isBlueVerified") else 0,
        "description": a.get("description") or "",
        "location": a.get("location") or "",
        "twitterId": a.get("id") or "",
        "followersCnt": a.get("followers") or 0,
        "followingCnt": a.get("following") or 0,
    }
    # imgUrls: 从 extendedEntities.media 提取 photo
    img_urls = []
    media = (t.get("extendedEntities") or {}).get("media") or []
    for m in media:
        if isinstance(m, dict) and m.get("type") == "photo":
            url = m.get("media_url_https") or m.get("url")
            if url:
                img_urls.append(url)
    # videoUrls: 从 extendedEntities.media 提取 video
    video_urls = []
    for m in media:
        if isinstance(m, dict) and m.get("type") in ("video", "animated_gif"):
            variants = m.get("videoInfo", {}).get("variants") or []
            # 选最高 bitrate 的 mp4
            mp4s = sorted(
                [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")],
                key=lambda v: v.get("bitrate", 0),
                reverse=True,
            )
            if mp4s:
                video_urls.append({
                    "videoPreviewUrl": m.get("media_url_https") or m.get("url") or "",
                    "variants": [{"contentType": v["content_type"], "bitrate": v.get("bitrate", 0), "url": v["url"]} for v in variants]
                })
                break  # 只取第一个视频
    # 递归转换 quoted_tweet / retweeted_tweet
    quoted_tweet = _convert_twitterapi_tweet(t.get("quoted_tweet")) if t.get("quoted_tweet") else None
    retweeted_tweet = _convert_twitterapi_tweet(t.get("retweeted_tweet")) if t.get("retweeted_tweet") else None
    # 注意：TwitterAPI.io 不直接返回 repliedToTweet，需要通过 inReplyToId 单独查询
    # 这里先不处理 repliedToTweet（保持 None），如需可在 hedged 层补一次查询
    return {
        "tweetId": t["id"],
        "text": t.get("text") or "",
        "textTranslation": "",  # TwitterAPI.io 无翻译
        "lang": t.get("lang") or "en",
        "tweetType": tweet_type,
        "createdAt": _parse_twitterapi_created_at(t.get("createdAt") or ""),
        "author": author,
        "likeCnt": t.get("likeCount") or 0,
        "retweetCnt": t.get("retweetCount") or 0,
        "replyCnt": t.get("replyCount") or 0,
        "quoteCnt": t.get("quoteCount") or 0,
        "imgUrls": img_urls,
        "videoUrls": video_urls,
        "article": None,  # TwitterAPI.io 不支持文章
        "quotedTweet": quoted_tweet,
        "repliedToTweet": None,
        "retweetedTweet": retweeted_tweet,
    }

def _convert_twitterapi_response(api_resp: dict) -> Optional[dict]:
    """把 TwitterAPI.io 顶层响应转换为 Binance 格式（带 data 包装层）。
    返回结构：{"data": {...binance tweet...}}
    """
    if not api_resp or not isinstance(api_resp, dict):
        return None
    tweets = api_resp.get("tweets") or []
    if not tweets:
        return None
    converted = _convert_twitterapi_tweet(tweets[0])
    if not converted:
        return None
    return {"data": converted}


# ============================================================
# TwitterAPI.io 调用 + hedged request
# ============================================================

# 已尝试过 TwitterAPI.io 的 tweet_id 集合：每 tweet_id 只调用一次（省钱）
_twitterapi_tried_tweet_ids: Set[str] = set()
_twitterapi_tried_lock = threading.Lock()

def _is_twitterapi_already_tried(tweet_id: str) -> bool:
    with _twitterapi_tried_lock:
        return tweet_id in _twitterapi_tried_tweet_ids

def _mark_twitterapi_tried(tweet_id: str):
    with _twitterapi_tried_lock:
        _twitterapi_tried_tweet_ids.add(tweet_id)

def get_tweet_data_from_twitterapi(tweet_id: str) -> Optional[dict]:
    """调用 TwitterAPI.io 获取 tweet（返回 Binance 格式，已转换字段）。
    - 未设置 API Key → 直接返回 None
    - 已对该 tweet_id 尝试过 → 直接返回 None（避免重复扣费）
    """
    if not TWITTERAPI_IO_KEY:
        return None
    if _is_twitterapi_already_tried(tweet_id):
        logger.debug("⏭️ TwitterAPI.io 已尝试过 tweet_id=%s，跳过", tweet_id)
        return None
    _mark_twitterapi_tried(tweet_id)

    url = "https://api.twitterapi.io/twitter/tweets"
    params = {"tweet_ids": str(tweet_id)}
    headers = {"X-API-Key": TWITTERAPI_IO_KEY}
    start_ts = time.time()
    stats_inc("twitterapi_total")
    try:
        resp = get_api_session().get(url, params=params, headers=headers, timeout=TWITTERAPI_IO_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("tweets"):
                converted = _convert_twitterapi_response(data)
                if converted:
                    logger.info("🔄 TwitterAPI.io 获取成功 tweet_id=%s", tweet_id)
                    stats_inc("twitterapi_success")
                    stats_add_latency("twitterapi_latency_sum", time.time() - start_ts)
                    return converted
            logger.warning("⚠️ TwitterAPI.io 无数据 tweet_id=%s", tweet_id)
            stats_inc("twitterapi_fail")
            return None
        logger.warning("⚠️ TwitterAPI.io 返回 %s tweet_id=%s", resp.status_code, tweet_id)
        stats_inc("twitterapi_fail")
        return None
    except Exception as e:
        logger.warning("⚠️ TwitterAPI.io 请求异常 tweet_id=%s: %s", tweet_id, e)
        stats_inc("twitterapi_fail")
        return None


# ============================================================
# Binance Twitter API (推文) - 带重试
# ============================================================

def _get_tweet_data_binance_single(tweet_id: str, timeout: float) -> Optional[dict]:
    """单次 Binance API 调用（不重试）。返回 Binance 格式 dict 或 None。"""
    headers = {
        "sec-ch-ua-platform": "\"macOS\"",
        "referer": "https://web3.binance.com/zh-CN/trenches?chain=bsc",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "sec-ch-ua": "\"Not=A?Brand\";v=\"99\", \"Google Chrome\";v=\"151\", \"Chromium\";v=\"151\"",
        "content-type": "application/json",
        "sec-ch-ua-mobile": "?0",
        "x-trace-id": str(uuid.uuid4()),
        "x-ui-request-trace": str(uuid.uuid4()),
        "lang": "zh-CN"
    }
    url = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/token/twitter/post/summary"
    params = {"tweetId": str(tweet_id)}
    start_ts = time.time()
    stats_inc("binance_api_total")
    try:
        response = get_api_session().get(url, params=params, headers=headers, timeout=timeout)
        if response.status_code == 200:
            try:
                data = response.json()
                if data and data.get("data"):
                    stats_inc("binance_api_success")
                    stats_add_latency("binance_api_latency_sum", time.time() - start_ts)
                    return data
                logger.warning("⚠️ Binance API 返回缺少 data 字段 | tweet_id=%s", tweet_id)
                stats_inc("binance_api_fail")
            except Exception:
                logger.exception("Binance API JSON 解析失败 | tweet_id=%s", tweet_id)
                stats_inc("binance_api_fail")
        else:
            logger.error("Binance API 返回 %s | tweet_id=%s | body=%s",
                        response.status_code, tweet_id, response.text[:300])
            stats_inc("binance_api_fail")
    except Exception as e:
        logger.error("Binance API 请求异常 | tweet_id=%s: %s", tweet_id, e)
        stats_inc("binance_api_fail")
    return None


def get_tweet_data(tweet_id: str, retries: int = API_RETRY_COUNT) -> Optional[dict]:
    """
    获取推文数据。Hedged request 策略：
    1. 第 1 次 Binance 调用用 BINANCE_HEDGE_TIMEOUT（1s）短超时
    2. 同时（并发）启动 TwitterAPI.io hedged call（如未尝试过且配了 Key）
    3. 哪个先返回有效结果就用哪个（race）
    4. Binance 第 1 次失败 → 按 1.5s→3s 退避重试，仅 Binance 重试（不再 hedged）
    5. TwitterAPI.io 成功后仍会保留 Binance 重试机会（后续 update_message 推翻译）

    返回 Binance 格式 dict 或 None。
    """
    if not tweet_id:
        return None
    tweet_id = str(tweet_id)

    # 是否需要 hedged：TwitterAPI.io 配置可用且该 tweet_id 未尝试过
    use_hedge = bool(TWITTERAPI_IO_KEY) and not _is_twitterapi_already_tried(tweet_id)

    # === Hedged attempt ===
    if use_hedge:
        # 并发发起 Binance(1s) + TwitterAPI.io(5s)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hedged") as ex:
            future_b = ex.submit(_get_tweet_data_binance_single, tweet_id, BINANCE_HEDGE_TIMEOUT)
            future_t = ex.submit(get_tweet_data_from_twitterapi, tweet_id)
            # 等待 Binance 短超时先返回
            try:
                binance_first = future_b.result(timeout=BINANCE_HEDGE_TIMEOUT + 0.5)
            except Exception:
                binance_first = None
            if binance_first:
                # Binance 1s 内返回了，取消/忽略 TwitterAPI.io（但已标记 tried，无法回头）
                # 注意：hedged 已发起，TwitterAPI.io 这一秒内可能已扣费，但避免了等待 Binance 重试
                logger.info("⚡ Binance hedged 抢到 tweet_id=%s", tweet_id)
                stats_inc("hedged_binance_won")
                # 立即保存到 DB 并标记 source=binance（API 数据是权威的，总是覆盖）
                save_tweet_to_db(tweet_id, binance_first, source="binance")
                # 等 TwitterAPI.io future 完成（避免线程泄漏），但其结果丢弃
                try:
                    future_t.result(timeout=0.1)
                except Exception:
                    pass
                return binance_first
            # Binance 1s 没回 → 等 TwitterAPI.io（最多 5s）
            try:
                twitter_result = future_t.result(timeout=TWITTERAPI_IO_TIMEOUT)
            except Exception:
                twitter_result = None
            if twitter_result:
                logger.info("🔄 hedged: TwitterAPI.io 抢到 tweet_id=%s（Binance 慢）", tweet_id)
                stats_inc("hedged_twitterapi_won")
                # 立即保存到 DB 并标记 source=twitterapi
                save_tweet_to_db(tweet_id, twitter_result, source="twitterapi")
                return twitter_result
            # 两个都没回/失败 → Binance 走重试（第 2、3 次，更长超时）
            logger.info("⏳ hedged 双方都未返回，Binance 进入重试阶段 tweet_id=%s", tweet_id)

    # === Binance retry phase (无 hedged 或 hedged 失败后) ===
    delay = API_RETRY_DELAY
    # 如果 hedged 已耗尽了 Binance 第 1 次机会，从这里开始第 2 次
    start_attempt = 2 if use_hedge else 1
    for attempt in range(start_attempt, retries + 1):
        if attempt > 1:
            time.sleep(delay)
            delay *= 2
        result = _get_tweet_data_binance_single(tweet_id, HTTP_TIMEOUT)
        if result:
            logger.info("✅ Binance 第 %d 次重试成功 tweet_id=%s", attempt, tweet_id)
            # 立即保存到 DB 并标记 source=binance
            save_tweet_to_db(tweet_id, result, source="binance")
            return result
        logger.warning("⚠️ Binance 第 %d/%d 次失败 tweet_id=%s", attempt, retries, tweet_id)

    logger.error("❌ 推文 API 重试 %d 次均失败 | tweet_id=%s", retries, tweet_id)
    return None

def get_tweet_cached(tweet_id: str) -> Optional[dict]:
    """优先从 SQLite 读，未命中则调 API。
    返回的 dict 含 _source 字段（'binance'|'twitterapi'|'social'|'unknown'），
    供上层统计区分缓存来源。
    """
    if not tweet_id:
        return None
    cached = get_tweet_from_db(tweet_id)
    if cached:
        source = cached.get("_source", "unknown")
        logger.info("📦 SQLite 命中 tweet_id=%s (source=%s)", tweet_id, source)
        stats_inc("cache_hit")
        if source == "social":
            stats_inc("social_cache_hits")
        elif source in ("binance", "twitterapi"):
            stats_inc("api_cache_hits")
        else:
            stats_inc("unknown_cache_hits")
        return cached
    logger.info("🔄 SQLite 未命中，调用 Twitter API (带重试): %s", tweet_id)
    stats_inc("cache_miss")
    api_response = get_tweet_data(tweet_id)
    # 注意：source 标记由 get_tweet_data 内部在抢到时通过 save_tweet_to_db 完成
    # 这里不重复保存（避免覆盖正确的 source 标记）
    return api_response


# ============================================================
# Social Stream 缓存：订阅 w3w@tracker@social 流，预热 SQLite 缓存
# ============================================================
# 社交动态流实时推送 reply/quote/retweet 事件，包含推文内容、作者、引用推文。
# 转换为 Binance tweet 格式后写入 SQLite（INSERT OR IGNORE，不覆盖 API 的丰富数据）。
# 后续当 NEW Token 关联到这些 tweet_id 时，get_tweet_cached 直接命中本地、跳过 API 调用。
#
# 性能设计：
# - 事件入队（put_nowait，微秒级），不阻塞 WS 事件循环
# - 后台 social_flush_loop 每 1s 批量 flush 到 SQLite（在专用 _social_executor 线程池执行）
# - 队列上限 5000，超出丢弃（社交流是"尽力缓存"，丢几条不影响功能）
# - tweets 表用 INSERT OR IGNORE（主键去重，不覆盖已有丰富数据）
# - translations 表用 INSERT OR REPLACE + 内存 merge（翻译是权威数据，总是更新）
# - tweets 表 24h TTL 清理，避免 DB 文件无限膨胀

_social_write_queue: list = []
_social_queue_lock = threading.Lock()
_SOCIAL_QUEUE_MAXSIZE = 5000        # 调小：超出丢弃，避免积压；社交流是"尽力缓存"
_SOCIAL_FLUSH_INTERVAL = 1.0        # 秒
_SOCIAL_TWEETS_TTL = 86400          # tweets 表保留 24h（超过的清理，避免 DB 膨胀）
_SOCIAL_TWEETS_CLEANUP_INTERVAL = 600  # 每 10 分钟清理一次过期 tweets
# 队列丢弃计数（用于监控）
_social_dropped_count = 0
_social_dropped_lock = threading.Lock()

# eventType → tweetType 映射
_SOCIAL_EVENT_TYPE_MAP = {
    "reply": "replied_to",
    "quote": "quoted",
    "retweet": "retweeted",
    "original": "original",
}

def _parse_social_media_urls(s) -> list:
    """解析社交流中的媒体 URL 字段（可能是 JSON 数组、逗号分隔字符串、空字符串）。"""
    if not s:
        return []
    if isinstance(s, list):
        return [str(u) for u in s if u]
    if not isinstance(s, str):
        return []
    s = s.strip()
    if not s:
        return []
    # 尝试 JSON 解析
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(u) for u in parsed if u]
        if isinstance(parsed, str) and parsed:
            return [parsed]
        return []
    except (json.JSONDecodeError, ValueError):
        pass
    # 兜底：逗号分隔
    return [u.strip() for u in s.split(",") if u.strip()]

def _convert_social_user_to_author(u: dict) -> dict:
    """把社交流的 user 对象转换为 Binance author 格式。"""
    return {
        "name": (u.get("username") or "") if u else "",
        "handle": (u.get("handle") or "") if u else "",
        "profileImgUrl": (u.get("profilePic") or "") if u else "",
        "profileBannerUrl": (u.get("profileBannerUrl") or "") if u else "",
        "isBlueVerified": 0,  # 社交流不提供蓝V状态
        "description": (u.get("bio") or "") if u else "",
        "location": "",
        "twitterId": "",
        "followersCnt": (u.get("follower") or 0) if u else 0,
        "followingCnt": (u.get("following") or 0) if u else 0,
    }

def _convert_social_event_to_binance(event: dict) -> Optional[dict]:
    """把社交流事件转换为 Binance tweet 格式 {"data": {...}}。
    社交流缺少 likeCnt/retweetCnt 等互动数据，用 0 占位。
    引用推文的 tweetId 不在社交流中提供，留空（不影响展示，仅影响深链跳转）。
    """
    if not event or not isinstance(event, dict):
        return None
    tweet_id = event.get("tweetId")
    if not tweet_id:
        return None
    tweet_id = str(tweet_id)

    event_type = event.get("eventType") or "original"
    tweet_type = _SOCIAL_EVENT_TYPE_MAP.get(event_type, "original")

    author = _convert_social_user_to_author(event.get("user") or {})
    img_urls = _parse_social_media_urls(event.get("fileUrls"))
    video_urls = _parse_social_media_urls(event.get("videoUrls"))
    # 如果 fileUrls/videoUrls 为空，尝试从 entities.media 提取
    if not img_urls and not video_urls:
        entities_raw = event.get("entities")
        if entities_raw and isinstance(entities_raw, str):
            try:
                entities = json.loads(entities_raw)
                media = entities.get("media") if isinstance(entities, dict) else None
                if isinstance(media, list):
                    for m in media:
                        if isinstance(m, dict):
                            url = m.get("media_url_https") or m.get("url")
                            if url:
                                if m.get("type") == "video" or m.get("video_info"):
                                    video_urls.append(url)
                                else:
                                    img_urls.append(url)
            except (json.JSONDecodeError, ValueError):
                pass

    # 构建引用推文（reply → repliedToTweet, quote → quotedTweet, retweet → retweetedTweet）
    referenced_tweet = None
    if event_type in ("reply", "quote", "retweet"):
        ref_user = event.get("referenceUser") or {}
        ref_author = _convert_social_user_to_author(ref_user)
        ref_img_urls = _parse_social_media_urls(event.get("referencedFiles"))
        ref_video_urls = _parse_social_media_urls(event.get("referencedVideos"))
        referenced_tweet = {
            "tweetId": "",  # 社交流不提供引用推文的 ID
            "text": event.get("contentOld") or "",
            "textTranslation": event.get("referencedTextTranslation") or "",
            "lang": "en",
            "tweetType": "original",
            "createdAt": (event.get("referenceTime") or 0) * 1000,
            "author": ref_author,
            "likeCnt": 0,
            "retweetCnt": 0,
            "replyCnt": 0,
            "quoteCnt": 0,
            "imgUrls": ref_img_urls,
            "videoUrls": ref_video_urls,
            "article": None,
            "quotedTweet": None,
            "repliedToTweet": None,
            "retweetedTweet": None,
        }

    event_time_ms = (event.get("eventTime") or 0) * 1000
    tweet_data = {
        "tweetId": tweet_id,
        "text": event.get("contentNew") or "",
        "textTranslation": event.get("tweetTextTranslation") or "",
        "lang": "en",  # 社交流不提供 lang，默认 en
        "tweetType": tweet_type,
        "createdAt": event_time_ms,
        "author": author,
        "likeCnt": 0,  # 社交流不提供互动数据
        "retweetCnt": 0,
        "replyCnt": 0,
        "quoteCnt": 0,
        "imgUrls": img_urls,
        "videoUrls": video_urls,
        "article": event.get("articleInfo"),
        "quotedTweet": referenced_tweet if event_type == "quote" else None,
        "repliedToTweet": referenced_tweet if event_type == "reply" else None,
        "retweetedTweet": referenced_tweet if event_type == "retweet" else None,
    }
    return {"data": tweet_data}


async def handle_social_event(data, target_url):
    """处理社交动态流事件：转换为 Binance 格式 → 入队（批量写 SQLite）。"""
    global _social_dropped_count
    if not isinstance(data, dict):
        return
    event = data.get("data")
    if not event or not isinstance(event, dict):
        return
    converted = _convert_social_event_to_binance(event)
    if not converted:
        return
    tweet_id = converted["data"]["tweetId"]
    stats_inc("social_events_received")
    with _social_queue_lock:
        if len(_social_write_queue) < _SOCIAL_QUEUE_MAXSIZE:
            _social_write_queue.append(("tweet", tweet_id, converted))
        else:
            with _social_dropped_lock:
                _social_dropped_count += 1
            stats_inc("social_dropped")


async def handle_social_translation_event(data, target_url):
    """处理社交翻译流事件：提取翻译 → 入队（merge 写 translations 表）。"""
    global _social_dropped_count
    if not isinstance(data, dict):
        return
    event = data.get("data")
    if not event or not isinstance(event, dict):
        return
    tweet_id = event.get("tweetId")
    if not tweet_id:
        return
    tweet_id = str(tweet_id)
    translation_data = {}
    if event.get("tweetTextTranslation"):
        translation_data["tweetTextTranslation"] = event["tweetTextTranslation"]
    if event.get("referencedTextTranslation"):
        translation_data["referencedTextTranslation"] = event["referencedTextTranslation"]
    if not translation_data:
        return
    stats_inc("social_events_received")
    with _social_queue_lock:
        if len(_social_write_queue) < _SOCIAL_QUEUE_MAXSIZE:
            _social_write_queue.append(("translation", tweet_id, translation_data))
        else:
            with _social_dropped_lock:
                _social_dropped_count += 1
            stats_inc("social_dropped")


def _flush_social_batch_sync(batch: list) -> tuple:
    """[同步函数，在线程池执行] 把一批 social 事件写入 SQLite。
    返回 (tweet_count, trans_count)。
    优化点：
    - translations 写入改为"内存预读 + 批量 UPSERT"：一次性 SELECT 已有的，
      在内存 merge，然后批量 INSERT OR REPLACE，避免 N 次串行 SELECT
    - 整个 batch 在 db_lock 持有期间完成（autocommit 模式下逐条 commit，
      WAL+synchronous=NORMAL 下 fsync 开销可接受）
    """
    if not batch:
        return (0, 0)
    tweet_count = 0
    trans_count = 0
    with db_lock:
        conn = _get_db_conn()
        try:
            # 1. tweets: INSERT OR IGNORE（不覆盖 API 数据），source='social'
            tweet_items = [it for it in batch if it[0] == "tweet"]
            trans_items = [it for it in batch if it[0] == "translation"]
            for _, tid, converted in tweet_items:
                conn.execute(
                    "INSERT OR IGNORE INTO tweets (tweet_id, raw_data, source, created_at, updated_at) VALUES (?, ?, 'social', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    (tid, json.dumps(converted, ensure_ascii=False))
                )
                tweet_count += 1
            # 2. translations: 批量 SELECT 已有 + 内存 merge + 批量 UPSERT
            if trans_items:
                # 一次性查所有涉及 tweet_id 的现有翻译
                trans_tids = list({t[1] for t in trans_items})
                placeholders = ",".join(["?"] * len(trans_tids))
                cur = conn.execute(
                    f"SELECT tweet_id, translation_data FROM translations WHERE tweet_id IN ({placeholders})",
                    trans_tids
                )
                existing_map = {}
                for row in cur.fetchall():
                    try:
                        existing_map[row[0]] = json.loads(row[1]) if row[1] else {}
                    except Exception:
                        existing_map[row[0]] = {}
                # 内存 merge：同 tweet_id 多条 trans_data 合并
                merged_map = {}
                for _, tid, trans_data in trans_items:
                    base = merged_map.get(tid) or dict(existing_map.get(tid) or {})
                    base.update(trans_data)
                    merged_map[tid] = base
                # 批量 UPSERT
                for tid, merged in merged_map.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO translations (tweet_id, translation_data, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                        (tid, json.dumps(merged, ensure_ascii=False))
                    )
                    trans_count += 1
        except Exception as e:
            logger.warning("⚠️ Social batch write failed: %s", e)
    # 更新统计计数器（锁外，避免锁嵌套）
    if tweet_count:
        stats_inc("social_tweets_written", tweet_count)
    if trans_count:
        stats_inc("social_translations_written", trans_count)
    return (tweet_count, trans_count)


def _cleanup_old_social_tweets_sync() -> int:
    """[同步函数，在线程池执行] 清理 tweets 表中超过 TTL 的老旧数据。
    返回删除的行数。保留 translations 表（翻译可能被复用，且量小）。
    """
    deleted = 0
    with db_lock:
        conn = _get_db_conn()
        try:
            # 用 datetime 函数计算过期。保留 _SOCIAL_TWEETS_TTL 秒内的数据。
            # 注意：会同时删 API 数据（24h 未访问的清理掉合理，因为 token 卡片
            # 上限 200 条，老 tweet_id 也不会再用到）
            cur = conn.execute(
                "DELETE FROM tweets WHERE created_at < datetime('now', ?)",
                (f"-{_SOCIAL_TWEETS_TTL} seconds",)
            )
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as e:
            logger.warning("⚠️ Social tweets cleanup failed: %s", e)
    return deleted


async def social_flush_loop():
    """后台任务：每 1s 把社交流队列批量写入 SQLite。
    关键：DB IO 在专用 _social_executor 线程池执行，不阻塞 asyncio 事件循环。
    """
    global _social_dropped_count  # 必须声明：函数内对它赋值（=0），否则 Python 当作局部变量导致 UnboundLocalError
    last_cleanup = time.time()
    loop = asyncio.get_event_loop()
    while True:
        try:
            await asyncio.sleep(_SOCIAL_FLUSH_INTERVAL)
            with _social_queue_lock:
                if not _social_write_queue:
                    # 队列空，但仍可能需要清理（看周期）
                    need_cleanup = (time.time() - last_cleanup) >= _SOCIAL_TWEETS_CLEANUP_INTERVAL
                    batch = []
                else:
                    batch = _social_write_queue[:]
                    _social_write_queue.clear()
                    need_cleanup = (time.time() - last_cleanup) >= _SOCIAL_TWEETS_CLEANUP_INTERVAL
            if batch:
                # 在专用线程池执行同步 DB IO
                tweet_count, trans_count = await loop.run_in_executor(
                    _social_executor, _flush_social_batch_sync, batch
                )
                if tweet_count or trans_count:
                    logger.info("📝 社交流缓存: %d tweets, %d translations (队列剩余 %d)",
                               tweet_count, trans_count, len(_social_write_queue))
                # 检查丢弃计数，有丢弃则告警一次
                with _social_dropped_lock:
                    dropped = _social_dropped_count
                    _social_dropped_count = 0
                if dropped > 0:
                    logger.warning("⚠️ 社交流队列溢出，丢弃 %d 条事件（队列上限 %d）",
                                  dropped, _SOCIAL_QUEUE_MAXSIZE)
            if need_cleanup:
                last_cleanup = time.time()
                deleted = await loop.run_in_executor(
                    _social_executor, _cleanup_old_social_tweets_sync
                )
                if deleted > 0:
                    logger.info("🧹 清理 %d 条过期 tweets（>%ds）", deleted, _SOCIAL_TWEETS_TTL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("❌ Social flush loop error: %s", e)


# ============================================================
# Binance Square API (币安广场 post/article) - 带重试
# ============================================================

def get_binance_square_content(content_id: str, retries: int = API_RETRY_COUNT) -> Optional[dict]:
    """通过币安广场 API 获取 post/article 内容。
    返回原始 JSON 或 None。
    """
    if not content_id:
        return None
    headers = {
        "sec-ch-ua-platform": "\"macOS\"",
        "referer": "https://web3.binance.com/zh-CN/trenches?chain=bsc",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "sec-ch-ua": "\"Not=A?Brand\";v=\"99\", \"Google Chrome\";v=\"151\", \"Chromium\";v=\"151\"",
        "content-type": "application/json",
        "sec-ch-ua-mobile": "?0",
        "x-trace-id": str(uuid.uuid4()),
        "x-ui-request-trace": str(uuid.uuid4()),
        "lang": "zh-CN",
        "bnc-location": "CN",
        "accept-language": "zh-CN,zh;q=0.9",
    }
    url = f"https://www.binance.com/bapi/composite/v3/friendly/pgc/special/content/detail/{content_id}"
    delay = API_RETRY_DELAY
    for attempt in range(1, retries + 1):
        try:
            response = get_api_session().get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data and data.get("data"):
                        return data
                    else:
                        logger.warning("⚠️ Binance Square API 返回缺少 data 字段 (尝试 %d/%d) | id=%s", attempt, retries, content_id)
                except Exception:
                    logger.exception("Binance Square API JSON 解析失败 | id=%s", content_id)
            else:
                logger.error("Binance Square API 返回 %s (尝试 %d/%d) | id=%s", response.status_code, attempt, retries, content_id)
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
        except Exception as e:
            logger.error("Binance Square API 请求异常 | id=%s: %s", content_id, e)
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
    logger.error("❌ Binance Square API 重试 %d 次均失败 | id=%s", retries, content_id)
    return None

def build_square_tweet(square_data: dict, tokens: list) -> Optional[dict]:
    """把币安广场 post/article 数据构建为推文格式（与 tweet 卡片兼容）。"""
    d = square_data.get("data")
    if not d:
        return None
    content_id = str(d.get("id") or "")
    if not content_id:
        return None
    author = {
        "name": d.get("displayName") or d.get("username") or "Binance Square",
        "handle": d.get("username") or "",
        "profileImgUrl": d.get("avatar") or "",
        "profileBannerUrl": "",
        "isBlueVerified": 1 if d.get("authorVerificationType") else 0,
        "description": "",
        "location": "",
        "twitterId": "",
        "followersCnt": d.get("totalFollowerCount") or 0,
        "followingCnt": 0,
    }
    # 内容：优先 summary，其次 bodyTextOnly
    text = d.get("summary") or d.get("bodyTextOnly") or ""
    # 图片
    img_urls = []
    image_list = d.get("imageList") or []
    for img in image_list:
        if isinstance(img, dict):
            url = img.get("url") or img.get("imageUrl") or ""
            if url:
                img_urls.append(url)
        elif isinstance(img, str) and img:
            img_urls.append(img)
    # 视频
    video_urls = []
    video_link = d.get("videoLink")
    if video_link:
        video_urls.append({"videoPreviewUrl": d.get("cover") or "", "variants": [{"contentType": "video/mp4", "url": video_link}]})
    # 翻译：translatedData 里不同类型字段不同
    # POST (contentType=1): 翻译在 translatedData.content
    # ARTICLE (contentType=2): 翻译标题在 translatedData.title，翻译正文在 translatedData.body（JSON RichText 结构）
    translated = d.get("translatedData")
    text_translation = ""
    trans_title = ""
    if translated and isinstance(translated, dict):
        text_translation = translated.get("content") or ""
        trans_title = translated.get("title") or ""
        # ARTICLE 翻译正文在 body 字段（JSON RichText 结构）
        if not text_translation:
            body_str = translated.get("body")
            if body_str and isinstance(body_str, str):
                try:
                    body_json = json.loads(body_str)
                    # 提取 hash.{uuid}.config.content[].config.content 的文本
                    hash_data = body_json.get("hash", {})
                    texts = []
                    for block in hash_data.values():
                        if not isinstance(block, dict):
                            continue
                        config = block.get("config", {})
                        content_list = config.get("content", [])
                        if isinstance(content_list, list):
                            for item in content_list:
                                if isinstance(item, dict):
                                    item_config = item.get("config", {})
                                    text = item_config.get("content", "")
                                    if text:
                                        texts.append(str(text))
                    if texts:
                        text_translation = "\n".join(texts)
                except (json.JSONDecodeError, TypeError):
                    pass
    # 原文：POST 用 summary 或 bodyTextOnly，ARTICLE 有 title
    orig_title = d.get("title") or ""
    text = ""
    if orig_title:
        text = orig_title
        if d.get("bodyTextOnly"):
            text += "\n\n" + str(d.get("bodyTextOnly"))
        # 如果有翻译标题，追加到翻译前面
        if trans_title and trans_title != orig_title:
            text_translation = trans_title + (("\n\n" + text_translation) if text_translation else "")
    else:
        text = d.get("summary") or d.get("bodyTextOnly") or ""
    create_time = d.get("createTime") or 0
    if create_time > 1e12:
        create_time = create_time  # 保持毫秒
    tweet_id = f"square_{content_id}"
    return {
        "tweet_id": tweet_id,
        "tweetId": tweet_id,
        "text": text,
        "content": text,
        "textTranslation": text_translation,
        "tweetTextTranslation": text_translation,
        "lang": d.get("detectedLang") or "en",
        "tweetType": "square",
        "createdAt": create_time,
        "timestamp": create_time,
        "author": author,
        "name": author["name"],
        "handle": author["handle"],
        "avatar": author["profileImgUrl"],
        "profileImgUrl": author["profileImgUrl"],
        "isBlueVerified": author["isBlueVerified"],
        "likeCnt": d.get("likeCount") or 0,
        "retweetCnt": d.get("shareCount") or 0,
        "replyCnt": d.get("commentCount") or 0,
        "quoteCnt": d.get("quoteCount") or 0,
        "imgUrls": img_urls,
        "videoUrls": video_urls,
        "article": None,
        "quotedTweet": None,
        "repliedToTweet": None,
        "retweetedTweet": None,
        "tokens": tokens,
    }

# ============================================================
# TikHub Weibo API (微博帖子) - 带重试
# ============================================================

def get_weibo_content(post_id: str, retries: int = 3) -> Optional[dict]:
    """通过 TikHub API 获取微博帖子详情。"""
    if not post_id:
        return None
    try:
        from weibo_client import get_weibo_post
    except ImportError:
        logger.warning("⚠️ weibo_client 未安装，跳过微博查询")
        return None
    import http.client as _hc
    result = get_weibo_post(post_id, retries)
    return result

def build_weibo_tweet(weibo_data: dict, tokens: list) -> Optional[dict]:
    """把微博帖子数据构建为推文格式（与 tweet 卡片兼容）。"""
    try:
        d = weibo_data.get("data", {}).get("data", {})
    except (AttributeError, KeyError):
        return None
    if not d:
        return None
    post_id = str(d.get("id") or "")
    if not post_id:
        return None
    user = d.get("user") or {}
    author = {
        "name": user.get("screen_name") or "Weibo User",
        "handle": str(user.get("id") or ""),
        "profileImgUrl": user.get("profile_image_url") or "",
        "profileBannerUrl": "",
        "isBlueVerified": 1 if user.get("verified") else 0,
        "description": user.get("description") or "",
        "location": user.get("location") or "",
        "twitterId": "",
        "followersCnt": user.get("followers_count") or 0,
        "followingCnt": user.get("follow_count") or 0,
    }
    # 正文：text 含 HTML 标签，text_raw 是纯文本
    text = d.get("text_raw") or d.get("text") or ""
    # 清理 HTML 标签
    if text:
        import re as _re
        text = _re.sub(r'<[^>]+>', '', text).strip()
    # 图片
    img_urls = []
    pic_infos = d.get("pic_infos") or {}
    for pic_id, pic_info in pic_infos.items():
        if isinstance(pic_info, dict):
            large = pic_info.get("large") or pic_info.get("original") or {}
            url = large.get("url") if isinstance(large, dict) else None
            if not url and isinstance(large, dict):
                url = large.get("pic") or ""
            if url:
                img_urls.append(url)
    # 视频
    video_urls = []
    page_info = d.get("page_info") or {}
    if page_info.get("type") in ("video", "search_topic"):
        media_info = page_info.get("media_info") or {}
        stream_url = media_info.get("stream_url") or ""
        preview_url = ""
        page_pic = page_info.get("page_pic")
        if isinstance(page_pic, dict):
            preview_url = page_pic.get("url") or ""
        elif isinstance(page_pic, str):
            preview_url = page_pic
        if stream_url:
            video_urls.append({"videoPreviewUrl": preview_url, "variants": [{"contentType": "video/mp4", "url": stream_url}]})
    # 时间
    created_at = d.get("created_at") or ""
    import time as _time
    try:
        from datetime import datetime
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        timestamp = int(dt.timestamp() * 1000)
    except Exception:
        timestamp = int(_time.time() * 1000)
    tweet_id = f"weibo_{post_id}"
    return {
        "tweet_id": tweet_id,
        "tweetId": tweet_id,
        "text": text,
        "content": text,
        "textTranslation": "",
        "tweetTextTranslation": "",
        "lang": "zh",
        "tweetType": "weibo",
        "createdAt": timestamp,
        "timestamp": timestamp,
        "author": author,
        "name": author["name"],
        "handle": author["handle"],
        "avatar": author["profileImgUrl"],
        "profileImgUrl": author["profileImgUrl"],
        "isBlueVerified": author["isBlueVerified"],
        "likeCnt": d.get("attitudes_count") or 0,
        "retweetCnt": d.get("reposts_count") or 0,
        "replyCnt": d.get("comments_count") or 0,
        "quoteCnt": 0,
        "imgUrls": img_urls,
        "videoUrls": video_urls,
        "article": None,
        "quotedTweet": None,
        "repliedToTweet": None,
        "retweetedTweet": None,
        "tokens": tokens,
    }

# ============================================================
# Binance Profile API (个人主页) - 带重试
# ============================================================

def get_profile_info(handle: str, retries: int = API_RETRY_COUNT) -> Optional[dict]:
    if not handle:
        return None
    headers = {
        "sec-ch-ua-platform": "\"macOS\"",
        "referer": "https://web3.binance.com/zh-CN/trenches?chain=bsc",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "sec-ch-ua": "\"Not=A?Brand\";v=\"99\", \"Google Chrome\";v=\"151\", \"Chromium\";v=\"151\"",
        "content-type": "application/json",
        "sec-ch-ua-mobile": "?0",
        "x-trace-id": str(uuid.uuid4()),
        "x-ui-request-trace": str(uuid.uuid4()),
        "lang": "zh-CN"
    }
    url = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/token/twitter/profile/info"
    params = {"twitterHandle": handle}
    delay = API_RETRY_DELAY
    for attempt in range(1, retries + 1):
        try:
            response = get_api_session().get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data and data.get("data"):
                        return data
                    else:
                        logger.warning("⚠️ Profile API 返回缺少 data 字段 (尝试 %d/%d) | handle=%s", attempt, retries, handle)
                except Exception:
                    logger.exception("Profile API JSON 解析失败 (尝试 %d/%d) | handle=%s", attempt, retries, handle)
            else:
                logger.error("Profile API 返回 %s (尝试 %d/%d) | handle=%s | body=%s",
                             response.status_code, attempt, retries, handle, response.text[:300])
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
        except Exception as e:
            logger.error("请求 Profile 失败 [%s] (尝试 %d/%d): %s", handle, attempt, retries, e)
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
    logger.error("❌ Profile API 重试 %d 次均失败 | handle=%s", retries, handle)
    return None

def get_profile_cached(handle: str) -> Optional[dict]:
    if not handle:
        return None
    profile_id = f"profile_{handle}"
    cached = get_tweet_from_db(profile_id)
    if cached:
        logger.info("📦 SQLite 命中 profile handle=%s", handle)
        return cached
    logger.info("🔄 SQLite 未命中，调用 Profile API (带重试): %s", handle)
    api_response = get_profile_info(handle)
    if api_response and api_response.get("data"):
        save_tweet_to_db(profile_id, api_response)
    return api_response

# ============================================================
# HTTP Push (完整推文)
# ============================================================

def push_tweet(tweet_obj, target_url) -> bool:
    """
    推送完整推文（占位卡 / 完整卡 / 更新）到 server。
    优先级：
      1. 进程内 hook（on_new_message / on_update_message）：直接调 server.tweet_manager + socketio.emit
         - 跳过 JSON 序列化+TCP 环回+反序列化，省下几～几十 ms
         - 同进程时这是默认路径
      2. HTTP POST fallback：跨进程部署时使用
    """
    if not tweet_obj:
        return False
    tweet_id = tweet_obj.get("tweet_id") or tweet_obj.get("tweetId")
    if not tweet_id:
        return False
    tweet_id = str(tweet_id)
    tokens = tweet_obj.get("tokens", [])
    logger.info("📤 准备推送 tweet_id=%s (含 %d 个 token)", tweet_id, len(tokens))

    # 1. 进程内 hook 优先：同进程时直接调用，零网络开销
    hook = _hooks["on_new_message"] if tweet_id not in pushed_tweet_ids else _hooks["on_update_message"]
    if hook is not None:
        try:
            hook(tweet_obj)
            return True
        except Exception as e:
            logger.error("❌ 进程内 hook 推送异常 tweet_id=%s: %s，回退到 HTTP", tweet_id, e)
            # 失败则继续走 HTTP fallback

    # 2. HTTP POST fallback
    url = target_url.rstrip("/") + "/api/tweet"
    try:
        response = get_push_session().post(url, json=tweet_obj, timeout=PUSH_TIMEOUT, headers={"Content-Type": "application/json"})
        logger.info("📥 推送响应 %s | tweet_id=%s", response.status_code, tweet_id)
        if response.status_code in (200, 201):
            return True
        logger.error("❌ 推送失败 %s | tweet_id=%s | %s", response.status_code, tweet_id, response.text[:300])
        return False
    except Exception as e:
        logger.error("❌ 推送异常 | tweet_id=%s | %s", tweet_id, e)
        return False

# ============================================================
# 增量 Token 更新推送（零延迟 + in-flight 合并）
# ============================================================
# 设计目标：meme token 交易场景下，token 数据（价格/市值/持仓）变更需要尽快
#           反映到前端，不能有秒级节流延迟。
#
# 实现策略（替代原 leading+trailing 节流）：
#   - 首次推送：立即执行，零延迟
#   - 推送进行中：标记 pending=True，当前推送完成后立即用最新数据再推一次
#   - 多次 pending 只触发一次追加推送（取最新数据），保证不丢更新
#   - 同一 tweet_id 永远只有一个推送在飞行中，避免并发 POST 浪费带宽
# ============================================================

_push_in_flight: Dict[str, bool] = {}
_push_pending: Dict[str, bool] = {}

async def _do_push_token_update(tweet_id: str, target_url: str):
    """实际执行：从 token_map 重建最新 tokens 并推送到 server。
    优先级：
      1. 进程内 hook（on_token_update）：直接调 server.tweet_manager.update_tokens + socketio.emit
         - 跳过 JSON 序列化+TCP 环回+反序列化，省下几～几十 ms
         - 同进程时这是默认路径
      2. HTTP POST fallback：跨进程部署时使用
    """
    # 双重保险：再次确认该 tweet_id 已创建过卡片（避免因 pushed_tweet_ids 与 token_tweets
    # 短暂不同步导致的 404）。即使跳过检查，server 返回 404 时也会静默处理。
    with DATA_LOCK:
        if tweet_id not in pushed_tweet_ids:
            logger.debug("⏭️ 跳过 token_update 推送：tweet_id=%s 尚未创建卡片", tweet_id)
            return

    tokens = build_tokens_for_tweet(tweet_id)

    # 如果 token 列表为空（所有 token 都被过滤/移除了），推送空列表让前端移除卡片，
    # 同时从 pushed_tweet_ids 中移除该 tweet_id，停止后续 token_update 推送
    if not tokens:
        # 进程内 hook 优先
        if _hooks["on_token_update"] is not None:
            try:
                _hooks["on_token_update"](tweet_id, [])
                logger.info("📤 hook 推送空 token 列表，前端将移除卡片: tweet_id=%s", tweet_id)
            except Exception as e:
                logger.error("❌ hook 推送空 token 列表异常 tweet_id=%s: %s，回退 HTTP", tweet_id, e)
        else:
            payload = {"tweet_id": tweet_id, "tokens": []}
            url = target_url.rstrip("/") + "/api/token_update"
            try:
                response = await asyncio.to_thread(lambda: get_push_session().post(url, json=payload, timeout=PUSH_TIMEOUT))
                if response.status_code == 200:
                    logger.info("📤 推送空 token 列表，前端将移除卡片: tweet_id=%s", tweet_id)
                elif response.status_code == 404:
                    logger.debug("⏭️ 空 token 列表推送 404：tweet_id=%s 卡片已不存在", tweet_id)
            except Exception as e:
                logger.error("❌ 空 token 列表推送异常 tweet_id=%s: %s", tweet_id, e)
        # 从 pushed_tweet_ids 移除，停止后续推送
        with DATA_LOCK:
            pushed_tweet_ids.discard(tweet_id)
            logger.info("🧹 tweet_id=%s 无可见 token，从 pushed_tweet_ids 移除", tweet_id)
        return

    # 有 token 数据：优先 hook
    if _hooks["on_token_update"] is not None:
        try:
            _hooks["on_token_update"](tweet_id, tokens)
            # logger.info("✅ hook Token 更新推送成功 tweet_id=%s", tweet_id)  # 太频繁，注释掉
            return
        except Exception as e:
            logger.error("❌ hook Token 更新推送异常 tweet_id=%s: %s，回退 HTTP", tweet_id, e)

    # HTTP POST fallback
    payload = {
        "tweet_id": tweet_id,
        "tokens": tokens
    }
    url = target_url.rstrip("/") + "/api/token_update"
    try:
        response = await asyncio.to_thread(lambda: get_push_session().post(url, json=payload, timeout=PUSH_TIMEOUT))
        if response.status_code == 200:
            logger.info("✅ Token 更新推送成功 tweet_id=%s", tweet_id)
        elif response.status_code == 404:
            # server 端该 tweet_id 尚未创建卡片，静默跳过，不打 warning
            logger.debug("⏭️ Token 更新推送 404：tweet_id=%s 尚未创建卡片，跳过", tweet_id)
            # 卡片不存在，从 pushed_tweet_ids 移除
            with DATA_LOCK:
                pushed_tweet_ids.discard(tweet_id)
        else:
            logger.warning("⚠️ Token 更新推送失败 tweet_id=%s, status=%s, body=%s",
                           tweet_id, response.status_code, response.text[:200])
    except Exception as e:
        logger.error("❌ Token 更新推送异常 tweet_id=%s: %s", tweet_id, e)

async def push_token_update(tweet_id: str, target_url: str):
    """
    仅推送 Token 数据更新到 server，不构建完整推文。
    零延迟设计（替代 leading+trailing 节流）：
      - 首次推送：立即执行，无任何 interval 延迟
      - 推送进行中：标记 pending，当前推送完成后用最新数据再推一次
      - 同一 tweet_id 永远只有一个推送在飞行中，避免并发 POST
      - 保证最新数据不丢，同时无固定延迟窗口
    """
    tweet_id = str(tweet_id)
    if _push_in_flight.get(tweet_id):
        # 已有推送在进行中 → 标记 pending，当前推送完成后会立即再推一次（取最新数据）
        _push_pending[tweet_id] = True
        return
    _push_in_flight[tweet_id] = True
    try:
        while True:
            _push_pending[tweet_id] = False
            await _do_push_token_update(tweet_id, target_url)
            # 推送期间若有新更新到达，再推一次（build_tokens_for_tweet 会读取最新 token_map）
            if not _push_pending.get(tweet_id):
                break
    finally:
        _push_in_flight[tweet_id] = False

# ============================================================
# Token PATCH Merge (扩展：返回更新过的合约列表)
# ============================================================

def update_token_map(items, source="UPDATE") -> List[str]:
    """
    合并 Token 数据，返回被更新的合约地址列表（用于通知前端）。
    """
    global last_update_time, last_new_token_time
    if not isinstance(items, list):
        return []
    now = time.time()
    added = 0
    updated = 0
    updated_contracts = []  # 记录本次更新涉及的合约
    with DATA_LOCK:
        for item in items:
            if not isinstance(item, dict):
                continue
            contract_raw = item.get("contractAddress")
            if not contract_raw:
                continue
            contract = normalize_contract(contract_raw)
            if not contract:
                continue
            # 判断是新增还是更新
            is_new = contract not in token_map
            if is_new:
                merged = deep_merge_patch({}, item)
                merged["contractAddress"] = contract_raw
                merged["_first_seen"] = now
                merged["_last_update"] = now
                token_map[contract] = merged
                # 记录首次发现时间，用于 grace period 判断
                token_first_seen[contract] = now
                # 加入 _grace_contracts：仅这些 contract 需要 check_and_remove 跟踪
                # 一旦超过 grace period 就移除（小集合，避免每秒扫描整个 token_map）
                _grace_contracts[contract] = now
                added += 1
                updated_contracts.append(contract)
            else:
                existing = token_map[contract]
                first_seen = existing.get("_first_seen", now)
                # 用 deep_merge_patch_changed 直接拿到 changed 标记，
                # 替代旧的 json.dumps 整份序列化对比（CPU 浪费且阻塞事件循环）
                existing, changed = deep_merge_patch_changed(existing, item)
                existing["contractAddress"] = contract_raw
                existing["_first_seen"] = first_seen
                existing["_last_update"] = now
                token_map[contract] = existing
                if changed:
                    updated_contracts.append(contract)
                updated += 1
            # 关联 Tweet ID
            current_token = token_map[contract]
            tweet_id = extract_tweet_id_from_token(current_token)
            if tweet_id:
                tweet_id = str(tweet_id)
                tweet_tokens[tweet_id].add(contract)
                token_tweets[contract].add(tweet_id)   # 反向映射
            if source == "NEW":
                if contract not in pending_new_tokens:
                    pending_new_tokens[contract] = {"first_seen": now}
                    logger.info("🆕 NEW Token 加入 pending: %s", contract)
        last_update_time = now
        if source == "NEW" and added > 0:
            last_new_token_time = now
        total = len(token_map)
        pending_count = len(pending_new_tokens)
    logger.info("📥 [%s] 收到 %d 个代币 | 新增 %d | 更新 %d | 总数 %d | pending %d", source, len(items), added, updated, total, pending_count)
    return updated_contracts

# ============================================================
# Pending Cleanup
# ============================================================

def cleanup_expired_pending():
    now = time.time()
    expired = []
    with DATA_LOCK:
        for contract, info in list(pending_new_tokens.items()):
            first_seen = info.get("first_seen", now)
            if now - first_seen >= NEW_TOKEN_PENDING_TTL:
                expired.append(contract)
        for contract in expired:
            pending_new_tokens.pop(contract, None)
    for contract in expired:
        logger.info("⌛ NEW Token 等待 Twitter 超时，删除 pending: %s", contract)
    return len(expired)

def cleanup_stale_state():
    """
    定期清理冷数据，防止 token_map / token_first_seen / removed_low_mc_tokens /
    twitter_query_done / tweet_tokens / token_tweets / pushed_tweet_ids / placeholder_sent
    等结构无界增长（运行数天后内存膨胀、GC 压力上升）。

    清理策略：
    - removed_low_mc_tokens：超过 REMOVED_TOKEN_TTL 的条目移除（已足够久，不再需要去重）
    - twitter_query_done：超过 TWITTER_QUERY_DONE_TTL 的条目移除
    - token_map / token_first_seen / tweet_tokens / token_tweets：
      对未被任何 pushed_tweet_ids 引用且 _last_update 超过 STALE_TOKEN_TTL 的 token 移除
    - pushed_tweet_ids / placeholder_sent：移除对应 tweet_id 已不在 token_tweets 任何 contract 反向映射中的条目
    """
    now = time.time()
    stats = {"removed_tokens": 0, "removed_low_mc": 0, "removed_twitter_query": 0}
    with DATA_LOCK:
        # 1. 清理 removed_low_mc_tokens 中的过期条目
        expired_removed = [c for c, ts in removed_low_mc_tokens.items()
                           if (now - ts) > REMOVED_TOKEN_TTL]
        for c in expired_removed:
            removed_low_mc_tokens.pop(c, None)
        stats["removed_low_mc"] = len(expired_removed)

        # 2. 清理 twitter_query_done（无时间戳，无法按 TTL 清理，但可以按"对应 contract 不再活跃"清理）
        # 简单策略：若 contract 已不在 token_map 中，则清理对应的 query 记录
        # twitter_query_done 是 contract -> tweet_id 映射
        orphan_twitter = [c for c in twitter_query_done if c not in token_map]
        for c in orphan_twitter:
            twitter_query_done.pop(c, None)
        stats["removed_twitter_query"] = len(orphan_twitter)

        # 3. 清理 token_map 中的冷数据：未被任何 pushed_tweet_ids 引用且 _last_update 超过 STALE_TOKEN_TTL
        # 先收集所有活跃 tweet_id 关联的 contract 集合
        active_contracts = set()
        for tweet_id in pushed_tweet_ids:
            active_contracts.update(tweet_tokens.get(tweet_id, set()))
        # 扫描 token_map，找出冷数据
        stale_contracts = []
        for contract, token in list(token_map.items()):
            if contract in active_contracts:
                continue  # 仍被活跃卡片引用，保留
            last_update = token.get("_last_update", 0.0) if isinstance(token, dict) else 0.0
            if (now - last_update) > STALE_TOKEN_TTL:
                stale_contracts.append(contract)
        # 执行清理（同时清理反向映射与相关结构）
        for contract in stale_contracts:
            token_map.pop(contract, None)
            token_first_seen.pop(contract, None)
            _grace_contracts.pop(contract, None)
            # 清理 token_tweets 反向映射 + 顺带清理已空的 tweet_tokens 条目
            related_tweet_ids = token_tweets.pop(contract, set())
            for tid in related_tweet_ids:
                contracts_set = tweet_tokens.get(tid)
                if contracts_set is not None:
                    contracts_set.discard(contract)
                    if not contracts_set:
                        tweet_tokens.pop(tid, None)
        stats["removed_tokens"] = len(stale_contracts)

        # 4. 清理 pushed_tweet_ids / placeholder_sent 中已无 token 关联的条目
        orphan_pushed = [tid for tid in pushed_tweet_ids
                         if not tweet_tokens.get(tid)]
        for tid in orphan_pushed:
            pushed_tweet_ids.discard(tid)
            placeholder_sent.discard(tid)

    if any(stats.values()):
        logger.info("🧹 冷数据清理: token_map -%d, removed_low_mc -%d, twitter_query -%d, pushed_orphan -%d",
                    stats["removed_tokens"], stats["removed_low_mc"],
                    stats["removed_twitter_query"], len(orphan_pushed) if 'orphan_pushed' in dir() else 0)

async def pending_cleanup_loop():
    last_state_cleanup = 0.0  # 上一次冷数据清理时间戳
    while True:
        try:
            cleanup_expired_pending()
            # 检查并自动移除超过 grace period 且 marketCap 过低的 NEW Token
            removed_contracts = check_and_remove_low_mc_new_tokens()
            if removed_contracts:
                # 对被移除 token 关联的所有 tweet_id 触发 token_update，
                # 让前端从 token 列表中移除这些 token
                affected_tweet_ids = set()
                with DATA_LOCK:
                    for contract in removed_contracts:
                        for tweet_id in token_tweets.get(contract, set()):
                            # 如果该 tweet_id 正在推送队列中（pending_push_tasks），不触发移除
                            # 避免卡片还没建好就被移除
                            if tweet_id in pending_push_tasks:
                                task = pending_push_tasks[tweet_id]
                                if task and not task.done():
                                    logger.info("⏳ tweet_id=%s 正在建卡中，延迟移除 token %s", tweet_id, contract)
                                    continue
                            affected_tweet_ids.add(tweet_id)
                for tweet_id in affected_tweet_ids:
                    asyncio.create_task(push_token_update(tweet_id, TARGET_APP_URL))
                logger.info("🗑️ 自动移除 %d 个低市值 token，影响 %d 个卡片",
                            len(removed_contracts), len(affected_tweet_ids))

            # 每 STALE_TOKEN_CLEANUP_INTERVAL 秒执行一次冷数据清理（不是每秒）
            now = time.time()
            if (now - last_state_cleanup) >= STALE_TOKEN_CLEANUP_INTERVAL:
                cleanup_stale_state()
                last_state_cleanup = now
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("❌ pending 清理异常: %s", e)
        await asyncio.sleep(1)

# ============================================================
# NEW Token
# ============================================================

async def handle_new_token(data, target_url):
    if not isinstance(data, dict):
        return
    d = data.get("data", {}).get("d", [])
    if not d:
        return
    update_token_map(d, source="NEW")
    logger.info("🆕 NEW Token 数据处理完成，等待 UPDATE 获取 Twitter")

    # 异步发起 GMGN 社交媒体查询，与 UPDATE 流竞争
    for item in d:
        if not isinstance(item, dict):
            continue
        contract_raw = item.get("contractAddress")
        if not contract_raw:
            continue
        contract = normalize_contract(contract_raw)
        if not contract:
            continue
        # 检查是否已查询过
        with _gmgn_queried_lock:
            if contract in _gmgn_queried:
                continue
            _gmgn_queried.add(contract)
        # 异步发起 GMGN 查询
        asyncio.create_task(_gmgn_async_query(contract, target_url))
        # 异步发起 Binance Meta 查询
        asyncio.create_task(_binance_meta_async_query(contract, target_url))

async def _gmgn_async_query(contract: str, target_url: str):
    """异步通过 GMGN 查询 token 社交媒体信息。
    返回值可能是：
    - "tweet_id:1234567890" → 触发推文建卡
    - "handle" → 触发 Profile 建卡
    - "broadcast:xxx" → 触发直播建卡
    - None → 查询失败或无 twitter 信息
    """
    # 429 ban 期内：完全跳过（不发请求、不计统计，避免污染成功率指标）
    if _gmgn_is_banned():
        return
    _gmgn_stats_inc("gmgn_total")
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            _gmgn_executor, _gmgn_query_token_social, contract
        )
        if not result:
            # 区分 ban 状态导致 vs 真查询失败
            if _gmgn_is_banned():
                # ban 期间 _gmgn_query_token_social 内部已经计过 fail，这里不重复计
                return
            _gmgn_stats_inc("gmgn_fail")
            return

        _gmgn_stats_inc("gmgn_success")

        # 检查是否已被其他来源获取
        with DATA_LOCK:
            already_done = contract in twitter_query_done
        if already_done:
            _gmgn_stats_inc("binance_won")
            logger.debug("🏃 GMGN 查询到 %s 但已被其他来源获取", contract)
            return

        result = result.strip()

        # 根据返回值类型决定建卡方式
        if result.startswith("tweet_id:"):
            tweet_id = result.split(":", 1)[1]
            with DATA_LOCK:
                if contract in twitter_query_done:
                    _gmgn_stats_inc("binance_won")
                    return
                twitter_query_done[contract] = tweet_id
                tweet_tokens[tweet_id].add(contract)
                token_tweets[contract].add(tweet_id)
                pending_new_tokens.pop(contract, None)
            _gmgn_stats_inc("gmgn_won")
            logger.info("🏆 GMGN 竞争胜出: %s -> tweet_id=%s，触发推文卡片创建", contract, tweet_id)
            await schedule_tweet_push(tweet_id, target_url)
        else:
            # handle 或 broadcast:xxx
            profile_id = f"profile_{result}"
            with DATA_LOCK:
                if contract in twitter_query_done:
                    _gmgn_stats_inc("binance_won")
                    return
                twitter_query_done[contract] = profile_id
                tweet_tokens[profile_id].add(contract)
                token_tweets[contract].add(profile_id)
                pending_new_tokens.pop(contract, None)
            _gmgn_stats_inc("gmgn_won")
            logger.info("🏆 GMGN 竞争胜出: %s -> @%s，触发 Profile 卡片创建", contract, result)
            await schedule_profile_push(result, target_url)
    except Exception as e:
        _gmgn_stats_inc("gmgn_fail")
        logger.debug("GMGN 异步查询异常 %s: %s", contract, e)

async def _binance_meta_async_query(contract: str, target_url: str):
    """异步通过 Binance Meta API 查询 token 社交媒体信息。
    与 GMGN 和 Binance UPDATE 流三方竞争。
    """
    _gmgn_stats_inc("binance_meta_total")
    try:
        handle = await asyncio.get_event_loop().run_in_executor(
            _binance_meta_executor, _binance_meta_query_social, contract
        )
        if handle:
            _gmgn_stats_inc("binance_meta_success")
            # 检查是否已被其他来源获取
            with DATA_LOCK:
                already_done = contract in twitter_query_done
            if already_done:
                # 其他来源先到了
                _gmgn_stats_inc("binance_won")
                logger.debug("🏃 BinanceMeta 查询到 %s 但已被其他来源获取", contract)
                return
            # Binance Meta 先到！
            handle = handle.strip()
            profile_id = f"profile_{handle}"
            with DATA_LOCK:
                if contract in twitter_query_done:
                    _gmgn_stats_inc("binance_won")
                    logger.debug("🏃 BinanceMeta 查询到 %s 但同时被其他来源获取", contract)
                    return
                twitter_query_done[contract] = profile_id
                tweet_tokens[profile_id].add(contract)
                token_tweets[contract].add(profile_id)
                pending_new_tokens.pop(contract, None)
            _gmgn_stats_inc("binance_meta_won")
            logger.info("🏆 BinanceMeta 竞争胜出: %s -> @%s，触发 Profile 卡片创建", contract, handle)
            await schedule_profile_push(handle, target_url)
        else:
            _gmgn_stats_inc("binance_meta_fail")
    except Exception as e:
        _gmgn_stats_inc("binance_meta_fail")
        logger.debug("BinanceMeta 异步查询异常 %s: %s", contract, e)

# ============================================================
# AI 叙事 / 首笔资金来源
# ============================================================

def parse_ai_narrative(token: dict) -> Optional[str]:
    raw = token.get("shortAiNarrative")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw.get("cn") or raw.get("en")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get("cn") or parsed.get("en")

def get_first_buy_source(token: dict) -> Optional[str]:
    info = token.get("firstGasInfo")
    if not isinstance(info, dict):
        return None
    entity = info.get("sourceEntity")
    source_type = info.get("sourceType")
    if entity and source_type:
        return str(entity) + " (" + str(source_type) + ")"
    return entity or source_type or None

# ============================================================
# Token 过滤 / 自动移除
# ============================================================

# 跟踪最近处于 grace period 的 token：一个相对小的集合
# 仅这些 contract 需要每秒检查是否超过 grace period
# 一旦超过 grace period 就从该集合移除（避免无限扫描已检查过的 token）
_grace_contracts: Dict[str, float] = {}  # contract -> first_seen timestamp

def is_token_in_grace_period(contract: str, now: float = None) -> bool:
    """判断 token 是否仍在 NEW Token 的 grace period 内（创建后 NEW_TOKEN_GRACE_SECONDS 秒）。"""
    if now is None:
        now = time.time()
    with DATA_LOCK:
        first_seen = token_first_seen.get(contract)
        if first_seen is None:
            return False
        return (now - first_seen) < NEW_TOKEN_GRACE_SECONDS

def get_token_market_cap(token: dict) -> float:
    """安全提取 marketCap，返回 float 或 0。"""
    if not isinstance(token, dict):
        return 0.0
    mc = token.get("marketCap")
    if mc is None:
        return 0.0
    try:
        return float(mc)
    except (TypeError, ValueError):
        return 0.0

def should_token_be_displayed(contract: str, now: float = None) -> bool:
    """
    判断 token 是否应在前端显示：
    - 买卖税任一 > 3% → 不显示
    - grace period 内（0-30s）：始终显示
    - 30s-60s：marketCap >= 5k 才显示
    - 60s+（OLD Token）：marketCap >= 6k 才显示
    - 已被自动移除：在观察期（10分钟）内市值恢复到 10k 则重新显示
    """
    if now is None:
        now = time.time()
    with DATA_LOCK:
        # 检查是否在移除观察期
        removed_ts = removed_low_mc_tokens.get(contract)
        if removed_ts is not None:
            # 超过观察期 → 彻底移除标记（不再观察，走正常 OLD Token 逻辑）
            if (now - removed_ts) > REMOVED_OBSERVATION_TTL:
                removed_low_mc_tokens.pop(contract, None)
                # 不 return，继续走下面的正常逻辑
            else:
                # 观察期内：市值 >= 10k 则恢复显示
                token = token_map.get(contract)
                if token:
                    mc = get_token_market_cap(token)
                    if mc >= REMOVED_RECOVERY_MC:
                        removed_low_mc_tokens.pop(contract, None)
                        logger.info("♻️ Token 恢复显示（观察期市值恢复）: %s (mc=%.2f >= %.2f)",
                                    contract, mc, REMOVED_RECOVERY_MC)
                        # 不 return，继续走下面的正常逻辑
                    else:
                        return False
                else:
                    return False
        token = token_map.get(contract)
        if not token:
            return False
        # 买卖税过滤：任一 > 3% 不显示
        tax_buy = token.get("taxRateBuy")
        tax_sell = token.get("taxRateSell")
        try:
            if tax_buy is not None and float(tax_buy) > 0.03:
                return False
            if tax_sell is not None and float(tax_sell) > 0.03:
                return False
        except (TypeError, ValueError):
            pass
        if is_token_in_grace_period(contract, now):
            # grace period 内（0-30s）：始终显示，不管市值
            return True
        # 30s-60s 之间：marketCap < 5k 不显示
        first_seen = token_first_seen.get(contract)
        if first_seen and (now - first_seen) < 60.0:
            mc = get_token_market_cap(token)
            return mc >= 5000.0
        # OLD Token（60s 后）：marketCap < 6k 不显示
        mc = get_token_market_cap(token)
        return mc >= OLD_TOKEN_MIN_MARKETCAP

def check_and_remove_low_mc_new_tokens(now: float = None) -> List[str]:
    """
    检查 _grace_contracts 中的 token，按阶段移除：
    - 0-30s（grace period）：不移除任何 token（新 token 无论市值多少都显示）
    - 30s-60s：marketCap < 5k → 移除
    - 60s+：从 _grace_contracts 移除（后续由 should_token_be_displayed 的 6k 阈值控制）

    优化：只扫描 _grace_contracts（仍在跟踪期的 token，量级远小于 token_map）。
    """
    if now is None:
        now = time.time()
    removed = []
    grace_expired = []
    GRACE_TRACK_SECONDS = 60.0   # 60s 后完全移出跟踪
    POST_GRACE_MIN_MC = 5000.0  # 30s 后的移除阈值
    with DATA_LOCK:
        for contract, first_seen in list(_grace_contracts.items()):
            age = now - first_seen
            # 0-30s：grace period 内，不移除任何 token
            if age < NEW_TOKEN_GRACE_SECONDS:
                continue
            # 60s+：移出 _grace_contracts，检查最后一次是否需要移除
            if age >= GRACE_TRACK_SECONDS:
                grace_expired.append(contract)
            # 30s-60s 或 60s+：检查 5k 阈值
            if contract in removed_low_mc_tokens:
                continue
            token = token_map.get(contract)
            if not token:
                continue
            mc = get_token_market_cap(token)
            if mc < POST_GRACE_MIN_MC:
                removed_low_mc_tokens[contract] = now
                removed.append(contract)
                logger.info("🗑️ 自动移除低市值 Token: %s (marketCap=%.2f < %.2f, age=%.0fs)",
                            contract, mc, POST_GRACE_MIN_MC, age)
        # 把 60s 后的 contract 从 _grace_contracts 中移除（保持小集合）
        for c in grace_expired:
            _grace_contracts.pop(c, None)
    return removed

# ============================================================
# 构建 Token Snapshot
# ============================================================

def build_tokens_for_tweet(tweet_id: str) -> List[dict]:
    tokens_list = []
    now = time.time()
    with DATA_LOCK:
        contracts = set(tweet_tokens.get(tweet_id, set()))
        # 同时收集嵌套推文关联的 token：通过 nested_to_parent 反向查找
        # nested_to_parent[nested_id] = {main_id1, main_id2, ...}
        # 这里需要反过来：如果有 nested_id 的 token 关联到此 tweet_id（作为主推文），
        # 需要收集那些 tweet_id 是嵌套推文 ID 的 token
        # 即：找所有 token_tweets[contract] 包含某个 nested_id 的 contract，
        # 而该 nested_id 在 nested_to_parent 里映射到此 tweet_id
        # 优化：直接遍历 nested_to_parent，找 value 包含 tweet_id 的 key
        for nested_id, parent_ids in nested_to_parent.items():
            if tweet_id in parent_ids:
                # nested_id 是此推文的嵌套推文，收集它的 token
                contracts.update(tweet_tokens.get(nested_id, set()))
        for contract in contracts:
            token = token_map.get(contract)
            if not token:
                continue
            # 过滤：grace period 外且 marketCap < 5k 的 OLD Token 不显示
            if not should_token_be_displayed(contract, now):
                continue
            create_time = get_token_create_time(token)
            age_str = format_age(create_time)
            tokens_list.append({
                "contract": contract,
                "symbol": token.get("symbol", ""),
                "name": token.get("name", ""),
                "icon": token.get("icon", ""),
                "marketCap": token.get("marketCap"),
                "price": token.get("price"),
                "liquidity": token.get("liquidity"),
                "volume": token.get("volume"),
                "holders": token.get("holders"),
                "createTime": create_time,
                "age": age_str,
                "holdersTop10Percent": token.get("holdersTop10Percent"),
                "devSellPercent": token.get("devSellPercent"),
                "taxRate": token.get("taxRate"),
                "taxRateBuy": token.get("taxRateBuy"),
                "taxRateSell": token.get("taxRateSell"),
                "devAddress": token.get("devAddress"),
                "isTaxToken": token.get("isTaxToken"),
                "bundlerHolders": token.get("bundlerHolders"),
                "insiderPercent": token.get("holdersInsiderPercent"),
                "holdersInsiderPercent": token.get("holdersInsiderPercent"),
                "migrateStatus": token.get("migrateStatus"),
                "twitterHandle": token.get("twitterHandle"),
                "kolHolders": token.get("kolHolders"),
                "sniperCount": token.get("sniperCount"),
                "website": safe_dict_get(token.get("socials"), "website"),
                "twitterUrl": safe_dict_get(token.get("socials"), "twitter") or safe_dict_get(token.get("socials"), "x"),
                "dividendQuoteAddress": token.get("dividendQuoteAddress") or safe_dict_get(token.get("taxFeeDistribution"), "dividendQuoteAddress"),
                "progress": token.get("progress"),
                "paidOnDexScreener": token.get("paidOnDexScreener"),
                "holdersDevPercent": token.get("holdersDevPercent"),
                "holdersSniperPercent": token.get("holdersSniperPercent"),
                "antiSniperEnabled": token.get("antiSniperEnabled"),
                "migrateTime": token.get("migrateTime"),
                "firstBuySource": get_first_buy_source(token),
                "twitterFollowers": safe_dict_get(token.get("twitterInfo"), "followersCnt"),
                "aiNarrative": parse_ai_narrative(token),
                "insiderWashTrading": bool(token.get("tagInsiderWashTrading")),
                "nameZh": safe_dict_get(token.get("nameTranslate"), "zh-CN"),
                "symbolZh": safe_dict_get(token.get("symbolTranslate"), "zh-CN")
            })
    return tokens_list

# ============================================================
# 迁移 Token 列表管理
# ============================================================

def _is_migrated_qualified(token: dict, now: float) -> bool:
    """判断 token 是否符合迁移展示条件：已迁移 + 市值>20k + 24h 内创建。"""
    if not isinstance(token, dict):
        return False
    # 已迁移：migrateStatus == 1
    if token.get("migrateStatus") != 1:
        return False
    # 市值 > 20k
    mc = get_token_market_cap(token)
    if mc < MIGRATED_MIN_MARKETCAP:
        return False
    # 创建时间 24h 内
    create_time = get_token_create_time(token)
    if create_time <= 0:
        return False
    age_hours = (now - create_time) / 3600.0
    if age_hours > MIGRATED_MAX_AGE_HOURS:
        return False
    return True

def update_migrated_tokens(updated_contracts: list, now: float = None):
    """根据本次更新的 contract 列表，增量更新迁移 token 缓存。
    - 符合条件的加入/更新
    - 不再符合条件的移除
    """
    if now is None:
        now = time.time()
    with DATA_LOCK:
        for contract in updated_contracts:
            token = token_map.get(contract)
            if not token:
                continue
            if _is_migrated_qualified(token, now):
                # 提取展示所需字段
                create_time = get_token_create_time(token)
                migrate_time = token.get("migrateTime") or 0
                # migrateTime 可能是字符串/整数/浮点，统一转 float
                try:
                    migrate_time = float(migrate_time)
                except (TypeError, ValueError):
                    migrate_time = 0
                # 毫秒转秒
                if migrate_time > 1e12:
                    migrate_time = migrate_time / 1000.0
                migrated_tokens[contract] = {
                    "contract": contract,
                    "symbol": token.get("symbol", ""),
                    "icon": token.get("icon", ""),
                    "marketCap": token.get("marketCap", 0),
                    "kolHolders": token.get("kolHolders"),
                    "createTime": create_time,
                    "migrateTime": migrate_time,
                }
            else:
                # 不符合条件 → 如果之前在列表里则移除
                migrated_tokens.pop(contract, None)
        # 定期清理过期的（超过 24h 的）
        # 只在有更新时清理，避免空转
        if updated_contracts:
            expired = []
            for contract, snap in migrated_tokens.items():
                age_hours = (now - snap.get("createTime", 0)) / 3600.0
                if age_hours > MIGRATED_MAX_AGE_HOURS:
                    expired.append(contract)
            for c in expired:
                migrated_tokens.pop(c, None)

def get_migrated_tokens_sorted() -> list:
    """返回按迁移时间排序的迁移 token 列表（最近的在最前，从左到右时间从近到远）。
    最多返回 MIGRATED_MAX_DISPLAY 个。
    """
    with DATA_LOCK:
        items = list(migrated_tokens.values())
    # 按 migrateTime 降序（最近的在最前）
    items.sort(key=lambda x: x.get("migrateTime", 0), reverse=True)
    # 最多展示 8 个
    return items[:MIGRATED_MAX_DISPLAY]

# ============================================================
# 占位卡片推送（仅在 NEW Token 首次发现 Twitter 时调用）
# ============================================================

async def push_placeholder_tweet(tweet_id: str, target_url: str):
    tweet_id = str(tweet_id)
    tokens = build_tokens_for_tweet(tweet_id)
    placeholder = {
        "tweet_id": tweet_id,
        "tweetId": tweet_id,
        "tokens": tokens,
        "text": "",
        "author": {
            "name": "加载中...",
            "handle": "",
            "profileImgUrl": "",
            "isBlueVerified": 0
        },
        "pending": True,
        "trigger_count": tweet_trigger_count.get(tweet_id, 0),  # 第 N 次触发建卡
        "timestamp": int(time.time() * 1000),
        "createdAt": int(time.time() * 1000)
    }
    success = await asyncio.to_thread(push_tweet, placeholder, target_url)
    if success:
        # 占位卡片已在 server 端创建，立即标记为已推送，避免后续 token_update 被守卫跳过
        pushed_tweet_ids.add(tweet_id)
        logger.info(f"🚀 占位卡片已推送 tweet_id={tweet_id} (第 {tweet_trigger_count.get(tweet_id, 0)} 次触发)")
    else:
        logger.warning(f"⚠️ 占位卡片推送失败 tweet_id={tweet_id}")

# ============================================================
# Process Twitter (完整推文)
# ============================================================

def is_english_text(text: str) -> bool:
    """判断文本是否主要为英文（含 ASCII 字母占比 > 60%）。"""
    if not text:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    ascii_count = sum(1 for c in letters if ord(c) < 128)
    return (ascii_count / len(letters)) > 0.6

async def fetch_translation_retry(tweet_id: str, target_url: str, tweet_obj: dict):
    """
    非阻塞重试获取翻译：若推文内容是英文但没有翻译，重试 API 最多 2 次。
    同时处理嵌套推文（quotedTweet/repliedToTweet/retweetedTweet）的翻译获取。
    重试期间不阻塞主流程；获取到翻译后通过 update_message 推送到前端。
    """
    tweet_id = str(tweet_id)
    # 延迟 1 秒（缩短自 2 秒）后重试，给 API 缓存时间
    await asyncio.sleep(1)

    # 收集需要获取翻译的 tweet_id 列表（主推文 + 嵌套推文）
    need_translation_ids = []
    # 主推文
    main_text = tweet_obj.get("text", "")
    main_trans = tweet_obj.get("textTranslation", "")
    if main_text and not main_trans and is_english_text(main_text):
        need_translation_ids.append(("main", tweet_id))
    # 嵌套推文
    for field in ("quotedTweet", "repliedToTweet", "retweetedTweet"):
        nested = tweet_obj.get(field)
        if nested and isinstance(nested, dict):
            nested_id = str(nested.get("tweetId") or nested.get("tweet_id") or "")
            nested_text = nested.get("text") or nested.get("content") or ""
            nested_trans = nested.get("textTranslation") or nested.get("tweetTextTranslation") or ""
            if nested_id and nested_text and not nested_trans and is_english_text(nested_text):
                need_translation_ids.append((field, nested_id))

    if not need_translation_ids:
        logger.info("🌐 无需翻译重试（所有内容已有翻译或非英文）: tweet_id=%s", tweet_id)
        return

    for attempt in range(1, 3):  # 重试 2 次
        logger.info("🌐 翻译重试 %d/2: tweet_id=%s (需获取 %d 条)", attempt, tweet_id, len(need_translation_ids))
        updated = False
        for field, tid in need_translation_ids:
            # 强制重新请求 API（绕过 DB 缓存）
            api_response = await run_in_twitter_executor(get_tweet_data, tid)
            if not api_response or not api_response.get("data"):
                logger.info("🌐 翻译重试 %d/2 失败（API 无数据）: %s tweet_id=%s", attempt, field, tid)
                continue
            data = api_response.get("data")
            text_translation = data.get("textTranslation", "")
            if text_translation:
                logger.info("🌐 翻译重试 %d/2 成功: %s tweet_id=%s", attempt, field, tid)
                if field == "main":
                    # 主推文翻译
                    save_translation_to_db(tid, {"tweetTextTranslation": text_translation})
                    tweet_obj["textTranslation"] = text_translation
                    tweet_obj["tweetTextTranslation"] = text_translation
                    save_tweet_to_db(tid, api_response)
                else:
                    # 嵌套推文翻译：更新 tweet_obj 中的嵌套推文
                    nested = tweet_obj.get(field)
                    if nested:
                        nested["textTranslation"] = text_translation
                        nested["tweetTextTranslation"] = text_translation
                    save_translation_to_db(tid, {"tweetTextTranslation": text_translation})
                    save_tweet_to_db(tid, api_response)
                updated = True
        if updated:
            # 推送完整 tweet_obj 作为 update
            await asyncio.to_thread(push_tweet, tweet_obj, target_url)
            # 检查是否还有未获取的翻译
            need_translation_ids = [(f, tid) for f, tid in need_translation_ids
                                    if not _has_translation(tweet_obj, f)]
            if not need_translation_ids:
                return
        logger.info("🌐 翻译重试 %d/2 仍有 %d 条未获取翻译", attempt, len(need_translation_ids))
        if attempt < 2:
            # 重试间隔缩短自 3s -> 1.5s：减小延迟同时仍给 API 缓存时间
            await asyncio.sleep(1.5)
    logger.info("🌐 翻译重试 2 次完成: tweet_id=%s", tweet_id)

def _has_translation(tweet_obj, field):
    """检查指定字段（main 或 nested field）是否已有翻译。"""
    if field == "main":
        return bool(tweet_obj.get("textTranslation"))
    nested = tweet_obj.get(field)
    if nested and isinstance(nested, dict):
        return bool(nested.get("textTranslation") or nested.get("tweetTextTranslation"))
    return True  # 嵌套不存在 → 视为已有翻译（无需获取）

def _register_nested_tweet_ids(tweet_obj: dict, main_tweet_id: str):
    """递归遍历推文的嵌套推文（quotedTweet/repliedToTweet/retweetedTweet），
    建立嵌套 tweet_id → 主推文 tweet_id 的映射。
    这样 OLD Token 如果关联的是嵌套推文的 tweet_id，也能通过映射找到主推文卡片。
    """
    def _collect(tweet, depth):
        if not tweet or not isinstance(tweet, dict) or depth > 3:
            return
        for field in ("quotedTweet", "repliedToTweet", "retweetedTweet"):
            nested = tweet.get(field)
            if nested and isinstance(nested, dict):
                nested_id = str(nested.get("tweetId") or nested.get("tweet_id") or "")
                if nested_id and nested_id != main_tweet_id:
                    nested_to_parent[nested_id].add(main_tweet_id)
                    logger.debug("🔗 嵌套推文映射: %s -> 主推文 %s", nested_id, main_tweet_id)
                _collect(nested, depth + 1)
    _collect(tweet_obj, 0)

async def process_twitter_for_token(tweet_id: str, target_url: str) -> bool:
    tweet_id = str(tweet_id)
    logger.info("🐦 执行推文推送: tweet_id=%s", tweet_id)
    api_response = await run_in_twitter_executor(get_tweet_cached, tweet_id)
    if not api_response:
        logger.warning("❌ Twitter API 获取失败 | tweet_id=%s", tweet_id)
        return False
    data = api_response.get("data")
    if not data:
        logger.warning("❌ API 响应缺少 data 字段 | tweet_id=%s", tweet_id)
        return False
    tweet_obj = parse_tweet(data)
    if not tweet_obj:
        logger.warning("❌ 推文解析失败 | tweet_id=%s", tweet_id)
        return False
    tokens_list = build_tokens_for_tweet(tweet_id)
    tweet_obj["tokens"] = tokens_list
    final_tweet_id = tweet_obj.get("tweet_id") or tweet_obj.get("tweetId") or tweet_id
    final_tweet_id = str(final_tweet_id)
    tweet_obj["tweet_id"] = final_tweet_id
    tweet_obj["tweetId"] = final_tweet_id
    # 附加触发次数（前端显示"第 N 次推送"）
    tweet_obj["trigger_count"] = tweet_trigger_count.get(final_tweet_id, 0)

    # 建立嵌套推文 tweet_id → 主推文 tweet_id 的映射
    # 这样 OLD Token 如果关联的是嵌套推文的 tweet_id，也能被加到主推文卡片
    _register_nested_tweet_ids(tweet_obj, final_tweet_id)

    # 从 DB 加载已缓存的翻译数据并合并到推文对象
    cached_translation = get_translation_from_db(final_tweet_id)
    if cached_translation:
        if cached_translation.get("tweetTextTranslation"):
            tweet_obj["tweetTextTranslation"] = cached_translation["tweetTextTranslation"]
            logger.info("🌐 卡片创建时合并 DB 缓存翻译: tweet_id=%s (tweetTextTranslation)", final_tweet_id)
        if cached_translation.get("referencedTextTranslation"):
            nested = tweet_obj.get("quotedTweet") or tweet_obj.get("repliedToTweet") or tweet_obj.get("retweetedTweet")
            if nested:
                nested["referencedTextTranslation"] = cached_translation["referencedTextTranslation"]
                logger.info("🌐 卡片创建时合并 DB 缓存翻译: tweet_id=%s (referencedTextTranslation)", final_tweet_id)
        if cached_translation.get("article"):
            if not tweet_obj.get("article"):
                tweet_obj["article"] = {}
            art = cached_translation["article"]
            if art.get("title"): tweet_obj["article"]["title"] = art["title"]
            if art.get("titleTranslation"): tweet_obj["article"]["titleTranslation"] = art["titleTranslation"]
            if art.get("previewText"): tweet_obj["article"]["previewText"] = art["previewText"]
            if art.get("previewTextTranslation"): tweet_obj["article"]["previewTextTranslation"] = art["previewTextTranslation"]
            if art.get("coverImgUrl"): tweet_obj["article"]["coverImgUrl"] = art["coverImgUrl"]
            logger.info("🌐 卡片创建时合并 DB 缓存翻译: tweet_id=%s (article)", final_tweet_id)

    # 映射 textTranslation → tweetTextTranslation（前端使用）
    text_trans = tweet_obj.get("textTranslation", "")
    if text_trans:
        tweet_obj["tweetTextTranslation"] = text_trans
    # 为嵌套推文也映射 textTranslation → tweetTextTranslation
    for field in ("quotedTweet", "repliedToTweet", "retweetedTweet"):
        nested = tweet_obj.get(field)
        if nested and isinstance(nested, dict):
            nested_trans = nested.get("textTranslation", "")
            if nested_trans:
                nested["tweetTextTranslation"] = nested_trans

    logger.info("📦 tweet_id=%s 当前关联 %d 个 Token", tweet_id, len(tokens_list))
    success = await asyncio.to_thread(push_tweet, tweet_obj, target_url)
    if success:
        pushed_tweet_ids.add(tweet_id)
        logger.info("✅ 推送成功 tweet_id=%s，包含 %d 个 token", tweet_id, len(tokens_list))
        # 检查是否需要重试获取翻译：主推文或嵌套推文是英文但无翻译
        need_retry = False
        # 主推文
        tweet_lang = tweet_obj.get("lang", "en")
        tweet_text = tweet_obj.get("text", "")
        if tweet_lang == "en" and not text_trans and is_english_text(tweet_text):
            need_retry = True
        # 嵌套推文
        for field in ("quotedTweet", "repliedToTweet", "retweetedTweet"):
            nested = tweet_obj.get(field)
            if nested and isinstance(nested, dict):
                nested_text = nested.get("text") or nested.get("content") or ""
                nested_trans = nested.get("textTranslation") or nested.get("tweetTextTranslation") or ""
                nested_id = str(nested.get("tweetId") or nested.get("tweet_id") or "")
                if nested_id and nested_text and not nested_trans and is_english_text(nested_text):
                    need_retry = True
                    break
        if need_retry:
            logger.info("🌐 推文或嵌套推文为英文但无翻译，调度非阻塞重试: tweet_id=%s", tweet_id)
            asyncio.create_task(fetch_translation_retry(tweet_id, target_url, tweet_obj))
    else:
        logger.warning("⚠️ 推送失败 tweet_id=%s，后续允许再次尝试", tweet_id)
    return success

# ============================================================
# Schedule Push (推文)
# ============================================================

async def schedule_tweet_push(tweet_id: str, target_url: str):
    tweet_id = str(tweet_id)

    existing_task = pending_push_tasks.get(tweet_id)
    if existing_task and not existing_task.done():
        logger.debug("⏳ tweet_id 已在推送队列: %s", tweet_id)
        return
    if existing_task and existing_task.done():
        pending_push_tasks.pop(tweet_id, None)

    # 递增触发计数（每次真正进入推送流程都算一次，前端显示"第 N 次推送"）
    tweet_trigger_count[tweet_id] += 1
    current_count = tweet_trigger_count[tweet_id]
    _mark_trigger_dirty(tweet_id)  # 标记待持久化
    logger.info("📊 tweet_id=%s 触发建卡 第 %d 次", tweet_id, current_count)

    # ============================================================
    # 缓存命中快路径：先做一次同步 SQLite 探测（~1ms，纯本地读）
    # 对高复用 Twitter 账号的批量刷图场景（同一 tweet_id 被几十个新 token 引用），
    # 后续 token 推送时 SQLite 已命中，此时走"占位卡 → 异步补全"会白白多花
    # 一轮 emit + DOM patch。命中缓存就直接同步构建并推送完整卡片。
    # ============================================================
    cached = get_tweet_from_db(tweet_id)
    if cached and cached.get("data"):
        # 缓存命中：标记已发送占位（避免后续重复触发），直接同步推完整卡片
        placeholder_sent.add(tweet_id)
        logger.info("⚡ SQLite 缓存命中，跳过占位卡直接推完整卡片: tweet_id=%s", tweet_id)
        try:
            await process_twitter_for_token(tweet_id, target_url)
        except Exception:
            logger.exception("❌ 缓存命中快路径推送异常: %s", tweet_id)
        return

    # ============================================================
    # 缓存未命中慢路径：需要等 Binance API，走"占位卡 + 异步补全"
    # ============================================================
    if tweet_id not in placeholder_sent:
        placeholder_sent.add(tweet_id)
        await push_placeholder_tweet(tweet_id, target_url)

    async def push_full_tweet():
        try:
            logger.info("🚀 立即执行完整推文推送: tweet_id=%s", tweet_id)
            await process_twitter_for_token(tweet_id, target_url)
        except asyncio.CancelledError:
            logger.info("🛑 推送任务取消: %s", tweet_id)
            raise
        except Exception:
            logger.exception("❌ 完整推文推送异常: %s", tweet_id)
        finally:
            current_task = asyncio.current_task()
            existing = pending_push_tasks.get(tweet_id)
            if existing is current_task:
                pending_push_tasks.pop(tweet_id, None)

    task = asyncio.create_task(push_full_tweet())
    pending_push_tasks[tweet_id] = task
    logger.info("🚀 调度完整推文推送 tweet_id=%s (异步，等待 API)", tweet_id)

# ============================================================
# Build Profile Tweet
# ============================================================

def build_profile_tweet(profile_data: dict, tokens: list) -> Optional[dict]:
    data = profile_data.get("data")
    if not data:
        return None
    twitter_id = data.get("twitterId")
    handle = data.get("handle")
    if not twitter_id and not handle:
        return None
    tweet_id = f"profile_{handle}" if handle else f"profile_{twitter_id}"
    author = {
        "name": data.get("name", "Unknown"),
        "handle": handle or "",
        "profileImgUrl": data.get("profileImgUrl", ""),
        "profileBannerUrl": data.get("profileBannerUrl", ""),
        "isBlueVerified": data.get("blueVerified", 0),
        "description": data.get("description", ""),
        "descriptionTranslation": data.get("descriptionTranslation", ""),
        "location": data.get("location", ""),
        "twitterId": twitter_id or "",
        "followersCnt": data.get("followersCnt", 0),
        "followingCnt": data.get("followingCnt", 0)
    }
    return {
        "tweet_id": tweet_id,
        "tweetId": tweet_id,
        "text": data.get("descriptionTranslation") or data.get("description", ""),
        "content": data.get("descriptionTranslation") or data.get("description", ""),
        "lang": "zh" if data.get("descriptionTranslation") else "en",
        "tweetType": "profile",
        "createdAt": data.get("createdAt", int(time.time() * 1000)),
        "timestamp": data.get("createdAt", int(time.time() * 1000)),
        "author": author,
        "name": author["name"],
        "handle": author["handle"],
        "avatar": author["profileImgUrl"],
        "profileImgUrl": author["profileImgUrl"],
        "isBlueVerified": author["isBlueVerified"],
        "likeCnt": 0,
        "retweetCnt": 0,
        "replyCnt": 0,
        "quoteCnt": 0,
        "imgUrls": [],
        "videoUrls": [],
        "article": None,
        "quotedTweet": None,
        "repliedToTweet": None,
        "retweetedTweet": None,
        "tokens": tokens
    }

# ============================================================
# Schedule Weibo Push (微博)
# ============================================================

async def schedule_weibo_push(weibo_id: str, target_url: str):
    """微博帖子建卡。weibo_id 格式："weibo:123456" """
    post_id = weibo_id.split(":", 1)[1] if ":" in weibo_id else weibo_id
    tweet_id = f"weibo_{post_id}"
    existing_task = pending_push_tasks.get(tweet_id)
    if existing_task and not existing_task.done():
        logger.debug("⏳ weibo 已在推送队列: %s", post_id)
        return
    if existing_task and existing_task.done():
        pending_push_tasks.pop(tweet_id, None)

    tweet_trigger_count[tweet_id] += 1
    _mark_trigger_dirty(tweet_id)
    logger.info("📊 weibo_id=%s 触发建卡 第 %d 次", tweet_id, tweet_trigger_count[tweet_id])

    async def push_weibo():
        try:
            weibo_data = await run_in_twitter_executor(get_weibo_content, post_id)
            if not weibo_data:
                logger.warning("❌ 获取 Weibo 数据失败 id=%s", post_id)
                return
            tokens = build_tokens_for_tweet(tweet_id)
            tweet_obj = build_weibo_tweet(weibo_data, tokens)
            if not tweet_obj:
                logger.warning("❌ 构建 Weibo Tweet 失败 id=%s", post_id)
                return
            tweet_obj["trigger_count"] = tweet_trigger_count.get(tweet_id, 0)
            success = await asyncio.to_thread(push_tweet, tweet_obj, target_url)
            if success:
                logger.info("✅ Weibo 推送成功 id=%s, 包含 %d 个 token", post_id, len(tokens))
                pushed_tweet_ids.add(tweet_id)
            else:
                logger.warning("⚠️ Weibo 推送失败 id=%s", post_id)
        except asyncio.CancelledError:
            logger.info("🛑 Weibo 推送任务取消: %s", post_id)
            raise
        except Exception:
            logger.exception("❌ Weibo 推送异常: %s", post_id)
        finally:
            current_task = asyncio.current_task()
            if pending_push_tasks.get(tweet_id) is current_task:
                pending_push_tasks.pop(tweet_id, None)

    task = asyncio.create_task(push_weibo())
    pending_push_tasks[tweet_id] = task
    logger.info("🚀 调度 Weibo 推送 id=%s", post_id)

# ============================================================
# Schedule Square Push (币安广场)
# ============================================================

async def schedule_square_push(square_id: str, target_url: str):
    """币安广场 post/article 建卡。
    square_id 格式："square:123456"
    """
    content_id = square_id.split(":", 1)[1] if ":" in square_id else square_id
    tweet_id = f"square_{content_id}"
    existing_task = pending_push_tasks.get(tweet_id)
    if existing_task and not existing_task.done():
        logger.debug("⏳ square 已在推送队列: %s", content_id)
        return
    if existing_task and existing_task.done():
        pending_push_tasks.pop(tweet_id, None)

    tweet_trigger_count[tweet_id] += 1
    current_count = tweet_trigger_count[tweet_id]
    _mark_trigger_dirty(tweet_id)
    logger.info("📊 square_id=%s 触发建卡 第 %d 次", tweet_id, current_count)

    async def push_square():
        try:
            square_data = await run_in_twitter_executor(get_binance_square_content, content_id)
            if not square_data:
                logger.warning("❌ 获取 Binance Square 数据失败 id=%s", content_id)
                return
            tokens = build_tokens_for_tweet(tweet_id)
            tweet_obj = build_square_tweet(square_data, tokens)
            if not tweet_obj:
                logger.warning("❌ 构建 Square Tweet 失败 id=%s", content_id)
                return
            tweet_obj["trigger_count"] = tweet_trigger_count.get(tweet_id, 0)
            success = await asyncio.to_thread(push_tweet, tweet_obj, target_url)
            if success:
                logger.info("✅ Square 推送成功 id=%s, 包含 %d 个 token", content_id, len(tokens))
                pushed_tweet_ids.add(tweet_id)
            else:
                logger.warning("⚠️ Square 推送失败 id=%s", content_id)
        except asyncio.CancelledError:
            logger.info("🛑 Square 推送任务取消: %s", content_id)
            raise
        except Exception:
            logger.exception("❌ Square 推送异常: %s", content_id)
        finally:
            current_task = asyncio.current_task()
            if pending_push_tasks.get(tweet_id) is current_task:
                pending_push_tasks.pop(tweet_id, None)

    task = asyncio.create_task(push_square())
    pending_push_tasks[tweet_id] = task
    logger.info("🚀 调度 Square 推送 id=%s", content_id)

# ============================================================
# Schedule Profile Push
# ============================================================

async def schedule_profile_push(handle: str, target_url: str):
    profile_id = f"profile_{handle}"
    existing_task = pending_push_tasks.get(profile_id)
    if existing_task and not existing_task.done():
        logger.debug("⏳ profile 已在推送队列: %s", handle)
        return
    if existing_task and existing_task.done():
        pending_push_tasks.pop(profile_id, None)

    # 递增触发计数（profile 卡片复用同一统计字段）
    tweet_trigger_count[profile_id] += 1
    current_count = tweet_trigger_count[profile_id]
    _mark_trigger_dirty(profile_id)  # 标记待持久化
    logger.info("📊 profile_id=%s 触发建卡 第 %d 次", profile_id, current_count)

    # 检查是否是 broadcast 链接
    is_broadcast = handle.startswith("broadcast:")
    broadcast_id = handle.split(":", 1)[1] if is_broadcast else None

    async def push_profile():
        try:
            if is_broadcast:
                # broadcast 不需要查 Profile API，直接构建占位卡片
                tweet_obj = {
                    "tweet_id": profile_id,
                    "tweetId": profile_id,
                    "text": f"🔴 Twitter Live Broadcast\nhttps://x.com/i/broadcasts/{broadcast_id}",
                    "lang": "en",
                    "tweetType": "live",
                    "timestamp": int(time.time() * 1000),
                    "createdAt": int(time.time() * 1000),
                    "author": {"name": "Twitter Live", "handle": "", "profileImgUrl": "", "isBlueVerified": 0},
                    "name": "Twitter Live",
                    "handle": "",
                    "avatar": "",
                    "profileImgUrl": "",
                    "isBlueVerified": 0,
                    "likeCnt": 0, "retweetCnt": 0, "replyCnt": 0, "quoteCnt": 0,
                    "imgUrls": [], "videoUrls": [], "article": None,
                    "quotedTweet": None, "repliedToTweet": None, "retweetedTweet": None,
                    "is_live": True,
                    "broadcast_id": broadcast_id,
                }
            else:
                profile_data = await run_in_twitter_executor(get_profile_cached, handle)
                if not profile_data:
                    logger.warning("❌ 获取 Profile 数据失败 handle=%s", handle)
                    return
                tweet_obj = build_profile_tweet(profile_data, None)
                if not tweet_obj:
                    logger.warning("❌ 构建 Profile Tweet 失败 handle=%s", handle)
                    return
            tokens = build_tokens_for_tweet(profile_id)
            tweet_obj["tokens"] = tokens
            # 附加触发次数
            tweet_obj["trigger_count"] = tweet_trigger_count.get(profile_id, 0)
            success = await asyncio.to_thread(push_tweet, tweet_obj, target_url)
            if success:
                logger.info("✅ Profile 推送成功 handle=%s, 包含 %d 个 token", handle, len(tokens))
                pushed_tweet_ids.add(profile_id)
            else:
                logger.warning("⚠️ Profile 推送失败 handle=%s", handle)
        except asyncio.CancelledError:
            logger.info("🛑 Profile 推送任务取消: %s", handle)
            raise
        except Exception:
            logger.exception("❌ Profile 推送异常: %s", handle)
        finally:
            current_task = asyncio.current_task()
            if pending_push_tasks.get(profile_id) is current_task:
                pending_push_tasks.pop(profile_id, None)

    task = asyncio.create_task(push_profile())
    pending_push_tasks[profile_id] = task
    logger.info("🚀 调度 Profile 推送 handle=%s", handle)

# ============================================================
# UPDATE Token (核心：处理更新并推送数据变化)
# ============================================================

async def handle_update_token(data, target_url):
    if not isinstance(data, dict):
        return
    d = data.get("data", {}).get("d", [])
    if not d:
        return

    # 1. 合并 Token 数据，获取被更新的合约列表
    updated_contracts = update_token_map(d, source="UPDATE")
    if not updated_contracts:
        logger.debug("ℹ️ 本次 UPDATE 无实际数据变化，跳过推送")
        return

    now = time.time()
    actions = []          # tweet_id 列表（仅来自 NEW Token 首次发现 Twitter，用于触发新建卡片）
    profile_actions = []  # (handle, contract) 列表（仅来自 NEW Token 首次发现 Handle）

    # 2. 逐个处理被更新的合约：
    #    - NEW Token（在 pending 中）：检测 Twitter 关联，首次发现时触发卡片创建
    #    - OLD Token（不在 pending 中）：绝不触发卡片创建，仅依赖 step 5 推送 token_update 增量
    #      如果该 OLD Token 关联的 tweet_id 已有卡片（被某个 NEW Token 创建），
    #      token_update 会把它的数据插入到该卡片的 token 列表；否则数据仅保留在 token_map，
    #      等待未来某个 NEW Token 触发同一 tweet_id 的卡片创建时一并带上。
    with DATA_LOCK:
        for contract in updated_contracts:
            pending_info = pending_new_tokens.get(contract)
            if pending_info is not None:
                # -------- NEW Token 分支 --------
                first_seen = pending_info.get("first_seen", now)
                if now - first_seen >= NEW_TOKEN_PENDING_TTL:
                    pending_new_tokens.pop(contract, None)
                    logger.info("⌛ NEW Token 等待 Twitter 超时，不再检测: %s", contract)
                    continue
                if contract in twitter_query_done:
                    logger.debug("⏭️ NEW Token 已发现 Twitter，仅继续更新数据: %s", contract)
                    continue
                token = token_map.get(contract)
                if not token:
                    continue
                # 建卡前过滤：如果 token 已知不符合显示条件（如买卖税>3%），不触发建卡
                # 注意：此时 token 可能仍在 grace period 内，但税字段如果已有且>3%就直接跳过
                if not should_token_be_displayed(contract, now):
                    pending_new_tokens.pop(contract, None)
                    logger.info("⏭️ NEW Token 不符合显示条件（税>3pct 或已移除），跳过建卡: %s", contract)
                    continue
                tweet_id = extract_tweet_id_from_token(token)
                if tweet_id:
                    tweet_id = str(tweet_id)
                    twitter_query_done[contract] = tweet_id
                    tweet_tokens[tweet_id].add(contract)
                    token_tweets[contract].add(tweet_id)
                    pending_new_tokens.pop(contract, None)
                    _gmgn_stats_inc("binance_won")
                    logger.info("🐦 NEW Token 首次发现 Tweet ID，触发卡片创建: %s -> %s", contract, tweet_id)
                    actions.append(tweet_id)
                    continue
                handle = extract_twitter_handle_from_token(token)
                if handle:
                    handle = handle.strip()
                    profile_id = f"profile_{handle}"
                    twitter_query_done[contract] = profile_id
                    tweet_tokens[profile_id].add(contract)
                    token_tweets[contract].add(profile_id)
                    pending_new_tokens.pop(contract, None)
                    _gmgn_stats_inc("binance_won")
                    logger.info("📱 NEW Token 首次发现 Twitter Handle，触发 Profile 卡片创建: %s -> %s", contract, handle)
                    profile_actions.append((handle, contract))
                    continue
                # 没有 Twitter → 检查币安广场链接
                square_id = extract_binance_square_from_token(token)
                if square_id:
                    square_id = square_id.strip()
                    tweet_id_sq = f"square_{square_id.split(':', 1)[1] if ':' in square_id else square_id}"
                    twitter_query_done[contract] = tweet_id_sq
                    tweet_tokens[tweet_id_sq].add(contract)
                    token_tweets[contract].add(tweet_id_sq)
                    pending_new_tokens.pop(contract, None)
                    _gmgn_stats_inc("binance_won")
                    logger.info("🟦 NEW Token 首次发现 Binance Square，触发卡片创建: %s -> %s", contract, square_id)
                    actions.append(tweet_id_sq)
                    continue
                # 没有 Twitter/Square → 检查微博链接
                weibo_id = extract_weibo_from_token(token)
                if weibo_id:
                    weibo_id = weibo_id.strip()
                    post_id = weibo_id.split(":", 1)[1] if ":" in weibo_id else weibo_id
                    tweet_id_wb = f"weibo_{post_id}"
                    twitter_query_done[contract] = tweet_id_wb
                    tweet_tokens[tweet_id_wb].add(contract)
                    token_tweets[contract].add(tweet_id_wb)
                    pending_new_tokens.pop(contract, None)
                    _gmgn_stats_inc("binance_won")
                    logger.info("🔴 NEW Token 首次发现微博链接，触发卡片创建: %s -> %s", contract, weibo_id)
                    actions.append(tweet_id_wb)
                    continue
                logger.debug("⏳ NEW Token UPDATE 暂无 Tweet ID / Handle / Square / Weibo，继续等待: %s", contract)
            else:
                # -------- OLD Token 分支 --------
                # OLD Token 绝不触发建卡。仅推送 token_update 到已存在卡片。
                # 例外：观察期恢复的 token 如果卡片已不存在，可以重新建卡
                removed_ts = removed_low_mc_tokens.get(contract)
                if removed_ts is not None and (now - removed_ts) <= REMOVED_OBSERVATION_TTL:
                    # 在观察期内：检查是否恢复显示 + 卡片是否存在
                    if should_token_be_displayed(contract, now):
                        token = token_map.get(contract)
                        if token:
                            tweet_id = extract_tweet_id_from_token(token)
                            if tweet_id:
                                tweet_id = str(tweet_id)
                                tweet_tokens[tweet_id].add(contract)
                                token_tweets[contract].add(tweet_id)
                                if tweet_id not in pushed_tweet_ids:
                                    logger.info("♻️ 观察期恢复的 Token，推文卡片不存在，重新建卡: %s -> %s", contract, tweet_id)
                                    actions.append(tweet_id)
                                    continue
                logger.debug("🔄 OLD Token %s 数据更新，仅推送增量到已存在卡片（不新建）", contract)

    # 3. 触发卡片创建（推文 / 币安广场 / 微博）
    for tweet_id in actions:
        if tweet_id.startswith("square_"):
            content_id = tweet_id.replace("square_", "", 1)
            await schedule_square_push(content_id, target_url)
        elif tweet_id.startswith("weibo_"):
            post_id = tweet_id.replace("weibo_", "", 1)
            await schedule_weibo_push(post_id, target_url)
        else:
            await schedule_tweet_push(tweet_id, target_url)

    # 4. 仅对 NEW Token 首次发现 Handle 的情况，触发 Profile 卡片创建
    for handle, contract in profile_actions:
        await schedule_profile_push(handle, target_url)

    # 5. 对所有被更新合约关联的 tweet_id 推送 token_update 增量（不创建卡片）
    #    包括直接关联的 tweet_id + 通过嵌套推文映射关联的主推文 tweet_id
    all_affected_tweet_ids = set()
    skipped = 0
    with DATA_LOCK:
        for contract in updated_contracts:
            for tweet_id in token_tweets.get(contract, set()):
                if tweet_id in pushed_tweet_ids:
                    all_affected_tweet_ids.add(tweet_id)
                else:
                    # 该 tweet_id 没有直接建卡，检查是否是某个主推文的嵌套推文
                    parent_ids = nested_to_parent.get(tweet_id)
                    if parent_ids:
                        for pid in parent_ids:
                            if pid in pushed_tweet_ids:
                                all_affected_tweet_ids.add(pid)
                            else:
                                skipped += 1
                    else:
                        skipped += 1
    if skipped:
        logger.debug("⏭️ 跳过 %d 个未创建卡片的 tweet_id 的 token_update 推送", skipped)

    for tweet_id in all_affected_tweet_ids:
        asyncio.create_task(push_token_update(tweet_id, target_url))

    # 更新迁移 token 列表（从本次更新的 contract 中提取符合条件的）
    update_migrated_tokens(updated_contracts, now)

    logger.info("📨 Token 更新通知已发送，影响 %d 个已存在卡片（跳过 %d 个未创建卡片）",
                len(all_affected_tweet_ids), skipped)

# ============================================================
# Subscribe Response
# ============================================================

def is_subscribe_response(data) -> bool:
    return isinstance(data, dict) and "result" in data and "id" in data

# ============================================================
# WebSocket
# ============================================================

async def listen_with_reconnect(target_url):
    global ws_connected, ws_connection_count, last_ws_message_time
    cleanup_task = asyncio.create_task(pending_cleanup_loop())
    social_flush_task = asyncio.create_task(social_flush_loop())
    stats_writer_task = asyncio.create_task(stats_writer_loop())
    trigger_flush_task = asyncio.create_task(trigger_count_flush_loop())
    migrated_push_task = asyncio.create_task(migrated_tokens_push_loop())
    retry_delay = RETRY_INITIAL
    try:
        while True:
            try:
                logger.info("🔌 正在连接 WebSocket...")
                async with websockets.connect(
                    WS_URI,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=5,
                    max_size=20 * 1024 * 1024
                ) as ws:
                    with DATA_LOCK:
                        ws_connected = True
                        ws_connection_count += 1
                    logger.info("✅ WebSocket 已连接，第 %d 次连接", ws_connection_count)
                    subscribe_msg = {
                        "id": SUBSCRIBE_ID,
                        "method": "SUBSCRIBE",
                        "params": [
                            NEW_TOKENS_STREAM,
                            UPDATE_TOKENS_STREAM,
                            SOCIAL_STREAM,
                            SOCIAL_TRANSLATION_STREAM,
                        ]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info("📡 已发送订阅请求")
                    retry_delay = RETRY_INITIAL
                    async for message in ws:
                        last_ws_message_time = time.time()
                        if not message:
                            continue
                        try:
                            data = json.loads(message)
                        except Exception:
                            logger.warning("❌ JSON 解析失败")
                            continue
                        if is_subscribe_response(data):
                            logger.info("✅ 订阅确认 id=%s", data.get("id"))
                            continue
                        stream = data.get("stream")
                        if not stream:
                            continue
                        if stream == NEW_TOKENS_STREAM:
                            await handle_new_token(data, target_url)
                        elif stream == UPDATE_TOKENS_STREAM:
                            await handle_update_token(data, target_url)
                        elif stream == SOCIAL_STREAM:
                            await handle_social_event(data, target_url)
                        elif stream == SOCIAL_TRANSLATION_STREAM:
                            await handle_social_translation_event(data, target_url)
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                with DATA_LOCK:
                    ws_connected = False
                logger.warning("⚠️ WebSocket 断开: %s", e)
            except Exception as e:
                with DATA_LOCK:
                    ws_connected = False
                logger.exception("❌ WebSocket 异常: %s", e)
            finally:
                with DATA_LOCK:
                    ws_connected = False
            logger.info("🔄 %.1f 秒后重新连接...", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, RETRY_MAX)
    finally:
        cleanup_task.cancel()
        social_flush_task.cancel()
        stats_writer_task.cancel()
        trigger_flush_task.cancel()
        migrated_push_task.cancel()
        for t in (cleanup_task, social_flush_task, stats_writer_task, trigger_flush_task, migrated_push_task):
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

# ============================================================
# Start
# ============================================================

def start_pusher(target_url):
    init_db()
    logger.info("🚀 Pusher 启动")
    logger.info("🎯 Target: %s", target_url)
    logger.info("📡 WS: %s", WS_URI)
    logger.info("🆕 NEW: %s", NEW_TOKENS_STREAM)
    logger.info("🔄 UPDATE: %s", UPDATE_TOKENS_STREAM)
    logger.info("📝 SOCIAL: %s", SOCIAL_STREAM)
    logger.info("🌐 SOCIAL_TRANS: %s", SOCIAL_TRANSLATION_STREAM)
    logger.info("ℹ️ 即时推送模式：占位卡片后立即获取完整推文；无推文则尝试 Handle")
    logger.info("ℹ️ API 请求重试次数: %d", API_RETRY_COUNT)
    logger.info("ℹ️ Token 数据更新将自动推送至前端（零延迟 + in-flight 合并）")
    logger.info("ℹ️ 社交流缓存：每 %.0fs 批量写入 SQLite，队列上限 %d", _SOCIAL_FLUSH_INTERVAL, _SOCIAL_QUEUE_MAXSIZE)
    logger.info("ℹ️ 统计指标：每 %.0fs 写入 %s", STATS_WRITE_INTERVAL, STATS_FILE)
    # 启动持仓轮询线程（前端持仓弹窗数据源）
    start_holdings_polling()
    asyncio.run(listen_with_reconnect(target_url))

if __name__ == "__main__":
    start_pusher(TARGET_APP_URL)
