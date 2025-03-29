from fastapi import FastAPI, Request, Response, HTTPException, BackgroundTasks, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
import httpx
import json
import uuid
import logging
import time
import os
import random
from typing import Optional, List, Dict, Any, Set, Tuple, Union
from pydantic import BaseModel, validator
import asyncio
import threading
import queue
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import sys
from dotenv import load_dotenv
load_dotenv()

import aiohttp
import uvicorn
from enum import Enum
import hashlib
import requests
import urllib.parse

# 日志配置
# 从环境变量获取日志级别，默认为INFO
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
VERBOSE_LOGGING = os.environ.get("VERBOSE_LOGGING", "1") == "1"  # 是否启用详细日志，默认开启

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 根据详细日志设置调整日志输出函数
def log_info(message, verbose_only=False):
    if not verbose_only or VERBOSE_LOGGING:
        logger.info(message)

def log_debug(message):
    if VERBOSE_LOGGING:
        logger.debug(message)

def log_error(message, exc_info=False):
    logger.error(message, exc_info=exc_info)

def log_warning(message):
    logger.warning(message)

# 会话池配置
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "10"))  # 最大活跃会话数
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "3600"))  # 会话超时时间(秒)
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "50"))  # 最大并发请求数

# 单账号配置
DEPLOYMENT_ID = os.environ.get("DEPLOYMENT_ID", "")
EXTERNAL_APP_ID = os.environ.get("EXTERNAL_APP_ID", "")
NEW_CHAT_NAME = os.environ.get("NEW_CHAT_NAME", "New Chat")

# 基础URL
BASE_URL = os.environ.get("BASE_URL", "https://apps.abacus.ai")

# 超时配置
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "10.0"))  # 连接超时，默认10秒
STREAM_TIMEOUT = float(os.environ.get("STREAM_TIMEOUT", "600.0"))   # 输出超时，默认10分钟
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))               # 最大重试次数
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "1.0"))           # 重试延迟（秒）

# HTTP连接池配置
HTTP_POOL_CONNECTIONS = int(os.environ.get("HTTP_POOL_CONNECTIONS", "100"))  # 连接池最大连接数
HTTP_POOL_KEEPALIVE = int(os.environ.get("HTTP_POOL_KEEPALIVE", "20"))       # 连接保持时间(秒)
HTTP_MAX_KEEPALIVE_CONNECTIONS = int(os.environ.get("HTTP_MAX_KEEPALIVE_CONNECTIONS", "10"))  # 最大保持连接数
HTTP_MAX_CONNECTIONS = int(os.environ.get("HTTP_MAX_CONNECTIONS", "100"))    # 最大连接数

# 全局HTTP客户端，用于连接复用
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(CONNECT_TIMEOUT, connect=CONNECT_TIMEOUT),
    limits=httpx.Limits(
        max_connections=HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS
    )
)

# 为兼容Pydantic v1和v2，添加辅助函数获取模型数据
def get_model_dict(model):
    """获取Pydantic模型的字典表示，兼容v1和v2版本"""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    else:
        return model.dict()

# 会话管理器
class SessionManager:
    def __init__(self):
        self.sessions = {}  # 会话字典 {session_id: session_data}
        self.session_lock = asyncio.Lock()  # 用于同步会话访问的锁
        self.request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)  # 请求并发数限制
        self.account_usage = {}  # 记录每个账号的使用情况 {(deployment_id, external_app_id): count}

    async def get_session(self, cookie: str, session_token: Optional[str] = None) -> Dict[str, Any]:
        """获取或创建会话，每次都创建新会话

        Args:
            cookie: Cookie值
            session_token: 可选的session-token值，如果提供则使用该值
        """
        async with self.session_lock:
            # 清理过期会话
            self._cleanup_expired_sessions()

            # 如果会话数已达上限，找出最旧的会话并替换
            if len(self.sessions) >= MAX_SESSIONS:
                oldest_session_id = min(
                    self.sessions.keys(),
                    key=lambda sid: self.sessions[sid]["last_used"]
                )
                log_info(f"会话数已达上限，替换最旧会话: {oldest_session_id}")
                del self.sessions[oldest_session_id]

            # 使用单一账号
            deployment_id = DEPLOYMENT_ID
            external_app_id = EXTERNAL_APP_ID

            # 创建新会话
            try:
                conv_resp = await create_conversation(cookie, deployment_id, external_app_id, session_token)
                session_id = conv_resp["result"]["deploymentConversationId"]
                session = {
                    "id": session_id,
                    "cookie": cookie,
                    "created": datetime.now(),
                    "last_used": datetime.now(),
                    "conv_resp": conv_resp,
                    "expired": False,
                    "deployment_id": deployment_id,
                    "external_app_id": external_app_id,
                    "session_token": session_token  # 保存session_token以便后续使用
                }

                # 更新账号使用情况
                account_key = (deployment_id, external_app_id)
                self.account_usage[account_key] = self.account_usage.get(account_key, 0) + 1

                # 保存会话
                self.sessions[session_id] = session
                log_info(f"创建新会话: {session_id}")
                return session
            except Exception as e:
                log_error(f"创建会话失败: {str(e)}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"创建会话未知错误: {str(e)}")

    def _cleanup_expired_sessions(self):
        """清理过期会话"""
        now = datetime.now()
        expired = []

        for session_id, session in self.sessions.items():
            if (now - session["last_used"]).total_seconds() > SESSION_TIMEOUT:
                session["expired"] = True
                expired.append(session_id)

        # 删除过期会话，保留最近10个以供重用
        if len(expired) > 0:
            for session_id in expired[:-10]:
                del self.sessions[session_id]
            log_info(f"清理了 {len(expired) - min(len(expired), 10)} 个过期会话")

    async def acquire_request_slot(self):
        """获取请求槽位"""
        return await self.request_semaphore.acquire()

    def release_request_slot(self):
        """释放请求槽位"""
        self.request_semaphore.release()

    def get_account_usage_stats(self):
        """获取账号使用统计"""
        return {f"{dep_id}/{app_id}": count for (dep_id, app_id), count in self.account_usage.items()}

# 创建会话管理器
session_manager = SessionManager()

# 创建请求计数和监控
class RequestMonitor:
    def __init__(self):
        self.total_requests = 0
        self.active_requests = 0
        self.success_requests = 0
        self.failed_requests = 0
        self.lock = threading.Lock()

    def increment_total(self):
        with self.lock:
            self.total_requests += 1
            self.active_requests += 1

    def decrement_active(self, success=True):
        with self.lock:
            self.active_requests -= 1
            if success:
                self.success_requests += 1
            else:
                self.failed_requests += 1

    def get_stats(self):
        with self.lock:
            return {
                "total_requests": self.total_requests,
                "active_requests": self.active_requests,
                "success_requests": self.success_requests,
                "failed_requests": self.failed_requests
            }

# 应用启动和关闭事件
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时执行
    log_info("应用启动...")

    # 启动后台任务清理过期长文本会话
    cleanup_task = asyncio.create_task(cleanup_long_text_sessions())

    # 使用单一账号
    log_info(f"使用单一账号，部署ID: {DEPLOYMENT_ID}, 应用ID: {EXTERNAL_APP_ID}")

    # 记录初始会话令牌
    initial_token = session_token_manager.get_token()
    if initial_token:
        log_info(f"初始会话令牌: {initial_token[:20]}...")
    else:
        log_info("初始会话令牌未设置，等待客户端请求时使用其cookie获取")
    
    yield

    # 应用关闭时执行
    log_info("应用关闭...")

    # 取消后台清理任务
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            log_info("长文本会话清理任务已取消")

    log_info("应用已成功关闭")

# 创建请求监控器
request_monitor = RequestMonitor()

# 创建FastAPI应用
app = FastAPI(lifespan=lifespan)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 定义数据模型
class Message(BaseModel):
    role: str
    content: Optional[str] = None  # 允许content为None或字符串
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None

    # 重写验证方法，确保content字段始终为字符串（null转为空字符串）
    @validator('content', pre=True)
    def validate_content(cls, v):
        # None转换为空字符串
        if v is None:
            return ""
        # 尝试转换非字符串值为字符串
        if not isinstance(v, str):
            try:
                return str(v)
            except Exception:
                return ""
        return v

class FunctionCall(BaseModel):
    name: str
    arguments: str

class Function(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None

class Tool(BaseModel):
    type: str
    function: Function

class ChatRequest(BaseModel):
    messages: List[Message]
    model: str
    frequency_penalty: Optional[float] = 0
    logit_bias: Optional[Dict[str, float]] = None
    logprobs: Optional[bool] = False
    top_logprobs: Optional[int] = None
    max_tokens: Optional[int] = None
    n: Optional[int] = 1
    presence_penalty: Optional[float] = 0
    response_format: Optional[Dict[str, str]] = None
    seed: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    user: Optional[str] = None

class AbacusRequest(BaseModel):
    requestId: str
    deploymentConversationId: str
    message: str
    isDesktop: bool
    chatConfig: Dict[str, str]
    llmName: str
    externalApplicationId: str

class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    usage: Optional[Dict[str, int]] = None
    system_fingerprint: Optional[str] = None

class ChatCompletionChoice(BaseModel):
    index: int
    message: Dict[str, Any]  # {"role": "assistant", "content": "..."}
    finish_reason: Optional[str] = "stop"
    logprobs: Optional[Dict[str, Any]] = None

class ChatCompletionUsage(BaseModel):
    prompt_tokens: int = 0  # Since we don't have accurate token counts
    completion_tokens: int = 0
    total_tokens: int = 0

# 模型映射
MODEL_MAPPING = {
    "gpt-4o-mini-abacus": "OPENAI_GPT4O_MINI",
    "claude-3.5-sonnet-abacus": "CLAUDE_V3_5_SONNET",
    "claude-3-sonnet": "CLAUDE_V3_5_SONNET",  # 别名
    "claude-3.7-sonnet-abacus": "CLAUDE_V3_7_SONNET",  # 新增
    "claude-3.7-sonnet-thinking-abacus": "CLAUDE_V3_7_SONNET_THINKING",  # 新增
    "o3-mini-abacus": "OPENAI_O3_MINI",
    "o3-mini-high-abacus": "OPENAI_O3_MINI_HIGH",
    "o1-mini-abacus": "OPENAI_O1_MINI",
    "deepseek-r1-abacus": "DEEPSEEK_R1",
    "gemini-2-pro-abacus": "GEMINI_2_PRO",
    "gemini-2-flash-thinking-abacus": "GEMINI_2_FLASH_THINKING",
    "gemini-2-flash-abacus": "GEMINI_2_FLASH",
    "gemini-1.5-pro-abacus": "GEMINI_1_5_PRO",
    "xai-grok-abacus": "XAI_GROK",
    "deepseek-v3-abacus": "DEEPSEEK_V3",
    "llama3-1-405b-abacus": "LLAMA3_1_405B",
    "gpt-4o-abacus": "OPENAI_GPT4O",
    "gpt-4o-2024-08-06-abacus": "OPENAI_GPT4O",  # 别名
    "gpt-3.5-turbo-abacus": "OPENAI_O3_MINI",  # 别名
    "QWQ_32B-abacus": "QWQ_32B",  # 别名
    "GEMINI_2_5_PRO-abacus": "GEMINI_2_5_PRO",
    }

# 全局会话令牌管理
class SessionTokenManager:
    def __init__(self):
        # 从环境变量获取初始会话令牌
        self.session_token = os.environ.get("INITIAL_SESSION_TOKEN", "")
        self.last_refresh_time = time.time()
        self.refresh_interval = 10 * 60  # 10分钟刷新一次
        self.lock = asyncio.Lock()
        self.cookie = ""  # 不再从环境变量获取cookie
        self.last_used_cookie = ""  # 记录最后一次使用的cookie

    def get_token(self) -> str:
        """获取当前的会话令牌"""
        return self.session_token

    # 更新当前使用的cookie
    def update_cookie(self, cookie: str) -> None:
        """更新当前使用的cookie"""
        if cookie and cookie != self.last_used_cookie:
            self.last_used_cookie = cookie
            self.cookie = cookie
            # 当cookie更新时，重置token刷新时间
            self.last_refresh_time = 0  # 这将触发下次请求立即刷新token

    async def refresh_token_if_needed(self, cookie: Optional[str] = None) -> str:
        """如果需要，刷新会话令牌"""
        # 如果提供了新cookie，则更新
        if cookie:
            self.update_cookie(cookie)
            
        current_time = time.time()

        # 如果距离上次刷新时间超过刷新间隔，则刷新令牌
        if current_time - self.last_refresh_time > self.refresh_interval:
            async with self.lock:
                # 再次检查，防止多个请求同时刷新
                if current_time - self.last_refresh_time > self.refresh_interval:
                    try:
                        new_token = await self.fetch_new_token()
                        if new_token:
                            self.session_token = new_token
                            log_info(f"成功刷新会话令牌")
                        else:
                            log_warning(f"刷新会话令牌失败，使用现有令牌")
                    except Exception as e:
                        log_error(f"刷新会话令牌时发生错误: {str(e)}", exc_info=True)
                    finally:
                        self.last_refresh_time = time.time()

        return self.session_token

    async def fetch_new_token(self) -> Optional[str]:
        """从 Abacus API 获取新的会话令牌"""
        if not self.cookie:
            log_warning("未设置cookie，无法刷新会话令牌")
            return None

        url = "https://apps.abacus.ai/api/v0/_getUserInfo"

        # 生成 Sentry 相关头部
        sentry_headers = generate_sentry_headers()

        headers = {
            "Host": "apps.abacus.ai",
            "accept": "application/json, text/plain, */*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,hu;q=0.5,zh-TW;q=0.4",
            "baggage": sentry_headers["baggage"],
            "content-type": "application/json",
            "origin": "https://apps.abacus.ai",
            "priority": "u=1, i",
            "reai-ui": "1",
            "referer": "https://apps.abacus.ai/",
            "sec-ch-ua": "\"Chromium\";v=\"134\", \"Not:A-Brand\";v=\"24\", \"Microsoft Edge\";v=\"134\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "sentry-trace": sentry_headers["sentry-trace"],
            "session-token": self.session_token,
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
            "x-abacus-org-host": "apps",
            "Cookie": self.cookie
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json={}, timeout=CONNECT_TIMEOUT)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success") and data.get("result", {}).get("sessionToken"):
                        return data["result"]["sessionToken"]
                    else:
                        log_warning(f"响应中未找到有效的 sessionToken: {data}")
                else:
                    log_error(f"获取会话令牌失败，状态码: {response.status_code}, 响应: {response.text}")
        except Exception as e:
            log_error(f"获取会话令牌时发生异常: {str(e)}", exc_info=True)

        return None

# 创建全局会话令牌管理器
session_token_manager = SessionTokenManager()

# 生成 Sentry 相关的请求头
def generate_sentry_headers() -> Dict[str, str]:
    """
    生成 Sentry 相关的请求头

    返回:
        包含 baggage 和 sentry-trace 的字典
    """
    # 生成唯一的跟踪 ID (32个十六进制字符)
    trace_id = uuid.uuid4().hex

    # 生成唯一的跨度 ID (16个十六进制字符)
    span_id = uuid.uuid4().hex[:16]

    # 固定的 Sentry 环境和版本信息
    environment = "production"
    release = "946244517de08b08598b94f18098411f5a5352d5"  # 这个可以固定或从配置中读取
    public_key = "3476ea6df1585dd10e92cdae3a66ff49"  # 这个可以固定或从配置中读取

    # 构建 baggage 头
    baggage = f"sentry-environment={environment},sentry-release={release},sentry-public_key={public_key},sentry-trace_id={trace_id}"

    # 构建 sentry-trace 头 (格式: trace_id-span_id)
    sentry_trace = f"{trace_id}-{span_id}"

    return {
        "baggage": baggage,
        "sentry-trace": sentry_trace
    }

# 工具函数：获取请求头
async def get_headers(cookie: str, session_token: Optional[str] = None, url: Optional[str] = None) -> Dict[str, str]:
    """生成请求头

    Args:
        cookie: Cookie值
        session_token: 可选的session-token值，如果提供则使用该值
        url: 请求的URL，用于设置正确的Host头
    """
    # 确保cookie值是合法的HTTP头值(移除前后空格)
    cookie = cookie.strip()

    # 使用会话令牌管理器获取令牌，如果提供了特定的令牌，则使用提供的令牌
    token_to_use = session_token if session_token else await session_token_manager.refresh_token_if_needed(cookie)

    # 生成 Sentry 相关头部
    sentry_headers = generate_sentry_headers()
    
    # 根据URL设置正确的HTTP/2头部
    authority = "apps.abacus.ai"  # 默认值
    path = "/api/_chatLLMSendMessageSSE"  # 默认路径
    
    if url:
        try:
            parsed_url = urllib.parse.urlparse(url)
            authority = parsed_url.netloc
            path = parsed_url.path
        except:
            pass  # 解析失败使用默认值

    headers = {
        ":authority": authority,
        ":method": "POST",
        ":path": path,
        ":scheme": "https",
        "accept": "text/event-stream",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,hu;q=0.5,zh-TW;q=0.4",
        "baggage": sentry_headers["baggage"],
        "content-type": "text/plain;charset=UTF-8",
        "origin": "https://apps.abacus.ai",
        "priority": "u=1, i",
        "referer": f"https://apps.abacus.ai/chatllm/?appId={EXTERNAL_APP_ID}&convoId={DEPLOYMENT_ID}",
        "sec-ch-ua": "\"Chromium\";v=\"134\", \"Not:A-Brand\";v=\"24\", \"Microsoft Edge\";v=\"134\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sentry-trace": sentry_headers["sentry-trace"],
        "session-token": token_to_use,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
        "x-abacus-org-host": "apps",
        "Cookie": cookie
    }
    
    # HTTP/2头部会被httpx自动处理，移除HTTP/2特定头部以避免冲突
    return {k: v for k, v in headers.items() if not k.startswith(":")}

async def create_conversation(cookie: str, deployment_id: str, external_app_id: str, session_token: Optional[str] = None) -> Dict[str, Any]:
    """创建新的会话

    Args:
        cookie: Cookie值
        deployment_id: 部署ID
        external_app_id: 外部应用ID
        session_token: 可选的session-token值，如果提供则使用该值
    """
    create_conv_url = "https://apps.abacus.ai/api/createDeploymentConversation"

    request_data = {
        "deploymentId": deployment_id,
        "name": NEW_CHAT_NAME,
        "externalApplicationId": external_app_id
    }

    # 确保cookie值是合法的HTTP头值(移除前后空格)
    cookie = cookie.strip()

    # 使用会话令牌管理器获取令牌，如果提供了特定的令牌，则使用提供的令牌
    token_to_use = session_token if session_token else await session_token_manager.refresh_token_if_needed()

    # 生成 Sentry 相关头部
    sentry_headers = generate_sentry_headers()

    headers = await get_headers(cookie, token_to_use, create_conv_url)
    headers.update({
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "referer": f"https://apps.abacus.ai/chatllm/?appId={external_app_id}",
    })

    try:
        log_info(f"创建会话请求: URL={create_conv_url}, 部署ID={deployment_id}, 应用ID={external_app_id}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                create_conv_url,
                headers=headers,
                json=request_data,
                timeout=CONNECT_TIMEOUT
            )

            if response.status_code != 200:
                error_text = response.text
                error_status = response.status_code

                # 尝试解析错误消息以获取更多信息
                try:
                    error_json = response.json()
                    error_message = error_json.get("error", "未知错误")
                    error_type = error_json.get("errorType", "未知类型")
                    log_error(f"创建会话失败: 状态码={error_status}, 错误类型={error_type}, 错误消息={error_message}")
                except Exception:
                    log_error(f"创建会话失败: 状态码={error_status}, 响应={error_text[:200]}")

                # 对于特定错误代码保留原始状态码
                if error_status in [401, 403]:
                    raise HTTPException(
                        status_code=error_status,
                        detail=f"权限错误: {error_text}"
                    )
                else:
                    raise HTTPException(
                        status_code=error_status,
                        detail=f"创建会话失败: {error_text}"
                    )

            resp_json = response.json()
            log_info(f"创建会话成功: {resp_json.get('result', {}).get('deploymentConversationId', 'unknown')}")
            return resp_json
    except httpx.RequestError as e:
        log_error(f"创建会话请求错误: {str(e)}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"创建会话请求错误: {str(e)}")
    except httpx.TimeoutException as e:
        log_error(f"创建会话超时: {str(e)}")
        raise HTTPException(status_code=504, detail=f"创建会话超时: {str(e)}")
    except HTTPException:
        # 直接重新抛出HTTP异常，保留原始状态码
        raise
    except Exception as e:
        log_error(f"创建会话未知错误: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"创建会话未知错误: {str(e)}")

def process_messages(messages: List[Message], previous_responses=None) -> str:
    """
    处理消息列表，将OpenAI API格式的对话历史转换为上游API需要的格式

    严格只处理客户端传入的消息，不引入任何服务端存储的上下文
    客户端需要在每次请求中提供完整的所需上下文

    如果提供了previous_responses，会将其作为上下文的一部分
    用于长文本分段处理时保持上下文连续性
    """
    # 提取系统消息
    system_messages = [msg.content for msg in messages if msg.role == "system"]
    system_message = "\n".join(system_messages) if system_messages else None

    # 获取非系统消息
    conversation_messages = [msg for msg in messages if msg.role != "system"]

    # 最后一条用户消息是当前消息
    current_message = None
    conversation_history = []

    if conversation_messages:
        # 如果最后一条是用户消息，将其作为当前消息
        if conversation_messages[-1].role == "user":
            current_message = conversation_messages[-1].content
            # 历史记录不包括当前消息
            conversation_history = conversation_messages[:-1]
        else:
            # 如果最后一条不是用户消息，全部作为历史记录
            conversation_history = conversation_messages
            current_message = ""  # 设置空的当前消息
            log_warning("未找到当前用户消息，使用空字符串")
    else:
        # 没有任何对话历史
        current_message = ""
        log_warning("没有任何对话消息")

    # 构建完整消息
    full_message = ""

    # 1. 添加系统消息（如果有）
    if system_message:
        full_message = f"System: {system_message}\n\n"

    # 2. 添加对话历史（如果有）
    if conversation_history:
        history_str = "\n".join(
            f"{msg.role.capitalize()}: {msg.content}"
            for msg in conversation_history
        )
        full_message += f"Previous conversation:\n{history_str}\n\n"

    # 3. 添加前面分段的响应（如果有）
    if previous_responses:
        full_message += f"Previous segments results:\n{previous_responses}\n\n"

    # 4. 添加当前消息
    full_message += f"Current message: {current_message}"

    log_debug(f"处理消息: 系统消息={bool(system_message)}, 历史消息数={len(conversation_history)}, 当前消息长度={len(current_message)}, 前面分段数量={1 if previous_responses else 0}")
    return full_message

async def process_non_streaming_response(response: httpx.Response) -> str:
    """处理非流式响应"""
    thinking_content = []
    output_content = []
    line_count = 0
    start_time = time.time()
    request_id = str(uuid.uuid4())
    log_info(f"[{request_id}] 开始处理非流式响应: 状态码={response.status_code}")

    # 当前正在处理的内容类型（思维链或输出）
    current_section = None  # "thinking" 或 "output"

    async for line in response.aiter_lines():
        line_count += 1
        if not line.strip():
            continue

        try:
            log_debug(f"[{request_id}] 非流式-收到行数据 #{line_count}: {line[:200]}...")
            data = json.loads(line)
            log_debug(f"[{request_id}] 非流式-解析JSON成功，类型: {data.get('type')}, 标题: {data.get('title', 'None')}")

            # 处理不同类型的消息
            if is_thinking_message(data):
                # 如果是思维链消息
                if data.get("segment"):
                    if isinstance(data.get("segment"), dict) and data["segment"].get("segment") is not None:
                        thinking_content.append(data["segment"]["segment"])
                    else:
                        thinking_content.append(data["segment"])
                current_section = "thinking"
            elif is_normal_output(data):
                # 如果是普通输出
                if data.get("segment"):
                    output_content.append(data.get("segment", ""))
                current_section = "output"
            elif data.get("type") == "text" and data.get("external") is True and data.get("segment"):
                # 特殊情况：外部思维链内容
                thinking_content.append(data["segment"])
                current_section = "thinking"

            if data.get("end"):
                log_debug(f"[{request_id}] 非流式-收到结束标记")
                break
        except Exception as e:
            log_warning(f"[{request_id}] 非流式-JSON解析错误: {str(e)}, 行内容: {line[:100]}...")
            continue

    # 组装最终内容
    final_content = ""

    # 如果有思维链内容，添加思维链部分
    if thinking_content:
        final_content += "<think>" + "".join(thinking_content) + "</think>"
        # 在思维链和输出之间添加换行
        if output_content:
            # 检查输出内容是否以代码块开始
            first_output = output_content[0] if output_content else ""
            starts_with_code_block = first_output.lstrip().startswith("```")

            # 如果以代码块开始，确保有足够的换行
            if starts_with_code_block:
                final_content += "\n\n\n"  # 额外的换行以确保代码块格式正确
                log_debug(f"[{request_id}] 非流式响应检测到代码块开始，添加额外换行")
            else:
                final_content += "\n\n"  # 标准换行

    # 添加输出内容
    if output_content:
        final_content += "".join(output_content)

    log_info(f"[{request_id}] 非流式响应处理完成: 处理了{line_count}行, 思维链长度={len(''.join(thinking_content))}, 输出长度={len(''.join(output_content))}, 耗时={time.time() - start_time:.2f}秒")
    return final_content

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, chat_request: ChatRequest = None, background_tasks: BackgroundTasks = None):
    """
    处理聊天完成请求

    每次请求都会创建新的会话，而不是复用现有会话
    客户端应当在每次请求中提供完整的对话历史作为上下文

    请求格式与OpenAI API兼容:
    {
        "model": "模型名称",
        "messages": [
            {"role": "system", "content": "系统指令"},
            {"role": "user", "content": "用户消息1"},
            {"role": "assistant", "content": "助手回复1"},
            {"role": "user", "content": "当前用户消息"}
        ],
        "stream": true/false
    }

    支持自动处理长文本：
    - 如果用户消息超过6000字符，会自动分割成多个片段处理
    - 所有片段将使用同一会话ID，保证上下文连续性
    - 分段处理完成后会自动合并结果

    服务器不会存储对话历史，所有的上下文都需要客户端在每次请求中提供
    """

    # 解析原始请求体，处理不符合ChatRequest模型的请求
    if chat_request is None:
        try:
            body = await request.json()
            # 确保必要字段存在
            if "messages" not in body:
                return Response(
                    content=json.dumps({
                        "error": {
                            "message": "必须提供messages字段",
                            "type": "invalid_request_error",
                            "param": "messages",
                            "code": "invalid_parameters"
                        }
                    }),
                    status_code=400,
                    media_type="application/json"
                )

            # 提取消息并转换为Message对象
            messages = []
            for msg in body.get("messages", []):
                if not isinstance(msg, dict):
                    return Response(
                        content=json.dumps({
                            "error": {
                                "message": "消息必须是一个对象",
                                "type": "invalid_request_error",
                                "param": "messages",
                                "code": "invalid_parameters"
                            }
                        }),
                        status_code=400,
                        media_type="application/json"
                    )

                # 验证必须有role字段
                if "role" not in msg:
                    return Response(
                        content=json.dumps({
                            "error": {
                                "message": "消息必须包含role字段",
                                "type": "invalid_request_error",
                                "param": "messages",
                                "code": "invalid_parameters"
                            }
                        }),
                        status_code=400,
                        media_type="application/json"
                    )

                # 处理content字段，确保它是字符串或None
                content = msg.get("content")
                if content is not None and not isinstance(content, str):
                    try:
                        content = str(content)
                    except:
                        content = ""

                # 创建Message对象
                message = Message(role=msg["role"], content=content)

                # 复制其他可选字段
                if "name" in msg:
                    message.name = msg["name"]
                if "tool_calls" in msg:
                    message.tool_calls = msg["tool_calls"]
                if "tool_call_id" in msg:
                    message.tool_call_id = msg["tool_call_id"]

                messages.append(message)

            # 获取模型名称，如果未提供则使用默认模型
            model = body.get("model", "claude-3.7-sonnet-thinking-abacus")

            # 提取流标志
            stream = body.get("stream", False)

            # 创建ChatRequest对象
            chat_request = ChatRequest(
                messages=messages,
                model=model,
                stream=stream
            )

            # 复制其他参数
            for key, value in body.items():
                if key not in ["messages", "model", "stream"] and hasattr(chat_request, key):
                    setattr(chat_request, key, value)

        except json.JSONDecodeError:
            return Response(
                content=json.dumps({
                    "error": {
                        "message": "无效的JSON请求体",
                        "type": "invalid_request_error",
                        "code": "json_parse_error"
                    }
                }),
                status_code=400,
                media_type="application/json"
            )

    # 如果background_tasks未提供，创建一个新的
    if background_tasks is None:
        background_tasks = BackgroundTasks()

    request_id = str(uuid.uuid4())
    request_monitor.increment_total()  # 增加请求计数

    # 验证模型名称是否有效
    model_name = chat_request.model
    if model_name not in MODEL_MAPPING and model_name not in MODEL_MAPPING.values():
        # 如果是无效模型，对于OpenAI的常见模型名，尝试寻找最佳匹配
        suggested_model = None

        # 对于gpt-4开头的模型，推荐使用GPT-4
        if model_name.startswith("gpt-4"):
            suggested_model = "gpt-4o-abacus"
        # 对于gpt-3.5开头的模型，推荐使用gpt-3.5-turbo
        elif model_name.startswith("gpt-3.5"):
            suggested_model = "gpt-3.5-turbo-abacus"
        # 对于claude开头的模型，推荐使用最匹配的claude模型
        elif model_name.startswith("claude"):
            if "3.5" in model_name:
                suggested_model = "claude-3.5-sonnet-abacus"
            elif "3.7" in model_name:
                suggested_model = "claude-3.7-sonnet-abacus"
            else:
                suggested_model = "claude-3.5-sonnet-abacus"

        # 如果没有找到匹配的模型，提供默认模型
        if not suggested_model:
            suggested_model = "claude-3.7-sonnet-abacus"

        log_warning(f"用户请求的模型 '{model_name}' 不存在，已自动替换为 '{suggested_model}'")
        model_name = suggested_model
        chat_request.model = model_name

    try:
        # 获取请求槽位（限制并发）
        await session_manager.acquire_request_slot()

        log_info(f"[{request_id}] 收到新请求: model={chat_request.model}, stream={chat_request.stream}, messages_count={len(chat_request.messages)}")

        # 获取认证token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            log_error(f"[{request_id}] 认证失败: 未提供有效的Authorization header")
            request_monitor.decrement_active(success=False)
            session_manager.release_request_slot()
            return Response(
                content=json.dumps({"error": "未提供有效的Authorization header"}),
                status_code=401
            )

        cookie = auth_header.replace("Bearer ", "").strip()  # 添加strip()移除前后空格
        log_info(f"[{request_id}] 提取cookie成功，长度: {len(cookie)}")

        # 从请求头中提取session-token
        session_token = request.headers.get("session-token")
        if session_token:
            log_info(f"[{request_id}] 从请求头中提取到session-token")

        # 获取或创建会话
        try:
            log_info(f"[{request_id}] 获取会话...")
            start_time = time.time()
            session = await session_manager.get_session(cookie, session_token)
            conv_resp = session["conv_resp"]
            session_id = session["id"]
            log_info(f"[{request_id}] 会话获取成功，耗时: {time.time() - start_time:.2f}秒, 会话ID: {session_id}")
        except HTTPException as e:
            # 保留原始HTTP异常的状态码
            log_error(f"[{request_id}] 获取会话失败: {e.status_code}: {e.detail}")
            request_monitor.decrement_active(success=False)
            session_manager.release_request_slot()
            return Response(
                content=json.dumps({
                    "error": {
                        "message": e.detail,
                        "type": "AuthenticationError" if e.status_code in [401, 403] else "ServerError",
                        "code": e.status_code
                    }
                }),
                status_code=e.status_code,
                media_type="application/json"
            )
        except Exception as e:
            log_error(f"[{request_id}] 获取会话失败: {str(e)}", exc_info=True)
            request_monitor.decrement_active(success=False)
            session_manager.release_request_slot()
            return Response(
                content=json.dumps({
                    "error": {
                        "message": f"获取会话失败: {str(e)}",
                        "type": "ServerError",
                        "code": 500
                    }
                }),
                status_code=500,
                media_type="application/json"
            )

        # 检查是否为长文本处理的后续请求
        continuation_id = request.headers.get("X-Continuation-ID")
        if continuation_id:
            log_info(f"[{request_id}] 检测到分段处理请求，continuation_id={continuation_id}")
            session_info = intermediate_results_store.get_session_info(continuation_id)
            if session_info:
                if intermediate_results_store.is_processing_complete(continuation_id):
                    # 所有分段已处理完成，返回合并结果
                    log_info(f"[{request_id}] 分段处理已完成，合并结果")
                    combined_response = intermediate_results_store.get_combined_response(continuation_id)

                    # 创建最终响应
                    response_id = f"chatcmpl-{uuid.uuid4().hex}"
                    create_time = int(time.time())
                    completion = {
                        "id": response_id,
                        "object": "chat.completion",
                        "created": create_time,
                        "model": chat_request.model,
                        "system_fingerprint": f"fp_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": combined_response
                                },
                                "finish_reason": "stop"
                            }
                        ],
                        "usage": {
                            "prompt_tokens": len(full_message) // 4,  # 粗略估计token数量
                            "completion_tokens": len(combined_response) // 4,
                            "total_tokens": (len(full_message) + len(combined_response)) // 4
                        },
                        "long_text_processing": {
                            "completed": True,
                            "total_segments": len(session_info["segments"]),
                            "processed_segments": len(session_info["responses"])
                        }
                    }

                    # 释放资源
                    background_tasks.add_task(session_manager.release_request_slot)
                    background_tasks.add_task(request_monitor.decrement_active, True)

                    return completion

                # 获取下一个要处理的分段
                next_segment = intermediate_results_store.get_next_segment(continuation_id)
                if next_segment:
                    # 获取已处理分段的响应
                    previous_responses = intermediate_results_store.get_combined_response(continuation_id)

                    # 创建包含当前分段的新消息
                    segment_messages = list(chat_request.messages)

                    # 找到最后一条用户消息并替换内容
                    for i in range(len(segment_messages) - 1, -1, -1):
                        if segment_messages[i].role == "user":
                            segment_messages[i].content = next_segment
                            break
                    else:
                        # 如果没有找到用户消息，添加一个
                        segment_messages.append(Message(role="user", content=next_segment))

                    # 处理该分段的消息
                    full_message = process_messages(segment_messages, previous_responses)
                    current_segment_index = session_info["current_segment"] + 1
                    total_segments = len(session_info["segments"])
                    log_info(f"[{request_id}] 处理分段 {current_segment_index}/{total_segments}，长度: {len(full_message)}")
                else:
                    log_error(f"[{request_id}] 未找到下一个要处理的分段")
                    request_monitor.decrement_active(success=False)
                    session_manager.release_request_slot()
                    return Response(
                        content=json.dumps({
                            "error": {
                                "message": "未找到下一个要处理的分段",
                                "type": "ServerError",
                                "code": 500
                            }
                        }),
                        status_code=500,
                        media_type="application/json"
                    )
            else:
                log_error(f"[{request_id}] 未找到continuation_id对应的会话信息: {continuation_id}")
                request_monitor.decrement_active(success=False)
                session_manager.release_request_slot()
                return Response(
                    content=json.dumps({
                        "error": {
                            "message": f"未找到continuation_id对应的会话信息: {continuation_id}",
                            "type": "ServerError",
                            "code": 404
                        }
                    }),
                    status_code=404,
                    media_type="application/json"
                )
        else:
            # 常规请求处理（非分段处理的续接请求）
            # 检查是否需要进行长文本分段处理
            user_message = None
            for msg in chat_request.messages:
                if msg.role == "user" and msg == chat_request.messages[-1]:
                    user_message = msg.content
                    break

            if user_message and len(user_message) > 6000:
                # 需要进行长文本分段处理
                log_info(f"[{request_id}] 检测到长文本，长度: {len(user_message)}，开始分段处理")
                segments = split_long_text(user_message)
                continuation_id = f"longtext-{uuid.uuid4().hex}"
                intermediate_results_store.initialize_session(continuation_id, segments)

                # 获取第一个分段
                first_segment = segments[0]

                # 创建包含第一个分段的新消息
                segment_messages = list(chat_request.messages)

                # 替换最后一条用户消息的内容
                for i in range(len(segment_messages) - 1, -1, -1):
                    if segment_messages[i].role == "user":
                        segment_messages[i].content = first_segment
                        break

                # 处理第一个分段
                full_message = process_messages(segment_messages)
                log_info(f"[{request_id}] 处理长文本第1/{len(segments)}段，长度: {len(full_message)}")

                # 准备标识符，用于后续请求
                long_text_info = {
                    "continuation_id": continuation_id,
                    "total_segments": len(segments),
                    "current_segment": 1,
                    "segment_length": len(first_segment),
                    "original_length": len(user_message)
                }
            else:
                # 标准消息处理
                full_message = process_messages(chat_request.messages)
                log_info(f"[{request_id}] 消息处理完成，长度: {len(full_message)}")
                long_text_info = None

        # 刷新session-token
        await session_token_manager.refresh_token_if_needed()

        # 准备请求数据
        abacus_request = AbacusRequest(
            requestId=request_id,
            deploymentConversationId=session_id,
            message=full_message,
            isDesktop=True,
            chatConfig={
                "timezone": "Asia/Shanghai",
                "language": "zh-CN"
            },
            llmName=MODEL_MAPPING.get(chat_request.model, chat_request.model),
            externalApplicationId=session["external_app_id"]
        )
        log_info(f"[{request_id}] 请求数据准备完成，目标模型: {abacus_request.llmName}")

        # 如果不是流式请求
        if not chat_request.stream:
            log_info(f"[{request_id}] 处理非流式请求...")
            request_url = f"{BASE_URL}/api/_chatLLMSendMessageSSE"
            headers = await get_headers(cookie, session_token, request_url)
            headers.update({
                "Accept": "text/event-stream",
                "Content-Type": "text/plain;charset=UTF-8"
            })

            for retry in range(MAX_RETRIES):
                try:
                    log_info(f"[{request_id}] 发送非流式请求到Abacus (尝试 {retry+1}/{MAX_RETRIES})...")
                    start_time = time.time()
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            request_url,
                            headers=headers,
                            content=json.dumps(get_model_dict(abacus_request)),
                            timeout=CONNECT_TIMEOUT
                        )
                        log_info(f"[{request_id}] 收到响应，状态码: {response.status_code}, 耗时: {time.time() - start_time:.2f}秒")

                        final_content = await process_non_streaming_response(response)
                        log_info(f"[{request_id}] 响应处理完成，内容长度: {len(final_content)}")

                        # 如果是长文本处理，保存当前分段的响应
                        if continuation_id:
                            intermediate_results_store.store_response(continuation_id, final_content)

                            # 查看是否还有更多分段需要处理
                            if not intermediate_results_store.is_processing_complete(continuation_id):
                                # 创建带有continuation_id的响应
                                response_id = f"chatcmpl-{uuid.uuid4().hex}"
                                create_time = int(time.time())
                                completion = {
                                    "id": response_id,
                                    "object": "chat.completion",
                                    "created": create_time,
                                    "model": chat_request.model,
                                    "system_fingerprint": f"fp_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                                    "choices": [
                                        {
                                            "index": 0,
                                            "message": {
                                                "role": "assistant",
                                                "content": final_content
                                            },
                                            "finish_reason": "length"  # 使用length表示因长度限制而截断
                                        }
                                    ],
                                    "usage": {
                                        "prompt_tokens": len(full_message) // 4,  # 粗略估计token数量
                                        "completion_tokens": len(final_content) // 4,
                                        "total_tokens": (len(full_message) + len(final_content)) // 4
                                    },
                                    "long_text_processing": {
                                        "completed": False,
                                        "continuation_id": continuation_id,
                                        "current_segment": long_text_info["current_segment"],
                                        "total_segments": long_text_info["total_segments"],
                                        "segment_length": long_text_info["segment_length"],
                                        "original_length": long_text_info["original_length"]
                                    }
                                }

                                # 请求完成，释放资源
                                background_tasks.add_task(session_manager.release_request_slot)
                                background_tasks.add_task(request_monitor.decrement_active, True)

                                return completion

                        # 处理生成的内容
                        response_id = f"chatcmpl-{uuid.uuid4().hex}"
                        create_time = int(time.time())

                        completion = {
                            "id": response_id,
                            "object": "chat.completion",
                            "created": create_time,
                            "model": chat_request.model,
                            "system_fingerprint": f"fp_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": final_content
                                    },
                                    "finish_reason": "stop"
                                }
                            ],
                            "usage": {
                                "prompt_tokens": len(full_message) // 4,  # 粗略估计token数量
                                "completion_tokens": len(final_content) // 4,
                                "total_tokens": (len(full_message) + len(final_content)) // 4
                            }
                        }

                        # 如果这是长文本处理的最后一个分段，添加长文本处理信息
                        if long_text_info:
                            completion["long_text_processing"] = {
                                "completed": intermediate_results_store.is_processing_complete(continuation_id),
                                "continuation_id": continuation_id,
                                "current_segment": long_text_info["current_segment"],
                                "total_segments": long_text_info["total_segments"]
                            }

                        # 请求完成，释放资源
                        background_tasks.add_task(session_manager.release_request_slot)
                        background_tasks.add_task(request_monitor.decrement_active, True)

                        return completion
                except (httpx.TimeoutException, httpx.RequestError) as e:
                    log_error(f"[{request_id}] 请求失败 (尝试 {retry+1}/{MAX_RETRIES}): {str(e)}")
                    if retry == MAX_RETRIES - 1:  # 最后一次重试
                        log_error(f"[{request_id}] 达到最大重试次数，放弃请求")

                        # 请求失败，释放资源
                        background_tasks.add_task(session_manager.release_request_slot)
                        background_tasks.add_task(request_monitor.decrement_active, False)

                        raise HTTPException(
                            status_code=504 if isinstance(e, httpx.TimeoutException) else 502,
                            detail=f"请求失败 (重试{MAX_RETRIES}次): {str(e)}"
                        )
                    await asyncio.sleep(RETRY_DELAY)
                    log_info(f"[{request_id}] 等待 {RETRY_DELAY} 秒后重试...")

            # 如果重试后仍未成功，确保不会无限循环
            log_error(f"[{request_id}] 所有重试都失败，返回错误")
            background_tasks.add_task(session_manager.release_request_slot)
            background_tasks.add_task(request_monitor.decrement_active, False)
            raise HTTPException(
                status_code=502,
                detail=f"请求重试{MAX_RETRIES}次后仍然失败"
            )
        else:
            # 处理流式请求
            if continuation_id:
                # 从会话中获取session_token
                session_token = session.get("session_token")
                return StreamingResponse(
                    generate_stream_with_long_text(
                        request_id, cookie, abacus_request, chat_request.model,
                        background_tasks, continuation_id, long_text_info, session_token
                    ),
                    media_type="text/event-stream"
                )
            else:
                # 从会话中获取session_token
                session_token = session.get("session_token")
                return StreamingResponse(
                    generate_stream(request_id, cookie, abacus_request, chat_request.model, background_tasks, session_token),
                    media_type="text/event-stream"
                )
    except Exception as e:
        # 处理意外异常
        log_error(f"[{request_id}] 处理请求时发生异常: {str(e)}", exc_info=True)

        # 确保释放资源
        try:
            session_manager.release_request_slot()
        except:
            pass
        request_monitor.decrement_active(success=False)

        # 返回错误响应
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"处理请求时发生异常: {str(e)}",
                    "type": "ServerError",
                    "code": 500
                }
            }),
            status_code=500,
            media_type="application/json"
        )

# 流式响应生成器函数
async def generate_stream(request_id: str, cookie: str, abacus_request: AbacusRequest, model: str, background_tasks: BackgroundTasks, session_token: Optional[str] = None):
    """生成流式响应"""
    try:
        request_url = f"{BASE_URL}/api/_chatLLMSendMessageSSE"
        headers = await get_headers(cookie, session_token, request_url)
        headers.update({
            "Accept": "text/event-stream",
            "Content-Type": "text/plain;charset=UTF-8"
        })

        # 处理状态
        thinking_content = []
        output_content = []
        thinking_buffer = ""
        output_buffer = ""
        has_sent_thinking_header = False
        has_closed_thinking = False
        processing_thinking = False  # 当前是否在处理思维链

        buffer_size_limit = int(os.environ.get("BUFFER_SIZE", "100"))  # 字符数，默认100
        buffer_time_limit = float(os.environ.get("BUFFER_TIME", "0.2"))  # 秒，默认0.2秒
        last_flush_time = time.time()

        for retry in range(MAX_RETRIES):
            try:
                log_info(f"[{request_id}] 发送流式请求到Abacus (尝试 {retry+1}/{MAX_RETRIES})...")
                start_time = time.time()
                
                # 使用全局HTTP客户端并开启HTTP/2以加速流式传输
                async with http_client.stream(
                    "POST",
                    request_url,
                    headers=headers,
                    content=json.dumps(get_model_dict(abacus_request))
                ) as response:
                    log_info(f"[{request_id}] 收到流式响应连接，状态码: {response.status_code}, 耗时: {time.time() - start_time:.2f}秒")

                    # 发送SSE响应头
                    yield "data: " + json.dumps({
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": int(uuid.uuid1().time_low),
                        "model": model,
                        "choices": [{
                            "delta": {"role": "assistant"},
                            "index": 0,
                            "finish_reason": None
                        }]
                    }) + "\n\n"
                    log_info(f"[{request_id}] 已发送流式响应头部")

                    content = ""
                    chunk_count = 0
                    last_log_time = time.time()

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue

                        try:
                            log_debug(f"[{request_id}] 收到行数据: {line[:200]}...")
                            data = json.loads(line)
                            log_debug(f"[{request_id}] 解析JSON成功，数据类型: {data.get('type')}, 标题: {data.get('title', 'None')}")

                            # 检查错误消息
                            if data.get("success") is False and data.get("error"):
                                error_msg = data.get("error")
                                log_error(f"[{request_id}] 服务端返回错误: {error_msg}")

                                # 发送错误消息给客户端
                                yield "data: " + json.dumps({
                                    "id": request_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(uuid.uuid1().time_low),
                                    "model": model,
                                    "choices": [{
                                        "delta": {"content": f"\n\n[错误: {error_msg}]"},
                                        "index": 0,
                                        "finish_reason": "stop"
                                    }]
                                }) + "\n\n"
                                yield "data: [DONE]\n\n"
                                log_info(f"[{request_id}] 发送错误消息到客户端并结束流")

                                # 释放资源
                                background_tasks.add_task(session_manager.release_request_slot)
                                background_tasks.add_task(request_monitor.decrement_active, False)
                                return

                            # 处理思维链和实际输出
                            if is_thinking_message(data):
                                # 提取思维链内容
                                segment = ""
                                if data.get("type") == "collapsible_component":
                                    if isinstance(data.get("segment"), dict) and data["segment"].get("segment") is not None:
                                        segment = data["segment"]["segment"]
                                elif data.get("segment"):
                                    segment = data.get("segment")

                                if segment:
                                    # 如果从输出切换到思维链，需要发送思维链开始标记
                                    if not processing_thinking and not has_closed_thinking:
                                        if not has_sent_thinking_header:
                                            yield "data: " + json.dumps({
                                                "id": request_id,
                                                "object": "chat.completion.chunk",
                                                "created": int(uuid.uuid1().time_low),
                                                "model": model,
                                                "choices": [{
                                                    "delta": {"content": "<think>"},
                                                    "index": 0,
                                                    "finish_reason": None
                                                }]
                                            }) + "\n\n"
                                            has_sent_thinking_header = True
                                        processing_thinking = True

                                    thinking_content.append(segment)
                                    thinking_buffer += segment

                                    # 发送思维链内容（如果缓冲区达到阈值）
                                    current_time = time.time()
                                    if len(thinking_buffer) >= buffer_size_limit or current_time - last_flush_time >= buffer_time_limit:
                                        yield "data: " + json.dumps({
                                            "id": request_id,
                                            "object": "chat.completion.chunk",
                                            "created": int(uuid.uuid1().time_low),
                                            "model": model,
                                            "choices": [{
                                                "delta": {"content": thinking_buffer},
                                                "index": 0,
                                                "finish_reason": None
                                            }]
                                        }) + "\n\n"
                                        thinking_buffer = ""
                                        last_flush_time = current_time

                            elif is_normal_output(data):
                                # 如果之前在处理思维链，现在切换到输出，需要关闭思维链
                                if processing_thinking and has_sent_thinking_header and not has_closed_thinking:
                                    # 发送剩余的思维链内容
                                    if thinking_buffer:
                                        yield "data: " + json.dumps({
                                            "id": request_id,
                                            "object": "chat.completion.chunk",
                                            "created": int(uuid.uuid1().time_low),
                                            "model": model,
                                            "choices": [{
                                                "delta": {"content": thinking_buffer},
                                                "index": 0,
                                                "finish_reason": None
                                            }]
                                        }) + "\n\n"
                                        thinking_buffer = ""

                                    # 发送思维链结束标记和换行
                                    yield "data: " + json.dumps({
                                        "id": request_id,
                                        "object": "chat.completion.chunk",
                                        "created": int(uuid.uuid1().time_low),
                                        "model": model,
                                        "choices": [{
                                            "delta": {"content": "</think>\n\n"},
                                            "index": 0,
                                            "finish_reason": None
                                        }]
                                    }) + "\n\n"
                                    has_closed_thinking = True
                                    processing_thinking = False

                                    # 添加一个小延迟，确保思维链结束和代码块开始之间有足够的分隔
                                    await asyncio.sleep(0.05)

                                # 处理输出内容
                                segment = data.get("segment", "")

                                # 检查segment是否以代码块开始
                                starts_with_code_block = segment.lstrip().startswith("```")

                                if segment:
                                    # 如果是刚从思维链结束且内容以代码块开始，确保有足够的换行
                                    if has_closed_thinking and starts_with_code_block and chunk_count == 0:
                                        # 添加额外的换行确保代码块格式正确
                                        segment = "\n" + segment
                                        log_info(f"[{request_id}] 检测到代码块开始，添加额外换行以避免格式问题")

                                    output_content.append(segment)
                                    output_buffer += segment
                                    content += segment
                                    chunk_count += 1

                                    # 刷新输出缓冲区
                                    current_time = time.time()
                                    if len(output_buffer) >= buffer_size_limit or current_time - last_flush_time >= buffer_time_limit:
                                        yield "data: " + json.dumps({
                                            "id": request_id,
                                            "object": "chat.completion.chunk",
                                            "created": int(uuid.uuid1().time_low),
                                            "model": model,
                                            "choices": [{
                                                "delta": {"content": output_buffer},
                                                "index": 0,
                                                "finish_reason": None
                                            }]
                                        }) + "\n\n"
                                        output_buffer = ""
                                        last_flush_time = current_time

                            # 处理结束标记
                            if data.get("end"):
                                # 如果有未完成的思维链，关闭它
                                if has_sent_thinking_header and not has_closed_thinking:
                                    if thinking_buffer:
                                        yield "data: " + json.dumps({
                                            "id": request_id,
                                            "object": "chat.completion.chunk",
                                            "created": int(uuid.uuid1().time_low),
                                            "model": model,
                                            "choices": [{
                                                "delta": {"content": thinking_buffer},
                                                "index": 0,
                                                "finish_reason": None
                                            }]
                                        }) + "\n\n"
                                        thinking_buffer = ""

                                    yield "data: " + json.dumps({
                                        "id": request_id,
                                        "object": "chat.completion.chunk",
                                        "created": int(uuid.uuid1().time_low),
                                        "model": model,
                                        "choices": [{
                                            "delta": {"content": "</think>\n\n"},
                                            "index": 0,
                                            "finish_reason": None
                                        }]
                                    }) + "\n\n"
                                    has_closed_thinking = True

                                # 发送剩余输出内容
                                if output_buffer:
                                    yield "data: " + json.dumps({
                                        "id": request_id,
                                        "object": "chat.completion.chunk",
                                        "created": int(uuid.uuid1().time_low),
                                        "model": model,
                                        "choices": [{
                                            "delta": {"content": output_buffer},
                                            "index": 0,
                                            "finish_reason": None
                                        }]
                                    }) + "\n\n"
                                    output_buffer = ""

                                log_info(f"[{request_id}] 收到结束标记，总共处理 {chunk_count} 个内容块，思维链长度: {len(''.join(thinking_content))}, 输出长度: {len(''.join(output_content))}")

                                # 发送结束标记
                                yield "data: " + json.dumps({
                                    "id": request_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(uuid.uuid1().time_low),
                                    "model": model,
                                    "choices": [{
                                        "delta": {},
                                        "index": 0,
                                        "finish_reason": "stop"
                                    }]
                                }) + "\n\n"
                                yield "data: [DONE]\n\n"

                                # 请求完成，释放资源
                                background_tasks.add_task(session_manager.release_request_slot)
                                background_tasks.add_task(request_monitor.decrement_active, True)
                                break
                        except json.JSONDecodeError as e:
                            log_warning(f"[{request_id}] JSON解析错误: {str(e)}, 行内容: {line[:100]}...")
                            continue

                    log_info(f"[{request_id}] 流式响应完成，总耗时: {time.time() - start_time:.2f}秒")
                    return

            except (httpx.TimeoutException, httpx.RequestError) as e:
                log_error(f"[{request_id}] 流式请求失败 (尝试 {retry+1}/{MAX_RETRIES}): {str(e)}")
                if retry == MAX_RETRIES - 1:  # 最后一次重试
                    log_error(f"[{request_id}] 达到最大重试次数，放弃流式请求")

                    # 请求失败，释放资源
                    background_tasks.add_task(session_manager.release_request_slot)
                    background_tasks.add_task(request_monitor.decrement_active, False)

                    # 发送错误信息
                    error_msg = f"请求失败 (重试{MAX_RETRIES}次): {str(e)}"
                    yield "data: " + json.dumps({
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": int(uuid.uuid1().time_low),
                        "model": model,
                        "choices": [{
                            "delta": {"content": f"\n\n[错误: {error_msg}]"},
                            "index": 0,
                            "finish_reason": "stop"
                        }]
                    }) + "\n\n"
                    yield "data: [DONE]\n\n"
                    return
                await asyncio.sleep(RETRY_DELAY)
                log_info(f"[{request_id}] 等待 {RETRY_DELAY} 秒后重试流式请求...")
    except Exception as e:
        # 处理意外异常
        log_error(f"[{request_id}] 流式处理中发生异常: {str(e)}", exc_info=True)
        background_tasks.add_task(session_manager.release_request_slot)
        background_tasks.add_task(request_monitor.decrement_active, False)

        # 发送错误信息
        error_msg = f"处理请求时发生错误: {str(e)}"
        yield "data: " + json.dumps({
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(uuid.uuid1().time_low),
            "model": model,
            "choices": [{
                "delta": {"content": f"\n\n[错误: {error_msg}]"},
                "index": 0,
                "finish_reason": "stop"
            }]
        }) + "\n\n"
        yield "data: [DONE]\n\n"

@app.get("/stats")
async def get_stats():
    """获取服务器状态统计"""
    stats = request_monitor.get_stats()

    # 添加会话信息
    async with session_manager.session_lock:
        stats["active_sessions"] = len(session_manager.sessions)
        stats["session_details"] = [
            {
                "id": session_id,
                "created": session["created"].isoformat(),
                "last_used": session["last_used"].isoformat(),
                "expired": session["expired"],
                "deployment_id": session.get("deployment_id", "unknown"),
                "external_app_id": session.get("external_app_id", "unknown")
            }
            for session_id, session in session_manager.sessions.items()
        ]

        # 添加账号使用统计
        stats["account_usage"] = session_manager.get_account_usage_stats()
        stats["account"] = {"deployment_id": DEPLOYMENT_ID, "external_app_id": EXTERNAL_APP_ID}

    return stats

@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理"""
    error_message = str(exc)
    error_type = exc.__class__.__name__

    # 记录详细错误信息
    logger.error(
        f"全局异常: {error_type}: {error_message}\n"
        f"路径: {request.url.path}\n"
        f"方法: {request.method}\n"
        f"客户端: {request.client.host if request.client else 'unknown'}\n",
        exc_info=True
    )

    # 对于某些类型的错误，可以自定义状态码
    status_code = 500
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
    elif isinstance(exc, httpx.TimeoutException):
        status_code = 504
    elif isinstance(exc, httpx.RequestError):
        status_code = 502

    return Response(
        content=json.dumps({
            "error": {
                "message": error_message,
                "type": error_type,
                "code": status_code
            }
        }),
        status_code=status_code,
        media_type="application/json"
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """OpenAI API兼容的验证错误处理"""
    errors = exc.errors()
    error_messages = []

    for error in errors:
        loc = error.get("loc", [])
        if len(loc) > 1:  # 第一个是 "body", 第二个是真正的字段名
            field = ".".join(str(l) for l in loc[1:])
            msg = error.get("msg", "验证错误")
            error_messages.append(f"{field}: {msg}")

    error_detail = "; ".join(error_messages) if error_messages else "请求参数无效"

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": {
                "message": error_detail,
                "type": "invalid_request_error",
                "param": errors[0].get("loc", ["body"])[1] if errors and len(errors[0].get("loc", [])) > 1 else None,
                "code": "invalid_parameters"
            }
        }
    )

def is_thinking_message(data: Dict[str, Any]) -> bool:
    """
    判断消息是否为思维链(thinking)部分
    """
    # 类型1: "type": "text", "temp": true, "isSpinny": true, "title": "Thinking"
    if data.get("type") == "text" and data.get("temp") is True and data.get("title") == "Thinking":
        return True

    # 类型2: "type": "collapsible_component", "isThoughts": true
    if data.get("type") == "collapsible_component" and (data.get("isThoughts") is True or data.get("title") == "Thoughts"):
        return True

    # 类型3: "type": "text", "external": true - 这是思维链中的内容
    if data.get("type") == "text" and data.get("external") is True:
        return True
        
    # 类型4: 任何标记为"external": true的消息，或包含在"collapsible_component"中的segment
    if data.get("external") is True:
        return True
        
    return False

def is_normal_output(data: Dict[str, Any]) -> bool:
    """
    判断消息是否为正常输出部分
    """
    # 正常输出: "type": "text", 但没有思维链的特征
    if data.get("type") == "text" and not is_thinking_message(data):
        # 确保不是其他特殊类型
        if not data.get("temp") and not data.get("external"):
            return True

    return False

def process_message(data: Dict[str, Any], thinking_content: List[str], output_content: List[str]):
    """
    处理消息，将内容添加到相应的列表中
    """
    if is_thinking_message(data):
        # 提取思维链内容
        if data.get("type") == "collapsible_component":
            # 处理collapsible_component中可能包含的segment
            if isinstance(data.get("segment"), dict) and data["segment"].get("segment") is not None:
                thinking_content.append(data["segment"]["segment"])
        elif data.get("segment"):
            thinking_content.append(data["segment"])
    elif is_normal_output(data):
        # 提取正常输出内容
        if data.get("segment"):
            output_content.append(data["segment"])

    # 特殊情况：外部思维链的实际内容
    elif data.get("type") == "text" and data.get("external") is True and data.get("segment"):
        thinking_content.append(data["segment"])

@app.get("/v1/models")
async def list_models():
    """返回支持的模型列表"""
    models = [
        {
            "id": model_id,
            "object": "model",
            "created": 1677610602,
            "owned_by": "system",
        }
        for model_id in MODEL_MAPPING.keys()
    ]
    return {
        "object": "list",
        "data": models
    }

# 文本分割和中间结果存储
class IntermediateResultsStore:
    """存储长文本分段处理的中间结果"""
    def __init__(self):
        self.results = {}  # {session_id: {"segments": [], "responses": [], "current_segment": 0}}
        self.lock = threading.Lock()

    def initialize_session(self, session_id, segments):
        """初始化会话的分段信息"""
        with self.lock:
            self.results[session_id] = {
                "segments": segments,
                "responses": [],
                "current_segment": 0,
                "last_updated": time.time()
            }

    def store_response(self, session_id, response):
        """存储某个分段的响应结果"""
        with self.lock:
            if session_id in self.results:
                self.results[session_id]["responses"].append(response)
                self.results[session_id]["current_segment"] += 1
                self.results[session_id]["last_updated"] = time.time()
                return len(self.results[session_id]["responses"])
            return 0

    def get_session_info(self, session_id):
        """获取会话信息"""
        with self.lock:
            return self.results.get(session_id)

    def is_processing_complete(self, session_id):
        """检查会话的所有分段是否都已处理完成"""
        with self.lock:
            if session_id in self.results:
                info = self.results[session_id]
                return info["current_segment"] >= len(info["segments"])
            return False

    def get_combined_response(self, session_id):
        """获取所有分段的组合响应"""
        with self.lock:
            if session_id in self.results:
                return "\n\n".join(self.results[session_id]["responses"])
            return ""

    def get_next_segment(self, session_id):
        """获取下一个要处理的分段"""
        with self.lock:
            if session_id in self.results:
                info = self.results[session_id]
                if info["current_segment"] < len(info["segments"]):
                    return info["segments"][info["current_segment"]]
            return None

    def cleanup_old_sessions(self, max_age_seconds=3600):
        """清理过期的会话结果"""
        with self.lock:
            current_time = time.time()
            to_remove = []
            for session_id, info in self.results.items():
                if current_time - info["last_updated"] > max_age_seconds:
                    to_remove.append(session_id)

            for session_id in to_remove:
                del self.results[session_id]

            return len(to_remove)


def split_long_text(text, max_length=6000):
    """
    将长文本分割成不超过指定长度的片段
    尽量在句子末尾进行分割，避免句子被截断
    """
    if len(text) <= max_length:
        return [text]

    segments = []
    start = 0

    while start < len(text):
        # 如果剩余文本小于最大长度，直接添加剩余全部
        if start + max_length >= len(text):
            segments.append(text[start:])
            break

        # 尝试在句子边界分割
        end = start + max_length

        # 向前查找句子结束标记
        sentence_end = end
        while sentence_end > start + max_length * 0.8:  # 确保分段不会太小
            # 查找常见的句子结束标记
            for marker in ['. ', '。', '! ', '？', '? ', '！', '\n\n']:
                pos = text.rfind(marker, start, sentence_end)
                if pos > start + max_length * 0.5:  # 确保分段不会太小
                    sentence_end = pos + len(marker)
                    break
            else:
                # 如果没有找到合适的句子结束标记，回退到段落或空格
                for marker in ['\n', ' ']:
                    pos = text.rfind(marker, start, end)
                    if pos > start + max_length * 0.7:
                        sentence_end = pos + 1
                        break
                else:
                    # 如果都没有找到，就直接在最大长度处截断
                    sentence_end = end
                break

        segments.append(text[start:sentence_end])
        start = sentence_end

    return segments


# 创建中间结果存储实例
intermediate_results_store = IntermediateResultsStore()

async def generate_stream_with_long_text(
    request_id: str,
    cookie: str,
    abacus_request: AbacusRequest,
    model: str,
    background_tasks: BackgroundTasks,
    continuation_id: str,
    long_text_info: Dict[str, Any],
    session_token: Optional[str] = None
):
    """
    用于长文本分段处理的流式响应生成器
    与普通的generate_stream类似，但会处理分段数据并存储中间结果
    """
    log_info(f"[{request_id}] 开始流式生成 (长文本分段处理)...")
    request_url = f"{BASE_URL}/api/_chatLLMSendMessageSSE"
    headers = await get_headers(cookie, session_token, request_url)
    headers.update({
        "Accept": "text/event-stream",
        "Content-Type": "text/plain;charset=UTF-8",
        "X-Abacus-Stream": "true"
    })

    # 只有第一帧需要发送这个标记
    first_frame_sent = False
    current_segment = long_text_info["current_segment"]
    total_segments = long_text_info["total_segments"]

    # 用于收集思维链和输出内容
    thinking_content = []
    output_content = []
    current_section = "thinking"  # 可以是 "thinking" 或 "output"

    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", request_url,
                                     headers=headers,
                                     content=json.dumps(get_model_dict(abacus_request)),
                                     timeout=STREAM_TIMEOUT) as response:

                if response.status_code != 200:
                    log_error(f"[{request_id}] 流式请求失败: 状态码 {response.status_code}")
                    error_json = json.dumps({
                        "error": {
                            "message": f"流式请求失败: {response.status_code}",
                            "type": "ServerError",
                            "code": response.status_code
                        }
                    })
                    yield f"data: {error_json}\n\n"
                    return

                line_count = 0
                start_time = time.time()

                # 发送初始数据
                if not first_frame_sent:
                    response_id = f"chatcmpl-{uuid.uuid4().hex}"
                    created = int(time.time())
                    init_data = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "system_fingerprint": f"fp_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant"},
                                "finish_reason": None
                            }
                        ],
                        "long_text_processing": {
                            "continuation_id": continuation_id,
                            "current_segment": current_segment,
                            "total_segments": total_segments
                        }
                    }
                    yield f"data: {json.dumps(init_data)}\n\n"
                    first_frame_sent = True

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue

                    line_count += 1
                    try:
                        data = json.loads(line)

                        # 处理不同类型的消息
                        if is_thinking_message(data):
                            # 思维链消息，收集但不实时流式输出
                            if data.get("segment"):
                                if isinstance(data.get("segment"), dict) and data["segment"].get("segment") is not None:
                                    thinking_content.append(data["segment"]["segment"])
                                else:
                                    thinking_content.append(data["segment"])
                            current_section = "thinking"
                        elif is_normal_output(data):
                            # 普通输出，实时流式输出并收集
                            if data.get("segment"):
                                chunk = data.get("segment", "")
                                output_content.append(chunk)

                                # 创建OpenAI兼容的流式响应
                                chunk_data = {
                                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": chunk},
                                            "finish_reason": None
                                        }
                                    ]
                                }

                                # 添加长文本处理信息
                                chunk_data["long_text_processing"] = {
                                    "continuation_id": continuation_id,
                                    "current_segment": current_segment,
                                    "total_segments": total_segments
                                }

                                # 发送当前块
                                yield f"data: {json.dumps(chunk_data)}\n\n"

                            current_section = "output"

                        # 如果数据中包含end标记，表示流处理结束
                        if data.get("end"):
                            break

                    except json.JSONDecodeError:
                        log_warning(f"[{request_id}] 流式-解析JSON失败: {line[:100]}...")
                        continue

                # 存储完整响应用于后续分段
                complete_response = "".join(output_content)
                intermediate_results_store.store_response(continuation_id, complete_response)

                # 发送最终的结束标记
                is_complete = intermediate_results_store.is_processing_complete(continuation_id)
                final_data = {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop" if is_complete else "length"
                        }
                    ],
                    "long_text_processing": {
                        "completed": is_complete,
                        "continuation_id": continuation_id,
                        "current_segment": current_segment,
                        "total_segments": total_segments,
                        "next_segment": current_segment + 1 if not is_complete else None
                    }
                }
                yield f"data: {json.dumps(final_data)}\n\n"

                log_info(f"[{request_id}] 流式生成完成: 处理了{line_count}行, 思维链长度={len(''.join(thinking_content))}, 输出长度={len(''.join(output_content))}, 耗时={time.time() - start_time:.2f}秒")

                # 释放资源
                background_tasks.add_task(session_manager.release_request_slot)
                background_tasks.add_task(request_monitor.decrement_active, True)

    except Exception as e:
        log_error(f"[{request_id}] 流式生成异常: {str(e)}", exc_info=True)
        error_data = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "error"
                }
            ],
            "error": {
                "message": str(e),
                "type": "ServerError",
                "code": 500
            }
        }
        yield f"data: {json.dumps(error_data)}\n\n"

        # 释放资源
        background_tasks.add_task(session_manager.release_request_slot)
        background_tasks.add_task(request_monitor.decrement_active, False)

# 定期清理过期的分段处理会话
async def cleanup_long_text_sessions():
    """定期清理过期的长文本分段处理会话数据"""
    while True:
        try:
            count = intermediate_results_store.cleanup_old_sessions(max_age_seconds=3600)  # 1小时过期
            if count > 0:
                log_info(f"清理了{count}个过期的长文本分段处理会话")
        except Exception as e:
            log_error(f"清理长文本会话时出错: {str(e)}", exc_info=True)

        await asyncio.sleep(300)  # 每5分钟执行一次

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8647, reload=True)
