# -*- coding: utf-8 -*-
"""
Flask WebSocket 实时消息推送系统

功能：
- Flask + SocketIO
- 接收 pusher_v4 推送的 Twitter 数据（含 Tweet 和 Profile）
- 左侧 Twitter 推文 / Profile 卡片
- 右侧 Token 数据
- 图片最大宽度为内容区域 50%
- 关注按钮位于用户名右侧
- 推文正文不保留换行
- 引用 / 回复 / 转推 / Article / 图片 / 视频
- Token age 前端每秒自动刷新
- 无文件监控
- 支持占位卡片更新
- 支持同 ID 新 Token 更新（update_message 事件）
- 支持 Token 增量更新（token_update 事件），避免频繁重绘 / 闪烁
"""

import time
import uuid
import threading
import queue as _stdqueue

# 关键：eventlet monkey_patch 必须在所有其他导入之前调用，否则 eventlet 无法把标准库
# 的阻塞 socket / threading / select 等改成协作式。否则 requests.get 这类阻塞调用
# 会卡死整个 eventlet 事件循环，导致 SocketIO 心跳超时、所有 /api/* 请求堆积。
import eventlet
eventlet.monkey_patch()

from flask import Flask, request, session, jsonify, render_template_string
from flask_socketio import SocketIO, emit, join_room

# 适配 pusher_v4（支持 Tweet + Profile）
import pusher

# ============================================================
# Flask
# ============================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = "your-secret-key-here-change-in-production"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# 模块级 requests.Session，用于 /api/refresh_translation 调用 Binance API
# 在第一次请求时延迟初始化（见 refresh_translation 函数）
_binance_api_session = None

# ============================================================
# Twitter Author Normalize
# ============================================================

def _normalize_author(author, fallback_name="Unknown", fallback_handle="unknown", fallback_avatar=""):
    if isinstance(author, dict):
        return {
            "name": author.get("name") or fallback_name,
            "handle": author.get("handle") or fallback_handle,
            "profileImgUrl": author.get("profileImgUrl") or author.get("avatar") or fallback_avatar,
            "profileBannerUrl": author.get("profileBannerUrl", ""),
            "isBlueVerified": author.get("isBlueVerified", 0),
            "description": author.get("description", ""),
            "location": author.get("location", ""),
            "twitterId": author.get("twitterId", ""),
            "followersCnt": author.get("followersCnt", 0),
            "followingCnt": author.get("followingCnt", 0),
        }
    return {
        "name": author if isinstance(author, str) else fallback_name,
        "handle": fallback_handle,
        "profileImgUrl": fallback_avatar,
        "profileBannerUrl": "",
        "isBlueVerified": 0,
        "description": "",
        "location": "",
        "twitterId": "",
        "followersCnt": 0,
        "followingCnt": 0,
    }

def _normalize_nested(data):
    if not data or not isinstance(data, dict):
        return None
    tweet_id = data.get("tweet_id") or data.get("tweetId") or data.get("id") or ""
    author = _normalize_author(data.get("author"), data.get("name", "Unknown"), data.get("handle", "unknown"), data.get("avatar") or data.get("profileImgUrl", ""))
    ts = data.get("timestamp") or data.get("createdAt") or int(time.time() * 1000)
    try:
        ts = int(ts)
    except Exception:
        ts = int(time.time() * 1000)
    video_urls = data.get("videoUrls") or []
    if not isinstance(video_urls, list):
        video_urls = [video_urls]
    return {
        "tweet_id": tweet_id,
        "tweetId": tweet_id,
        "text": data.get("text") or data.get("content") or "",
        "textTranslation": data.get("textTranslation", ""),
        "tweetTextTranslation": data.get("tweetTextTranslation", ""),
        "lang": data.get("lang", "en"),
        "tweetType": data.get("tweetType") or data.get("tweet_type") or "original",
        "timestamp": ts,
        "createdAt": ts,
        "author": author,
        "name": author["name"],
        "handle": author["handle"],
        "avatar": author["profileImgUrl"],
        "profileImgUrl": author["profileImgUrl"],
        "profileBannerUrl": author.get("profileBannerUrl", ""),
        "isBlueVerified": author["isBlueVerified"],
        "likeCnt": data.get("likeCnt") if data.get("likeCnt") is not None else data.get("likes", 0),
        "retweetCnt": data.get("retweetCnt") if data.get("retweetCnt") is not None else data.get("retweets", 0),
        "replyCnt": data.get("replyCnt") if data.get("replyCnt") is not None else data.get("replies", 0),
        "quoteCnt": data.get("quoteCnt", 0),
        "imgUrls": data.get("imgUrls") or data.get("img_urls") or [],
        "videoUrls": video_urls,
        "article": data.get("article"),
        "quotedTweet": _normalize_nested(data.get("quotedTweet")) if data.get("quotedTweet") else None,
        "repliedToTweet": _normalize_nested(data.get("repliedToTweet")) if data.get("repliedToTweet") else None,
        "retweetedTweet": _normalize_nested(data.get("retweetedTweet")) if data.get("retweetedTweet") else None,
    }

class TweetManager:
    MAX_MESSAGES = 200  # 内存中最多保留 200 条消息，超过则丢弃最老的（防止无限增长导致内存膨胀与查询变慢）

    def __init__(self):
        self.messages = []
        self.follows = {}
        self.id_counter = 1
        self.lock = threading.RLock()
        # tweet_id -> message 索引：O(1) 查找，避免对 self.messages 线性扫描
        # 必须与 self.messages 保持同步：每次 insert/更新/移除都要更新这个 Map
        self._index = {}

    def _build_message(self, data):
        """内部方法：根据 data 构建 message 对象，与 add_message 相同但不插入列表。"""
        author = _normalize_author(data.get("author"), data.get("name") or (data.get("author") if isinstance(data.get("author"), str) else "Unknown"), data.get("handle", "unknown"), data.get("avatar") or data.get("profileImgUrl", ""))
        ts = data.get("timestamp") or data.get("createdAt") or int(time.time() * 1000)
        try:
            ts = int(ts)
        except Exception:
            ts = int(time.time() * 1000)
        video_urls = data.get("videoUrls") or []
        if not isinstance(video_urls, list):
            video_urls = [video_urls]
        tweet_id = str(data.get("tweet_id") or data.get("tweetId") or data.get("id") or "")
        return {
            "id": str(self.id_counter),  # 临时 id，后面会覆盖
            "tweet_id": tweet_id,
            "tweetId": tweet_id,
            "text": data.get("text") or data.get("content") or "",
            "tweetTextTranslation": data.get("tweetTextTranslation", ""),
            "textTranslation": data.get("textTranslation", ""),
            "author": author["name"],
            "handle": author["handle"],
            "avatar": author["profileImgUrl"],
            "profileImgUrl": author["profileImgUrl"],
            "profileBannerUrl": author.get("profileBannerUrl", ""),
            "isBlueVerified": author.get("isBlueVerified", 0),
            "timestamp": ts,
            "createdAt": ts,
            "lang": data.get("lang", "en"),
            "is_quoted": data.get("is_quoted", False),
            "has_article": bool(data.get("article") or data.get("has_article")),
            "has_video": bool(video_urls or data.get("has_video")),
            "likeCnt": data.get("likeCnt") if data.get("likeCnt") is not None else data.get("likes", 0),
            "retweetCnt": data.get("retweetCnt") if data.get("retweetCnt") is not None else data.get("retweets", 0),
            "replyCnt": data.get("replyCnt") if data.get("replyCnt") is not None else data.get("replies", 0),
            "quoteCnt": data.get("quoteCnt", 0),
            "tweetType": data.get("tweetType") or data.get("tweet_type") or "original",
            "description": author.get("description") or data.get("description", ""),
            "location": author.get("location") or data.get("location", ""),
            "imgUrls": data.get("imgUrls") or data.get("img_urls") or [],
            "videoUrls": video_urls,
            "article": data.get("article"),
            "quotedTweet": _normalize_nested(data.get("quotedTweet")) if data.get("quotedTweet") else None,
            "repliedToTweet": _normalize_nested(data.get("repliedToTweet")) if data.get("repliedToTweet") else None,
            "retweetedTweet": _normalize_nested(data.get("retweetedTweet")) if data.get("retweetedTweet") else None,
            "authorObj": author,
            "tokens": data.get("tokens", []),
            "pending": data.get("pending", False),
            "trigger_count": data.get("trigger_count", 0)  # 第 N 次触发建卡（来自 pusher）
        }

    def add_message(self, data):
        with self.lock:
            message = self._build_message(data)
            message["id"] = str(self.id_counter)
            tweet_id = str(message.get("tweet_id") or "")
            self.messages.insert(0, message)
            if tweet_id:
                self._index[tweet_id] = message
            self.id_counter += 1
            # 修剪：超过上限则从尾部移除最老的消息（防止内存无限增长）
            if len(self.messages) > self.MAX_MESSAGES:
                # 仅移除未被关注的旧消息，避免误删用户关注的内容
                # 关注列表内的消息优先保留：按 tweet_id 集合过滤
                overflow = len(self.messages) - self.MAX_MESSAGES
                # 收集所有会话关注的 tweet_id
                followed_ids = set()
                for ids in self.follows.values():
                    followed_ids.update(ids)
                # 从尾部开始扫描移除（最老的）
                removed = 0
                # 从后往前找非关注的消息移除
                i = len(self.messages) - 1
                while i >= 0 and removed < overflow:
                    old_msg = self.messages[i]
                    old_tid = str(old_msg.get("tweet_id") or "")
                    if old_tid not in followed_ids:
                        self.messages.pop(i)
                        # 同步从索引中移除（仅当索引仍指向这条消息时）
                        if self._index.get(old_tid) is old_msg:
                            self._index.pop(old_tid, None)
                        removed += 1
                    i -= 1
                # 如果关注消息过多导致无法移除足够数量，强制再裁剪尾部
                if removed < overflow:
                    drop_count = overflow - removed
                    dropped = self.messages[self.MAX_MESSAGES:]
                    del self.messages[self.MAX_MESSAGES:]
                    for m in dropped:
                        old_tid = str(m.get("tweet_id") or "")
                        if self._index.get(old_tid) is m:
                            self._index.pop(old_tid, None)
            return message

    def update_message(self, data):
        """更新已有消息，返回更新后的消息；若不存在则返回 None。"""
        with self.lock:
            tweet_id = str(data.get("tweet_id") or data.get("tweetId") or data.get("id") or "")
            # O(1) 查找：优先走索引
            old_msg = self._index.get(tweet_id)
            if old_msg is None:
                return None
            try:
                idx = self.messages.index(old_msg)
            except ValueError:
                return None
            new_msg = self._build_message(data)
            new_msg["id"] = old_msg["id"]  # 保留原 id
            self.messages[idx] = new_msg
            self._index[tweet_id] = new_msg
            return new_msg

    def update_tokens(self, tweet_id: str, tokens: list) -> bool:
        """仅更新指定消息的 tokens 列表，返回是否成功。"""
        with self.lock:
            tweet_id = str(tweet_id)
            # O(1) 查找：优先走索引
            msg = self._index.get(tweet_id)
            if msg is None:
                return False
            msg["tokens"] = tokens
            return True

    def get_messages(self, limit=50):
        with self.lock:
            return list(self.messages[:limit])

    def toggle_follow(self, session_id, tweet_id):
        with self.lock:
            if session_id not in self.follows:
                self.follows[session_id] = set()
            followed = self.follows[session_id]
            tweet_id = str(tweet_id)
            if tweet_id in followed:
                followed.remove(tweet_id)
                return False
            followed.add(tweet_id)
            return True

    def get_followed(self, session_id):
        with self.lock:
            followed_ids = self.follows.get(session_id, set())
            # O(followed_count) 而不是 O(messages_count)：直接从索引拿，避免全列表扫描
            result = []
            for tid in followed_ids:
                msg = self._index.get(tid)
                if msg is not None:
                    result.append(msg)
            return result

tweet_manager = TweetManager()

# ============================================================
# Routes
# ============================================================

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/favicon.ico")
def favicon():
    # 返回一个空的 1x1 透明 PNG，避免 404
    import base64
    transparent_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    return transparent_png, 200, {"Content-Type": "image/png"}

@app.route("/icons.json")
def serve_icons_json():
    import os
    icons_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons.json")
    if os.path.exists(icons_path):
        with open(icons_path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "application/json"}
    return jsonify({"error": "not found"}), 404

@app.route("/icons/<path:filepath>")
def serve_icon_files(filepath):
    """提供 icons/ 目录下的静态文件（token icon SVG、平台 icon PNG）"""
    import os
    from flask import send_from_directory
    icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    if os.path.exists(os.path.join(icons_dir, filepath)):
        return send_from_directory(icons_dir, filepath)
    return jsonify({"error": "not found"}), 404

@app.route("/api/refresh_translation", methods=["POST"])
def refresh_translation():
    """手动触发翻译刷新：直接调用 Binance API 获取翻译并更新推文"""
    try:
        import uuid as _uuid
        # 复用模块级 requests.Session（避免每次手动翻译请求都重建 TCP/TLS）
        global _binance_api_session
        if _binance_api_session is None:
            import requests as req
            from requests.adapters import HTTPAdapter
            _binance_api_session = req.Session()
            adapter = HTTPAdapter(pool_connections=5, pool_maxsize=10, max_retries=0)
            _binance_api_session.mount("https://", adapter)
            _binance_api_session.mount("http://", adapter)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "请求体必须是 JSON 对象"}), 400
        tweet_id = data.get("tweet_id")
        if not tweet_id:
            return jsonify({"success": False, "error": "缺少 tweet_id"}), 400
        tweet_id = str(tweet_id)

        # 调用 Binance API 获取推文（含翻译）
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
        url = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/token/twitter/post/summary"
        params = {"tweetId": tweet_id}
        response = _binance_api_session.get(url, params=params, headers=headers, timeout=5)
        if response.status_code != 200:
            return jsonify({"success": False, "error": f"API 返回 {response.status_code}"}), 500
        api_data = response.json()
        if not api_data or not api_data.get("data"):
            return jsonify({"success": False, "error": "API 无数据"}), 500
        tweet_data = api_data["data"]
        text_translation = tweet_data.get("textTranslation", "")
        if not text_translation:
            return jsonify({"success": False, "error": "API 未返回翻译"}), 404

        # 更新 server 内存中的推文（锁内仅做数据更新，emit 在锁外）
        target_msg = None
        with tweet_manager.lock:
            target_msg = tweet_manager._index.get(tweet_id)
            if target_msg is not None:
                target_msg["tweetTextTranslation"] = text_translation
                target_msg["textTranslation"] = text_translation
        if target_msg is not None:
            # 在锁外广播，避免广播耗时阻塞其他请求对 tweet_manager 的访问
            socketio.emit("update_message", {"message": target_msg}, room="global")
            return jsonify({"success": True, "message": "翻译已更新"})
        return jsonify({"success": False, "error": "推文不在内存中"}), 404
    except Exception as e:
        app.logger.exception("翻译刷新失败")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/messages")
def get_messages():
    return jsonify({"success": True, "messages": tweet_manager.get_messages()})

@app.route("/api/tweet", methods=["POST"])
def receive_tweet():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "请求体必须是 JSON 对象"}), 400
        tweet_id = data.get("tweet_id") or data.get("tweetId") or data.get("id")
        if not tweet_id:
            return jsonify({"success": False, "error": "缺少 tweet_id"}), 400
        tweet_id = str(tweet_id)

        # 锁内只做存在性检查和数据更新；emit 在锁外，避免广播耗时阻塞其他请求
        with tweet_manager.lock:
            existing_msg = tweet_manager._index.get(tweet_id)
            if existing_msg is not None:
                updated = tweet_manager.update_message(data)
            else:
                updated = None
                new_message = None  # placeholder
        if existing_msg is not None:
            if updated:
                # 在锁外广播，避免广播耗时阻塞其他请求对 tweet_manager 的访问
                socketio.emit("update_message", {"message": updated}, room="global")
                return jsonify({"success": True, "updated": True, "message": updated}), 200
            else:
                return jsonify({"success": False, "error": "更新失败"}), 500
        else:
            # 新增消息（add_message 内部会加锁）
            message = tweet_manager.add_message(data)
            # 在锁外广播
            socketio.emit("new_message", {"message": message}, room="global")
            return jsonify({"success": True, "message": message}), 200
    except Exception as e:
        app.logger.exception("接收推文失败")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/token_update", methods=["POST"])
def receive_token_update():
    """
    接收 Token 增量更新，仅更新内存中对应消息的 tokens 列表，
    并通过 WebSocket 广播 token_update 事件。
    """
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "请求体必须是 JSON 对象"}), 400
        tweet_id = data.get("tweet_id")
        if not tweet_id:
            return jsonify({"success": False, "error": "缺少 tweet_id"}), 400
        tokens = data.get("tokens")
        if tokens is None:
            return jsonify({"success": False, "error": "缺少 tokens"}), 400

        updated = tweet_manager.update_tokens(tweet_id, tokens)
        if not updated:
            return jsonify({"success": False, "error": "tweet not found"}), 404

        socketio.emit("token_update", {"tweet_id": tweet_id, "tokens": tokens}, room="global")
        return jsonify({"success": True, "updated": True})
    except Exception as e:
        app.logger.exception("接收 Token 更新失败")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/follow/<tweet_id>", methods=["POST"])
def toggle_follow(tweet_id):
    session_id = request.headers.get("X-Session-ID", "default_user")
    is_followed = tweet_manager.toggle_follow(session_id, tweet_id)
    return jsonify({
        "success": True,
        "is_followed": is_followed,
        "followed_messages": tweet_manager.get_followed(session_id)
    })

# ============================================================
# 一键买入：调用 GMGN swap（BSC，0.0001 BNB → 目标 token）
# 单次 swap 权重 = 5，占满 GMGN 每秒限流配额，故 pusher 端 token_info 已停用
# ============================================================

# 复用 pusher 中的 GMGN 客户端单例（已延迟初始化、自动签名）
BUY_FROM_ADDRESS = "0xaced8b129c0eb5ea65d00b92ef3d063e512fd5ff"
BUY_INPUT_TOKEN  = "0x0000000000000000000000000000000000000000"  # BNB (native)
BUY_INPUT_AMOUNT = "100000000000000"  # 0.0001 BNB (18 decimals, smallest unit)
BUY_SLIPPAGE     = 3                  # 3% (GMGN 文档：integer 0-100)
BUY_GAS_PRICE_GWEI = 0.2               # 用户指定 0.2 GWei
# ⚠️ 关键：GMGN API 要求 gas_price 是 wei 单位的字符串，不是 GWei 浮点数
# 参考 gmgn-cli 官方源码 swap.ts：
#   params.gas_price = String(Math.round(parseFloat(opts.gasPrice) * 1e9));
# 故 0.2 GWei → "200000000" wei
BUY_GAS_PRICE_WEI = str(round(BUY_GAS_PRICE_GWEI * 1_000_000_000))
BUY_CHAIN        = "bsc"

def _get_gmgn_client_for_buy():
    """延迟初始化 GMGN 交易客户端（使用交易专用 API Key）。
    与 token_info 客户端完全隔离，避免互相影响限流。
    """
    try:
        # 优先复用 pusher 中的交易客户端单例（共享连接池）
        return pusher._get_gmgn_trade_client()
    except Exception:
        # 兜底：直接 new 一个，强制使用交易 API Key
        try:
            from gmgn_client import GmGnClient
            return GmGnClient(api_key=pusher.GMGN_TRADE_API_KEY)
        except Exception as e:
            app.logger.exception("GMGN 交易客户端初始化失败")
            return None

@app.route("/api/buy", methods=["POST"])
def buy_token():
    """一键买入：将 0.0001 BNB 兑换为目标 token。
    Body: {"contract": "0x..."}
    Response:
      200 {"success": true,  "tx_hash": "...", "order_id": "...", "raw": {...}}
      400 {"success": false, "error": "缺少 contract"}
      500 {"success": false, "error": "...", "api_error": "...", "api_message": "..."}
    """
    try:
        data = request.get_json(silent=True) or {}
        contract = (data.get("contract") or data.get("address") or "").strip()
        if not contract:
            return jsonify({"success": False, "error": "缺少 contract"}), 400
        # 简单校验 EVM 地址
        if not (contract.startswith("0x") and len(contract) == 42):
            return jsonify({"success": False, "error": f"无效的合约地址: {contract}"}), 400

        client = _get_gmgn_client_for_buy()
        if not client:
            return jsonify({"success": False, "error": "GMGN 客户端不可用（未配置 API Key / 私钥）"}), 500

        # GMGN swap body schema (基于官方 gmgn-cli src/commands/swap.ts + src/client/OpenApiClient.ts SwapParams)
        # 字段说明：
        #   chain         (string)  必填，"bsc"
        #   from_address  (string)  必填，EVM 地址需小写
        #   input_token   (string)  必填，原生币用 0x0
        #   output_token  (string)  必填，目标 CA
        #   input_amount  (string)  必填，最小单位字符串
        #   slippage      (number) 可选，0-100 整数 (3 = 3%)
        #   gas_price     (string) 可选，wei 单位字符串（GWei × 1e9）
        #   is_anti_mev   (bool)   可选，默认 true（BSC 支持）
        swap_params = {
            "chain":         BUY_CHAIN,
            "from_address":  BUY_FROM_ADDRESS.lower(),  # EVM 地址必须小写
            "input_token":   BUY_INPUT_TOKEN,
            "output_token":  contract.lower(),         # 输出地址也保持小写
            "input_amount":  BUY_INPUT_AMOUNT,
            "slippage":      BUY_SLIPPAGE,
            "gas_price":     BUY_GAS_PRICE_WEI,         # "200000000" (wei)
            "is_anti_mev":   True,
        }
        app.logger.info("💰 触发买入 swap: %s", swap_params)
        try:
            result = client.swap(swap_params)
        except Exception as e:
            # GmGnError 携带 api_code / api_error / api_message
            api_error = getattr(e, "api_error", None)
            api_message = getattr(e, "api_message", None)
            api_code = getattr(e, "api_code", None)
            app.logger.warning("❌ GMGN swap 失败: %s | api_code=%s api_error=%s api_message=%s",
                               e, api_code, api_error, api_message)
            return jsonify({
                "success": False,
                "error": str(e),
                "api_code": api_code,
                "api_error": api_error,
                "api_message": api_message,
            }), 500

        # GMGN swap 成功响应字段（参考官方文档）：
        #   order_id  - 订单 ID（后续可用 query_order 查询状态）
        #   hash      - 链上交易 hash（可能为空，需轮询 order_id 拿最终结果）
        #   status    - pending / processed / confirmed / failed / expired
        #   state     - 状态码 (30 = 成功)
        tx_hash = None
        order_id = None
        if isinstance(result, dict):
            tx_hash = result.get("hash") or result.get("tx_hash") or result.get("txHash")
            order_id = result.get("order_id") or result.get("orderId")
            # 兼容嵌套 data
            if not tx_hash and isinstance(result.get("data"), dict):
                d = result["data"]
                tx_hash = d.get("hash") or d.get("tx_hash") or d.get("txHash")
                order_id = d.get("order_id") or d.get("orderId")
        app.logger.info("✅ GMGN swap 成功: contract=%s tx_hash=%s order_id=%s",
                        contract, tx_hash, order_id)
        return jsonify({
            "success": True,
            "tx_hash": tx_hash,
            "order_id": order_id,
            "contract": contract,
            "input_amount": BUY_INPUT_AMOUNT,
            "raw": result if isinstance(result, (dict, list)) else str(result),
        }), 200
    except Exception as e:
        app.logger.exception("买入接口异常")
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# 持仓弹窗相关接口：/api/holdings, /api/kline, /api/sell
# ============================================================

@app.route("/api/holdings")
def get_holdings():
    """返回 GMGN wallet_holdings 缓存快照。
    数据由 pusher 后台线程每 0.5s 拉取一次（GMGN_HOLDINGS_API_KEY，独立限流）。
    前端可直接轮询此接口（每 1-2s 一次足够）。
    """
    snap = pusher.get_holdings_snapshot()
    return jsonify({
        "success": True,
        "wallet": snap["wallet"],
        "data": snap["data"],
        "updated_at": snap["updated_at"],
        "age_seconds": snap["age_seconds"],
        "error": snap["error"],
        "poll_total": snap["poll_total"],
        "poll_success": snap["poll_success"],
        "poll_fail": snap["poll_fail"],
    })

# K线历史接口（直接代理 Binance Web3 K线 API，避免前端跨域）
@app.route("/api/kline")
def get_kline():
    """获取 K线历史数据。
    Query: address=<contract>&interval=1s&limit=500&to=<ms>
    数据源：Binance Web3 Kline API (https://dquery.sintral.io/u-kline/v1/k-line/candles)
    """
    address = request.args.get("address", "").strip()
    if not address:
        return jsonify({"success": False, "error": "缺少 address"}), 400
    interval = request.args.get("interval", "1s")
    limit = int(request.args.get("limit", "500"))
    to_ts = request.args.get("to", "")
    try:
        # 复用 requests.Session（已 monkey patch by eventlet）
        import requests as _req
        url = "https://dquery.sintral.io/u-kline/v1/k-line/candles"
        params = {
            "address": address,
            "interval": interval,
            "limit": limit,
            "platform": "bsc",
        }
        if to_ts:
            params["to"] = to_ts
        headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "zh-CN,zh;q=0.9",
            "origin": "https://web3.binance.com",
            "referer": "https://web3.binance.com/",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/151.0.0.0 Safari/537.36",
        }
        resp = _req.get(url, params=params, headers=headers, timeout=10)
        data = resp.json()
        # 数据格式: {"data": [[open, high, low, close, volume, timestamp_ms, count], ...]}
        return jsonify({
            "success": True,
            "address": address,
            "interval": interval,
            "candles": data.get("data") or [],
        })
    except Exception as e:
        app.logger.exception("K线查询失败")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/sell", methods=["POST"])
def sell_token():
    """一键卖出某 token 的全部持仓。
    Body: {"contract": "0x..."}
    实现方式：
      1. 从持仓缓存中找到该 token 的当前持有数量
      2. 用 GMGN swap（input_token = token, output_token = BNB, percent=100 或 input_amount）
      3. GMGN swap 接口支持 input_amount_bps（按百分比卖），这里用 100% = "10000" bps
    """
    try:
        data = request.get_json(silent=True) or {}
        contract = (data.get("contract") or data.get("address") or "").strip()
        if not contract:
            return jsonify({"success": False, "error": "缺少 contract"}), 400
        if not (contract.startswith("0x") and len(contract) == 42):
            return jsonify({"success": False, "error": f"无效的合约地址: {contract}"}), 400

        client = _get_gmgn_client_for_buy()
        if not client:
            return jsonify({"success": False, "error": "GMGN 交易客户端不可用"}), 500

        # 卖出参数：token → BNB
        # GMGN swap schema (signed)：
        #   chain          : "bsc"
        #   from_address   : 小写
        #   input_token    : 要卖出的 token CA
        #   output_token   : 0x0 (BNB native)
        #   input_amount_bps : "10000" = 100% (BPS 单位：1% = 100 bps)
        #                     注意：GMGN 文档要求 input_token 不是 currency 时才能用 percent
        #   slippage       : 5 (卖出滑点稍大避免失败)
        #   gas_price      : wei 字符串
        #   is_anti_mev    : true
        swap_params = {
            "chain":            "bsc",
            "from_address":     BUY_FROM_ADDRESS.lower(),
            "input_token":      contract.lower(),
            "output_token":     BUY_INPUT_TOKEN,  # 0x0 = BNB
            "input_amount_bps": "10000",          # 100% 卖出
            "slippage":         5,                 # 5% 滑点
            "gas_price":        BUY_GAS_PRICE_WEI, # "200000000" wei
            "is_anti_mev":      True,
        }
        app.logger.info("💸 触发卖出 swap: %s", swap_params)
        try:
            result = client.swap(swap_params)
        except Exception as e:
            api_error = getattr(e, "api_error", None)
            api_message = getattr(e, "api_message", None)
            api_code = getattr(e, "api_code", None)
            app.logger.warning("❌ GMGN 卖出失败: %s | api_code=%s api_error=%s api_message=%s",
                               e, api_code, api_error, api_message)
            return jsonify({
                "success": False,
                "error": str(e),
                "api_code": api_code,
                "api_error": api_error,
                "api_message": api_message,
            }), 500

        tx_hash = None
        order_id = None
        if isinstance(result, dict):
            tx_hash = result.get("hash") or result.get("tx_hash") or result.get("txHash")
            order_id = result.get("order_id") or result.get("orderId")
            if not tx_hash and isinstance(result.get("data"), dict):
                d = result["data"]
                tx_hash = d.get("hash") or d.get("tx_hash") or d.get("txHash")
                order_id = d.get("order_id") or d.get("orderId")
        app.logger.info("✅ GMGN 卖出成功: contract=%s tx_hash=%s order_id=%s",
                        contract, tx_hash, order_id)
        return jsonify({
            "success": True,
            "tx_hash": tx_hash,
            "order_id": order_id,
            "contract": contract,
            "raw": result if isinstance(result, (dict, list)) else str(result),
        }), 200
    except Exception as e:
        app.logger.exception("卖出接口异常")
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# 进程内 hook 处理：pusher → thread-safe queue → eventlet 消费者 → socketio.emit
# ============================================================
# 关键架构：pusher 跑在 asyncio 线程，server 跑在 eventlet。
# 直接从 pusher 调 socketio.emit 是跨调度器调用，可能线程不安全。
# 因此 hook 只做一件事：把数据塞进 thread-safe queue.Queue（微秒级，无阻塞）。
# 由 eventlet 消费者 greenthread 从队列取出，调 tweet_manager + socketio.emit。
# 这样所有 Flask/SocketIO 调用都在 eventlet 线程内完成，杜绝跨调度器问题。
_HOOK_QUEUE_MAXSIZE = 10000  # 防止消费慢时无限堆积
_hook_queue = _stdqueue.Queue(maxsize=_HOOK_QUEUE_MAXSIZE)


def _handle_incoming_tweet(tweet_obj):
    """hook 入口（pusher asyncio 线程调用）：仅入队，不阻塞。"""
    try:
        _hook_queue.put_nowait(("tweet", tweet_obj))
    except _stdqueue.Full:
        app.logger.warning("⚠️ hook 队列已满(%d)，丢弃 tweet: %s",
                           _HOOK_QUEUE_MAXSIZE,
                           tweet_obj.get("tweet_id") if isinstance(tweet_obj, dict) else "?")
    except Exception:
        app.logger.exception("hook enqueue error (tweet)")


def _handle_incoming_token_update(tweet_id, tokens):
    """hook 入口（pusher asyncio 线程调用）：仅入队，不阻塞。"""
    try:
        _hook_queue.put_nowait(("token_update", str(tweet_id), tokens))
    except _stdqueue.Full:
        app.logger.warning("⚠️ hook 队列已满(%d)，丢弃 token_update: %s",
                           _HOOK_QUEUE_MAXSIZE, tweet_id)
    except Exception:
        app.logger.exception("hook enqueue error (token_update)")


def _handle_incoming_migrated_tokens(tokens_list):
    """hook 入口（pusher asyncio 线程调用）：迁移 token 列表更新，仅入队。"""
    try:
        _hook_queue.put_nowait(("migrated_tokens", tokens_list))
    except _stdqueue.Full:
        app.logger.warning("⚠️ hook 队列已满(%d)，丢弃 migrated_tokens",
                           _HOOK_QUEUE_MAXSIZE)
    except Exception:
        app.logger.exception("hook enqueue error (migrated_tokens)")


def _process_tweet_internal(tweet_obj):
    """实际处理 tweet（在 eventlet 消费者线程中调用）。"""
    if not isinstance(tweet_obj, dict):
        return
    tweet_id = tweet_obj.get("tweet_id") or tweet_obj.get("tweetId") or tweet_obj.get("id")
    if not tweet_id:
        return
    tweet_id = str(tweet_id)

    # 锁内只做存在性检查和数据更新；emit 在锁外
    with tweet_manager.lock:
        existing_msg = tweet_manager._index.get(tweet_id)
        if existing_msg is not None:
            updated = tweet_manager.update_message(tweet_obj)
        else:
            updated = None
    if existing_msg is not None:
        if updated:
            socketio.emit("update_message", {"message": updated}, room="global")
    else:
        message = tweet_manager.add_message(tweet_obj)
        socketio.emit("new_message", {"message": message}, room="global")


def _process_token_update_internal(tweet_id, tokens):
    """实际处理 token_update（在 eventlet 消费者线程中调用）。"""
    tweet_id = str(tweet_id)
    updated = tweet_manager.update_tokens(tweet_id, tokens)
    if not updated:
        return
    socketio.emit("token_update", {"tweet_id": tweet_id, "tokens": tokens}, room="global")


def _hook_consumer_loop():
    """eventlet 消费者 greenthread：从队列取数据，调 tweet_manager + socketio.emit。
    所有 Flask/SocketIO 调用都在这里完成，杜绝 pusher 跨调度器直接调用。
    由 socketio.start_background_task 启动。"""
    while True:
        try:
            item = _hook_queue.get(timeout=1)  # 1s 超时：让出 eventlet 调度，不空转
        except _stdqueue.Empty:
            continue
        try:
            kind = item[0]
            if kind == "tweet":
                _process_tweet_internal(item[1])
            elif kind == "token_update":
                _process_token_update_internal(item[1], item[2])
            elif kind == "migrated_tokens":
                socketio.emit("migrated_tokens", {"tokens": item[1]}, room="global")
        except Exception:
            app.logger.exception("hook consumer error")

# ============================================================
# SocketIO
# ============================================================

@socketio.on("connect")
def handle_connect():
    session_id = request.headers.get("X-Session-ID", str(uuid.uuid4()))
    session["session_id"] = session_id
    join_room("global")
    emit("init_messages", {
        "messages": tweet_manager.get_messages(),
        "followed": tweet_manager.get_followed(session_id)
    })

@socketio.on("follow_toggle")
def handle_follow_toggle(data):
    session_id = session.get("session_id", "default_user")
    if not isinstance(data, dict):
        return
    tweet_id = data.get("tweet_id")
    if not tweet_id:
        return
    is_followed = tweet_manager.toggle_follow(session_id, tweet_id)
    emit("follow_updated", {
        "tweet_id": tweet_id,
        "is_followed": is_followed,
        "followed_messages": tweet_manager.get_followed(session_id)
    }, room=request.sid)

# ============================================================
# HTML 模板（完整，已包含 update_message 及 token_update 增量防闪烁支持）
# ============================================================

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Twitter + Token 卡片</title>
<script src="https://cdn.socket.io/4.7.0/socket.io.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
body { background: #f7f9fc; padding: 20px; display: flex; flex-direction: column; align-items: center; min-height: 100vh; color: #0f1419; }
.container { width: 100%; max-width: 100%; padding: 0 20px; display: flex; gap: 20px; align-items: flex-start; height: calc(100vh - 40px); }
/* main-content 占满宽度（sidebar 已移除） */
.main-content { flex: 1; min-width: 0; background: #ffffff; border-radius: 16px; padding: 12px 14px; border: 1px solid #eff3f4; height: 100%; display: flex; flex-direction: column; overflow: hidden; }
.header { margin-bottom: 8px; flex-shrink: 0; display: flex; justify-content: space-between; align-items: center; gap: 10px; padding-bottom: 6px; border-bottom: 1px solid #eff3f4; }
.header h1 { font-size: 0.95rem; font-weight: 700; display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.header h1 small { font-size: 0.8rem; font-weight: 400; color: #536471; }
/* 迁移 token 横向滚动条：在 h1 和 header-info 之间，占满剩余空间 */
.migrated-bar { flex: 1; min-width: 0; display: flex; gap: 6px; overflow-x: auto; overflow-y: hidden; padding: 2px 0; }
.migrated-bar::-webkit-scrollbar { height: 4px; }
.migrated-bar::-webkit-scrollbar-thumb { background: #cfd9de; border-radius: 2px; }
.migrated-bar:empty { display: none; }
.header-info { display: flex; gap: 10px; flex-wrap: wrap; flex-shrink: 0; }
.badge.live { background: #ff6b6b; color: white; animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.55; } }
.badge { padding: 2px 10px; border-radius: 9999px; font-size: 0.72rem; background: #e7e9ea; color: #536471; }
.mig-token { display: flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 8px; background: #f1f5f9; border: 1px solid #e2e8f0; flex-shrink: 0; cursor: pointer; transition: background 0.15s; }
.mig-token:hover { background: #e0f2fe; border-color: #93c5fd; }
.mig-token img { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: #e2e8f0; }
.mig-token .mig-icon-fallback { width: 20px; height: 20px; border-radius: 50%; background: linear-gradient(135deg, #e2e8f0, #cbd5e1); display: flex; align-items: center; justify-content: center; font-size: 0.5rem; font-weight: 700; color: #64748b; flex-shrink: 0; }
.mig-token .mig-symbol { font-size: 0.7rem; font-weight: 700; color: #0f1419; white-space: nowrap; }
.mig-token .mig-mc { font-size: 0.65rem; color: #16a34a; font-weight: 600; white-space: nowrap; }
.mig-token .mig-kol { font-size: 0.58rem; color: #2563eb; white-space: nowrap; }
/* 瀑布流：CSS columns，按顺序填充（不平衡），新卡片总是在最左列顶部 */
.card-grid { flex: 1; overflow-y: auto; column-gap: 10px; column-fill: auto; padding-right: 4px; }
.card-grid::-webkit-scrollbar { width: 6px; }
.card-grid::-webkit-scrollbar-thumb { background: #cfd9de; border-radius: 3px; }
/* 瀑布流内卡片：break-inside avoid 防止卡片被列分割 */
/* contain: layout 隔离卡片内部布局，浏览器不需要在滚动时重排外部 */
.tweet-card { break-inside: avoid; margin-bottom: 10px; background: #ffffff; border-radius: 12px; padding: 10px 12px 8px; border: 1px solid #eff3f4; display: flex; flex-direction: column; gap: 8px; contain: layout style; will-change: auto; }
.tweet-card:hover { background: #f7f9fa; }
.tweet-card.new { border-left: 3px solid #1d9bf0; background: #f0f8ff; }
.tweet-card.is-new { animation: slideIn 0.28s ease-out; }
@keyframes slideIn { from { opacity: 0; transform: translateY(-12px); } to { opacity: 1; transform: translateY(0); } }
/* 推文区域（上） + token 区域（下），纵向排列 */
.tweet-left { min-width: 0; }
.tweet-right { display: flex; flex-direction: column; gap: 4px; }
.token-item { background: #f8fafc; border-radius: 8px; padding: 5px 7px; border: 1px solid #e2e8f0; display: flex; align-items: stretch; gap: 7px; font-size: 0.72rem; height: 88px; overflow: hidden; transition: background 0.3s ease; }
/* 新加入 token 的高亮：10s 内底色为浅蓝，渐变消失 */
.token-item.token-new { background: #dbeafe; border-color: #93c5fd; animation: tokenNewFade 10s ease-out forwards; }
@keyframes tokenNewFade { 0% { background: #dbeafe; border-color: #93c5fd; } 80% { background: #dbeafe; border-color: #93c5fd; } 100% { background: #f8fafc; border-color: #e2e8f0; } }
/* 有"早"标签的 token：底色高亮为浅黄，强调这是最早创建的 token */
.token-item.has-early-tag { background: #fef9c3; border-color: #fde047; }
/* icon-column: 固定宽高 */
.token-item .icon-column { display: flex; flex-direction: column; align-items: center; flex-shrink: 0; width: 54px; height: 100%; }
/* icon-wrap: 固定 54x54 正方形（缩小） */
.token-item .icon-wrap { width: 54px; height: 54px; flex-shrink: 0; position: relative; border-radius: 9px; margin-top: 0; cursor: pointer; }
/* ca-display: 固定宽高 */
.token-item .ca-display { flex-shrink: 0; flex: 1 1 auto; width: 54px; display: flex; align-items: center; justify-content: center; font-size: 0.48rem; color: #94a3b8; text-align: center; font-family: monospace; line-height: 1.2; cursor: pointer; overflow: hidden; padding: 0; }
.token-item .ca-display:hover { color: #2563eb; }
/* progress-border: 灰色底环 + 彩色进度环叠加 */
.token-item .progress-border { position: absolute; inset: 0; border-radius: 9px; padding: 2px; z-index: 1; pointer-events: none; }
/* 灰色底环 */
.token-item .progress-border::after { content: ""; position: absolute; inset: 0; border-radius: 9px; background: #cbd5e1; -webkit-mask: radial-gradient(circle, transparent 62%, #000 64%); mask: radial-gradient(circle, transparent 62%, #000 64%); }
/* 彩色进度环 */
.token-item .progress-border::before { content: ""; position: absolute; inset: 0; border-radius: 9px; background: conic-gradient(var(--progress-color, #22c55e) calc(var(--progress-pct, 0) * 1%), transparent 0); -webkit-mask: radial-gradient(circle, transparent 62%, #000 64%); mask: radial-gradient(circle, transparent 62%, #000 64%); }
.token-item .icon { position: absolute; inset: 2px; width: calc(100% - 4px); height: calc(100% - 4px); border-radius: 7px; object-fit: cover; background: #e2e8f0; z-index: 2; }
.token-item .icon-fallback { position: absolute; inset: 2px; width: calc(100% - 4px); height: calc(100% - 4px); border-radius: 7px; background: linear-gradient(135deg, #e2e8f0, #cbd5e1); display: flex; align-items: center; justify-content: center; color: #94a3b8; font-size: 10px; font-weight: 700; z-index: 2; }
.token-item .platform-badge { position: absolute; bottom: 2px; right: 2px; width: 14px; height: 14px; z-index: 3; display: flex; align-items: center; justify-content: center; }
.token-item .platform-badge svg { width: 12px; height: 12px; }
.token-item .info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; justify-content: space-between; height: 100%; overflow: hidden; }
/* symbol 行 */
.token-item .symbol-row { display: flex; align-items: center; justify-content: space-between; gap: 5px; width: 100%; height: 16px; flex-shrink: 0; }
.token-item .symbol-left { display: flex; align-items: baseline; gap: 3px; min-width: 0; overflow: hidden; }
.token-item .symbol { font-weight: 700; font-size: 0.75rem; color: #0f1419; }
.token-item .name { color: #94a3b8; font-weight: 400; font-size: 0.65rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 1; min-width: 0; max-width: 70px; }
.token-item .name-zh { color: #94a3b8; font-weight: 400; font-size: 0.65rem; margin-left: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 1; min-width: 0; max-width: 50px; }
.token-item .symbol-right { display: flex; flex-direction: column; align-items: flex-end; gap: 0; flex-shrink: 0; }
.token-item .market-cap { font-weight: 700; font-size: 0.65rem; white-space: nowrap; line-height: 1; }
.token-item .age { font-weight: 600; font-size: 0.55rem; white-space: nowrap; line-height: 1; }
/* meta 行 */
.token-item .meta { display: flex; flex-wrap: wrap; gap: 2px 5px; color: #64748b; font-size: 0.55rem; align-items: center; height: 14px; overflow: hidden; flex-shrink: 0; }
.token-item .meta span { white-space: nowrap; display: inline-flex; align-items: center; gap: 2px; }
.token-item .meta .meta-icon { width: 9px; height: 9px; flex-shrink: 0; vertical-align: middle; }
.token-item .social-links .social-icon { width: 9px; height: 9px; vertical-align: middle; margin-right: 2px; }
.token-item .meta .meta-spacer { flex: 1; min-width: 4px; }
.token-item .meta .meta-volume { color: #475569; font-weight: 500; margin-left: auto; }
.token-item .meta .meta-top10 { font-weight: 600; }
.token-item .meta .meta-holders { }
.token-item .meta .meta-sniper { }
.token-item .meta .meta-insider { }
.token-item .meta .meta-kol { }
.token-item .meta .meta-tax { }
/* color states for meta items */
.meta-color-red { color: #dc2626 !important; }
.meta-color-green { color: #16a34a !important; }
.meta-color-yellow { color: #d97706 !important; }
.meta-color-blue { color: #2563eb !important; }
.meta-color-purple { color: #7c3aed !important; }
/* market-cap dynamic colors */
.mc-purple { color: #7c3aed; }
.mc-magenta { color: #c026d3; }
.mc-darkred { color: #991b1b; }
.mc-yellow { color: #d97706; }
/* token age dynamic colors */
.age-green { color: #16a34a; }
.age-blue { color: #2563eb; }
.age-yellow { color: #d97706; }
/* "早" tag replaces "新" tag */
.token-item .token-early-tag { display: inline-block; background: #fde047; color: #422006; font-size: 0.5rem; font-weight: 700; width: 15px; height: 15px; line-height: 15px; text-align: center; border-radius: 50%; flex-shrink: 0; margin-left: 2px; cursor: default; }
/* clickable elements */
.token-item .symbol { cursor: pointer; }
.token-item .name { cursor: pointer; }
/* social links icon-only */
.token-item .social-links a { color: #2563eb; font-size: 0.65rem; text-decoration: none; display: inline-flex; align-items: center; }
.token-item .social-links a:hover { text-decoration: underline; }
/* tweet clickable */
/* tweet-left cursor 已在上方定义 */
.author-name { cursor: pointer; }
.avatar { cursor: pointer; }
/* copy tooltip */
.copy-tooltip { position: fixed; z-index: 9999; background: #0f1419; color: #fff; padding: 4px 12px; border-radius: 6px; font-size: 0.7rem; pointer-events: none; display: none; }
.copy-tooltip.active { display: block; }
/* icon hover popup (reuse hover-preview) */
.token-item .badges { display: flex; flex-wrap: wrap; gap: 2px 3px; height: 14px; overflow: hidden; flex-shrink: 0; }
.token-item .badge { display: inline-block; padding: 0 3px; border-radius: 3px; font-size: 0.5rem; line-height: 12px; white-space: nowrap; }
.token-item .badge.badge-warn { background: #fee2e2; color: #dc2626; }
.token-item .badge.badge-info { background: #e0f2fe; color: #0284c7; }
.token-item .badge.badge-neutral { background: #f1f5f9; color: #64748b; }
/* tax 显示在 badge 位置，作为 badge-neutral 样式 */
.token-item .badge.badge-tax { background: #f1f5f9; color: #64748b; }
.token-item .badge.badge-tax-low { background: #dcfce7; color: #16a34a; }
.token-item .div-quote-icon { display: inline-flex; align-items: center; justify-content: center; width: 12px; height: 12px; overflow: hidden; vertical-align: middle; }
.token-item .div-quote-icon svg { width: 12px; height: 12px; }
.token-item .div-quote-icon svg image { width: 12px; height: 12px; }
.token-item .age-row { display: flex; align-items: center; gap: 3px; height: 14px; flex-shrink: 0; }
.token-item .age-row .age { font-size: 0.65rem; }
.token-item .social-links { display: flex; gap: 5px; }
.token-item .ai-narrative { color: #64748b; font-size: 0.5rem; height: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }
/* 买入按钮：放在 age-row 最右侧 */
.token-item .buy-btn { margin-left: auto; padding: 1px 6px; font-size: 0.55rem; font-weight: 700; line-height: 14px; height: 16px; border: none; border-radius: 4px; cursor: pointer; color: #fff; background: #16a34a; transition: background 0.15s, transform 0.05s; flex-shrink: 0; user-select: none; }
.token-item .buy-btn:hover { background: #15803d; }
.token-item .buy-btn:active { transform: scale(0.95); }
.token-item .buy-btn.is-loading { background: #64748b; cursor: wait; opacity: 0.85; }
.token-item .buy-btn.is-success { background: #16a34a; }
.token-item .buy-btn.is-error { background: #dc2626; }
/* 买入结果浮层（卡片底部） */
.token-item .buy-toast { position: absolute; left: 50%; bottom: 4px; transform: translateX(-50%); padding: 3px 8px; border-radius: 4px; font-size: 0.55rem; font-weight: 600; color: #fff; background: rgba(15, 20, 25, 0.92); z-index: 10; pointer-events: none; opacity: 0; transition: opacity 0.2s; max-width: 90%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.token-item .buy-toast.show { opacity: 1; }
.token-item .buy-toast.success { background: rgba(22, 163, 74, 0.95); }
.token-item .buy-toast.error { background: rgba(220, 38, 38, 0.95); }
.token-item { position: relative; }
.no-tokens { color: #999; font-size: 0.75rem; text-align: center; padding: 10px 0; }
.card-header { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 2px; position: relative; border-radius: 8px; padding: 4px 6px; }
.card-header-overlay { position: absolute; inset: 0; background: rgba(255,255,255,0.75); border-radius: 8px; z-index: 0; }
.card-header > *:not(.card-header-overlay) { position: relative; z-index: 1; }
.card-header img.avatar, .card-header .author-info { position: relative; z-index: 1; }
.avatar { width: 32px; height: 32px; border-radius: 9999px; flex-shrink: 0; object-fit: cover; border: 1px solid #eff3f4; background: #cfd9de; }
.author-info { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.author-name-row { display: flex; align-items: center; justify-content: space-between; width: 100%; }
.author-name { font-weight: 700; font-size: 0.8rem; display: flex; align-items: center; flex-wrap: wrap; gap: 3px; line-height: 1.25; }
.verified { display: inline-flex; width: 1em; height: 1em; color: #1d9bf0; flex-shrink: 0; vertical-align: middle; }
.author-handle { color: #536471; font-size: 0.7rem; font-weight: 400; }
.author-meta-row { display: flex; align-items: center; gap: 3px; flex-wrap: wrap; margin-top: 1px; }
/* trigger-count 标签：推到最右侧 */
.author-meta-row .trigger-count { margin-left: auto; padding: 1px 6px; border-radius: 9999px; font-size: 0.58rem; font-weight: 600; background: #e7e9ea; color: #536471; flex-shrink: 0; }
.author-meta-row .trigger-count.tc-first { background: #dcfce7; color: #16a34a; }  /* 第 1 次：绿色 */
.author-meta-row .trigger-count.tc-few { background: #fef3c7; color: #d97706; }  /* 2-5 次：黄色 */
.author-meta-row .trigger-count.tc-many { background: #fee2e2; color: #dc2626; }  /* 6+ 次：红色 */
.dot { color: #536471; font-size: 0.7rem; }
.follow-btn { padding: 2px 12px; border: none; border-radius: 9999px; font-size: 0.7rem; font-weight: 700; cursor: pointer; background: #e7e9ea; color: #0f1419; flex-shrink: 0; margin-left: 8px; }
.follow-btn:hover { background: #d7dbdc; }
.follow-btn.followed { background: #1d9bf0; color: white; }
.tweet-text { font-size: 0.82rem; line-height: 1.45; color: #0f1419; white-space: normal; word-break: break-word; overflow-wrap: anywhere; margin: 3px 0 2px; }
.tweet-text a { color: #1d9bf0; text-decoration: none; }
.tweet-text a:hover { text-decoration: underline; }
/* 翻译显示区 */
.tweet-translation { font-size: 0.78rem; line-height: 1.45; color: #536471; white-space: normal; word-break: break-word; overflow-wrap: anywhere; margin: 0 0 3px; padding: 4px 8px; background: #f8fafc; border-left: 3px solid #1d9bf0; border-radius: 4px; }
.tweet-translation a { color: #1d9bf0; text-decoration: none; }
.tweet-translation a:hover { text-decoration: underline; }
.tweet-translation-full { font-size: 0.78rem; line-height: 1.45; color: #536471; white-space: normal; word-break: break-word; overflow-wrap: anywhere; margin: 0 0 3px; padding: 4px 8px; background: #f8fafc; border-left: 3px solid #1d9bf0; border-radius: 4px; }
.tweet-text-full { font-size: 0.82rem; line-height: 1.45; color: #0f1419; white-space: normal; word-break: break-word; overflow-wrap: anywhere; margin: 3px 0 2px; }
.tweet-text-full a { color: #1d9bf0; text-decoration: none; }
/* token symbol/name 匹配高亮 */
.tweet-text .hl-token-partial { background: #dcfce7; border-radius: 3px; padding: 0 2px; }
.tweet-text .hl-token-full { background: #fee2e2; border-radius: 3px; padding: 0 2px; font-weight: 600; }
.tweet-text .hl-quote { background: #fef9c3; border-radius: 3px; padding: 0 2px; }
.tweet-translation .hl-token-partial, .tweet-translation-full .hl-token-partial { background: #dcfce7; border-radius: 3px; padding: 0 2px; }
.tweet-translation .hl-token-full, .tweet-translation-full .hl-token-full { background: #fee2e2; border-radius: 3px; padding: 0 2px; font-weight: 600; }
.tweet-translation .hl-quote, .tweet-translation-full .hl-quote { background: #fef9c3; border-radius: 3px; padding: 0 2px; }
.nested-tweet .nt-body .hl-token-partial, .nested-tweet .nt-body-full .hl-token-partial { background: #dcfce7; border-radius: 3px; padding: 0 2px; }
.nested-tweet .nt-body .hl-token-full, .nested-tweet .nt-body-full .hl-token-full { background: #fee2e2; border-radius: 3px; padding: 0 2px; font-weight: 600; }
.nested-tweet .nt-body .hl-quote, .nested-tweet .nt-body-full .hl-quote { background: #fef9c3; border-radius: 3px; padding: 0 2px; }
.show-more-btn { display: inline-block; margin: 2px 0 4px; padding: 1px 8px; border: 1px solid #e7e9ea; border-radius: 9999px; background: transparent; cursor: pointer; font-size: 0.62rem; font-weight: 600; color: #1d9bf0; transition: background 0.15s; }
.show-more-btn:hover { background: #f0f8ff; }
/* 媒体盒子：单图正常显示，多图双行网格排列 */
.media-box { margin: 4px 0 3px; border-radius: 10px; overflow: hidden; border: none; background: transparent; max-width: 100%; min-width: 0; }
/* 单图：正常块级显示 */
.media-box.single { display: block; }
/* 2张图：1x2 网格 */
.media-box.double { display: grid; grid-template-columns: 1fr 1fr; gap: 2px; }
/* 3张图：2+1 布局（第一行2张，第二行1张占一半） */
.media-box.triple { display: grid; grid-template-columns: 1fr 1fr; gap: 2px; }
/* 4张图：2x2 网格 */
.media-box.quad { display: grid; grid-template-columns: 1fr 1fr; gap: 2px; }
/* 单图：保持原比例，不裁剪 */
.media-box.single img { max-width: 100%; width: auto; height: auto; max-height: 360px; display: block; background: transparent; margin: 0; border-radius: 8px; cursor: zoom-in; }
/* 多图：每张图填满 grid cell，固定高度，object-fit cover 裁剪 */
.media-box.double img, .media-box.triple img, .media-box.quad img { width: 100%; height: 120px; object-fit: cover; display: block; border-radius: 6px; cursor: zoom-in; }
.media-box img:hover { opacity: 0.9; }
.video-wrap { margin: 6px 0 4px; border-radius: 12px; overflow: hidden; border: none; background: transparent; display: flex; justify-content: flex-start; }
.video-wrap video { width: 100%; max-width: 100%; height: auto; display: block; background: transparent; border-radius: 12px; }
.video-wrap .video-fallback { display: flex; align-items: center; gap: 10px; padding: 10px 12px; color: #fff; background: #0f1419; width: 100%; max-width: 100%; justify-content: flex-start; border-radius: 12px; }
.video-wrap .video-fallback img { width: 100px; height: 60px; object-fit: cover; border-radius: 6px; flex-shrink: 0; }
.video-placeholder { position: relative; cursor: pointer; max-width: 100%; margin: 0; }
.video-placeholder .video-poster { width: 100%; height: auto; object-fit: cover; display: block; border-radius: 12px; max-height: 360px; }
.video-placeholder .video-play-btn { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 48px; height: 48px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 20px; pointer-events: none; }
.nested-tweet { margin: 6px 0 3px; border: 1px solid #cfd9de; border-radius: 10px; padding: 7px 9px; background: #fff; }
.nested-tweet:hover { background: #f7f9fa; }
.nested-tweet .nt-header { display: flex; align-items: center; gap: 5px; margin-bottom: 3px; }
.nested-tweet .nt-avatar { width: 18px; height: 18px; border-radius: 9999px; object-fit: cover; background: #cfd9de; flex-shrink: 0; }
.nested-tweet .nt-name { font-weight: 700; font-size: 0.8rem; display: flex; align-items: center; gap: 3px; }
.nested-tweet .nt-handle { color: #536471; font-size: 0.75rem; }
.nested-tweet .nt-body { font-size: 0.78rem; line-height: 1.4; white-space: normal; word-break: break-word; margin-top: 2px; }
.nested-tweet .nt-body-full { font-size: 0.78rem; line-height: 1.4; white-space: normal; word-break: break-word; margin-top: 2px; display: none; }
.nested-tweet .nt-translation { font-size: 0.75rem; line-height: 1.4; color: #536471; margin-top: 2px; padding: 4px 8px; background: #f8fafc; border-left: 3px solid #1d9bf0; border-radius: 4px; white-space: normal; word-break: break-word; }
.nested-tweet .nt-translation-full { font-size: 0.75rem; line-height: 1.4; color: #536471; margin-top: 2px; padding: 4px 8px; background: #f8fafc; border-left: 3px solid #1d9bf0; border-radius: 4px; white-space: normal; word-break: break-word; display: none; }
.nested-tweet .nt-show-more-btn { display: inline-block; margin: 2px 0 2px; padding: 1px 6px; border: 1px solid #e7e9ea; border-radius: 9999px; background: transparent; cursor: pointer; font-size: 0.58rem; font-weight: 600; color: #1d9bf0; transition: background 0.15s; }
.nested-tweet .nt-show-more-btn:hover { background: #f0f8ff; }
.nested-tweet .nt-media { margin-top: 6px; border-radius: 10px; overflow: hidden; border: none; display: grid; grid-template-columns: 1fr 1fr; gap: 2px; max-width: 100%; justify-items: center; align-items: start; }
/* 嵌套单图：单列；嵌套多图：双列。统一 100% 宽度避免压窄 */
.nested-tweet .nt-media:has(img:only-child) { grid-template-columns: 1fr; }
/* 嵌套图片：严格保持原始比例 */
.nested-tweet .nt-media img { max-width: 100%; width: auto; height: auto; max-height: 240px; display: block; border-radius: 10px; cursor: zoom-in; }
.nested-tweet .nt-media img:hover { opacity: 0.9; }
/* 图片大图预览（lightbox） */
.image-lightbox { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); z-index: 9999; display: none; align-items: center; justify-content: center; cursor: zoom-out; }
.image-lightbox.active { display: flex; }
.image-lightbox img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: 8px; box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
.image-lightbox .close-hint { position: fixed; top: 20px; right: 24px; color: #fff; font-size: 14px; background: rgba(0,0,0,0.5); padding: 6px 12px; border-radius: 6px; pointer-events: none; }
/* 图片悬浮预览（原位置放大弹窗） */
.image-hover-preview { position: fixed; z-index: 9000; pointer-events: none; display: none; max-width: 500px; max-height: 500px; }
.image-hover-preview.active { display: block; }
.image-hover-preview img { width: 100%; height: 100%; max-width: 500px; max-height: 500px; object-fit: contain; border-radius: 8px; box-shadow: 0 4px 24px rgba(0,0,0,0.3); background: #fff; }
.nested-tweet .nt-video { margin-top: 6px; border-radius: 10px; overflow: visible; border: none; background: transparent; display: flex; justify-content: flex-start; }
.nested-tweet .nt-video video { width: 100%; max-width: 50%; height: auto; display: block; border-radius: 10px; }
.nested-tweet .nt-article { margin-top: 6px; border: 1px solid #eff3f4; border-radius: 10px; overflow: hidden; }
.nested-tweet .nt-article img { width: 100%; max-width: 100%; height: auto; object-fit: cover; display: block; }
.nested-tweet .nt-article .art-body { padding: 8px 10px; }
.nested-tweet .nt-article .art-title { font-weight: 700; font-size: 0.85rem; }
.nested-tweet .nt-article .art-desc { font-size: 0.75rem; color: #536471; margin-top: 2px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.article-preview { margin: 6px 0 3px; border: 1px solid #cfd9de; border-radius: 10px; overflow: hidden; background: #fff; }
.article-preview:hover { background: #f7f9fa; }
.article-preview .art-cover { width: 100%; max-width: 100%; height: auto; object-fit: cover; display: block; background: #eff3f4; }
.article-preview .art-body { padding: 7px 9px 9px; }
.article-preview .art-title { font-weight: 700; font-size: 0.82rem; }
.article-preview .art-desc { color: #536471; font-size: 0.75rem; margin-top: 3px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.article-preview .art-domain { color: #536471; font-size: 0.68rem; margin-top: 5px; }
.tweet-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 0.68rem; color: #536471; padding-top: 5px; border-top: 1px solid #eff3f4; margin-top: 4px; }
.tweet-meta span { display: inline-flex; align-items: center; gap: 4px; }
.tweet-meta .tweet-type-badge-wrap { margin-left: auto; }
.tweet-type-badge { background: #e7e9ea; border-radius: 9999px; padding: 0 8px; font-size: 0.65rem; font-weight: 700; color: #0f1419; line-height: 18px; text-transform: uppercase; }
/* 不同推文类型不同颜色 */
.tweet-type-badge.badge-type-original { background: #dcfce7; color: #16a34a; }
.tweet-type-badge.badge-type-reply { background: #e0f2fe; color: #0284c7; }
.tweet-type-badge.badge-type-quote { background: #fef3c7; color: #d97706; }
.tweet-type-badge.badge-type-retweet { background: #f3e8ff; color: #7c3aed; }
.tweet-type-badge.badge-type-profile { background: #fce7f3; color: #db2777; }
.tweet-type-badge.badge-type-live { background: #fee2e2; color: #dc2626; animation: pulse 2s infinite; }
.tweet-type-badge.badge-type-square { background: #dbeafe; color: #2563eb; }
.tweet-type-badge.badge-type-weibo { background: #fef2f2; color: #e11d48; }
.reply-context { font-size: 0.8rem; color: #536471; margin-bottom: 4px; padding-left: 50px; }
.reply-context a { color: #1d9bf0; text-decoration: none; }
.sidebar { gap: 14px; padding-bottom: 14px; }
.sidebar-header { display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; padding-bottom: 6px; border-bottom: 1px solid #eff3f4; }
.sidebar-header h2 { font-size: 1rem; }
.sidebar-badge { background: #1d9bf0; color: white; padding: 2px 10px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
.followed-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding-right: 4px; }
.empty-state { text-align: center; color: #999; padding: 28px 0; }
.empty-state .hint { font-size: 0.8rem; color: #bbb; margin-top: 4px; }
/* 瀑布流列数随屏宽自适应：宽屏 4 列，中屏 3 列，窄屏 2 列，手机 1 列 */
@media (min-width: 1400px) { .card-grid { column-count: 4; } }
@media (min-width: 1000px) and (max-width: 1399px) { .card-grid { column-count: 3; } }
@media (min-width: 700px) and (max-width: 999px) { .card-grid { column-count: 2; } }
@media (max-width: 699px) { .card-grid { column-count: 1; } body { padding: 10px; } .tweet-card { padding: 8px 10px 6px; } .avatar { width: 28px; height: 28px; } }
/* ============================================================
   持仓弹窗：FAB 按钮 + 弹窗 + K线 + 列表
   ============================================================ */
.holdings-fab { position: fixed; right: 20px; bottom: 20px; z-index: 9999; display: flex; align-items: center; gap: 6px; padding: 10px 16px; border: none; border-radius: 24px; background: linear-gradient(135deg, #2563eb, #7c3aed); color: #fff; font-size: 0.85rem; font-weight: 700; cursor: pointer; box-shadow: 0 4px 14px rgba(37,99,235,0.45); transition: transform 0.15s, box-shadow 0.15s; }
.holdings-fab:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(37,99,235,0.55); }
.holdings-fab:active { transform: translateY(0); }
.holdings-fab-icon { font-size: 1rem; line-height: 1; }
.holdings-panel { position: fixed; right: 20px; bottom: 70px; z-index: 9998; width: 720px; max-width: calc(100vw - 40px); max-height: calc(100vh - 100px); display: flex; flex-direction: column; background: #ffffff; border: 1px solid #d1d5db; border-radius: 10px; box-shadow: 0 8px 32px rgba(0,0,0,0.2); overflow: hidden; font-size: 0.8rem; }
.holdings-panel-header { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: #1f2937; color: #fff; cursor: move; user-select: none; flex-shrink: 0; }
.holdings-panel-title { display: flex; align-items: center; gap: 8px; font-weight: 700; }
.holdings-panel-wallet { font-family: monospace; font-size: 0.7rem; color: #9ca3af; padding: 2px 6px; background: rgba(255,255,255,0.08); border-radius: 4px; }
.holdings-panel-actions { display: flex; gap: 4px; }
.holdings-panel-btn { width: 26px; height: 26px; border: none; border-radius: 4px; background: rgba(255,255,255,0.1); color: #fff; font-size: 0.8rem; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; }
.holdings-panel-btn:hover { background: rgba(255,255,255,0.25); }
.holdings-panel-body { flex: 1; overflow: hidden; display: flex; flex-direction: column; min-height: 0; }
/* K线区 */
.holdings-kline-section { flex: 0 0 280px; display: flex; flex-direction: column; border-bottom: 1px solid #e5e7eb; background: #fafafa; }
.holdings-kline-toolbar { display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; background: #f3f4f6; border-bottom: 1px solid #e5e7eb; flex-shrink: 0; }
.holdings-kline-title { font-weight: 600; color: #1f2937; font-size: 0.78rem; }
.holdings-kline-controls { display: flex; align-items: center; gap: 6px; }
.holdings-kline-controls select { font-size: 0.7rem; padding: 2px 4px; border: 1px solid #d1d5db; border-radius: 3px; background: #fff; }
.holdings-kline-toggle { padding: 3px 10px; font-size: 0.7rem; font-weight: 600; border: 1px solid #d1d5db; border-radius: 4px; background: #fff; color: #4b5563; cursor: pointer; }
.holdings-kline-toggle:hover { background: #f9fafb; }
.holdings-kline-toggle.active { background: #22c55e; color: #fff; border-color: #16a34a; }
.holdings-kline-wrap { flex: 1; position: relative; overflow: hidden; }
.holdings-kline-wrap.loading::after { content: "加载中..."; position: absolute; top: 8px; left: 50%; transform: translateX(-50%); padding: 2px 8px; background: rgba(0,0,0,0.6); color: #fff; font-size: 0.7rem; border-radius: 4px; }
.holdings-kline-wrap canvas { display: block; width: 100%; height: 100%; }
.holdings-kline-empty { position: absolute; top: 0; left: 0; right: 0; bottom: 0; display: flex; align-items: center; justify-content: center; color: #9ca3af; font-size: 0.78rem; pointer-events: none; }
/* 持仓列表区 */
.holdings-list-section { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.holdings-list-toolbar { display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; flex-shrink: 0; }
.holdings-list-title { font-weight: 700; color: #1f2937; font-size: 0.78rem; }
.holdings-list-meta { font-size: 0.65rem; color: #6b7280; font-family: monospace; }
.holdings-list { flex: 1; overflow-y: auto; padding: 4px; }
.holdings-list-empty { text-align: center; color: #9ca3af; padding: 24px 0; font-size: 0.78rem; }
.holdings-list-error { color: #dc2626; }
.holding-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; transition: background 0.15s; border: 1px solid transparent; }
.holding-row:hover { background: #f3f4f6; }
.holding-row.selected { background: #dbeafe; border-color: #93c5fd; }
.holding-row-main { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }
.holding-icon, .holding-icon-fallback { width: 24px; height: 24px; border-radius: 50%; flex-shrink: 0; object-fit: cover; background: #e5e7eb; }
.holding-icon-fallback { display: flex; align-items: center; justify-content: center; color: #6b7280; font-size: 0.6rem; font-weight: 700; }
.holding-row-info { flex: 1; min-width: 0; }
.holding-row-top { display: flex; align-items: baseline; gap: 6px; }
.holding-symbol { font-weight: 700; color: #1f2937; font-size: 0.78rem; }
.holding-ca { color: #9ca3af; font-size: 0.6rem; font-family: monospace; }
.holding-row-bottom { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
.holding-amount { color: #4b5563; font-size: 0.7rem; font-family: monospace; }
.holding-usd { color: #6b7280; font-size: 0.7rem; font-weight: 600; }
.holding-pnl { font-size: 0.65rem; font-weight: 700; padding: 1px 4px; border-radius: 3px; }
.pnl-positive { color: #16a34a; background: #dcfce7; }
.pnl-negative { color: #dc2626; background: #fee2e2; }
.pnl-zero { color: #6b7280; background: #f3f4f6; }
.sell-btn { padding: 4px 10px; font-size: 0.7rem; font-weight: 700; border: none; border-radius: 4px; cursor: pointer; color: #fff; background: #ef4444; flex-shrink: 0; transition: background 0.15s; }
.sell-btn:hover { background: #dc2626; }
.sell-btn:disabled { cursor: wait; opacity: 0.85; }
.sell-btn.loading { background: #6b7280; }
.sell-btn.success { background: #16a34a; }
.sell-btn.error { background: #991b1b; }
/* 弹窗 toast */
.holdings-panel-toast { position: absolute; left: 50%; bottom: 12px; transform: translateX(-50%); padding: 6px 12px; border-radius: 4px; font-size: 0.72rem; font-weight: 600; color: #fff; background: rgba(15,23,42,0.92); z-index: 10; pointer-events: none; opacity: 0; transition: opacity 0.2s; max-width: 90%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.holdings-panel-toast.show { opacity: 1; }
.holdings-panel-toast.success { background: rgba(22,163,74,0.95); }
.holdings-panel-toast.error { background: rgba(220,38,38,0.95); }
.holdings-panel-toast.warn { background: rgba(217,119,6,0.95); }
</style>
</head>
<body>
<!-- 图片大图预览 lightbox -->
<div class="image-lightbox" id="imageLightbox" onclick="closeImageLightbox()">
    <img id="lightboxImg" src="" alt="">
    <div class="close-hint">点击任意处关闭</div>
</div>
<!-- 图片悬浮预览（原位置放大弹窗） -->
<div class="image-hover-preview" id="imageHoverPreview">
    <img id="hoverPreviewImg" src="" alt="">
</div>
<!-- 复制提示 tooltip -->
<div class="copy-tooltip" id="copyTooltip"></div>
<div class="container">
    <div class="main-content">
        <div class="header">
            <h1>🐦 推文 + Token <small>· Binance Trenches</small></h1>
            <div class="migrated-bar" id="migratedBar"></div>
            <div class="header-info">
                <span class="badge" id="messageCount">加载中...</span>
                <span class="badge live">● 实时</span>
            </div>
        </div>
        <div class="card-grid" id="cardGrid"></div>
    </div>
</div>
<script>
const socket = io({ transports: ["websocket"], upgrade: false });
let allMessages = [];
// 前端内存上限：与服务端 MAX_MESSAGES 对齐，避免长时间运行后内存膨胀与查找变慢
const MAX_ALL_MESSAGES = 200;
// 按 tweet_id 索引的 Map：O(1) 查找（替代 allMessages.find）
const messageById = new Map();
const cardGrid = document.getElementById("cardGrid");
const migratedBar = document.getElementById("migratedBar");
const messageCount = document.getElementById("messageCount");
const FALLBACK_AVATAR = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='48' height='48'%3E%3Ccircle cx='24' cy='24' r='24' fill='%23cfd9de'/%3E%3C/svg%3E";

// 渲染迁移 token 横向列表
function renderMigratedBar(tokens) {
    if (!migratedBar) return;
    if (!tokens || !tokens.length) {
        migratedBar.innerHTML = "";
        return;
    }
    // 已有 DOM 按 contract 建索引，做增量 patch
    const existing = new Map();
    migratedBar.querySelectorAll(".mig-token").forEach(el => {
        existing.set(el.dataset.contract, el);
    });
    const keep = new Set();
    tokens.forEach(t => {
        const contract = String(t.contract || "").toLowerCase();
        if (!contract) return;
        keep.add(contract);
        const symbol = escapeHtml(t.symbol || "");
        const mc = t.marketCap !== undefined && t.marketCap !== null ? formatNumber(t.marketCap) : "";
        const kolHolders = t.kolHolders !== undefined && t.kolHolders !== null && String(t.kolHolders) !== "0" && String(t.kolHolders) !== "" ? t.kolHolders : "";
        const icon = t.icon ? escapeHtml(t.icon) : "";
        const iconHtml = icon
            ? `<img src="${icon}" alt="" referrerpolicy="no-referrer" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><span class="mig-icon-fallback" style="display:none;">${escapeHtml((t.symbol || "?").slice(0, 2).toUpperCase())}</span>`
            : `<span class="mig-icon-fallback">${escapeHtml((t.symbol || "?").slice(0, 2).toUpperCase())}</span>`;
        let el = existing.get(contract);
        if (el) {
            // 字段级 patch
            const symEl = el.querySelector(".mig-symbol");
            if (symEl && symEl.textContent !== symbol) symEl.textContent = symbol;
            const mcEl = el.querySelector(".mig-mc");
            const mcText = mc ? "💰" + mc : "";
            if (mcEl && mcEl.textContent !== mcText) mcEl.textContent = mcText;
            const kolEl = el.querySelector(".mig-kol");
            const kolText = kolHolders ? "KOL:" + kolHolders : "";
            if (kolEl && kolEl.textContent !== kolText) kolEl.textContent = kolText;
        } else {
            // 新建
            const div = document.createElement("div");
            div.className = "mig-token";
            div.dataset.contract = contract;
            div.title = `${t.symbol || ""} · ${mc || "—"} · KOL: ${kolHolders || "—"}`;
            div.onclick = () => openGmgnUrl(contract);
            div.innerHTML = `${iconHtml}<span class="mig-symbol">${symbol}</span>${mc ? `<span class="mig-mc">💰${mc}</span>` : ""}${kolHolders ? `<span class="mig-kol">KOL:${kolHolders}</span>` : ""}`;
            migratedBar.appendChild(div);
        }
    });
    // 移除不再存在的
    existing.forEach((el, contract) => {
        if (!keep.has(contract)) el.remove();
    });
}

// O(1) 消息查找：优先走 Map，未命中则回退到 find（兼容历史调用）
function findMessageById(tweetId) {
    if (!tweetId) return null;
    const key = String(tweetId);
    return messageById.get(key) || allMessages.find(m => String(m.tweet_id) === key) || null;
}

// 统一同步内存：allMessages + messageById 保持一致
function upsertMessage(message) {
    if (!message || !message.tweet_id) return;
    const key = String(message.tweet_id);
    const existing = messageById.get(key);
    if (existing) {
        // 已存在：原地替换 allMessages 中的条目
        const idx = allMessages.findIndex(m => String(m.tweet_id) === key);
        if (idx !== -1) allMessages[idx] = message;
        else allMessages.unshift(message);
    } else {
        // 新增：插到头部
        allMessages.unshift(message);
    }
    messageById.set(key, message);
    // 超过上限：从尾部裁剪（最老的消息）
    if (allMessages.length > MAX_ALL_MESSAGES) {
        const removed = allMessages.splice(MAX_ALL_MESSAGES);
        removed.forEach(m => {
            const k = String(m.tweet_id);
            // 仅当 Map 中对应的还是这条旧消息时才删除（避免删错）
            if (messageById.get(k) === m) messageById.delete(k);
        });
    }
}

function removeMessageById(tweetId) {
    if (!tweetId) return;
    const key = String(tweetId);
    const idx = allMessages.findIndex(m => String(m.tweet_id) === key);
    if (idx !== -1) allMessages.splice(idx, 1);
    messageById.delete(key);
}

function formatTime(ts) {
    if (!ts) return "";
    const d = typeof ts === "number" ? new Date(ts > 1e12 ? ts : ts * 1000) : new Date(ts);
    if (isNaN(d.getTime())) return "";
    const diff = (Date.now() - d) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + "分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + "小时前";
    if (diff < 604800) return Math.floor(diff / 86400) + "天前";
    return d.toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatTokenAge(createTime) {
    if (!createTime) return "—";
    let ts = Number(createTime);
    if (!Number.isFinite(ts) || ts <= 0) return "—";
    if (ts > 1e12) ts /= 1000;
    const now = Date.now() / 1000;
    const delta = now - ts;
    if (delta < 0) return "—";
    if (delta < 60) return Math.floor(delta) + "s";
    if (delta < 3600) return Math.floor(delta / 60) + "m";
    if (delta < 86400) return Math.floor(delta / 3600) + "h";
    return Math.floor(delta / 86400) + "d";
}

// ============================================================
// SVG 图标定义（从 icon_csv 文件导入，用于替换 emoji）
// ============================================================
// 图标数据：从 icons.json 异步加载（全部内联 SVG，不依赖外部文件）
// icons.json 结构：扁平 dict，0x 开头的 key 是 token（contract → {name, icon}），
// 其余 key 是平台/功能图标（flap/fourmeme/top10/kol/sniper/dev/insider/twitter → SVG string）
// ============================================================
let ICONS = { holders: "", top10: "", kol: "", sniper: "", dev: "", insider: "", twitter: "" };
let TOKEN_ICONS = {}; // contract(lowercase) -> inline SVG string
let PLATFORM_ICONS = {}; // platform name -> inline SVG string
let ICONS_LOADED = false;

// 异步加载 icons.json
fetch("/icons.json").then(r => r.json()).then(data => {
    if (!data || typeof data !== "object") return;
    Object.entries(data).forEach(([key, value]) => {
        const icon = typeof value === "object" ? (value.icon || "") : (typeof value === "string" ? value : "");
        if (!icon) return;
        if (key.startsWith("0x")) {
            TOKEN_ICONS[key.toLowerCase()] = icon;
        } else if (key === "flap" || key === "fourmeme") {
            PLATFORM_ICONS[key] = icon;
        } else {
            ICONS[key] = icon;
        }
    });
    // holders 图标在 icons.json 中可能叫 holders，如果没有则用 kol 图标作为 fallback
    if (!ICONS.holders) ICONS.holders = ICONS.kol || "";
    ICONS_LOADED = true;
    // 图标加载完成后：对所有已渲染 token 卡片执行字段级 patch，刷新 meta/badge/platform icon
    // 不触发 innerHTML 全量重建，避免闪烁和图片重拉
    refreshAllTokenCardsMeta(cardGrid);
}).catch(e => console.error("Failed to load icons.json:", e));

// 判断文本是否需要翻译：
// - 含中文 → 不需要
// - 纯表情/纯符号/纯图片 → 不需要
// - 纯链接 → 不需要
// - 纯无意义连续英文字符（如 "aaaaaaa"） → 不需要
// - 纯英文有意义内容 → 需要
function needsTranslation(text) {
    if (!text || !text.trim()) return false;
    const trimmed = text.trim();
    // 1. 含中文 → 不需要
    if (/[\u4e00-\u9fff]/.test(trimmed)) return false;
    // 2. 纯链接 → 不需要
    if (/^https?:\/\/\S+$/i.test(trimmed)) return false;
    // 3. 提取有意义的字母词（≥2个字母，含元音）
    const words = trimmed.match(/[a-zA-Z]{2,}/g) || [];
    if (words.length === 0) return false; // 无字母词 → 纯符号/表情
    // 4. 纯无意义连续英文字符（如 "aaaaaaa"） → 检查是否有至少1个有意义的词
    const meaningfulWords = words.filter(w => {
        // 排除重复单字符（如 "aaa", "bbb"）
        if (new Set(w.toLowerCase().split("")).size <= 1) return false;
        // 排除无元音的连续辅音（如 "xzxzxz"）
        if (!/[aeiouAEIOU]/.test(w)) return false;
        return true;
    });
    if (meaningfulWords.length === 0) return false;
    return true;
}

function getTweetTypeLabel(type) {
    const map = { original: "原创", replied_to: "回复", quoted: "引用", retweeted: "转推", profile: "个人资料", live: "🔴 LIVE", square: "广场", weibo: "微博" };
    return map[type] || type || "原创";
}

// 不同推文类型用不同颜色标签
function getTweetTypeBadgeClass(type) {
    const map = {
        original: "badge-type-original",
        replied_to: "badge-type-reply",
        quoted: "badge-type-quote",
        retweeted: "badge-type-retweet",
        profile: "badge-type-profile",
        live: "badge-type-live",
        square: "badge-type-square",
        weibo: "badge-type-weibo"
    };
    return map[type] || "badge-type-original";
}

function getAvatarUrl(url) {
    return (url && typeof url === "string" && url.startsWith("http")) ? url : FALLBACK_AVATAR;
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function linkify(text) {
    if (!text) return "";
    let cleaned = String(text).replace(/\r\n/g, " ").replace(/\r/g, " ").replace(/\n/g, " ").replace(/\s+/g, " ").trim();
    let html = escapeHtml(cleaned);
    html = html.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/@([A-Za-z0-9_]+)/g, '<a href="https://x.com/$1" target="_blank" rel="noopener noreferrer">@$1</a>');
    html = html.replace(/#([^\s#@<]+)/g, '<a href="https://x.com/hashtag/$1" target="_blank" rel="noopener noreferrer">#$1</a>');
    return html;
}

// 简繁统一映射（常见字符）
const TC_SC_MAP = { '發':'发', '哥':'哥', '幣':'币', '鏈':'链', '寶':'宝', '龍':'龙', '聯':'联', '網':'网', '電':'电', '車':'车', '馬':'马', '鳥':'鸟', '樂':'乐', '業':'业', '東':'东', '兩':'两', '個':'个', '為':'为', '來':'来', '區':'区', '場':'场', '頭':'头', '們':'们', '這':'这', '國':'国', '學':'学', '機':'机', '動':'动', '點':'点', '記':'记', '員':'员', '會':'会', '員':'员', '時':'时', '經':'经', '說':'说', '對':'对', '現':'现', '稱':'称', '證':'证', '計':'计', '論':'论', '講':'讲', '識':'识', '藝':'艺', '術':'术', '運':'运', '營':'营', '護':'护', '農':'农', '豐':'丰', '體':'体', '關':'关', '觀':'观', '讓':'让', '認':'认', '論':'论', '議':'议' };
function normalizeText(s) {
    if (!s) return "";
    let result = "";
    for (const ch of String(s).toLowerCase()) {
        result += TC_SC_MAP[ch] || ch;
    }
    return result;
}

// 高亮推文文本中的 token 相关内容和引号内容
// 在 linkify 之后执行，用正则只替换 HTML 标签外的纯文本部分
function highlightTokens(html, tokens) {
    if (!html || !tokens || !tokens.length) {
        // 没有tokens也要处理引号高亮
        return highlightQuotes(html);
    }

    // 收集所有 token 的 symbol 和 name，构建匹配列表
    const matchList = []; // {text: 原文, normalized: 归一化后, isFull: 是否完全匹配（name 优先）}
    const seen = new Set();
    tokens.forEach(t => {
        const symbol = (t.symbol || "").trim();
        const name = (t.name || "").trim();
        if (symbol && symbol.length >= 2 && !seen.has(normalizeText(symbol))) {
            seen.add(normalizeText(symbol));
            matchList.push({ text: symbol, normalized: normalizeText(symbol), isFull: false });
        }
        if (name && name.length >= 3 && !seen.has(normalizeText(name))) {
            seen.add(normalizeText(name));
            matchList.push({ text: name, normalized: normalizeText(name), isFull: true });
        }
    });

    // 按长度降序排列（长的先匹配，避免短的覆盖长的）
    matchList.sort((a, b) => b.text.length - a.text.length);

    if (!matchList.length) return highlightQuotes(html);

    // 把 HTML 拆成"标签内"和"标签外"的片段，只对标签外的文本做替换
    // 用占位符标记已匹配的位置，避免重复匹配
    const normalizedHtml = normalizeText(html);
    const replacements = []; // {start, end, replacementHtml}

    matchList.forEach(m => {
        const normalizedTarget = m.normalized;
        if (!normalizedTarget || normalizedTarget.length < 2) return;
        let idx = 0;
        while ((idx = normalizedHtml.indexOf(normalizedTarget, idx)) !== -1) {
            // 检查这个位置不在 HTML 标签内（<...>之间）
            // 找 idx 前面最近的 < 和 >，如果 < 在 > 后面（或没有 <），说明在标签内
            const before = html.substring(0, idx);
            const lastLT = before.lastIndexOf("<");
            const lastGT = before.lastIndexOf(">");
            // 在标签外的条件：lastGT >= lastLT（> 在 < 之后，或都没有）
            if (lastGT >= lastLT) {
                // 在标签外，可以替换
                // 检查是否已被之前的替换覆盖
                const overlap = replacements.some(r => idx >= r.start && idx < r.end);
                if (!overlap) {
                    // 获取原文（可能是大小写/简繁不同的版本）
                    const origText = html.substring(idx, idx + m.text.length);
                    // 检查是否完全匹配（大小写+简繁无关的完全匹配）
                    const isFull = m.isFull && normalizedTarget === normalizeText(origText);
                    const cls = isFull ? "hl-token-full" : "hl-token-partial";
                    replacements.push({
                        start: idx,
                        end: idx + m.text.length,
                        replacement: `<span class="${cls}">${origText}</span>`
                    });
                }
            }
            idx += m.text.length;
        }
    });

    // 从后往前替换，避免位置偏移
    replacements.sort((a, b) => b.start - a.start);
    let result = html;
    replacements.forEach(r => {
        result = result.substring(0, r.start) + r.replacement + result.substring(r.end);
    });

    return highlightQuotes(result);
}

// 高亮引号内容（"..." 或 "..." 或 「...」）
function highlightQuotes(html) {
    if (!html) return html;
    // 匹配 "..." 或 "..." 或 「...」（至少2个字符）
    // 只对标签外的文本替换
    // 用正则匹配引号对，注意不匹配 HTML 属性中的引号
    return html.replace(/&quot;([^&<>]{2,50}?)&quot;/g, '<span class="hl-quote">&quot;$1&quot;</span>');
}

function verifiedSvg() {
    return `<svg class="verified" viewBox="0 0 24 24"><path fill="currentColor" d="M20.396 11c-.018-.646-.215-1.275-.57-1.816-.354-.54-.852-.972-1.438-1.246.223-.607.27-1.264.14-1.897-.131-.634-.437-1.218-.882-1.687-.47-.445-1.053-.75-1.687-.882-.633-.13-1.29-.083-1.897.14-.273-.587-.704-1.086-1.245-1.44S11.647 1.62 11 1.604c-.646.017-1.273.213-1.813.568s-.969.854-1.24 1.44c-.608-.223-1.267-.272-1.902-.14-.635.13-1.22.436-1.69.882-.445.47-.749 1.055-.878 1.688-.13.633-.08 1.29.144 1.896-.587.274-1.087.705-1.443 1.245-.356.54-.555 1.17-.574 1.817.02.647.218 1.276.574 1.817.356.54.856.972 1.443 1.245-.224.606-.274 1.263-.144 1.896.13.634.433 1.218.877 1.688.47.443 1.054.747 1.687.878.633.132 1.29.084 1.897-.136.274.586.705 1.084 1.246 1.439.54.354 1.17.551 1.816.569.647-.016 1.276-.213 1.817-.567s.972-.854 1.245-1.44c.604.239 1.266.296 1.903.164.636-.132 1.22-.447 1.68-.907.46-.46.776-1.044.908-1.681s.075-1.299-.165-1.903c.586-.274 1.084-.705 1.439-1.246.354-.54.551-1.17.569-1.816zM9.662 14.85l-3.429-3.428 1.293-1.302 2.072 2.072 4.4-4.794 1.347 1.246z"/></svg>`;
}

function pickBestVideoSrc(videoEntry) {
    if (!videoEntry) return null;
    const variants = videoEntry.variants || [];
    const mp4s = variants.filter(v => v.contentType === "video/mp4" && v.url).sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
    if (mp4s.length) return mp4s[0].url;
    const any = variants.find(v => v.url);
    return any ? any.url : null;
}

function openVideoNoReferrer(url) {
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

function renderVideoPlayer(videoUrls, compact) {
    if (!videoUrls || !videoUrls.length) return "";
    const v = videoUrls[0];
    const src = pickBestVideoSrc(v);
    const poster = v.videoPreviewUrl || "";
    const cls = compact ? "nt-video" : "video-wrap";
    if (src) {
        return `<div class="${cls} video-placeholder" onclick="openVideoNoReferrer('${escapeHtml(src)}')">${poster ? `<img class="video-poster" src="${escapeHtml(poster)}" alt="" onerror="this.style.display='none'">` : ""}<div class="video-play-btn">▶</div></div>`;
    }
    return `<div class="${cls}"><div class="video-fallback">${poster ? `<img src="${escapeHtml(poster)}" alt="video" onerror="this.style.display='none'">` : ""}<div class="info">▶️ 视频预览（暂无可播放源）</div></div></div>`;
}

function renderArticle(article, compact) {
    if (!article) return "";
    // 优先使用翻译字段
    const title = article.titleTranslation || article.title || "文章";
    const desc = article.previewTextTranslation || article.previewText || "";
    const cover = article.coverImgUrl || "";
    // 原文作为 title 属性（鼠标悬浮显示）
    const origTitle = article.title || "";
    const origDesc = article.previewText || "";
    if (compact) {
        return `<div class="nt-article">${cover ? `<img src="${escapeHtml(cover)}" alt="cover" referrerpolicy="no-referrer" onerror="this.style.display='none'">` : ""}<div class="art-body"><div class="art-title" title="${escapeHtml(origTitle)}">${escapeHtml(title)}</div>${desc ? `<div class="art-desc" title="${escapeHtml(origDesc)}">${escapeHtml(desc)}</div>` : ""}</div></div>`;
    }
    return `<div class="article-preview">${cover ? `<img class="art-cover" src="${escapeHtml(cover)}" alt="cover" referrerpolicy="no-referrer" onerror="this.style.display='none'">` : ""}<div class="art-body"><div class="art-title" title="${escapeHtml(origTitle)}">📄 ${escapeHtml(title)}</div>${desc ? `<div class="art-desc" title="${escapeHtml(origDesc)}">${escapeHtml(desc)}</div>` : ""}<div class="art-domain">x.com · 文章</div></div></div>`;
}

function renderImages(imgUrls, compact) {
    if (!imgUrls || !imgUrls.length) return "";
    const n = imgUrls.length;
    const cls = compact ? "nt-media" : ("media-box " + (n === 1 ? "single" : n === 2 ? "double" : n === 3 ? "triple" : "quad"));
    return `<div class="${cls}">${imgUrls.map(u => `<img src="${escapeHtml(u)}" alt="" referrerpolicy="no-referrer" onerror="this.style.display='none'">`).join("")}</div>`;
}

function renderNestedTweet(tweet, depth, parentTokens) {
    depth = depth || 1;
    if (!tweet || depth > 3) return depth > 3 ? `<div class="nested-tweet"><div class="nt-body">…</div></div>` : "";
    const authorObj = tweet.author && typeof tweet.author === "object" ? tweet.author : null;
    const name = (authorObj && authorObj.name) || tweet.name || (typeof tweet.author === "string" ? tweet.author : "") || "Unknown";
    const handle = (authorObj && authorObj.handle) || tweet.handle || "";
    const avatar = getAvatarUrl((authorObj && authorObj.profileImgUrl) || tweet.avatar || tweet.profileImgUrl || "");
    const isVerified = (authorObj && authorObj.isBlueVerified) || tweet.isBlueVerified || 0;
    // 嵌套推文原文 + 翻译（textTranslation 由 API 直接返回）
    const origText = tweet.text || tweet.content || "";
    const transText = tweet.referencedTextTranslation || tweet.textTranslation || "";
    const imgUrls = tweet.imgUrls || tweet.img_urls || [];
    const videoUrls = tweet.videoUrls || [];
    const article = tweet.article || null;

    let html = `<div class="nested-tweet" onclick="openTweetUrl('${escapeHtml(tweet.tweetId || tweet.tweet_id || "")}')"><div class="nt-header"><img class="nt-avatar" src="${avatar}" alt="" referrerpolicy="no-referrer" onerror="this.src='${FALLBACK_AVATAR}'" onclick="event.stopPropagation(); openAuthorUrl('${escapeHtml(handle)}')"><span class="nt-name" onclick="event.stopPropagation(); openAuthorUrl('${escapeHtml(handle)}')">${escapeHtml(name)}${isVerified ? verifiedSvg() : ""}</span>${handle ? `<span class="nt-handle">@${escapeHtml(handle)}</span>` : ""}</div>`;
    // 嵌套推文截断：超过 NESTED_TEXT_TRUNCATE 字符截断，加"显示更多"按钮
    const NESTED_TEXT_TRUNCATE = 140;
    if (origText) {
        const needsTrunc = origText.length > NESTED_TEXT_TRUNCATE;
        if (needsTrunc) {
            const truncOrig = origText.slice(0, NESTED_TEXT_TRUNCATE);
            html += `<div class="nt-body" data-field="nt-body">${highlightTokens(linkify(truncOrig), parentTokens)}…</div>`;
            html += `<div class="nt-body-full" data-field="nt-body-full">${highlightTokens(linkify(origText), parentTokens)}</div>`;
        } else {
            html += `<div class="nt-body">${highlightTokens(linkify(origText), parentTokens)}</div>`;
        }
    }
    // 嵌套推文翻译：仅当有翻译且原文需要翻译时显示
    if (transText && needsTranslation(origText)) {
        const transNeedsTrunc = transText.length > NESTED_TEXT_TRUNCATE;
        if (transNeedsTrunc) {
            const transTrunc = transText.slice(0, NESTED_TEXT_TRUNCATE);
            html += `<div class="nt-translation" data-field="nt-translation">${highlightTokens(linkify(transTrunc), parentTokens)}…</div>`;
            html += `<div class="nt-translation-full" data-field="nt-translation-full">${highlightTokens(linkify(transText), parentTokens)}</div>`;
        } else {
            html += `<div class="nt-translation">${highlightTokens(linkify(transText), parentTokens)}</div>`;
        }
    }
    // 截断时显示"显示更多"按钮
    const showMore = (origText && origText.length > NESTED_TEXT_TRUNCATE) || (transText && transText.length > NESTED_TEXT_TRUNCATE);
    if (showMore) {
        html += `<button class="nt-show-more-btn" onclick="event.stopPropagation(); toggleNestedExpand(this)">显示更多</button>`;
    }
    if (article) html += renderArticle(article, true);
    if (imgUrls.length) html += renderImages(imgUrls, true);
    if (videoUrls.length) html += renderVideoPlayer(videoUrls, true);
    if (tweet.quotedTweet) html += renderNestedTweet(tweet.quotedTweet, depth + 1, parentTokens);
    if (tweet.repliedToTweet) html += renderNestedTweet(tweet.repliedToTweet, depth + 1, parentTokens);
    if (tweet.retweetedTweet) html += renderNestedTweet(tweet.retweetedTweet, depth + 1, parentTokens);
    html += `</div>`;
    return html;
}

// ============================================================
// 图片加载：直接使用浏览器原生 <img> 加载，依赖浏览器 HTTP 缓存实现"同一 URL 只下载一次"
// 不使用 fetch/canvas：避免 CORS 污染与预检失败
// 不使用 prefetchImage：浏览器 HTTP 缓存已经能避免重复下载，无需手动 prefetch
// ============================================================
const IMAGE_MAX_RETRIES = 3;     // 失败重试次数
const IMAGE_RETRY_DELAY = 500;   // 失败重试初始延迟（递增 500/1000/1500ms）

// 为一个 <img> 元素绑定加载：直接设置 src，浏览器原生处理缓存+加载
// 使用 referrerPolicy=no-referrer 避免 403
// 支持多次重试，加载失败后延迟重试（带缓存清除参数），最多 3 次
function bindCachedImage(imgEl, src) {
    if (!imgEl || !src) return;
    const originalSrc = imgEl.getAttribute("data-original-src") || "";
    if (originalSrc === src && imgEl.dataset.loaded === "1") return;  // 已加载同一 src
    imgEl.setAttribute("data-original-src", src);
    imgEl.dataset.loaded = "0";
    imgEl.dataset.retryCount = "0";
    imgEl.referrerPolicy = "no-referrer";

    // 加载成功：显示 img 覆盖 fallback
    imgEl.onload = () => {
        if (imgEl.getAttribute("data-original-src") === src) {
            imgEl.style.display = "block";
            imgEl.dataset.loaded = "1";
        }
    };

    // 加载失败：延迟重试（带缓存清除参数），最多 5 次，递增延迟
    // 3次不够：推文先建卡、icon URL 后到时，首次加载会 404，需要更多重试覆盖 icon 上传延迟
    imgEl.onerror = () => {
        if (imgEl.getAttribute("data-original-src") !== src) return;
        let retryCount = parseInt(imgEl.dataset.retryCount || "0", 10);
        if (retryCount >= 5) {
            imgEl.dataset.loaded = "0";
            return;
        }
        retryCount++;
        imgEl.dataset.retryCount = String(retryCount);
        const delay = 300 * retryCount;  // 300ms, 600ms, 900ms, 1200ms, 1500ms
        setTimeout(() => {
            if (imgEl.getAttribute("data-original-src") !== src) return;
            const retrySrc = src + (src.includes("?") ? "&" : "?") + "_r" + retryCount + "=" + Date.now();
            imgEl.src = retrySrc;
        }, delay);
    };

    // 直接设置 src：浏览器原生加载，自动复用 HTTP keep-alive 连接 + 浏览器缓存
    imgEl.src = src;
}

function formatNumber(n) {
    if (n === undefined || n === null) return "—";
    const num = Number(n);
    if (!Number.isFinite(num)) return "—";
    if (num >= 1e9) return (num / 1e9).toFixed(1) + "B";
    if (num >= 1e6) return (num / 1e6).toFixed(1) + "M";
    if (num >= 1e3) return (num / 1e3).toFixed(1) + "K";
    return num.toFixed(1);
}

// 根据进度返回颜色：0%=绿色 → 50%=黄色 → 100%=红色
function getProgressColor(progress) {
    // progress 0-100
    const p = Math.max(0, Math.min(100, progress));
    if (p < 50) {
        // 绿 → 黄
        const r = Math.round(34 + (250 - 34) * (p / 50));
        const g = Math.round(197 + (204 - 197) * (p / 50));
        const b = Math.round(94 + (21 - 94) * (p / 50));
        return `rgb(${r},${g},${b})`;
    } else {
        // 黄 → 红
        const t = (p - 50) / 50;
        const r = Math.round(250 + (220 - 250) * t);
        const g = Math.round(204 + (38 - 204) * t);
        const b = Math.round(21 + (38 - 21) * t);
        return `rgb(${r},${g},${b})`;
    }
}

// Token age color: <1min green, 1min-1h blue, >1h yellow
function getTokenAgeColorClass(createTime) {
    if (!createTime) return "age-yellow";
    let ts = Number(createTime);
    if (!Number.isFinite(ts) || ts <= 0) return "age-yellow";
    if (ts > 1e12) ts /= 1000;
    const delta = (Date.now() / 1000) - ts;
    if (delta < 0) return "age-yellow";
    if (delta < 60) return "age-green";
    if (delta < 3600) return "age-blue";
    return "age-yellow";
}

// Tweet age color: <1h green, <24h blue, >24h red
function getTweetAgeColorClass(timestamp) {
    if (!timestamp) return "meta-color-red";
    const ts = Number(timestamp);
    if (!Number.isFinite(ts) || ts <= 0) return "meta-color-red";
    const delta = (Date.now() - ts) / 1000;
    if (delta < 0) return "meta-color-red";
    if (delta < 3600) return "meta-color-green";
    if (delta < 86400) return "meta-color-blue";
    return "meta-color-red";
}

// Market cap color: 5-10k purple, 10-20k magenta, 20-50k darkred, >50k yellow
function getMarketCapColorClass(marketCap) {
    if (marketCap === undefined || marketCap === null) return "";
    const mc = Number(marketCap);
    if (!Number.isFinite(mc)) return "";
    if (mc < 5000) return "";
    if (mc < 10000) return "mc-purple";
    if (mc < 20000) return "mc-magenta";
    if (mc < 50000) return "mc-darkred";
    return "mc-yellow";
}

// Dev hold color: >5% red
function getDevHoldColorClass(devPercent) {
    if (devPercent === undefined || devPercent === null) return "";
    const v = Number(devPercent) * 100;
    if (v > 5) return "meta-color-red";
    return "";
}

// Sniper color: >10% red
function getSniperColorClass(sniperPercent) {
    if (sniperPercent === undefined || sniperPercent === null) return "";
    const v = Number(sniperPercent) * 100;
    if (v > 10) return "meta-color-red";
    return "";
}

// Top10 color: >30% red, <=30% green
function getTop10ColorClass(top10Percent) {
    if (top10Percent === undefined || top10Percent === null) return "";
    const v = Number(top10Percent) * 100;
    if (v > 30) return "meta-color-red";
    return "meta-color-green";
}

// KOL color: >2 yellow
function getKolColorClass(kolHolders) {
    if (kolHolders === undefined || kolHolders === null) return "";
    const v = Number(kolHolders);
    if (v > 2) return "meta-color-yellow";
    return "";
}

// Copy to clipboard with tooltip feedback
function copyToClipboard(text, event) {
    navigator.clipboard.writeText(text).then(() => {
        const tooltip = document.getElementById("copyTooltip");
        if (!tooltip) return;
        tooltip.textContent = "已复制: " + (text.length > 20 ? text.slice(0, 20) + "..." : text);
        tooltip.style.left = (event.clientX + 10) + "px";
        tooltip.style.top = (event.clientY - 30) + "px";
        tooltip.classList.add("active");
        setTimeout(() => tooltip.classList.remove("active"), 1500);
    }).catch(() => {});
}

// Open tweet on x.com
function openTweetUrl(tweetId) {
    if (tweetId && !tweetId.startsWith("profile_")) {
        window.open("https://x.com/i/status/" + tweetId, "_blank", "noopener,noreferrer");
    }
}

// Open author profile on x.com
function openAuthorUrl(handle) {
    if (handle) {
        window.open("https://x.com/" + handle, "_blank", "noopener,noreferrer");
    }
}

// Open gmgn.ai token page
function openGmgnUrl(contract) {
    if (contract) {
        window.open("https://gmgn.ai/bsc/token/" + contract, "_blank", "noopener,noreferrer");
    }
}

// ============================================================
// 一键买入：调用 /api/buy → GMGN swap（0.0001 BNB → 目标 token）
// ============================================================
const BUY_INFLIGHT = new Set();  // 正在买入中的 contract，防止重复点击

async function buyToken(contract, btn) {
    if (!contract) return;
    // 防重入：同一个 contract 在飞行中时，按钮短暂抖动提示
    if (BUY_INFLIGHT.has(contract)) {
        shakeButton(btn);
        return;
    }
    // 已经是 loading 状态，避免重复触发
    if (btn.classList.contains("is-loading")) return;

    const card = btn.closest(".token-item");
    // 准备 toast 元素（如果还没有）
    let toast = card && card.querySelector(".buy-toast");
    if (card && !toast) {
        toast = document.createElement("div");
        toast.className = "buy-toast";
        card.appendChild(toast);
    }

    const originalText = "买入";
    const setBtnState = (cls, text) => {
        btn.classList.remove("is-loading", "is-success", "is-error");
        if (cls) btn.classList.add(cls);
        btn.textContent = text;
        btn.disabled = !!cls;
    };
    const showToast = (msg, kind) => {
        if (!toast) return;
        toast.classList.remove("success", "error", "show");
        if (kind) toast.classList.add(kind);
        toast.textContent = msg;
        // 强制 reflow 后加 show，触发 transition
        void toast.offsetWidth;
        toast.classList.add("show");
    };
    const hideToast = (delay = 2500) => {
        if (!toast) return;
        setTimeout(() => toast.classList.remove("show"), delay);
    };
    const revertBtn = (delay = 2500) => {
        setTimeout(() => setBtnState("", originalText), delay);
    };

    BUY_INFLIGHT.add(contract);
    setBtnState("is-loading", "买入中…");
    showToast("正在发送 swap…", null);

    try {
        const resp = await fetch("/api/buy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ contract }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data && data.success) {
            const txHash = data.tx_hash || "";
            const orderId = data.order_id || "";
            // 优先显示 tx_hash；没有则显示 order_id；都没有则显示"已提交"
            let shortId = "已提交";
            if (txHash) {
                shortId = txHash.slice(0, 6) + "…" + txHash.slice(-4);
            } else if (orderId) {
                shortId = "order:" + orderId.slice(0, 8);
            }
            setBtnState("is-success", "✓成功");
            showToast("买入成功 " + shortId, "success");
            // 控制台保留完整信息方便查
            if (txHash) console.log("[BUY] success tx:", txHash, "contract:", contract);
            if (orderId) console.log("[BUY] order_id:", orderId, "contract:", contract);
            revertBtn(2500);
            hideToast(3500);
        } else {
            const errMsg = (data && (data.api_message || data.api_error || data.error)) || ("HTTP " + resp.status);
            setBtnState("is-error", "✗失败");
            showToast("失败: " + errMsg, "error");
            console.warn("[BUY] failed:", data, "contract:", contract);
            revertBtn(3500);
            hideToast(4500);
        }
    } catch (err) {
        setBtnState("is-error", "✗失败");
        showToast("网络异常: " + (err && err.message ? err.message : err), "error");
        console.error("[BUY] network error:", err, "contract:", contract);
        revertBtn(3500);
        hideToast(4500);
    } finally {
        BUY_INFLIGHT.delete(contract);
    }
}

function shakeButton(btn) {
    btn.animate(
        [
            { transform: "translateX(0)" },
            { transform: "translateX(-2px)" },
            { transform: "translateX(2px)" },
            { transform: "translateX(0)" },
        ],
        { duration: 180, iterations: 1 }
    );
}

function buildTokenFields(token) {
    const contract = escapeHtml(String(token.contract || "").toLowerCase());
    const icon = token.icon ? escapeHtml(token.icon) : "";
    const symbol = escapeHtml(token.symbol || "");
    const name = escapeHtml(token.name || "");
    const nameZh = token.nameZh ? escapeHtml(token.nameZh) : (token.symbolZh ? escapeHtml(token.symbolZh) : "");
    const mc = token.marketCap !== undefined && token.marketCap !== null ? formatNumber(token.marketCap) : "";
    const vol = token.volume !== undefined && token.volume !== null ? formatNumber(token.volume) : "";
    const holders = token.holders !== undefined && token.holders !== null ? token.holders : "";
    const createTime = token.createTime || token.create_time || "";
    // 买卖税率合并显示：tax 1%/1%
    const taxBuy = token.taxRateBuy !== undefined && token.taxRateBuy !== null ? (Number(token.taxRateBuy) * 100).toFixed(0) : "";
    const taxSell = token.taxRateSell !== undefined && token.taxRateSell !== null ? (Number(token.taxRateSell) * 100).toFixed(0) : "";
    const tax = (taxBuy || taxSell) ? (taxBuy || taxSell) + "/" + (taxSell || taxBuy) : "";
    // dev 持仓比例（已要求保留）
    const devHold = token.holdersDevPercent !== undefined && token.holdersDevPercent !== null ? (Number(token.holdersDevPercent) * 100).toFixed(1) + "%" : "";
    const top10 = token.holdersTop10Percent !== undefined && token.holdersTop10Percent !== null ? (Number(token.holdersTop10Percent) * 100).toFixed(1) + "%" : "";
    const sniperHold = token.holdersSniperPercent !== undefined && token.holdersSniperPercent !== null ? (Number(token.holdersSniperPercent) * 100).toFixed(1) + "%" : "";
    const insiderHold = token.holdersInsiderPercent !== undefined && token.holdersInsiderPercent !== null ? (Number(token.holdersInsiderPercent) * 100).toFixed(1) + "%" : "";
    const kolHolders = (token.kolHolders !== undefined && token.kolHolders !== null && String(token.kolHolders) !== "0" && String(token.kolHolders) !== "") ? token.kolHolders : "";
    const progress = token.progress !== undefined && token.progress !== null ? Math.max(0, Math.min(100, Number(token.progress) * 100)) : null;
    const aiNarrative = token.aiNarrative ? escapeHtml(token.aiNarrative) : "";
    const website = token.website ? escapeHtml(token.website) : "";
    const twitterUrl = token.twitterUrl ? escapeHtml(token.twitterUrl) : "";

    // meta 顺序：持仓比例优先在左侧，top10 放最前；volume 放最右边
    let metaParts = [];
    if (top10) metaParts.push({ key: "top10", text: top10, cls: "meta-top10 " + getTop10ColorClass(token.holdersTop10Percent), icon: ICONS.top10 });
    if (devHold) metaParts.push({ key: "devHold", text: devHold, cls: "meta-color-default " + getDevHoldColorClass(token.holdersDevPercent), icon: ICONS.dev });
    if (sniperHold) metaParts.push({ key: "sniper", text: sniperHold, cls: "meta-sniper " + getSniperColorClass(token.holdersSniperPercent), icon: ICONS.sniper });
    if (insiderHold) metaParts.push({ key: "insider", text: insiderHold, cls: "meta-insider", icon: ICONS.insider });
    if (kolHolders !== "") metaParts.push({ key: "kol", text: kolHolders, cls: "meta-kol " + getKolColorClass(token.kolHolders), icon: ICONS.kol });
    if (holders !== "") metaParts.push({ key: "holders", text: holders, cls: "meta-holders", icon: ICONS.holders });
    // volume 放最右边（margin-left:auto 实现）
    if (vol) metaParts.push({ key: "vol", text: "Vol " + vol, cls: "meta-volume" });

    // tax 显示在 badge 位置：买卖均 <= 1% 用绿色，否则用灰色
    let badges = [];
    if (tax) {
        const taxBuyNum = taxBuy ? parseFloat(taxBuy) : 0;
        const taxSellNum = taxSell ? parseFloat(taxSell) : 0;
        const taxCls = (taxBuyNum <= 1 && taxSellNum <= 1) ? "badge-tax-low" : "badge-tax";
        badges.push({ key: "tax", text: "税 " + tax, cls: taxCls });
    }
    // dividendQuoteAddress 图标（从 icons.json 匹配）
    const divQuoteAddr = token.dividendQuoteAddress || (token.tokenRaw && token.tokenRaw.dividendQuoteAddress) || "";
    // Check if divQuoteAddr matches any key in TOKEN_ICONS (inline SVG)
    if (divQuoteAddr && TOKEN_ICONS) {
        const matchedIcon = TOKEN_ICONS[String(divQuoteAddr).toLowerCase()];
        if (matchedIcon) {
            badges.push({ key: "divQuote", text: "", cls: "badge-neutral", iconHtml: '<span class="div-quote-icon">' + matchedIcon + '</span>' });
        }
    }
    if (token.insiderWashTrading) badges.push({ key: "insiderWash", text: "疑似刷量", cls: "badge-warn" });
    if (token.paidOnDexScreener) badges.push({ key: "dexScreener", text: "Dex加速", cls: "badge-info" });
    if (token.migrateStatus === 1) badges.push({ key: "migrated", text: "已迁移", cls: "badge-neutral" });

    return {
        contract, icon, symbol, name, nameZh, mc, vol, holders, createTime,
        tax, devHold, top10, sniperHold, insiderHold, kolHolders,
        progress, aiNarrative, website, twitterUrl, metaParts, badges,
        tokenAgeColorClass: getTokenAgeColorClass(createTime),
        marketCapColorClass: getMarketCapColorClass(token.marketCap),
        tokenRaw: token
    };
}

function getPlatformFromContract(contract) {
    if (!contract) return null;
    const c = contract.toLowerCase();
    if (c.endsWith("4444") || c.endsWith("ffff")) return "fourmeme";
    if (c.endsWith("7777") || c.endsWith("8888")) return "flap";
    return null;
}

// 前端兜底过滤：判断 token 是否应该渲染
// 对 marketCap 为空/0 且已过 grace period（60s）的 token 返回 false
// 这是后端过滤的兜底，防止后端漏网时前端仍显示无市值的 OLD token
const TOKEN_GRACE_PERIOD_MS = 60 * 1000;  // 60 秒，与后端 NEW_TOKEN_GRACE_SECONDS 对齐
function shouldRenderToken(token) {
    if (!token) return false;
    const mc = Number(token.marketCap || 0);
    let ct = Number(token.createTime || token.create_time || 0);
    if (ct > 1e12) ct = ct / 1000;  // 毫秒转秒
    if (ct <= 0) return true;  // 没有 createTime → 默认允许显示
    const ageSec = (Date.now() / 1000) - ct;
    // 0-30s：始终显示（新 token 给机会，不管市值）
    if (ageSec < 30) return true;
    // 30-60s：市值 >= 5k 才显示
    if (mc > 0) {
        if (ageSec < 60) return mc >= 5000;       // 30-60s: 5k
        return mc >= 6000;                         // 60s+: 6k
    }
    // 市值为空 + 过 grace period → 不显示
    return false;
}

function renderTokenCard(token, isEarlyToken) {
    // 前端兜底：marketCap 为空且过 grace period 的 token 不渲染
    if (!shouldRenderToken(token)) return "";
    const f = buildTokenFields(token);
    const createTimeStr = escapeHtml(String(f.createTime));
    const age = formatTokenAge(f.createTime);
    // icon：使用 icon-fallback div + 内部 img（缓存加载），占满整个卡片高度
    // icon-wrap：包含 progress-border（conic-gradient 进度环）+ icon + icon-fallback
    // progress 为 null 时显示灰色边框包裹整个图标
    const progressStyle = f.progress !== null
        ? `--progress-pct:${f.progress.toFixed(0)};--progress-color:${getProgressColor(f.progress)};`
        : `--progress-pct:0;--progress-color:#cbd5e1;`;
    const progressBorderHtml = `<div class="progress-border" data-field="progress-border" style="${progressStyle}"${f.progress !== null ? ` title="Bonding Curve 进度 ${f.progress.toFixed(0)}%"` : ""}></div>`;
    const platform = getPlatformFromContract(f.contract);
    const platformBadge = platform && PLATFORM_ICONS[platform] ? `<span class="platform-badge">${PLATFORM_ICONS[platform]}</span>` : "";
    const iconHtml = `<div class="icon-wrap" data-field="icon" onclick="openGmgnUrl('${f.contract}')">${progressBorderHtml}${
        f.icon
            ? `<div class="icon-fallback" data-field="icon-fallback">${escapeHtml((f.symbol || "?").slice(0, 2).toUpperCase())}</div><img class="icon" data-field="icon-img" alt="" src="${escapeHtml(f.icon)}" referrerpolicy="no-referrer" style="display:none;" onerror="this.style.display='none'">`
            : `<div class="icon-fallback" data-field="icon-fallback">${escapeHtml((f.symbol || "?").slice(0, 2).toUpperCase())}</div><img class="icon" data-field="icon-img" alt="" style="display:none;">`
    }${platformBadge}</div>`;
    const caShort = f.contract ? f.contract.slice(0, 4) + "..." + f.contract.slice(-4) : "";
    const iconColumnHtml = `<div class="icon-column">${iconHtml}<div class="ca-display" onclick="event.stopPropagation(); copyToClipboard('${f.contract}', event)" title="点击复制合约地址">${caShort}</div></div>`;
    // symbol 行右侧：仅 marketCap（volume 已移到 meta，age 移到 social 前）
    const rightHtml = `<div class="symbol-right" data-field="symbol-right">${
        f.mc ? `<span class="market-cap ${f.marketCapColorClass}" data-field="market-cap">💰${f.mc}</span>` : ""
    }</div>`;
    return `<div class="token-item${isEarlyToken ? " has-early-tag" : ""}" data-contract="${f.contract}" data-create-time="${createTimeStr}">
        ${iconColumnHtml}
        <div class="info">
            <div class="symbol-row">
                <div class="symbol-left" data-field="symbol-left">
                    <span class="symbol" data-field="symbol" onclick="copyToClipboard('${f.symbol}', event)">${f.symbol}</span>${isEarlyToken ? `<span class="token-early-tag" title="同推文5分钟内最早创建">早</span>` : ""}${f.name ? `<span class="name" data-field="name" onclick="copyToClipboard('${f.contract}', event)">${f.name}</span>` : ""}${f.nameZh ? `<span class="name-zh" data-field="name-zh">${f.nameZh}</span>` : ""}
                </div>
                ${rightHtml}
            </div>
            <div class="meta" data-field="meta">${f.metaParts.map(p => `<span data-meta-key="${escapeHtml(p.key)}"${p.cls ? ` class="${p.cls}"` : ""}>${p.icon || ""}${escapeHtml(p.text)}</span>`).join("")}</div>
            ${f.badges.length ? `<div class="badges" data-field="badges">${f.badges.map(b => `<span class="badge ${b.cls}" data-badge-key="${escapeHtml(b.key)}">${b.iconHtml || ""}${escapeHtml(b.text)}</span>`).join("")}</div>` : ""}
            <div class="age-row" data-field="age-row"><span class="age ${f.tokenAgeColorClass}" data-field="age">${age}</span>${(f.website || f.twitterUrl) ? `<div class="social-links" data-field="social">${f.website ? `<a href="${f.website}" target="_blank" rel="noopener noreferrer" title="官网">🌐</a>` : ""}${f.twitterUrl ? `<a href="${f.twitterUrl}" target="_blank" rel="noopener noreferrer" title="推特">${ICONS.twitter}</a>` : ""}</div>` : ""}<button class="buy-btn" data-field="buy-btn" onclick="event.stopPropagation(); buyToken('${f.contract}', this)" title="买入 0.0001 BNB">买入</button></div>
            ${f.aiNarrative ? `<div class="ai-narrative" data-field="ai-narrative" title="${f.aiNarrative}">${f.aiNarrative}</div>` : `<div class="ai-narrative ai-narrative-placeholder" data-field="ai-narrative">&nbsp;</div>`}
        </div>
    </div>`;
}

// ============================================================
// 关键：字段级 patch 工具，确保只有真正变化的文字才被改写
// ============================================================

function setTextIfChanged(el, newText) {
    if (!el) return false;
    if (el.textContent !== newText) {
        el.textContent = newText;
        return true;
    }
    return false;
}

function setAttrIfChanged(el, name, value) {
    if (!el) return false;
    const next = String(value);
    const current = el.getAttribute(name);
    if (current !== next) {
        if (next === "" || next === "null") el.removeAttribute(name);
        else el.setAttribute(name, next);
        return true;
    }
    return false;
}

function setStyleIfChanged(el, prop, value) {
    if (!el) return false;
    if (el.style[prop] !== value) {
        el.style[prop] = value;
        return true;
    }
    return false;
}

// 在父元素中按既定顺序插入新元素：meta → badges → age-row → ai-narrative
// 注意：progress 现在在 icon-wrap 内（不在 info 中），不参与该顺序
const TOKEN_FIELD_ORDER = ["meta", "badges", "age-row", "ai-narrative"];
function insertTokenFieldInOrder(infoEl, newEl, fieldName) {
    const idx = TOKEN_FIELD_ORDER.indexOf(fieldName);
    let anchor = null;
    for (let i = idx + 1; i < TOKEN_FIELD_ORDER.length; i++) {
        anchor = infoEl.querySelector(`[data-field="${TOKEN_FIELD_ORDER[i]}"]`);
        if (anchor) break;
    }
    if (anchor) infoEl.insertBefore(newEl, anchor);
    else infoEl.appendChild(newEl);
}

// 在 symbol-left 内按顺序插入：symbol → name → name-zh
const SYMBOL_LEFT_ORDER = ["symbol", "name", "name-zh"];
function insertSymbolLeftField(symbolLeftEl, newEl, fieldName) {
    const idx = SYMBOL_LEFT_ORDER.indexOf(fieldName);
    let anchor = null;
    for (let i = idx + 1; i < SYMBOL_LEFT_ORDER.length; i++) {
        anchor = symbolLeftEl.querySelector(`[data-field="${SYMBOL_LEFT_ORDER[i]}"]`);
        if (anchor) break;
    }
    if (anchor) symbolLeftEl.insertBefore(newEl, anchor);
    else symbolLeftEl.appendChild(newEl);
}

// 在 symbol-right 内按顺序插入：market-cap（volume 已移到 meta，age 已移到 age-row）
const SYMBOL_RIGHT_ORDER = ["market-cap"];
function insertSymbolRightField(symbolRightEl, newEl, fieldName) {
    const idx = SYMBOL_RIGHT_ORDER.indexOf(fieldName);
    let anchor = null;
    for (let i = idx + 1; i < SYMBOL_RIGHT_ORDER.length; i++) {
        anchor = symbolRightEl.querySelector(`[data-field="${SYMBOL_RIGHT_ORDER[i]}"]`);
        if (anchor) break;
    }
    if (anchor) symbolRightEl.insertBefore(newEl, anchor);
    else symbolRightEl.appendChild(newEl);
}

// 字段级 patch：仅更新真正变化的文字/属性，绝不触碰 .age（由 1s 定时器负责）
function patchTokenCard(el, token) {
    if (!el || !token) return;
    const f = buildTokenFields(token);

    // 0. 更新 createTime dataset（但不改 .age 文字）
    if (f.createTime && el.dataset.createTime !== String(f.createTime)) {
        el.dataset.createTime = String(f.createTime);
    }

    // 1. icon：优先用 TOKEN_ICONS 内联 SVG，否则用 CDN URL 加载
    const contractLower = f.contract ? f.contract.toLowerCase() : "";
    if (contractLower && TOKEN_ICONS[contractLower]) {
        // 内联 SVG：替换 icon-fallback 内容，隐藏 icon-img
        const fallbackEl = el.querySelector('[data-field="icon-fallback"]');
        if (fallbackEl) {
            fallbackEl.innerHTML = TOKEN_ICONS[contractLower];
            fallbackEl.style.display = "flex";
        }
        const iconImgEl2 = el.querySelector('[data-field="icon-img"]');
        if (iconImgEl2) iconImgEl2.style.display = "none";
    } else if (f.icon) {
        let iconImgEl = el.querySelector('[data-field="icon-img"]');
        if (!iconImgEl) {
            // icon 从「无」变「有」：DOM 里当初没有 img 节点，补建一个
            const iconWrap = el.querySelector('[data-field="icon"]');
            if (iconWrap) {
                iconImgEl = document.createElement("img");
                iconImgEl.className = "icon";
                iconImgEl.dataset.field = "icon-img";
                iconImgEl.alt = "";
                iconImgEl.style.display = "none";
                iconWrap.insertBefore(iconImgEl, iconWrap.querySelector(".platform-badge") || null);
            }
        }
        if (iconImgEl) {
            // 仅在 icon URL 变化时才重新绑定（避免每次 patch 都重新加载图片）
            const currentSrc = iconImgEl.getAttribute("data-original-src") || "";
            if (currentSrc !== f.icon) {
                bindCachedImage(iconImgEl, f.icon);
            }
        }
    }

    // 2. symbol
    const symbolEl = el.querySelector('[data-field="symbol"]');
    if (symbolEl) setTextIfChanged(symbolEl, f.symbol);

    // 3. name：可能需要新增/移除（symbol 之后）
    let nameEl = el.querySelector('[data-field="name"]');
    if (f.name) {
        if (nameEl) {
            setTextIfChanged(nameEl, f.name);
        } else {
            nameEl = document.createElement("span");
            nameEl.className = "name";
            nameEl.dataset.field = "name";
            nameEl.textContent = f.name;
            const symbolLeftEl = el.querySelector('[data-field="symbol-left"]');
            if (symbolLeftEl) insertSymbolLeftField(symbolLeftEl, nameEl, "name");
        }
    } else if (nameEl) {
        nameEl.remove();
    }

    // 4. name-zh：在 name 之后
    let nameZhEl = el.querySelector('[data-field="name-zh"]');
    if (f.nameZh) {
        if (nameZhEl) {
            setTextIfChanged(nameZhEl, f.nameZh);
        } else {
            nameZhEl = document.createElement("span");
            nameZhEl.className = "name-zh";
            nameZhEl.dataset.field = "name-zh";
            nameZhEl.textContent = f.nameZh;
            const symbolLeftEl = el.querySelector('[data-field="symbol-left"]');
            if (symbolLeftEl) insertSymbolLeftField(symbolLeftEl, nameZhEl, "name-zh");
        }
    } else if (nameZhEl) {
        nameZhEl.remove();
    }

    // 5. symbol-right 中的 market-cap（volume 已移到 meta，age 已移到 age-row）
    // market-cap
    let mcEl = el.querySelector('[data-field="market-cap"]');
    if (f.mc) {
        const newText = "💰" + f.mc;
        if (!mcEl) {
            mcEl = document.createElement("span");
            mcEl.className = "market-cap";
            mcEl.dataset.field = "market-cap";
            mcEl.textContent = newText;
            const srEl = el.querySelector('[data-field="symbol-right"]');
            if (srEl) insertSymbolRightField(srEl, mcEl, "market-cap");
        } else {
            setTextIfChanged(mcEl, newText);
        }
    } else if (mcEl) {
        mcEl.remove();
    }

    // 6. meta：key 序列相同 → 只 patch text + class；否则重建该容器
    // volume 现在作为 meta 中的 key="vol" 项，由这里统一处理
    const metaEl = el.querySelector('[data-field="meta"]');
    if (metaEl) {
        const oldSpans = Array.from(metaEl.querySelectorAll('span[data-meta-key]'));
        const oldKeys = oldSpans.map(s => s.dataset.metaKey);
        const newKeys = f.metaParts.map(p => p.key);
        const keysMatch = oldKeys.length === newKeys.length && oldKeys.every((k, i) => k === newKeys[i]);
        if (keysMatch) {
            oldSpans.forEach((span, i) => {
                // 重建 innerHTML 保留 icon + 更新 text（避免 setTextIfChanged 清掉 icon）
                const iconHtml = f.metaParts[i].icon || "";
                const newText = f.metaParts[i].text;
                const newInner = iconHtml + escapeHtml(newText);
                if (span.innerHTML !== newInner) {
                    span.innerHTML = newInner;
                }
                const newCls = f.metaParts[i].cls || "";
                if (span.className !== newCls) span.className = newCls;
            });
        } else {
            metaEl.innerHTML = f.metaParts.map(p => `<span data-meta-key="${escapeHtml(p.key)}"${p.cls ? ` class="${p.cls}"` : ""}>${p.icon || ""}${escapeHtml(p.text)}</span>`).join("");
        }
    }

    // 7. badges：可能整体新增/移除；存在时按 key diff
    let badgesEl = el.querySelector('[data-field="badges"]');
    if (f.badges.length) {
        if (!badgesEl) {
            badgesEl = document.createElement("div");
            badgesEl.className = "badges";
            badgesEl.dataset.field = "badges";
            badgesEl.innerHTML = f.badges.map(b => `<span class="badge ${b.cls}" data-badge-key="${escapeHtml(b.key)}">${b.iconHtml || ""}${escapeHtml(b.text)}</span>`).join("");
            const infoEl = el.querySelector('.info');
            if (infoEl) insertTokenFieldInOrder(infoEl, badgesEl, "badges");
        } else {
            const oldSpans = Array.from(badgesEl.querySelectorAll('span[data-badge-key]'));
            const oldKeys = oldSpans.map(s => s.dataset.badgeKey);
            const newKeys = f.badges.map(b => b.key);
            const keysMatch = oldKeys.length === newKeys.length && oldKeys.every((k, i) => k === newKeys[i]);
            if (keysMatch) {
                oldSpans.forEach((span, i) => {
                    // 重建 innerHTML 保留 iconHtml + 更新 text
                    const iconHtml = f.badges[i].iconHtml || "";
                    const newText = f.badges[i].text;
                    const newInner = iconHtml + escapeHtml(newText);
                    if (span.innerHTML !== newInner) {
                        span.innerHTML = newInner;
                    }
                    const newCls = "badge " + f.badges[i].cls;
                    if (span.className !== newCls) span.className = newCls;
                });
            } else {
                badgesEl.innerHTML = f.badges.map(b => `<span class="badge ${b.cls}" data-badge-key="${escapeHtml(b.key)}">${b.iconHtml || ""}${escapeHtml(b.text)}</span>`).join("");
            }
        }
    } else if (badgesEl) {
        badgesEl.remove();
    }

    // 8. progress-border：更新 CSS 变量（progress-pct + progress-color）和 title
    // progress 为 null 时显示灰色边框（--progress-color: #cbd5e1）
    let progressBorderEl = el.querySelector('[data-field="progress-border"]');
    const newPct = f.progress !== null ? f.progress.toFixed(0) : "0";
    const newColor = f.progress !== null ? getProgressColor(f.progress) : "#cbd5e1";
    const iconWrap = el.querySelector('[data-field="icon"]');
    if (!progressBorderEl && iconWrap) {
        progressBorderEl = document.createElement("div");
        progressBorderEl.className = "progress-border";
        progressBorderEl.dataset.field = "progress-border";
        progressBorderEl.style.setProperty("--progress-pct", newPct);
        progressBorderEl.style.setProperty("--progress-color", newColor);
        if (f.progress !== null) progressBorderEl.title = "Bonding Curve 进度 " + newPct + "%";
        else progressBorderEl.removeAttribute("title");
        iconWrap.insertBefore(progressBorderEl, iconWrap.firstChild);
    } else if (progressBorderEl) {
        if (f.progress !== null) setAttrIfChanged(progressBorderEl, "title", "Bonding Curve 进度 " + newPct + "%");
        else progressBorderEl.removeAttribute("title");
        const oldPct = progressBorderEl.style.getPropertyValue("--progress-pct");
        if (oldPct !== newPct) progressBorderEl.style.setProperty("--progress-pct", newPct);
        const oldColor = progressBorderEl.style.getPropertyValue("--progress-color");
        if (oldColor !== newColor) progressBorderEl.style.setProperty("--progress-color", newColor);
    }

    // 9. age-row：social-links + age 的容器，确保存在
    // age 文本由 1s 定时器负责，这里只确保 age-row + social 结构正确
    // 顺序约定：age | social-links | buy-btn（buy-btn 永远在最右侧）
    let ageRowEl = el.querySelector('[data-field="age-row"]');
    if (!ageRowEl) {
        ageRowEl = document.createElement("div");
        ageRowEl.className = "age-row";
        ageRowEl.dataset.field = "age-row";
        const infoEl = el.querySelector('.info');
        if (infoEl) insertTokenFieldInOrder(infoEl, ageRowEl, "age-row");
    }
    // 确保 buy-btn 存在（age-row 重建时丢失则补建）
    let buyBtnEl = ageRowEl.querySelector('[data-field="buy-btn"]');
    if (!buyBtnEl) {
        buyBtnEl = document.createElement("button");
        buyBtnEl.className = "buy-btn";
        buyBtnEl.dataset.field = "buy-btn";
        buyBtnEl.textContent = "买入";
        buyBtnEl.title = "买入 0.0001 BNB";
        const contractAttr = f.contract.replace(/'/g, "\\'");
        buyBtnEl.setAttribute("onclick", `event.stopPropagation(); buyToken('${contractAttr}', this)`);
        ageRowEl.appendChild(buyBtnEl);
    }

    // social：放在 age-row 内部（social 在前，age 在后）
    let socialEl = ageRowEl ? ageRowEl.querySelector('[data-field="social"]') : el.querySelector('[data-field="social"]');
    const expectedCount = (f.website ? 1 : 0) + (f.twitterUrl ? 1 : 0);
    if (expectedCount > 0) {
        if (!socialEl) {
            socialEl = document.createElement("div");
            socialEl.className = "social-links";
            socialEl.dataset.field = "social";
            let html = "";
            if (f.website) html += `<a href="${f.website}" target="_blank" rel="noopener noreferrer" title="官网">🌐</a>`;
            if (f.twitterUrl) html += `<a href="${f.twitterUrl}" target="_blank" rel="noopener noreferrer" title="推特">${ICONS.twitter}</a>`;
            socialEl.innerHTML = html;
            // 插入到 age-row 的最前面（age 在最后）→ 改为：插入到 age 之后、buy-btn 之前
            // 由于此时 social 是新插入的，age 可能尚未创建；先插到最前，age 后续会插到 social 前
            if (ageRowEl) ageRowEl.insertBefore(socialEl, ageRowEl.firstChild);
        } else if (socialEl.children.length !== expectedCount) {
            let html = "";
            if (f.website) html += `<a href="${f.website}" target="_blank" rel="noopener noreferrer" title="官网">🌐</a>`;
            if (f.twitterUrl) html += `<a href="${f.twitterUrl}" target="_blank" rel="noopener noreferrer" title="推特">${ICONS.twitter}</a>`;
            socialEl.innerHTML = html;
        } else {
            // 仅 patch href（顺序：website 在前，twitter 在后）
            const links = socialEl.querySelectorAll('a');
            let i = 0;
            if (f.website) { setAttrIfChanged(links[i++], "href", f.website); }
            if (f.twitterUrl) { setAttrIfChanged(links[i++], "href", f.twitterUrl); }
        }
    } else if (socialEl) {
        socialEl.remove();
    }

    // age 元素：确保存在（文本由 1s 定时器负责，这里只确保元素在 age-row 中、buy-btn 之前）
    let ageEl = el.querySelector('[data-field="age"]');
    if (!ageEl && ageRowEl) {
        ageEl = document.createElement("span");
        ageEl.className = "age";
        ageEl.dataset.field = "age";
        ageEl.textContent = formatTokenAge(f.createTime);
        // 插入到 age-row 的最前面（age 在 social 之前，buy-btn 永远在最后）
        ageRowEl.insertBefore(ageEl, ageRowEl.firstChild);
    }

    // 10. ai-narrative：text + title
    let aiEl = el.querySelector('[data-field="ai-narrative"]');
    if (f.aiNarrative) {
        if (!aiEl) {
            aiEl = document.createElement("div");
            aiEl.className = "ai-narrative";
            aiEl.dataset.field = "ai-narrative";
            aiEl.title = f.aiNarrative;
            aiEl.textContent = f.aiNarrative;
            const infoEl = el.querySelector('.info');
            if (infoEl) infoEl.appendChild(aiEl);
        } else {
            setAttrIfChanged(aiEl, "title", f.aiNarrative);
            setTextIfChanged(aiEl, f.aiNarrative);
        }
    } else if (aiEl) {
        aiEl.remove();
    }
}

function renderTweetCard(data, isNew) {
    const isProfile = data.tweetType === "profile";
    const authorObj = data.authorObj || (data.author && typeof data.author === "object" ? data.author : null);
    const avatar = getAvatarUrl((authorObj && authorObj.profileImgUrl) || data.avatar || data.profileImgUrl || "");
    const name = (authorObj && authorObj.name) || data.author || "Unknown";
    const handle = (authorObj && authorObj.handle) || data.handle || "";
    const isVerified = (authorObj && authorObj.isBlueVerified) || data.isBlueVerified || 0;
    // 原文 + 翻译分开（翻译独立显示区）
    const originalText = data.text || data.content || "";
    const translationText = data.tweetTextTranslation || "";
    const tweetType = data.tweetType || data.tweet_type || "original";
    const timestamp = data.timestamp || data.createdAt;
    const likeCnt = data.likeCnt !== undefined ? data.likeCnt : (data.likes || 0);
    const retweetCnt = data.retweetCnt !== undefined ? data.retweetCnt : (data.retweets || 0);
    const replyCnt = data.replyCnt !== undefined ? data.replyCnt : (data.replies || 0);
    const quoteCnt = data.quoteCnt || 0;
    const videoUrls = data.videoUrls || [];
    const imgUrls = data.imgUrls || data.img_urls || [];
    const quotedTweet = data.quotedTweet || null;
    const repliedToTweet = data.repliedToTweet || null;
    const retweetedTweet = data.retweetedTweet || null;
    const article = data.article || null;
    const tokens = data.tokens || [];
    let leftHtml = `<div class="tweet-left" onclick="openTweetUrl('${escapeHtml(String(data.tweet_id))}')">`;
    if (tweetType === "replied_to" && repliedToTweet) {
        const rHandle = (repliedToTweet.author && typeof repliedToTweet.author === "object" && repliedToTweet.author.handle) || repliedToTweet.handle || "";
        leftHtml += `<div class="reply-context">回复<a href="#">@${escapeHtml(rHandle)}</a></div>`;
    }
    // profileBannerUrl 作为 card-header 背景，半透明遮罩
    const bannerUrl = (authorObj && authorObj.profileBannerUrl) || data.profileBannerUrl || "";
    const bannerStyle = bannerUrl ? ` style="background-image: url('${escapeHtml(bannerUrl)}'); background-size: cover; background-position: center;"` : "";
    const overlayHtml = bannerUrl ? `<div class="card-header-overlay"></div>` : "";
    // trigger-count 标签：第 N 次触发建卡，颜色按次数分级
    const triggerCount = Number(data.trigger_count || 0);
    let tcCls = "trigger-count";
    if (triggerCount === 1) tcCls += " tc-first";
    else if (triggerCount >= 2 && triggerCount <= 5) tcCls += " tc-few";
    else if (triggerCount >= 6) tcCls += " tc-many";
    const triggerHtml = triggerCount > 0 ? `<span class="${tcCls}" data-field="trigger-count" title="该推文第 ${triggerCount} 次触发建卡">#${triggerCount}</span>` : "";
    leftHtml += `<div class="card-header"${bannerStyle}>${overlayHtml}<img class="avatar" src="${avatar}" alt="${escapeHtml(name)}" referrerpolicy="no-referrer" onerror="this.src='${FALLBACK_AVATAR}'" onclick="event.stopPropagation(); openAuthorUrl('${escapeHtml(handle)}')"><div class="author-info"><div class="author-name-row"><span class="author-name" onclick="event.stopPropagation(); openAuthorUrl('${escapeHtml(handle)}')">${escapeHtml(name)}${isVerified ? verifiedSvg() : ""}</span></div><div class="author-meta-row">${handle ? `<span class="author-handle">@${escapeHtml(handle)}</span>` : ""}<span class="dot">·</span><span class="author-handle ${getTweetAgeColorClass(timestamp)}">${formatTime(timestamp)}</span>${triggerHtml}</div></div></div>`;
    // 推文文本 + 翻译显示区：长文本截断，点击"显示更多"展开
    const TWEET_TEXT_TRUNCATE = 280;  // 超过此长度截断
    const isArticleLinkOnly = article && /^https?:\/\/x\.com\/i\/article\//.test(originalText.trim());
    if (originalText && !isArticleLinkOnly) {
        const needsTrunc = originalText.length > TWEET_TEXT_TRUNCATE;
        const truncatedOrig = needsTrunc ? originalText.slice(0, TWEET_TEXT_TRUNCATE) : originalText;
        const origFullHtml = highlightTokens(linkify(originalText), tokens);
        const origTruncHtml = highlightTokens(linkify(truncatedOrig), tokens);
        // 原文
        leftHtml += `<div class="tweet-text" data-field="tweet-text">${origTruncHtml}</div>`;
        if (needsTrunc) {
            // 隐藏的完整原文
            leftHtml += `<div class="tweet-text-full" data-field="tweet-text-full" style="display:none;">${origFullHtml}</div>`;
        }
        // 翻译显示区（收到 translation_update 后填充）
        if (translationText) {
            const transNeedsTrunc = translationText.length > TWEET_TEXT_TRUNCATE;
            const transTrunc = transNeedsTrunc ? translationText.slice(0, TWEET_TEXT_TRUNCATE) : translationText;
            const transFullHtml = highlightTokens(linkify(translationText), tokens);
            const transTruncHtml = highlightTokens(linkify(transTrunc), tokens);
            leftHtml += `<div class="tweet-translation" data-field="tweet-translation">${transTruncHtml}</div>`;
            if (transNeedsTrunc) {
                leftHtml += `<div class="tweet-translation-full" data-field="tweet-translation-full" style="display:none;">${transFullHtml}</div>`;
            }
        } else {
            // 占位：翻译未到达时显示空容器，便于后续 patch
            leftHtml += `<div class="tweet-translation" data-field="tweet-translation" style="display:none;"></div>`;
        }
        // "显示更多"/"收起" 按钮（仅当原文或翻译需要截断时显示）+ "显示翻译" 按钮（仅当需要翻译且无翻译时显示）
        if (needsTrunc || (translationText && translationText.length > TWEET_TEXT_TRUNCATE)) {
            leftHtml += `<button class="show-more-btn" data-field="show-more-btn" onclick="event.stopPropagation(); toggleTweetExpand(this)">显示更多</button>`;
        }
        // 手动显示翻译按钮（仅当内容需要翻译且当前无翻译时显示）
        if (!translationText && needsTranslation(originalText)) {
            leftHtml += `<button class="show-more-btn" onclick="event.stopPropagation(); refreshTranslation(this)">显示翻译</button>`;
        }
    }
    if (article) leftHtml += renderArticle(article, false);
    if (quotedTweet) leftHtml += renderNestedTweet(quotedTweet, 1, tokens);
    if (repliedToTweet) leftHtml += renderNestedTweet(repliedToTweet, 1, tokens);
    if (retweetedTweet) leftHtml += renderNestedTweet(retweetedTweet, 1, tokens);
    if (imgUrls && imgUrls.length) leftHtml += renderImages(imgUrls, false);
    if (videoUrls && videoUrls.length) leftHtml += renderVideoPlayer(videoUrls, false);
    let metaHtml = "";
    if (isProfile) {
        const followers = (authorObj && authorObj.followersCnt) || 0;
        if (followers) metaHtml += `<span>👥 ${followers} 关注者</span>`;
    } else {
        metaHtml = `<span>❤️ ${likeCnt}</span><span>🔁 ${retweetCnt}</span><span>💬 ${replyCnt}</span><span>📎 ${quoteCnt}</span>`;
    }
    // tweet-type-badge 在 tweet-meta 内靠右显示，不同类型不同颜色
    const typeBadgeCls = getTweetTypeBadgeClass(tweetType);
    metaHtml += `<span class="tweet-type-badge ${typeBadgeCls} tweet-type-badge-wrap">${escapeHtml(getTweetTypeLabel(tweetType))}</span>`;
    leftHtml += `<div class="tweet-meta">${metaHtml}</div></div>`;
    let rightHtml = '<div class="tweet-right">';
    let renderedTokenCount = 0;
    if (tokens.length) {
        // 找到 createTime 最小的 token（时间最早），当 token > 2 个且都在5分钟内时标记为"早"
        // 同一推文同时只能有一个"早"标签：即使多个 token 的 createTime 完全相同，
        // 也只取第一个匹配的（用 break 跳出循环）
        let earlyContract = null;
        if (tokens.length >= 2) {
            let minTime = Infinity, maxTime = 0;
            tokens.forEach(t => {
                const ct = Number(t.createTime || t.create_time || 0);
                if (ct > 0) { if (ct < minTime) minTime = ct; if (ct > maxTime) maxTime = ct; }
            });
            if (minTime !== Infinity && (maxTime - minTime) <= 300000) { // 5 minutes = 300000ms
                for (const t of tokens) {
                    const ct = Number(t.createTime || t.create_time || 0);
                    if (ct === minTime) {
                        earlyContract = String(t.contract || "").toLowerCase();
                        break;  // 只取第一个，确保唯一
                    }
                }
            }
        }
        tokens.forEach(token => {
            const isEarly = earlyContract && String(token.contract || "").toLowerCase() === earlyContract;
            const tokenHtml = renderTokenCard(token, isEarly);
            if (tokenHtml) renderedTokenCount++;
            rightHtml += tokenHtml;
        });
    }
    // 如果没有 token 被渲染（全部被 shouldRenderToken 过滤），不创建卡片
    if (!renderedTokenCount && tokens.length > 0) return "";
    rightHtml += "</div>";
    return `<div class="tweet-card${isNew ? " new" : ""}" data-tweet-id="${escapeHtml(String(data.tweet_id))}">${leftHtml}${rightHtml}</div>`;
}

function renderMessages(messages, isNew) {
    if (!messages || !messages.length) {
        cardGrid.innerHTML = `<div class="empty-state"><p>暂无消息</p></div>`;
        updateMessageCount();
        return;
    }
    // 初次加载：全量渲染是合理的，因为没有已存在节点可以 patch
    cardGrid.innerHTML = messages.map((msg, i) => renderTweetCard(msg, isNew && i === 0)).join("");
    // 仅给第一张卡片加上 is-new 入场动画
    const firstCard = cardGrid.querySelector(".tweet-card:first-child");
    if (firstCard && isNew) {
        firstCard.classList.add("is-new");
        setTimeout(() => firstCard.classList.remove("is-new"), 350);
    }
    // 渲染后绑定所有 token icon 图片加载
    bindAllTokenIcons(cardGrid);
    updateMessageCount();
}

// 为 grid 中所有 token icon img 绑定图片加载
function bindAllTokenIcons(grid) {
    grid.querySelectorAll(".token-item[data-contract]").forEach(tokenEl => {
        const contract = tokenEl.dataset.contract;
        // 1. 优先检查 TOKEN_ICONS 是否有该 contract 的内联 SVG
        if (contract && TOKEN_ICONS[contract]) {
            // 直接用内联 SVG 替换 icon-fallback 内容
            const fallbackEl = tokenEl.querySelector('[data-field="icon-fallback"]');
            if (fallbackEl) {
                fallbackEl.innerHTML = TOKEN_ICONS[contract];
                fallbackEl.style.display = "flex";
                // 隐藏 icon-img（如果存在）
                const iconImg = tokenEl.querySelector('[data-field="icon-img"]');
                if (iconImg) iconImg.style.display = "none";
            }
            return;
        }
        // 2. 没有 TOKEN_ICONS 匹配 → 从 token 数据中找到 icon URL 加载
        const iconImg = tokenEl.querySelector('[data-field="icon-img"]');
        if (iconImg) {
            const card = tokenEl.closest(".tweet-card");
            if (card) {
                const tweetId = card.dataset.tweetId;
                const msg = findMessageById(tweetId);
                if (msg && msg.tokens) {
                    const token = msg.tokens.find(t => String(t.contract || "").toLowerCase() === contract);
                    if (token && token.icon) {
                        bindCachedImage(iconImg, token.icon);
                        iconImg.addEventListener("load", () => { iconImg.style.display = "block"; });
                    }
                }
            }
        }
    });
}

// 刷新 grid 中所有 token 卡片的 meta/badge/platform/icon 字段
// 用于 icons.json 异步加载完成后，给已渲染的卡片补上之前缺失的 SVG 图标
// 不重建 innerHTML，仅按需 patch（字段级 diff），避免图片重新加载与闪烁
function refreshAllTokenCardsMeta(grid) {
    if (!grid) return;
    grid.querySelectorAll(".token-item[data-contract]").forEach(tokenEl => {
        const contract = tokenEl.dataset.contract;
        if (!contract) return;
        const card = tokenEl.closest(".tweet-card");
        if (!card) return;
        const tweetId = card.dataset.tweetId;
        const msg = findMessageById(tweetId);
        if (!msg || !msg.tokens) return;
        const token = msg.tokens.find(t => String(t.contract || "").toLowerCase() === contract);
        if (!token) return;
        // 字段级 patch：重新构建 meta/badge/icon/platform 等（patchTokenCard 内部已做 diff）
        patchTokenCard(tokenEl, token);
        // 单独刷新 icon-fallback（若 TOKEN_ICONS 有匹配）
        if (TOKEN_ICONS[contract.toLowerCase()]) {
            const fallbackEl = tokenEl.querySelector('[data-field="icon-fallback"]');
            if (fallbackEl) {
                fallbackEl.innerHTML = TOKEN_ICONS[contract.toLowerCase()];
                fallbackEl.style.display = "flex";
                const iconImgEl = tokenEl.querySelector('[data-field="icon-img"]');
                if (iconImgEl) iconImgEl.style.display = "none";
            }
        }
        // 刷新 platform badge（在 icon-wrap 内）
        const iconWrap = tokenEl.querySelector('[data-field="icon"]');
        if (iconWrap) {
            const platform = getPlatformFromContract(contract);
            let badgeEl = iconWrap.querySelector('.platform-badge');
            if (platform && PLATFORM_ICONS[platform]) {
                const html = `<span class="platform-badge">${PLATFORM_ICONS[platform]}</span>`;
                if (!badgeEl) {
                    iconWrap.insertAdjacentHTML("beforeend", html);
                } else if (badgeEl.innerHTML !== PLATFORM_ICONS[platform]) {
                    badgeEl.innerHTML = PLATFORM_ICONS[platform];
                }
            } else if (badgeEl) {
                badgeEl.remove();
            }
        }
    });
}

// ============================================================
// 关键：增量 DOM 工具，避免 innerHTML 全量替换造成的闪烁
// ============================================================

// 在替换 innerHTML 前后，保留 src 未变且已加载完成的 <img>，避免图标/头像重新拉取闪烁
function patchInnerHTML(parent, newInnerHTML) {
    const oldImgs = new Map();
    parent.querySelectorAll("img").forEach(img => {
        const src = img.getAttribute("src");
        if (src && img.complete && img.naturalWidth > 0) {
            if (!oldImgs.has(src)) oldImgs.set(src, img);
        }
    });
    parent.innerHTML = newInnerHTML;
    if (oldImgs.size) {
        parent.querySelectorAll("img").forEach(img => {
            const src = img.getAttribute("src");
            if (src && oldImgs.has(src)) {
                img.replaceWith(oldImgs.get(src));
            }
        });
    }
}

// 更新顶部消息计数文本（不重绘 DOM）
function updateMessageCount() {
    messageCount.textContent = allMessages.length + " 条消息";
}

function updateFollowedCount() {
    // 关注功能已移除，空函数保留避免调用报错
}

// 在指定 grid 中按 data-tweet-id 增量更新单张卡片：
// - 已存在：只 patch innerHTML，保留外层 .tweet-card 元素，不触发 slideIn 动画
// - 不存在：prepend 新卡片，可选触发 is-new 入场动画
function upsertCard(grid, data, options) {
    options = options || {};
    const tweetId = String(data && data.tweet_id || "");
    if (!tweetId) return null;
    const existing = grid.querySelector(`.tweet-card[data-tweet-id="${CSS.escape(tweetId)}"]`);
    const tmp = document.createElement("div");
    tmp.innerHTML = renderTweetCard(data, false);
    const newCard = tmp.firstElementChild;
    if (!newCard) return null;

    if (existing) {
        // 就地 patch 内容，不动外层元素，避免重新触发动画 / 图片重拉
        patchInnerHTML(existing, newCard.innerHTML);
        // 清掉残留的 is-new 类，避免被误触动画
        existing.classList.remove("is-new");
        return existing;
    }

    // 新增卡片：先清掉空状态
    const empty = grid.querySelector(".empty-state");
    if (empty) empty.remove();

    if (options.isNew) {
        newCard.classList.add("is-new", "new");
        // 动画结束后移除 is-new（避免下次 patch 时被残留类影响）
        setTimeout(() => newCard.classList.remove("is-new"), 350);
        // 高亮保留稍久一些
        setTimeout(() => newCard.classList.remove("new"), 2500);
    }

    if (grid.firstChild) {
        grid.insertBefore(newCard, grid.firstChild);
    } else {
        grid.appendChild(newCard);
    }
    updateMessageCount();
    return newCard;
}

// 关注列表相关函数已移除（sidebar 取消），保留空壳避免调用报错
function syncFollowedList() {}
function updateFollowButton() {}
function renderFollowed() {}

function reRenderCurrentTab() {
    // 无 tab 切换，始终渲染 allMessages
    renderMessages(allMessages);
}

// 推文展开/收起：切换 原文+翻译 的截断/完整显示
function toggleTweetExpand(btn) {
    if (!btn) return;
    const left = btn.closest(".tweet-left") || btn.parentElement;
    if (!left) return;
    const textEl = left.querySelector('[data-field="tweet-text"]');
    const textFullEl = left.querySelector('[data-field="tweet-text-full"]');
    const transEl = left.querySelector('[data-field="tweet-translation"]');
    const transFullEl = left.querySelector('[data-field="tweet-translation-full"]');
    const isExpanded = btn.dataset.expanded === "1";
    if (isExpanded) {
        // 收起
        if (textEl) textEl.style.display = "block";
        if (textFullEl) textFullEl.style.display = "none";
        // 翻译区：仅在有内容时显示，空内容保持隐藏避免占位
        if (transEl) {
            transEl.style.display = (transEl.textContent && transEl.textContent.trim()) ? "block" : "none";
        }
        if (transFullEl) transFullEl.style.display = "none";
        btn.textContent = "显示更多";
        btn.dataset.expanded = "0";
    } else {
        // 展开
        if (textFullEl) {
            textEl.style.display = "none";
            textFullEl.style.display = "block";
        }
        if (transFullEl) {
            if (transEl) transEl.style.display = "none";
            transFullEl.style.display = "block";
        }
        btn.textContent = "收起";
        btn.dataset.expanded = "1";
    }
}

// 嵌套推文展开/收起：切换 nt-body / nt-body-full / nt-translation / nt-translation-full
function toggleNestedExpand(btn) {
    if (!btn) return;
    const wrapper = btn.closest(".nested-tweet");
    if (!wrapper) return;
    const bodyEl = wrapper.querySelector('[data-field="nt-body"]');
    const bodyFullEl = wrapper.querySelector('[data-field="nt-body-full"]');
    const transEl = wrapper.querySelector('[data-field="nt-translation"]');
    const transFullEl = wrapper.querySelector('[data-field="nt-translation-full"]');
    const isExpanded = btn.dataset.expanded === "1";
    if (isExpanded) {
        // 收起
        if (bodyEl) bodyEl.style.display = "block";
        if (bodyFullEl) bodyFullEl.style.display = "none";
        if (transEl) transEl.style.display = (transEl.textContent && transEl.textContent.trim()) ? "block" : "none";
        if (transFullEl) transFullEl.style.display = "none";
        btn.textContent = "显示更多";
        btn.dataset.expanded = "0";
    } else {
        // 展开
        if (bodyFullEl) {
            if (bodyEl) bodyEl.style.display = "none";
            bodyFullEl.style.display = "block";
        }
        if (transFullEl) {
            if (transEl) transEl.style.display = "none";
            transFullEl.style.display = "block";
        }
        btn.textContent = "收起";
        btn.dataset.expanded = "1";
    }
}

// 手动刷新翻译：触发后端重新获取翻译并推送
function refreshTranslation(btn) {
    if (!btn) return;
    const card = btn.closest(".tweet-card");
    if (!card) return;
    const tweetId = card.dataset.tweetId;
    if (!tweetId) return;
    btn.disabled = true;
    btn.textContent = "获取中...";
    // 通过 HTTP 请求触发后端重新获取翻译
    fetch("/api/refresh_translation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tweet_id: tweetId })
    }).then(r => r.json()).then(data => {
        if (data.success) {
            btn.textContent = "已获取";
            // update_message 事件会自动刷新卡片，2秒后移除按钮
            setTimeout(() => { btn.remove(); }, 2000);
        } else {
            btn.textContent = data.error || "获取失败";
            setTimeout(() => { btn.disabled = false; btn.textContent = "显示翻译"; }, 2000);
        }
    }).catch(() => {
        btn.textContent = "获取失败";
        setTimeout(() => { btn.disabled = false; btn.textContent = "显示翻译"; }, 2000);
    });
}

function toggleFollow(tweetId) {
    // 关注功能已移除，空函数保留避免 onclick 报错
}

// ============================================================
// 图片大图预览（lightbox）：点击推文图片放大显示
// 图片悬浮预览：鼠标悬浮时在原位置附近显示放大弹窗
// ============================================================
function openImageLightbox(src) {
    const box = document.getElementById("imageLightbox");
    const img = document.getElementById("lightboxImg");
    if (!box || !img) return;
    img.src = src;
    box.classList.add("active");
}
function closeImageLightbox() {
    const box = document.getElementById("imageLightbox");
    if (!box) return;
    box.classList.remove("active");
    const img = document.getElementById("lightboxImg");
    if (img) img.src = "";
}
// 悬浮预览：显示放大的图片在鼠标附近
function showHoverPreview(src, clientX, clientY) {
    const box = document.getElementById("imageHoverPreview");
    const img = document.getElementById("hoverPreviewImg");
    if (!box || !img) return;
    img.src = src;
    box.classList.add("active");
    // 定位：跟随鼠标，但避免超出视口
    const previewWidth = 500;
    const previewHeight = 500;
    let x = clientX + 16;
    let y = clientY + 16;
    if (x + previewWidth > window.innerWidth) x = clientX - previewWidth - 16;
    if (y + previewHeight > window.innerHeight) y = window.innerHeight - previewHeight - 8;
    if (x < 8) x = 8;
    if (y < 8) y = 8;
    box.style.left = x + "px";
    box.style.top = y + "px";
}
function hideHoverPreview() {
    const box = document.getElementById("imageHoverPreview");
    if (!box) return;
    box.classList.remove("active");
    const img = document.getElementById("hoverPreviewImg");
    if (img) img.src = "";
}
// ESC 关闭 lightbox
document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
        closeImageLightbox();
        hideHoverPreview();
    }
});
// 鼠标滚轮：向下滚动 → 水平向右滚动（瀑布流横向浏览）
// 仅在 cardGrid 上生效，避免影响 lightbox 等其他元素
cardGrid.addEventListener("wheel", e => {
    // 只拦截垂直滚动（deltaY），转成水平滚动
    if (e.deltaY !== 0) {
        e.preventDefault();
        cardGrid.scrollLeft += e.deltaY;
    }
}, { passive: false });
// 事件委托：点击 .media-box img / .nt-media img 打开大图
document.addEventListener("click", e => {
    const img = e.target.closest(".media-box img, .nt-media img");
    if (img && img.src) {
        e.preventDefault();
        openImageLightbox(img.src);
    }
});
let iconHoverTimer = null;
// 事件委托：悬浮 .media-box img / .nt-media img / .token-item .icon-wrap 显示放大预览
document.addEventListener("mouseover", e => {
    const mediaImg = e.target.closest(".media-box img, .nt-media img");
    if (mediaImg && mediaImg.src) {
        showHoverPreview(mediaImg.src, e.clientX, e.clientY);
        return;
    }
    const iconWrap = e.target.closest(".token-item .icon-wrap");
    if (iconWrap) {
        const iconImg = iconWrap.querySelector("[data-field='icon-img']");
        const src = iconImg ? iconImg.getAttribute("data-original-src") : null;
        if (src) {
            // Delay 1s before showing
            clearTimeout(iconHoverTimer);
            iconHoverTimer = setTimeout(() => {
                showHoverPreview(src, e.clientX, e.clientY);
            }, 1000);
        }
    }
});
// mousemove 仅在 hover preview 激活时才处理位置更新，否则直接 return（避免滚动时大量 closest 调用）
document.addEventListener("mousemove", e => {
    const box = document.getElementById("imageHoverPreview");
    if (!box || !box.classList.contains("active")) return;  // early return：preview 未激活时不做任何 DOM 操作
    // 跟随鼠标移动更新位置
    const previewWidth = 500;
    const previewHeight = 500;
    let x = e.clientX + 16;
    let y = e.clientY + 16;
    if (x + previewWidth > window.innerWidth) x = e.clientX - previewWidth - 16;
    if (y + previewHeight > window.innerHeight) y = window.innerHeight - previewHeight - 8;
    if (x < 8) x = 8;
    if (y < 8) y = 8;
    box.style.left = x + "px";
    box.style.top = y + "px";
});
document.addEventListener("mouseout", e => {
    const img = e.target.closest(".media-box img, .nt-media img, .token-item .icon-wrap");
    if (img) {
        clearTimeout(iconHoverTimer);
        // 检查是否移到了另一个 media img，若是则不隐藏
        const related = e.relatedTarget;
        const stillOnMedia = related && related.closest && related.closest(".media-box img, .nt-media img, .token-item .icon-wrap");
        if (!stillOnMedia) {
            hideHoverPreview();
        }
    }
});

socket.on("connect", () => { console.log("WebSocket connected"); });

// 迁移 token 列表更新（来自 pusher 每 5s 推送）
socket.on("migrated_tokens", data => {
    if (!data || !data.tokens) return;
    renderMigratedBar(data.tokens);
});

socket.on("init_messages", data => {
    allMessages = data.messages || [];
    // 重建 messageById Map，后续 O(1) 查找
    messageById.clear();
    allMessages.forEach(m => {
        if (m && m.tweet_id) messageById.set(String(m.tweet_id), m);
    });
    renderMessages(allMessages);
});

// 新增消息：仅 prepend 单张卡片，绝不全量重绘
socket.on("new_message", data => {
    if (!data.message) return;
    const incoming = data.message;
    const tweetIdStr = String(incoming.tweet_id);
    const incomingTokens = incoming.tokens || [];
    if (!incomingTokens.length) {
        return;
    }
    // 检查是否有 token 能通过 shouldRenderToken 过滤
    const visibleTokens = incomingTokens.filter(t => shouldRenderToken(t));
    if (visibleTokens.length === 0) {
        // 所有 token 都被过滤 → 不创建卡片，也不加入内存
        return;
    }
    // 通过 upsertMessage 统一同步 allMessages + messageById（含裁剪）
    upsertMessage(incoming);

    upsertCard(cardGrid, incoming, { isNew: true });
});

// 更新事件：仅就地 patch 左侧内容（推文文本/翻译/文章），绝不触碰右侧 token 列表
// token 列表由 token_update 事件独立管理，避免整体重绘
socket.on("update_message", data => {
    if (!data.message) return;
    const updatedMsg = data.message;
    const tweetId = String(updatedMsg.tweet_id);
    // 如果更新后 token 列表为空 → 移除整张卡片
    const updatedTokens = updatedMsg.tokens || [];
    if (!updatedTokens.length) {
        removeMessageById(tweetId);
        const card = cardGrid.querySelector(`.tweet-card[data-tweet-id="${CSS.escape(tweetId)}"]`);
        if (card) card.remove();
        updateMessageCount();
        return;
    }
    // 检查是否有 token 能通过 shouldRenderToken 过滤
    const visibleTokens = updatedTokens.filter(t => shouldRenderToken(t));
    if (visibleTokens.length === 0) {
        // 所有 token 都被过滤 → 移除卡片
        removeMessageById(tweetId);
        const card = cardGrid.querySelector(`.tweet-card[data-tweet-id="${CSS.escape(tweetId)}"]`);
        if (card) card.remove();
        updateMessageCount();
        return;
    }

    const oldMsg = findMessageById(tweetId);
    if (oldMsg) {
        // 保留旧消息的 tokens（避免覆盖 token_update 的增量更新），仅更新非 token 字段
        updatedMsg.tokens = oldMsg.tokens || updatedMsg.tokens;
        // 通过 upsertMessage 统一同步 allMessages + messageById（含裁剪）
        upsertMessage(updatedMsg);
    } else {
        upsertMessage(updatedMsg);
    }

    // 仅 patch 左侧内容（tweet-left），不触碰右侧 token 列表
    // 主区域：始终处理
    {
        const card = cardGrid.querySelector(`.tweet-card[data-tweet-id="${CSS.escape(tweetId)}"]`);
        if (!card) {
            upsertCard(cardGrid, updatedMsg, { isNew: false });
        } else {
            const existingLeft = card.querySelector(".tweet-left");
            if (existingLeft) {
                const tmp = document.createElement("div");
                tmp.innerHTML = renderTweetCard(updatedMsg, false);
                const newLeft = tmp.querySelector(".tweet-left");
                if (newLeft) {
                    patchInnerHTML(existingLeft, newLeft.innerHTML);
                }
            }
            // 推文更新后不重新绑定 token icon（patchTokenCard 里已有 icon 检查，避免重复加载）
        }
    }
    updateMessageCount();
});

// ========== Token 增量更新（按 contract 精准更新 + 保留同 src 图片，杜绝闪烁） ==========
socket.on("token_update", data => {
    const tweetId = String(data.tweet_id || "");
    const tokens = data.tokens || [];
    if (!tweetId) return;

    // 1. 同步内存
    const msg = findMessageById(tweetId);
    if (msg) msg.tokens = tokens;

    // 2. 如果 token 列表为空 → 整张卡片移除（不再显示"暂无关联代币"占位）
    if (!tokens.length) {
        // 从内存中移除（allMessages + messageById 同步）
        removeMessageById(tweetId);
        // 从 DOM 中移除卡片
        const card = cardGrid.querySelector(`.tweet-card[data-tweet-id="${CSS.escape(tweetId)}"]`);
        if (card) card.remove();
        updateMessageCount();
        updateFollowedCount();
        return;
    }

    // 3. patch 主区域中该 tweet_id 的卡片右侧 token 容器
    const grid = cardGrid;
    {
        const card = grid.querySelector(`.tweet-card[data-tweet-id="${CSS.escape(tweetId)}"]`);
        if (!card) return;
        const right = card.querySelector(".tweet-right");
        if (!right) return;

        // 4. 现有节点按 contract 建索引
        const existing = new Map();
        right.querySelectorAll(".token-item[data-contract]").forEach(el => {
            existing.set(el.dataset.contract, el);
        });

        const keep = new Set();

        // 找到 createTime 最小的 token（时间最早），当 token > 2 个且都在5分钟内时标记为"早"
        // 同一推文同时只能有一个"早"标签：即使多个 token 的 createTime 完全相同，
        // 也只取第一个匹配的（用 break 跳出循环）
        let earlyContract = null;
        if (tokens.length >= 2) {
            let minTime = Infinity, maxTime = 0;
            tokens.forEach(t => {
                const ct = Number(t.createTime || t.create_time || 0);
                if (ct > 0) { if (ct < minTime) minTime = ct; if (ct > maxTime) maxTime = ct; }
            });
            if (minTime !== Infinity && (maxTime - minTime) <= 300000) { // 5 minutes = 300000ms
                for (const t of tokens) {
                    const ct = Number(t.createTime || t.create_time || 0);
                    if (ct === minTime) {
                        earlyContract = String(t.contract || "").toLowerCase();
                        break;  // 只取第一个，确保唯一
                    }
                }
            }
        }

        tokens.forEach(token => {
            const contract = String(token.contract || "").toLowerCase();
            if (!contract) return;
            // 前端兜底过滤：不符合条件的 token 不加入 keep，这样旧 DOM 节点会被删除
            if (!shouldRenderToken(token)) return;
            keep.add(contract);
            const isEarly = earlyContract && contract === earlyContract;

            let el = existing.get(contract);
            if (el) {
                // 检查 early 状态是否变化（DOM 上的 has-early-tag class vs 当前 isEarly）
                const hasEarlyClass = el.classList.contains("has-early-tag");
                if (hasEarlyClass !== !!isEarly) {
                    // early 状态变化 → 直接重新渲染整个 token 卡片（早标签由 renderTokenCard 自动处理）
                    const tmp = document.createElement("div");
                    tmp.innerHTML = renderTokenCard(token, isEarly);
                    const newEl = tmp.firstElementChild;
                    if (newEl) {
                        el.replaceWith(newEl);
                        // 重新绑定 icon 缓存
                        const iconImg = newEl.querySelector('[data-field="icon-img"]');
                        if (iconImg && token.icon) {
                            bindCachedImage(iconImg, token.icon);
                            iconImg.addEventListener("load", () => {
                                iconImg.style.display = "block";
                            });
                        }
                    } else {
                        // renderTokenCard 返回空（如市值空且过 grace period）→ 至少移除 has-early-tag 防残留
                        el.classList.remove("has-early-tag");
                        const earlyTag = el.querySelector('.token-early-tag');
                        if (earlyTag) earlyTag.remove();
                    }
                } else {
                    // early 状态未变 → 字段级 patch：仅更新真正变化的文字/属性，绝不触碰 .age
                    patchTokenCard(el, token);
                }
            } else {
                // 新 token → 渲染并插入到第 2 个位置（前 4 个保持不变，新加入的排第 2）
                // 如果当前 token 数 < 4，则追加到末尾（不影响前 4）
                const tmp = document.createElement("div");
                tmp.innerHTML = renderTokenCard(token, isEarly);
                const newEl = tmp.firstElementChild;
                if (!newEl) return;  // renderTokenCard 返回空（如市值空且过 grace period），跳过
                // 加高亮 class（10s 底色区分）
                newEl.classList.add("token-new");
                setTimeout(() => newEl.classList.remove("token-new"), 10000);
                // 插入位置：如果当前已有 >= 4 个 token，插到第 2 个位置（index 1）
                // 否则追加到末尾
                const existingItems = right.querySelectorAll(".token-item[data-contract]");
                if (existingItems.length >= 4) {
                    // 插到第 2 个位置（existingItems[1] 之前）
                    if (existingItems[1]) {
                        right.insertBefore(newEl, existingItems[1]);
                    } else {
                        right.appendChild(newEl);
                    }
                } else {
                    right.appendChild(newEl);
                }
                // 绑定 icon 图片缓存加载
                const iconImg = newEl.querySelector('[data-field="icon-img"]');
                if (iconImg && token.icon) {
                    bindCachedImage(iconImg, token.icon);
                    // 监听加载完成事件，成功后显示 img 覆盖 fallback
                    iconImg.addEventListener("load", () => {
                        iconImg.style.display = "block";
                    });
                }
            }
        });

        // 5. 删除已不存在的 token
        existing.forEach((el, contract) => {
            if (!keep.has(contract)) el.remove();
        });

        // 清理"暂无关联代币"
        const noTokens = right.querySelector(".no-tokens");
        if (noTokens) noTokens.remove();

        // 6. 检查卡片是否还有 token，没有就移除整个卡片
        const remainingTokens = right.querySelectorAll(".token-item[data-contract]");
        if (remainingTokens.length === 0) {
            const card = right.closest(".tweet-card");
            if (card) {
                const tweetId = card.dataset.tweetId;
                card.remove();
                removeMessageById(tweetId);
                updateMessageCount();
            }
        }
    }
});

socket.on("follow_updated", data => {
    // 关注功能已移除，不再处理 follow_updated 事件
});

// Token Age 自动刷新（3秒一次，减少 DOM 扫描频率）
// 仅当 age 文本真的变化时才写 textContent，杜绝无谓重绘
setInterval(() => {
    const items = document.querySelectorAll(".token-item");
    if (!items.length) return;
    items.forEach(item => {
        const createTime = item.dataset.createTime;
        if (!createTime) return;
        const ageEl = item.querySelector('[data-field="age"]');
        if (!ageEl) return;
        const newAge = formatTokenAge(createTime);
        if (ageEl.textContent !== newAge) {
            ageEl.textContent = newAge;
        }
    });
}, 3000);

// ============================================================
// 持仓弹窗：右下角悬浮按钮 + 可拖拽弹窗（上K线 + 下持仓列表）
// ============================================================
// 全局状态
const holdingsState = {
    selectedContract: null,   // 当前选中的 token CA
    pollTimer: null,          // 持仓列表轮询定时器
    wsKline: null,            // K线实时订阅 WebSocket
    wsKlineStream: null,      // 当前订阅的 stream 名
    wsKlineActive: false,     // K线实时订阅是否开启（默认关闭）
    klineChart: null,         // K线图实例
    klineInterval: "1s",
    klineHistory: [],         // 历史K线数据
    lastKlineTs: 0,           // 最后一根K线的时间戳（用于增量更新）
    sellInflight: new Set(),  // 卖出中的 CA，防重入
};

// =========== 1. 悬浮按钮 ===========
function injectHoldingsUI() {
    if (document.getElementById("holdingsFab")) return;
    // FAB 按钮
    const fab = document.createElement("button");
    fab.id = "holdingsFab";
    fab.className = "holdings-fab";
    fab.title = "持仓";
    fab.innerHTML = `<span class="holdings-fab-icon">💰</span><span class="holdings-fab-text">持仓</span>`;
    fab.addEventListener("click", toggleHoldingsPanel);
    document.body.appendChild(fab);

    // 弹窗容器（默认隐藏）
    const panel = document.createElement("div");
    panel.id = "holdingsPanel";
    panel.className = "holdings-panel";
    panel.style.display = "none";
    panel.innerHTML = `
        <div class="holdings-panel-header" id="holdingsPanelHeader">
            <div class="holdings-panel-title">
                <span>📊 持仓</span>
                <span class="holdings-panel-wallet" id="holdingsWalletAddr" title=""></span>
            </div>
            <div class="holdings-panel-actions">
                <button class="holdings-panel-btn" id="holdingsRefreshBtn" title="立即刷新">🔄</button>
                <button class="holdings-panel-btn" id="holdingsCloseBtn" title="关闭">✕</button>
            </div>
        </div>
        <div class="holdings-panel-body">
            <!-- K线区 -->
            <div class="holdings-kline-section">
                <div class="holdings-kline-toolbar">
                    <span class="holdings-kline-title" id="holdingsKlineTitle">未选择 token</span>
                    <div class="holdings-kline-controls">
                        <select id="holdingsKlineInterval" title="K线周期">
                            <option value="1s" selected>1s</option>
                            <option value="1m">1m</option>
                            <option value="5m">5m</option>
                            <option value="15m">15m</option>
                            <option value="1h">1h</option>
                        </select>
                        <button class="holdings-kline-toggle" id="holdingsKlineToggleBtn" title="开关实时K线订阅">▶ 实时</button>
                    </div>
                </div>
                <div class="holdings-kline-wrap" id="holdingsKlineWrap">
                    <canvas id="holdingsKlineCanvas" width="640" height="240"></canvas>
                    <div class="holdings-kline-empty" id="holdingsKlineEmpty">选择持仓列表中的 token 查看K线</div>
                </div>
            </div>
            <!-- 持仓列表区 -->
            <div class="holdings-list-section">
                <div class="holdings-list-toolbar">
                    <span class="holdings-list-title">持仓列表</span>
                    <span class="holdings-list-meta" id="holdingsListMeta">-</span>
                </div>
                <div class="holdings-list" id="holdingsList">
                    <div class="holdings-list-empty">加载中...</div>
                </div>
            </div>
        </div>
        <div class="holdings-panel-toast" id="holdingsPanelToast"></div>
    `;
    document.body.appendChild(panel);

    // 绑定事件
    document.getElementById("holdingsCloseBtn").addEventListener("click", () => toggleHoldingsPanel(false));
    document.getElementById("holdingsRefreshBtn").addEventListener("click", () => fetchHoldings(true));
    document.getElementById("holdingsKlineToggleBtn").addEventListener("click", toggleKlineRealtime);
    document.getElementById("holdingsKlineInterval").addEventListener("change", (e) => {
        holdingsState.klineInterval = e.target.value;
        if (holdingsState.selectedContract) loadKlineHistory(holdingsState.selectedContract);
    });

    // 拖拽
    enableDrag(document.getElementById("holdingsPanel"), document.getElementById("holdingsPanelHeader"));
}

function toggleHoldingsPanel(forceShow) {
    const panel = document.getElementById("holdingsPanel");
    if (!panel) return;
    const willShow = (forceShow === undefined) ? (panel.style.display === "none") : forceShow;
    if (willShow) {
        panel.style.display = "flex";
        if (!holdingsState.pollTimer) {
            fetchHoldings();
            holdingsState.pollTimer = setInterval(fetchHoldings, 2000);
        }
    } else {
        panel.style.display = "none";
        if (holdingsState.pollTimer) {
            clearInterval(holdingsState.pollTimer);
            holdingsState.pollTimer = null;
        }
        // 关闭时也停掉实时K线
        if (holdingsState.wsKlineActive) toggleKlineRealtime(false);
    }
}

// =========== 2. 拖拽 ===========
function enableDrag(panel, handle) {
    let dragging = false, startX = 0, startY = 0, panelX = 0, panelY = 0;
    handle.addEventListener("mousedown", (e) => {
        // 排除按钮点击
        if (e.target.closest("button")) return;
        dragging = true;
        startX = e.clientX; startY = e.clientY;
        const rect = panel.getBoundingClientRect();
        panelX = rect.left; panelY = rect.top;
        panel.style.right = "auto"; panel.style.bottom = "auto";
        panel.style.left = panelX + "px"; panel.style.top = panelY + "px";
        e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
        if (!dragging) return;
        const dx = e.clientX - startX, dy = e.clientY - startY;
        // 限制在视口内
        const maxX = window.innerWidth - panel.offsetWidth;
        const maxY = window.innerHeight - panel.offsetHeight;
        panel.style.left = Math.max(0, Math.min(maxX, panelX + dx)) + "px";
        panel.style.top = Math.max(0, Math.min(maxY, panelY + dy)) + "px";
    });
    document.addEventListener("mouseup", () => { dragging = false; });
}

// =========== 3. 持仓列表 ===========
async function fetchHoldings(showToast) {
    try {
        const resp = await fetch("/api/holdings");
        const data = await resp.json();
        if (!data.success) {
            renderHoldingsList(null, data.error || "查询失败");
            return;
        }
        // 钱包地址
        const walletEl = document.getElementById("holdingsWalletAddr");
        if (walletEl) {
            const w = data.wallet || "";
            walletEl.textContent = w ? w.slice(0, 6) + "…" + w.slice(-4) : "";
            walletEl.title = w;
        }
        // meta
        const metaEl = document.getElementById("holdingsListMeta");
        if (metaEl) {
            const age = data.age_seconds !== null && data.age_seconds !== undefined ? data.age_seconds.toFixed(1) + "s前" : "-";
            metaEl.textContent = `更新: ${age} | 总${data.poll_total} 成功${data.poll_success} 失败${data.poll_fail}`;
        }
        renderHoldingsList(data.data, data.error);
        if (showToast) holdingsPanelToast("已刷新", "success");
    } catch (e) {
        renderHoldingsList(null, "网络异常: " + (e.message || e));
    }
}

function renderHoldingsList(holdingsData, errorMsg) {
    const listEl = document.getElementById("holdingsList");
    if (!listEl) return;
    if (errorMsg) {
        listEl.innerHTML = `<div class="holdings-list-empty holdings-list-error">❌ ${escapeHtml(errorMsg)}</div>`;
        return;
    }
    // GMGN holdings 真实返回结构（已验证）：
    // { "list": [ { balance, usd_value, total_profit_pnl, token: {token_address, symbol, decimals, logo, price, ...}, ... } ], "next": ... }
    let holdings = [];
    if (Array.isArray(holdingsData)) {
        holdings = holdingsData;
    } else if (holdingsData && Array.isArray(holdingsData.list)) {
        holdings = holdingsData.list;
    } else if (holdingsData && Array.isArray(holdingsData.holdings)) {
        holdings = holdingsData.holdings;
    } else if (holdingsData && Array.isArray(holdingsData.data)) {
        holdings = holdingsData.data;
    }
    if (!holdings.length) {
        listEl.innerHTML = `<div class="holdings-list-empty">无持仓</div>`;
        return;
    }
    // 排序：按 USD 价值倒序
    holdings.sort((a, b) => {
        const av = parseFloat(a.usd_value || 0);
        const bv = parseFloat(b.usd_value || 0);
        return bv - av;
    });
    listEl.innerHTML = holdings.map(h => buildHoldingRow(h)).join("");
    // 绑定行点击 + 卖出按钮
    listEl.querySelectorAll(".holding-row").forEach(row => {
        row.addEventListener("click", (e) => {
            if (e.target.closest(".sell-btn")) return;
            const ca = row.dataset.contract;
            selectHolding(ca);
        });
    });
    listEl.querySelectorAll(".sell-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const ca = btn.dataset.contract;
            sellHolding(ca, btn);
        });
    });
}

function buildHoldingRow(h) {
    // 兼容 GMGN 真实返回 + 一些可能的其他字段名
    const token = h.token || {};
    const ca = token.token_address || h.token_address || h.address || h.contract || "";
    const symbol = token.symbol || h.symbol || "???";
    const name = token.name || h.name || "";
    const decimals = token.decimals || h.decimals || 18;
    const logo = token.logo || h.logo_url || h.icon || "";
    const price = parseFloat(token.price || h.price || 0);
    const balance = h.balance || h.amount || "0";
    const usdValue = parseFloat(h.usd_value || 0);
    // total_profit_pnl: -1.0 ~ 1.0+ （例如 0.5 = +50%, -0.3 = -30%）
    const pnlPnl = h.total_profit_pnl;
    const totalProfit = h.total_profit || "0";

    // 格式化数量
    let amountStr = formatTokenAmount(balance, decimals);
    let usdStr = "$" + formatUsd(usdValue);
    // PnL
    let pnlClass = "pnl-zero";
    let pnlStr = "-";
    if (pnlPnl !== null && pnlPnl !== undefined && pnlPnl !== "") {
        const pnlNum = parseFloat(pnlPnl);
        if (!isNaN(pnlNum) && pnlNum !== 0) {
            pnlClass = pnlNum >= 0 ? "pnl-positive" : "pnl-negative";
            const sign = pnlNum >= 0 ? "+" : "";
            pnlStr = `${sign}${(pnlNum * 100).toFixed(2)}%`;
        }
    } else if (parseFloat(totalProfit) !== 0) {
        // 用 total_profit 兜底显示（USD）
        const tpNum = parseFloat(totalProfit);
        pnlClass = tpNum >= 0 ? "pnl-positive" : "pnl-negative";
        const sign = tpNum >= 0 ? "+" : "";
        pnlStr = `${sign}$${formatUsd(Math.abs(tpNum))}`;
    }
    const isSelected = holdingsState.selectedContract && ca.toLowerCase() === holdingsState.selectedContract.toLowerCase();
    const caShort = ca ? ca.slice(0, 6) + "…" + ca.slice(-4) : "";
    const iconHtml = logo
        ? `<img class="holding-icon" src="${escapeHtml(logo)}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" referrerpolicy="no-referrer"><div class="holding-icon-fallback" style="display:none">${escapeHtml(symbol.slice(0,2).toUpperCase())}</div>`
        : `<div class="holding-icon-fallback">${escapeHtml(symbol.slice(0,2).toUpperCase())}</div>`;
    return `
        <div class="holding-row${isSelected ? " selected" : ""}" data-contract="${escapeHtml(ca)}" title="${escapeHtml(name || symbol)}">
            <div class="holding-row-main">
                ${iconHtml}
                <div class="holding-row-info">
                    <div class="holding-row-top">
                        <span class="holding-symbol">${escapeHtml(symbol)}</span>
                        <span class="holding-ca">${escapeHtml(caShort)}</span>
                    </div>
                    <div class="holding-row-bottom">
                        <span class="holding-amount" title="${escapeHtml(balance)}">${escapeHtml(amountStr)}</span>
                        <span class="holding-usd">${escapeHtml(usdStr)}</span>
                        <span class="holding-pnl ${pnlClass}">${escapeHtml(pnlStr)}</span>
                    </div>
                </div>
            </div>
            <button class="sell-btn" data-contract="${escapeHtml(ca)}" title="卖出全部 ${escapeHtml(symbol)}">卖出</button>
        </div>
    `;
}

function formatTokenAmount(amount, decimals) {
    if (!amount) return "0";
    try {
        const v = parseFloat(amount) / Math.pow(10, decimals || 18);
        if (v === 0) return "0";
        if (v < 0.0001) return v.toExponential(2);
        if (v < 1) return v.toFixed(6);
        if (v < 1000) return v.toFixed(3);
        return v.toLocaleString(undefined, {maximumFractionDigits: 2});
    } catch { return String(amount); }
}

function formatUsd(v) {
    const n = parseFloat(v || 0);
    if (n < 0.01) return n.toFixed(6);
    if (n < 100) return n.toFixed(2);
    if (n < 10000) return n.toFixed(1);
    return n.toLocaleString(undefined, {maximumFractionDigits: 0});
}

function selectHolding(contract) {
    if (!contract) return;
    holdingsState.selectedContract = contract;
    // 高亮选中行
    document.querySelectorAll(".holding-row").forEach(r => {
        r.classList.toggle("selected", r.dataset.contract && r.dataset.contract.toLowerCase() === contract.toLowerCase());
    });
    // 更新K线标题
    const titleEl = document.getElementById("holdingsKlineTitle");
    if (titleEl) {
        const row = document.querySelector(`.holding-row[data-contract="${CSS.escape(contract)}"]`);
        const sym = row ? row.querySelector(".holding-symbol")?.textContent : "";
        titleEl.textContent = `${sym || ""} ${contract.slice(0,6)}…${contract.slice(-4)}`;
    }
    // 加载历史K线
    loadKlineHistory(contract);
}

// =========== 4. K线历史 + 实时订阅 ===========
async function loadKlineHistory(contract) {
    if (!contract) return;
    const wrap = document.getElementById("holdingsKlineWrap");
    const empty = document.getElementById("holdingsKlineEmpty");
    if (empty) empty.style.display = "none";
    wrap.classList.add("loading");
    try {
        const url = `/api/kline?address=${encodeURIComponent(contract)}&interval=${holdingsState.klineInterval}&limit=500&to=${Date.now()}`;
        const resp = await fetch(url);
        const data = await resp.json();
        if (!data.success || !Array.isArray(data.candles) || !data.candles.length) {
            if (empty) { empty.style.display = "block"; empty.textContent = "无K线数据"; }
            holdingsState.klineHistory = [];
            drawKlineChart();
            return;
        }
        // candles 格式: [open, high, low, close, volume, timestamp_ms, count]
        holdingsState.klineHistory = data.candles.map(c => ({
            t: c[5], o: c[0], h: c[1], l: c[2], c: c[3], v: c[4]
        }));
        holdingsState.lastKlineTs = holdingsState.klineHistory[holdingsState.klineHistory.length - 1].t;
        drawKlineChart();
    } catch (e) {
        if (empty) { empty.style.display = "block"; empty.textContent = "查询失败: " + (e.message || e); }
    } finally {
        wrap.classList.remove("loading");
    }
    // 如果实时订阅是开启状态，重新订阅新 token
    if (holdingsState.wsKlineActive) {
        subscribeKlineStream(contract);
    }
}

function toggleKlineRealtime(forceOn) {
    const willOn = (forceOn === undefined) ? !holdingsState.wsKlineActive : forceOn;
    const btn = document.getElementById("holdingsKlineToggleBtn");
    if (willOn) {
        if (!holdingsState.selectedContract) {
            holdingsPanelToast("请先选择一个 token", "error");
            return;
        }
        holdingsState.wsKlineActive = true;
        if (btn) { btn.classList.add("active"); btn.textContent = "⏸ 实时"; }
        connectKlineWS();
        subscribeKlineStream(holdingsState.selectedContract);
    } else {
        holdingsState.wsKlineActive = false;
        if (btn) { btn.classList.remove("active"); btn.textContent = "▶ 实时"; }
        unsubscribeKlineStream();
        // 关闭 WS（但保持连接池可复用，下次重开快速）
        // 简单实现：直接 close
        if (holdingsState.wsKline) {
            try { holdingsState.wsKline.close(); } catch {}
            holdingsState.wsKline = null;
        }
    }
}

function connectKlineWS() {
    if (holdingsState.wsKline && holdingsState.wsKline.readyState === WebSocket.OPEN) return;
    holdingsState.wsKline = new WebSocket("wss://nbstream.binance.com/w3w/stream");
    holdingsState.wsKline.onopen = () => {
        console.log("[K线WS] 已连接");
        // 连接成功后如果已有订阅记录，重新发送
        if (holdingsState.wsKlineStream) {
            sendKlineSubscribe(holdingsState.wsKlineStream);
        }
    };
    holdingsState.wsKline.onmessage = (ev) => {
        try {
            const msg = JSON.parse(ev.data);
            handleKlineWSMessage(msg);
        } catch (e) { /* ignore */ }
    };
    holdingsState.wsKline.onclose = () => {
        console.log("[K线WS] 已关闭");
        // 如果还应该开着，自动重连
        if (holdingsState.wsKlineActive) {
            setTimeout(() => { if (holdingsState.wsKlineActive) connectKlineWS(); }, 2000);
        }
    };
    holdingsState.wsKline.onerror = (e) => {
        console.warn("[K线WS] 错误", e);
    };
}

function subscribeKlineStream(contract) {
    if (!contract) return;
    // stream 名：kl@14@<address>@1s
    // 14 = BSC chain id (Binance Web3 内部编码)
    const stream = `kl@14@${contract.toLowerCase()}@${holdingsState.klineInterval}`;
    // 先取消旧订阅
    if (holdingsState.wsKlineStream && holdingsState.wsKlineStream !== stream) {
        unsubscribeKlineStream();
    }
    holdingsState.wsKlineStream = stream;
    if (holdingsState.wsKline && holdingsState.wsKline.readyState === WebSocket.OPEN) {
        sendKlineSubscribe(stream);
    }
    // 如果 WS 还没连上，connectKlineWS 会在 onopen 里重发
}

function unsubscribeKlineStream() {
    if (!holdingsState.wsKlineStream) return;
    if (holdingsState.wsKline && holdingsState.wsKline.readyState === WebSocket.OPEN) {
        try {
            holdingsState.wsKline.send(JSON.stringify({
                id: "kline-unsub-" + Date.now(),
                method: "UNSUBSCRIBE",
                params: [holdingsState.wsKlineStream]
            }));
        } catch {}
    }
    holdingsState.wsKlineStream = null;
}

function sendKlineSubscribe(stream) {
    try {
        holdingsState.wsKline.send(JSON.stringify({
            id: "kline-sub-" + Date.now(),
            method: "SUBSCRIBE",
            params: [stream]
        }));
        console.log("[K线WS] 订阅:", stream);
    } catch {}
}

function handleKlineWSMessage(msg) {
    // Binance Web3 stream 消息格式:
    // {"stream":"kl@14@0x...@1s","data":{"o":"1.23","h":"1.25","l":"1.22","c":"1.24","v":"100","t":1700000000000,"c":1}}
    // 字段名：o=open, h=high, l=low, c=close, v=volume, t=timestamp_ms
    if (!msg || !msg.data) return;
    const d = msg.data;
    // 兼容不同字段名
    const t = parseInt(d.t || d.T || d.timestamp || 0);
    const o = parseFloat(d.o || d.open || 0);
    const h = parseFloat(d.h || d.high || 0);
    const l = parseFloat(d.l || d.low || 0);
    const c = parseFloat(d.c || d.close || 0);
    const v = parseFloat(d.v || d.volume || 0);
    if (!t) return;
    // 增量更新：同根 K线 → 替换；新根 → 追加
    const arr = holdingsState.klineHistory;
    const last = arr[arr.length - 1];
    if (last && last.t === t) {
        // 替换最后一根
        last.o = o; last.h = h; last.l = l; last.c = c; last.v = v;
    } else if (last && t > last.t) {
        // 新增
        arr.push({t, o, h, l, c, v});
        // 保留最多 600 根
        if (arr.length > 600) arr.shift();
    }
    holdingsState.lastKlineTs = t;
    // 节流绘制：最多 5 帧/s
    scheduleKlineDraw();
}

let _klineDrawScheduled = false;
function scheduleKlineDraw() {
    if (_klineDrawScheduled) return;
    _klineDrawScheduled = true;
    setTimeout(() => {
        _klineDrawScheduled = false;
        drawKlineChart();
    }, 200);
}

// =========== 5. K线绘制（canvas，简易蜡烛图）===========
function drawKlineChart() {
    const canvas = document.getElementById("holdingsKlineCanvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    const data = holdingsState.klineHistory;
    ctx.clearRect(0, 0, W, H);
    if (!data || !data.length) {
        // 显示"无数据"已在DOM处理
        return;
    }
    // 计算价格范围
    let pMin = Infinity, pMax = -Infinity;
    data.forEach(k => { if (k.l < pMin) pMin = k.l; if (k.h > pMax) pMax = k.h; });
    if (pMin === pMax) { pMin -= 1; pMax += 1; }
    const padding = (pMax - pMin) * 0.1;
    pMin -= padding; pMax += padding;
    // 绘制区域
    const padLeft = 4, padRight = 60, padTop = 8, padBottom = 18;
    const chartW = W - padLeft - padRight;
    const chartH = H - padTop - padBottom;
    // 背景
    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, W, H);
    // 网格
    ctx.strokeStyle = "#1f2937";
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
        const y = padTop + chartH * i / 4;
        ctx.beginPath(); ctx.moveTo(padLeft, y); ctx.lineTo(padLeft + chartW, y); ctx.stroke();
    }
    // 价格刻度
    ctx.fillStyle = "#6b7280";
    ctx.font = "10px monospace";
    for (let i = 0; i <= 4; i++) {
        const y = padTop + chartH * i / 4;
        const v = pMax - (pMax - pMin) * i / 4;
        ctx.fillText(formatPriceTick(v), padLeft + chartW + 4, y + 3);
    }
    // 蜡烛宽度
    const n = data.length;
    const candleW = Math.max(1, chartW / n * 0.7);
    const step = chartW / n;
    // 绘制蜡烛
    data.forEach((k, i) => {
        const x = padLeft + i * step + step / 2;
        const yOpen = padTop + chartH * (pMax - k.o) / (pMax - pMin);
        const yClose = padTop + chartH * (pMax - k.c) / (pMax - pMin);
        const yHigh = padTop + chartH * (pMax - k.h) / (pMax - pMin);
        const yLow = padTop + chartH * (pMax - k.l) / (pMax - pMin);
        // 涨跌色
        const up = k.c >= k.o;
        ctx.strokeStyle = up ? "#22c55e" : "#ef4444";
        ctx.fillStyle = up ? "#22c55e" : "#ef4444";
        // 高低影线
        ctx.beginPath();
        ctx.moveTo(x, yHigh); ctx.lineTo(x, yLow);
        ctx.stroke();
        // 实体
        const bodyTop = Math.min(yOpen, yClose);
        const bodyH = Math.max(1, Math.abs(yClose - yOpen));
        ctx.fillRect(x - candleW / 2, bodyTop, candleW, bodyH);
    });
    // 时间轴（最后一个时间戳）
    const lastK = data[data.length - 1];
    if (lastK) {
        ctx.fillStyle = "#9ca3af";
        ctx.font = "10px monospace";
        const dt = new Date(lastK.t);
        const ts = `${String(dt.getHours()).padStart(2,"0")}:${String(dt.getMinutes()).padStart(2,"0")}:${String(dt.getSeconds()).padStart(2,"0")}`;
        ctx.fillText(ts, padLeft + chartW - 60, H - 4);
    }
    // 当前价格线
    const lastClose = lastK ? lastK.c : 0;
    if (lastClose) {
        const y = padTop + chartH * (pMax - lastClose) / (pMax - pMin);
        ctx.strokeStyle = "#fbbf24";
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(padLeft, y); ctx.lineTo(padLeft + chartW, y);
        ctx.stroke();
        ctx.setLineDash([]);
        // 价格标签
        ctx.fillStyle = "#fbbf24";
        ctx.fillRect(padLeft + chartW, y - 7, padRight, 14);
        ctx.fillStyle = "#0d1117";
        ctx.font = "bold 10px monospace";
        ctx.fillText(formatPriceTick(lastClose), padLeft + chartW + 2, y + 3);
    }
}

function formatPriceTick(v) {
    if (v === 0) return "0";
    if (Math.abs(v) < 0.0001) return v.toExponential(2);
    if (Math.abs(v) < 1) return v.toFixed(6);
    if (Math.abs(v) < 100) return v.toFixed(4);
    if (Math.abs(v) < 10000) return v.toFixed(2);
    return v.toLocaleString(undefined, {maximumFractionDigits: 0});
}

// =========== 6. 卖出 ===========
async function sellHolding(contract, btn) {
    if (!contract) return;
    if (holdingsState.sellInflight.has(contract)) {
        holdingsPanelToast("正在卖出中，请稍候", "warn");
        return;
    }
    if (!confirm(`确认卖出 ${contract.slice(0,6)}…${contract.slice(-4)} 的全部持仓？`)) return;
    holdingsState.sellInflight.add(contract);
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.classList.add("loading");
    btn.textContent = "卖出中…";
    try {
        const resp = await fetch("/api/sell", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({contract}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.success) {
            const txHash = data.tx_hash || "";
            const orderId = data.order_id || "";
            let short = "已提交";
            if (txHash) short = txHash.slice(0, 6) + "…" + txHash.slice(-4);
            else if (orderId) short = "order:" + orderId.slice(0, 8);
            btn.classList.remove("loading");
            btn.classList.add("success");
            btn.textContent = "✓ " + short;
            holdingsPanelToast("卖出成功 " + short, "success");
            console.log("[SELL] success:", {txHash, orderId, contract});
            // 1.5s 后立即刷新持仓
            setTimeout(() => fetchHoldings(false), 1500);
            setTimeout(() => {
                btn.classList.remove("success");
                btn.textContent = originalText;
                btn.disabled = false;
            }, 3000);
        } else {
            const errMsg = (data && (data.api_message || data.api_error || data.error)) || ("HTTP " + resp.status);
            btn.classList.remove("loading");
            btn.classList.add("error");
            btn.textContent = "✗ 失败";
            holdingsPanelToast("卖出失败: " + errMsg, "error");
            console.warn("[SELL] failed:", data, contract);
            setTimeout(() => {
                btn.classList.remove("error");
                btn.textContent = originalText;
                btn.disabled = false;
            }, 3500);
        }
    } catch (e) {
        btn.classList.remove("loading");
        btn.classList.add("error");
        btn.textContent = "✗ 网络异常";
        holdingsPanelToast("网络异常: " + (e.message || e), "error");
        setTimeout(() => {
            btn.classList.remove("error");
            btn.textContent = originalText;
            btn.disabled = false;
        }, 3500);
    } finally {
        holdingsState.sellInflight.delete(contract);
    }
}

// =========== 7. 弹窗 toast ===========
let _holdingsToastTimer = null;
function holdingsPanelToast(msg, kind) {
    const el = document.getElementById("holdingsPanelToast");
    if (!el) return;
    el.classList.remove("success", "error", "warn", "show");
    if (kind) el.classList.add(kind);
    el.textContent = msg;
    void el.offsetWidth;
    el.classList.add("show");
    if (_holdingsToastTimer) clearTimeout(_holdingsToastTimer);
    _holdingsToastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}

// =========== 启动 ===========
// 在 DOMContentLoaded 后注入 UI（确保不阻塞首屏）
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectHoldingsUI);
} else {
    injectHoldingsUI();
}
</script>
</body>
</html>
"""

# ============================================================
# 注册进程内推送 hooks：pusher → server 直接调用，跳过环回 HTTP
# ============================================================
# 关键架构优化：pusher 与 server 同进程时，不再走 HTTP POST localhost/api/tweet，
# 而是直接调用下面的 hook，省去 JSON 序列化 + TCP 环回 + Flask 解析 + 反序列化，
# 每条更新省下几～几十 ms 延迟。在 UPDATE 高频场景下避免排队堆积。
# HTTP 路由仍保留，用于跨进程部署或手动测试时的 fallback。
pusher.register_hooks(
    on_new_message=_handle_incoming_tweet,        # 新增卡片
    on_update_message=_handle_incoming_tweet,     # 更新卡片（逻辑相同：内部按 tweet_id 路由）
    on_token_update=_handle_incoming_token_update,  # token 增量更新
    on_migrated_tokens=_handle_incoming_migrated_tokens  # 迁移 token 列表
)

# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    target_url = "http://localhost:50000"
    # 启动 hook 消费者 greenthread（在 eventlet 调度器内消费队列，调 socketio.emit）
    # 必须在 socketio.run 之前启动，确保 pusher 推送的数据有人消费
    socketio.start_background_task(_hook_consumer_loop)
    print("🚀 Hook 消费者已启动 (eventlet greenthread)")
    # 使用 pusher_v4 启动后台任务（支持 Tweet + Profile）
    # 注意：eventlet 已 monkey_patch，threading.Thread 会被自动改成绿色线程
    thread = threading.Thread(target=pusher.start_pusher, args=(target_url,), daemon=True)
    thread.start()
    print("🚀 Flask 服务器启动中...")
    print("📡 Binance WebSocket / Twitter 后台监控已启动 (支持 Handle Profile)")
    print("📍 访问地址: http://localhost:50000")
    # debug=False：关闭 Werkzeug 调试器（避免安全风险与额外开销），适合长期运行
    # use_reloader=False：避免 pusher 后台线程被重载逻辑干扰
    socketio.run(app, host="0.0.0.0", port=50000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
