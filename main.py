from fastapi import FastAPI, Request, Response, HTTPException

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
import json
import uuid
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
import asyncio

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
    stream: Optional[bool] = True

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
    "claude-3.7-sonnet-abacus": "CLAUDE_V3_7_SONNET", 
    "claude-3.7-sonnet-thinking-abacus": "CLAUDE_V3_7_SONNET_THINKING", 
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
    "gpt-4o-2024-08-06-abacus": "OPENAI_GPT4O", 
    "gpt-3.5-turbo-abacus": "OPENAI_O3_MINI",  
    "gpt-3.5-turbo-16k-abacus": "OPENAI_O3_MINI_HIGH" 
}

BASE_URL = "https://pa002.abacus.ai"

TIMEOUT = 30.0  # 请求超时时间（秒）
MAX_RETRIES = 3  # 最大重试次数
RETRY_DELAY = 1  # 重试延迟（秒）

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
def get_headers(auth_token: str) -> Dict[str, str]:
    """生成请求头"""
    return {
        "sec-ch-ua-platform": "Windows",
        "sec-ch-ua": '"Not(A:Brand";v="99", "Microsoft Edge";v="133", "Chromium";v="133"',
        "sec-ch-ua-mobile": "?0",
        "X-Abacus-Org-Host": "apps",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "host": "pa002.abacus.ai",
        "Cookie": auth_token,
        "Accept": "text/event-stream",
        "Content-Type": "text/plain;charset=UTF-8"
    }

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

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, chat_request: ChatRequest):
    """处理聊天完成请求"""
    # 获取认证token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return Response(
            content=json.dumps({"error": "未提供有效的Authorization header"}),
            status_code=401
        )
    
    auth_token = auth_header.replace("Bearer ", "")
    
    # 创建会话ID
    conversation_id = str(uuid.uuid4())
    
    # 处理消息
    full_message = process_messages(chat_request.messages)
    
    # 准备请求数据
    request_data = {
        "requestId": str(uuid.uuid4()),
        "deploymentConversationId": conversation_id,
        "message": full_message,
        "isDesktop": True,
        "chatConfig": {
            "timezone": "Asia/Shanghai",
            "language": "zh-CN"
        },
        "llmName": MODEL_MAPPING.get(chat_request.model, chat_request.model),
        "externalApplicationId": str(uuid.uuid4())
    }

    # 流式请求处理
    async def generate_stream():
        headers = get_headers(auth_token)
        
        for retry in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "POST",
                        f"{BASE_URL}/api/_chatLLMSendMessageSSE",
                        headers=headers,
                        content=json.dumps(request_data),
                        timeout=TIMEOUT
                    ) as response:
                        async for line in response.aiter_lines():
                            if not line.strip():
                                continue
                                
                            try:
                                data = json.loads(line)
                                
                                if data.get("type") == "text" and data.get("title") != "Thinking...":
                                    chunk = {
                                        "id": str(uuid.uuid4()),
                                        "object": "chat.completion.chunk",
                                        "created": int(uuid.uuid1().time_low),
                                        "model": chat_request.model,
                                        "choices": [{
                                            "delta": {
                                                "role": "assistant",
                                                "content": data.get("segment", "")
                                            },
                                            "index": 0
                                        }]
                                    }
                                    yield f"data: {json.dumps(chunk)}\n\n"
                                    
                                if data.get("end"):
                                    # 发送结束标记
                                    chunk = {
                                        "id": str(uuid.uuid4()),
                                        "object": "chat.completion.chunk",
                                        "created": int(uuid.uuid1().time_low),
                                        "model": chat_request.model,
                                        "choices": [{
                                            "delta": {"content": ""},
                                            "index": 0,
                                            "finish_reason": "stop"
                                        }]
                                    }
                                    yield f"data: {json.dumps(chunk)}\n\n"
                                    yield "data: [DONE]\n\n"
                                    break  # 成功完成，退出重试循环
                                    
                            except json.JSONDecodeError:
                                continue
            except (httpx.TimeoutException, httpx.RequestError) as e:
                if retry == MAX_RETRIES - 1:  # 最后一次重试
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                await asyncio.sleep(RETRY_DELAY)
    
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
    return Response(
        content=json.dumps({
            "error": {
                "message": error_message,
                "type": exc.__class__.__name__,
                "code": 500
            }
        }),
        status_code=500,
        media_type="application/json"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
