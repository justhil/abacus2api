from fastapi import FastAPI, Request, Response, HTTPException

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
import json
import uuid
import logging
import time
import os
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
import asyncio
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

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

# 创建FastAPI应用
app = FastAPI()

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
}

# 基础URL
BASE_URL = os.environ.get("BASE_URL", "https://pa002.abacus.ai")

# 超时配置
CONNECT_TIMEOUT = float(os.environ["CONNECT_TIMEOUT"])  # 连接超时
STREAM_TIMEOUT = float(os.environ["STREAM_TIMEOUT"])    # 输出超时
MAX_RETRIES = int(os.environ["MAX_RETRIES"])            # 最大重试次数
RETRY_DELAY = float(os.environ["RETRY_DELAY"])          # 重试延迟（秒）

# 会话配置
DEPLOYMENT_ID = os.environ["DEPLOYMENT_ID"]         # 部署ID
EXTERNAL_APP_ID = os.environ["EXTERNAL_APP_ID"]     # 外部应用ID
NEW_CHAT_NAME = os.environ["NEW_CHAT_NAME"]         # 新会话名称

# 请求头配置
USER_AGENT = os.environ.get("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0")

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
        "User-Agent": USER_AGENT,
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "host": BASE_URL.replace("https://", ""),
        "Cookie": cookie
    }

async def create_conversation(cookie: str) -> Dict[str, Any]:
    """创建新的会话"""
    create_conv_url = "https://apps.abacus.ai/api/createDeploymentConversation"
    
    request_data = {
        "deploymentId": DEPLOYMENT_ID,
        "name": NEW_CHAT_NAME,
        "externalApplicationId": EXTERNAL_APP_ID
    }
    
    # 确保cookie值是合法的HTTP头值(移除前后空格)
    cookie = cookie.strip()
    
    headers = {
        "sec-ch-ua-platform": "Windows",
        "sec-ch-ua": '"Not(A:Brand";v="99", "Microsoft Edge";v="133", "Chromium";v="133"',
        "sec-ch-ua-mobile": "?0",
        "X-Abacus-Org-Host": "apps",
        "User-Agent": USER_AGENT,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,hu;q=0.5,zh-TW;q=0.4",
        "Content-Type": "application/json",
        "origin": "https://apps.abacus.ai",
        "referer": f"https://apps.abacus.ai/chatllm/?appId={EXTERNAL_APP_ID}",
        "host": "apps.abacus.ai",
        "Cookie": cookie
    }
    
    try:
        log_info(f"创建会话请求: URL={create_conv_url}, headers长度={len(str(headers))}, cookie长度={len(cookie)}")
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
    """处理消息列表，合并成单个消息"""
    system_message = next((msg.content for msg in messages if msg.role == "system"), None)
    context_messages = [msg for msg in messages if msg.role != "system"][:-1]
    current_message = messages[-1].content
    
    full_message = current_message
    
    if system_message:
        full_message = f"System: {system_message}\n\n{full_message}"
        
    if context_messages:
        context_str = "\n".join(f"{msg.role}: {msg.content}" for msg in context_messages)
        full_message = f"Previous conversation:\n{context_str}\nCurrent message: {full_message}"
        
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
async def chat_completions(request: Request, chat_request: ChatRequest):
    """处理聊天完成请求"""
    request_id = str(uuid.uuid4())
    log_info(f"[{request_id}] 收到新请求: model={chat_request.model}, stream={chat_request.stream}, messages_count={len(chat_request.messages)}")
    
    # 获取认证token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        log_error(f"[{request_id}] 认证失败: 未提供有效的Authorization header")
        return Response(
            content=json.dumps({"error": "未提供有效的Authorization header"}),
            status_code=401
        )
    
    cookie = auth_header.replace("Bearer ", "").strip()  # 添加strip()移除前后空格
    log_info(f"[{request_id}] 提取cookie成功，长度: {len(cookie)}")
    
    # 创建会话
    try:
        log_info(f"[{request_id}] 开始创建会话...")
        start_time = time.time()
        conv_resp = await create_conversation(cookie)
        log_info(f"[{request_id}] 会话创建成功，耗时: {time.time() - start_time:.2f}秒, 会话ID: {conv_resp['result']['deploymentConversationId']}")
    except HTTPException as e:
        # 保留原始HTTP异常的状态码
        log_error(f"[{request_id}] 创建会话失败: {e.status_code}: {e.detail}")
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
        log_error(f"[{request_id}] 创建会话失败: {str(e)}", exc_info=True)
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"创建会话失败: {str(e)}",
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
        deploymentConversationId=conv_resp["result"]["deploymentConversationId"],
        message=full_message,
        isDesktop=True,
        chatConfig={
            "timezone": "Asia/Shanghai",
            "language": "zh-CN"
        },
        llmName=MODEL_MAPPING.get(chat_request.model, chat_request.model),
        externalApplicationId=conv_resp["result"]["externalApplicationId"]
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
                    return response_dict
            except (httpx.TimeoutException, httpx.RequestError) as e:
                log_error(f"[{request_id}] 请求失败 (尝试 {retry+1}/{MAX_RETRIES}): {str(e)}")
                if retry == MAX_RETRIES - 1:  # 最后一次重试
                    log_error(f"[{request_id}] 达到最大重试次数，放弃请求")
                    raise HTTPException(
                        status_code=504 if isinstance(e, httpx.TimeoutException) else 502,
                        detail=f"请求失败 (重试{MAX_RETRIES}次): {str(e)}"
                    )
                await asyncio.sleep(RETRY_DELAY)
                log_info(f"[{request_id}] 等待 {RETRY_DELAY} 秒后重试...")

    # 流式请求处理
    log_info(f"[{request_id}] 处理流式请求...")
    # 由于generate_stream函数较大，将其定义为嵌套函数，可以访问周围作用域中的变量
    async def generate_stream():
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
                            "model": chat_request.model,
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
                                        "model": chat_request.model,
                                        "choices": [{
                                            "delta": {"content": f"\n\n[错误: {error_msg}]"},
                                            "index": 0,
                                            "finish_reason": "stop"
                                        }]
                                    }) + "\n\n"
                                    yield "data: [DONE]\n\n"
                                    log_info(f"[{request_id}] 发送错误消息到客户端并结束流")
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
                                                    "model": chat_request.model,
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
                                                "model": chat_request.model,
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
                                                "model": chat_request.model,
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
                                            "model": chat_request.model,
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
                                                "model": chat_request.model,
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
                                                "model": chat_request.model,
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
                                            "model": chat_request.model,
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
                                            "model": chat_request.model,
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
                                        "model": chat_request.model,
                                        "choices": [{
                                            "delta": {},
                                            "index": 0,
                                            "finish_reason": "stop"
                                        }]
                                    }) + "\n\n"
                                    yield "data: [DONE]\n\n"
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
                    # 发送错误信息
                    error_msg = f"请求失败 (重试{MAX_RETRIES}次): {str(e)}"
                    yield "data: " + json.dumps({
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": int(uuid.uuid1().time_low),
                        "model": chat_request.model,
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

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream"
    )

@app.get("/")
async def health_check():
    """健康检查"""
    return {"status": "ok", "version": "1.0.0"}

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8647)
