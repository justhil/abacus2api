from fastapi import FastAPI, Request, Response, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
import json
import uuid
import logging
import time
import os
import random
from typing import Optional, List, Dict, Any, Set, Tuple
from pydantic import BaseModel
import asyncio
import threading
import queue
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import sys

# 检查并导入可选依赖
missing_deps = []

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("已加载环境变量配置")
except ImportError:
    print("警告: python-dotenv 未安装，跳过环境变量加载。若需环境变量支持，请安装: pip install python-dotenv")
    missing_deps.append("python-dotenv")

try:
    import aiohttp
except ImportError:
    print("警告: aiohttp 未安装。若需异步HTTP支持，请安装: pip install aiohttp")
    missing_deps.append("aiohttp")
    # 提供一个简单的替代实现，这样代码可以继续运行
    class aiohttp:
        class ClientSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs): 
                raise NotImplementedError("aiohttp未安装，无法使用异步HTTP功能")

try:
    import uvicorn
except ImportError:
    print("警告: uvicorn 未安装。若需启动服务器，请安装: pip install uvicorn")
    missing_deps.append("uvicorn")

try:
    from enum import Enum
except ImportError:
    print("警告: enum 模块在当前Python版本中不可用")
    # 提供一个简单的Enum替代
    class Enum:
        pass

try:
    import hashlib
except ImportError:
    print("警告: hashlib 模块在当前Python版本中不可用")
    # 不提供替代，因为这是核心模块

try:
    import requests
except ImportError:
    print("警告: requests 未安装。若需同步HTTP支持，请安装: pip install requests")
    missing_deps.append("requests")

if missing_deps:
    print(f"请安装以下依赖以获取完整功能: pip install {' '.join(missing_deps)}")

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

# 多账号配置
DEPLOYMENT_IDS = os.environ.get("DEPLOYMENT_IDS", "23090466a").split(",")
EXTERNAL_APP_IDS = os.environ.get("EXTERNAL_APP_IDS", "46456e1b4").split(",")
NEW_CHAT_NAME = os.environ.get("NEW_CHAT_NAME", "New Chat")
ACCOUNT_SELECTION_STRATEGY = os.environ.get("ACCOUNT_SELECTION_STRATEGY", "round_robin")  # round_robin 或 random

# 确保ID列表长度一致，如果不一致，使用较短的列表长度
if len(DEPLOYMENT_IDS) != len(EXTERNAL_APP_IDS):
    min_length = min(len(DEPLOYMENT_IDS), len(EXTERNAL_APP_IDS))
    log_warning(f"DEPLOYMENT_IDS和EXTERNAL_APP_IDS长度不一致，将只使用前{min_length}个配置")
    DEPLOYMENT_IDS = DEPLOYMENT_IDS[:min_length]
    EXTERNAL_APP_IDS = EXTERNAL_APP_IDS[:min_length]

# 基础URL
BASE_URL = os.environ.get("BASE_URL", "https://pa002.abacus.ai")

# 超时配置
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "10.0"))  # 连接超时，默认10秒
STREAM_TIMEOUT = float(os.environ.get("STREAM_TIMEOUT", "600.0"))   # 输出超时，默认10分钟
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))               # 最大重试次数
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "1.0"))           # 重试延迟（秒）

# 账号选择器
class AccountSelector:
    def __init__(self, deployment_ids: List[str], external_app_ids: List[str], strategy: str = "round_robin"):
        self.deployment_ids = deployment_ids
        self.external_app_ids = external_app_ids
        self.strategy = strategy
        self.current_index = 0
        self.lock = threading.Lock()
        
        log_info(f"初始化账号选择器，策略: {strategy}, 账号数量: {len(deployment_ids)}")
    
    def get_next_account(self) -> Tuple[str, str]:
        """获取下一个要使用的账号配置"""
        with self.lock:
            if self.strategy == "random":
                # 随机选择
                index = random.randint(0, len(self.deployment_ids) - 1)
            else:
                # 轮询选择
                index = self.current_index
                self.current_index = (self.current_index + 1) % len(self.deployment_ids)
            
            return self.deployment_ids[index], self.external_app_ids[index]

# 创建账号选择器
account_selector = AccountSelector(DEPLOYMENT_IDS, EXTERNAL_APP_IDS, ACCOUNT_SELECTION_STRATEGY)

# 会话管理器
class SessionManager:
    def __init__(self):
        self.sessions = {}  # 会话字典 {session_id: session_data}
        self.session_lock = asyncio.Lock()  # 用于同步会话访问的锁
        self.request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)  # 请求并发数限制
        self.account_usage = {}  # 记录每个账号的使用情况 {(deployment_id, external_app_id): count}
    
    async def get_session(self, cookie: str) -> Dict[str, Any]:
        """获取或创建会话，每次都创建新会话"""
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
            
            # 获取下一个要使用的账号
            deployment_id, external_app_id = account_selector.get_next_account()
            
            # 创建新会话
            try:
                conv_resp = await create_conversation(cookie, deployment_id, external_app_id)
                session_id = conv_resp["result"]["deploymentConversationId"]
                session = {
                    "id": session_id,
                    "cookie": cookie,
                    "created": datetime.now(),
                    "last_used": datetime.now(),
                    "conv_resp": conv_resp,
                    "expired": False,
                    "deployment_id": deployment_id,
                    "external_app_id": external_app_id
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
    log_info("应用程序启动，初始化会话管理器")
    yield
    # 应用关闭时执行
    log_info("应用程序关闭，清理资源")

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
    content: str

class ChatRequest(BaseModel):
    messages: List[Message]
    model: str
    stream: Optional[bool] = False

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
    "gpt-3.5-turbo-16k-abacus": "OPENAI_O3_MINI_HIGH"  # 别名
    }

# 工具函数：获取请求头
def get_headers(cookie: str) -> Dict[str, str]:
    """生成请求头"""
    # 确保cookie值是合法的HTTP头值(移除前后空格)
    cookie = cookie.strip()
    
    return {
        "sec-ch-ua-platform": "Windows",
        "sec-ch-ua": '"Not(A:Brand";v="99", "Microsoft Edge";v="133", "Chromium";v="133"',
        "sec-ch-ua-mobile": "?0",
        "X-Abacus-Org-Host": "apps",
        "User-Agent": os.environ.get("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0"),
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "host": BASE_URL.replace("https://", ""),
        "Cookie": cookie
    }

async def create_conversation(cookie: str, deployment_id: str, external_app_id: str) -> Dict[str, Any]:
    """创建新的会话"""
    create_conv_url = "https://apps.abacus.ai/api/createDeploymentConversation"
    
    request_data = {
        "deploymentId": deployment_id,
        "name": NEW_CHAT_NAME,
        "externalApplicationId": external_app_id
    }
    
    # 确保cookie值是合法的HTTP头值(移除前后空格)
    cookie = cookie.strip()
    
    headers = {
        "sec-ch-ua-platform": "Windows",
        "sec-ch-ua": '"Not(A:Brand";v="99", "Microsoft Edge";v="133", "Chromium";v="133"',
        "sec-ch-ua-mobile": "?0",
        "X-Abacus-Org-Host": "apps",
        "User-Agent": os.environ.get("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0"),
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,hu;q=0.5,zh-TW;q=0.4",
        "Content-Type": "application/json",
        "origin": "https://apps.abacus.ai",
        "referer": f"https://apps.abacus.ai/chatllm/?appId={external_app_id}",
        "host": "apps.abacus.ai",
        "Cookie": cookie
    }
    
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

def process_messages(messages: List[Message]) -> str:
    """
    处理消息列表，将OpenAI API格式的对话历史转换为上游API需要的格式
    
    客户端每次对话都传入完整的上下文，不依赖服务器存储的会话历史
    
    消息格式应为 {"role": "user"|"assistant"|"system", "content": "消息内容"}
    """
    # 提取系统消息
    system_messages = [msg.content for msg in messages if msg.role == "system"]
    system_message = "\n".join(system_messages) if system_messages else None
    
    # 构建对话历史
    conversation_history = []
    
    # 按顺序处理所有非系统消息，构建对话历史
    user_messages = []
    assistant_messages = []
    
    for msg in [m for m in messages if m.role != "system"]:
        # 将所有非系统消息添加到对话历史中，保持原始顺序
        conversation_history.append({
            "role": msg.role,
            "content": msg.content
        })
    
    # 最后一条用户消息是当前消息
    current_message = None
    if conversation_history and conversation_history[-1]["role"] == "user":
        current_message = conversation_history[-1]["content"]
        # 从历史中移除当前消息，因为它将单独处理
        conversation_history.pop()
    
    # 如果没有找到当前用户消息
    if current_message is None:
        # 如果历史中没有用户消息，使用空字符串
        current_message = ""
        log_warning("未找到当前用户消息，使用空字符串")
    
    # 构建完整消息
    # 1. 首先添加系统消息（如果有）
    full_message = ""
    if system_message:
        full_message = f"System: {system_message}\n\n"
    
    # 2. 添加对话历史
    if conversation_history:
        history_str = "\n".join(
            f"{msg['role'].capitalize()}: {msg['content']}" 
            for msg in conversation_history
        )
        full_message += f"Previous conversation:\n{history_str}\n\n"
    
    # 3. 添加当前消息
    full_message += f"Current message: {current_message}"
        
    log_debug(f"处理完成的消息: 系统消息={bool(system_message)}, 历史消息数={len(conversation_history)}, 当前消息长度={len(current_message)}")
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
        except json.JSONDecodeError as e:
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
async def chat_completions(request: Request, chat_request: ChatRequest, background_tasks: BackgroundTasks):
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
    
    服务器不会存储对话历史，所有的上下文都需要客户端在每次请求中提供
    """
    request_id = str(uuid.uuid4())
    request_monitor.increment_total()  # 增加请求计数
    
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
        
        # 获取或创建会话
        try:
            log_info(f"[{request_id}] 获取会话...")
            start_time = time.time()
            session = await session_manager.get_session(cookie)
            conv_resp = session["conv_resp"]
            log_info(f"[{request_id}] 会话获取成功，耗时: {time.time() - start_time:.2f}秒, 会话ID: {session['id']}")
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
        
        # 处理消息
        full_message = process_messages(chat_request.messages)
        log_info(f"[{request_id}] 消息处理完成，长度: {len(full_message)}")
        
        # 准备请求数据
        abacus_request = AbacusRequest(
            requestId=request_id,
            deploymentConversationId=session["id"],
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
            headers = get_headers(cookie)
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
                            f"{BASE_URL}/api/_chatLLMSendMessageSSE",
                            headers=headers,
                            content=json.dumps(abacus_request.model_dump()),
                            timeout=CONNECT_TIMEOUT
                        )
                        log_info(f"[{request_id}] 收到响应，状态码: {response.status_code}, 耗时: {time.time() - start_time:.2f}秒")
                        
                        content = await process_non_streaming_response(response)
                        log_info(f"[{request_id}] 响应处理完成，内容长度: {len(content)}")
                        
                        chat_response = ChatResponse(
                            id=str(uuid.uuid4()),
                            created=int(uuid.uuid1().time_low),
                            model=chat_request.model,
                            choices=[{
                                "message": {
                                    "role": "assistant",
                                    "content": content
                                },
                                "finish_reason": "stop",
                                "index": 0
                            }],
                            usage={
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0
                            }
                        )
                        response_dict = chat_response.model_dump()
                        log_info(f"[{request_id}] 返回非流式响应，长度: {len(json.dumps(response_dict))}")
                        
                        # 请求完成，释放资源
                        background_tasks.add_task(session_manager.release_request_slot)
                        background_tasks.add_task(request_monitor.decrement_active, True)
                        
                        return response_dict
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
                    
        # 处理流式请求
        return StreamingResponse(
            generate_stream(request_id, cookie, abacus_request, chat_request.model, background_tasks),
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
        
        raise HTTPException(status_code=500, detail=f"处理请求时发生错误: {str(e)}")

# 流式响应生成器函数
async def generate_stream(request_id: str, cookie: str, abacus_request: AbacusRequest, model: str, background_tasks: BackgroundTasks):
    """生成流式响应"""
    try:
        headers = get_headers(cookie)
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
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "POST",
                        f"{BASE_URL}/api/_chatLLMSendMessageSSE",
                        headers=headers,
                        content=json.dumps(abacus_request.model_dump()),
                        timeout=STREAM_TIMEOUT
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
        stats["available_accounts"] = [
            {"deployment_id": dep_id, "external_app_id": app_id}
            for dep_id, app_id in zip(DEPLOYMENT_IDS, EXTERNAL_APP_IDS)
        ]
        stats["account_selection_strategy"] = ACCOUNT_SELECTION_STRATEGY
    
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

def is_thinking_message(data: Dict[str, Any]) -> bool:
    """
    判断消息是否为思维链(thinking)部分
    """
    # 类型1: "type": "text", "temp": true, "isSpinny": true, "title": "Thinking"
    if data.get("type") == "text" and data.get("temp") is True and data.get("title") == "Thinking":
        return True
    
    # 类型2: "type": "collapsible_component", "isThoughts": true
    if data.get("type") == "collapsible_component" and data.get("isThoughts") is True:
        return True
    
    # 类型3: "type": "text", "external": true - 这是思维链中的内容
    if data.get("type") == "text" and data.get("external") is True:
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8647)
