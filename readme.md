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

# Abacus API Python实现

这是一个使用FastAPI实现的Abacus API代理，功能与Go版本完全一致。它支持处理OpenAI风格的API请求，并将其转发到Abacus API，同时支持流式和非流式响应。

## 功能特点

- 完全兼容OpenAI API格式的请求
- 支持OpenAI标准API路径 (/v1/chat/completions、/v1/models)
- 支持多种模型映射，自动将OpenAI模型名称转换为Abacus模型
- 支持带"-abacus"后缀的模型名和标准模型名
- 支持流式和非流式响应
- 自动创建会话
- 处理cookies和必要的请求头

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## 使用示例

### 发送非流式请求

```python
import requests
import json

url = "http://localhost:8000/v1/chat/completions"  # 使用标准OpenAI API路径
headers = {
    "Content-Type": "application/json",
    "Cookie": "your_abacus_cookie_here"  # 标准Cookie头
    # 或者使用 "Authorization": "Bearer your_abacus_cookie_here"
}
data = {
    "messages": [
        {"role": "user", "content": "Hello, how are you?"}
    ],
    "model": "gpt-4o-abacus",  # 支持带-abacus后缀的模型名
    # 也可以在请求体中传递cookie: "cookie": "your_abacus_cookie_here"
}

response = requests.post(url, headers=headers, json=data)
print(json.dumps(response.json(), indent=2))
```

### 发送流式请求

```python
import requests
import json

url = "http://localhost:8000/v1/chat/completions"  # 使用标准OpenAI API路径
headers = {
    "Content-Type": "application/json",
    "Cookie": "your_abacus_cookie_here"  # 标准Cookie头
    # 或者使用 "Authorization": "Bearer your_abacus_cookie_here"
}
data = {
    "messages": [
        {"role": "user", "content": "Hello, how are you?"}
    ],
    "model": "gpt-4o-abacus",  # 支持带-abacus后缀的模型名
    "stream": True,
    # 也可以在请求体中传递cookie: "cookie": "your_abacus_cookie_here"
}

response = requests.post(url, headers=headers, json=data, stream=True)
for line in response.iter_lines():
    if line:
        line = line.decode('utf-8')
        if line.startswith('data: ') and line != 'data: [DONE]':
            json_str = line[6:]
            data = json.loads(json_str)
            content = data['choices'][0]['delta']['content']
            if content:
                print(content, end='', flush=True)
```

### 获取可用模型列表

```python
import requests

url = "http://localhost:8000/v1/models"
headers = {
    "Cookie": "your_abacus_cookie_here"  # 标准Cookie头
    # 或者使用 "Authorization": "Bearer your_abacus_cookie_here"
}

response = requests.get(url, headers=headers)
print(json.dumps(response.json(), indent=2))
```

### Cookie传递方式

本API支持以下几种方式传递Abacus cookie:

1. 在请求头中使用标准Cookie头: `"Cookie": "your_cookie_here"`
2. 在请求头中使用Bearer令牌: `"Authorization": "Bearer your_cookie_here"`
3. 在请求体中直接传递: `{"cookie": "your_cookie_here", ...}`
4. 在任何包含"cookie"或"auth"的自定义头中传递

最灵活的方式是直接在请求体JSON中包含cookie字段，特别是当cookie内容较长或包含特殊字符时。

## 支持的模型映射

系统支持多种模型名称映射到Abacus的模型，既可以使用标准模型名称（如gpt-4、gpt-4o）也可以使用带"-abacus"后缀的模型名称（如gpt-4-abacus、gpt-4o-abacus）：

- gpt-4 / gpt-4-abacus → OPENAI_GPT4O
- gpt-4o / gpt-4o-abacus → OPENAI_GPT4O
- gpt-4o-mini / gpt-4o-mini-abacus → OPENAI_GPT4O_MINI
- gpt-3.5-turbo / gpt-3.5-turbo-abacus → OPENAI_O3_MINI
- claude-3.5-sonnet-abacus → CLAUDE_V3_5_SONNET
- claude-3.7-sonnet-abacus → CLAUDE_V3_7_SONNET

当使用/v1/models接口时，会同时返回标准模型名和带"-abacus"后缀的模型名，可以根据实际需要选择使用。

更多模型映射请参考代码中的`MODEL_MAPPING`字典。

## 模型名称使用说明

本API支持两种模型命名方式：

1. **标准模型名称**：与OpenAI接口兼容的模型名，如`gpt-4`、`gpt-4o`等
2. **带后缀模型名称**：在标准名称后添加`-abacus`后缀，如`gpt-4-abacus`、`gpt-4o-abacus`等

两种命名都会被正确映射到对应的Abacus模型。这种灵活性使得API既能兼容OpenAI的标准调用方式，又能明确指示此请求是针对Abacus平台的调用。

推荐使用带`-abacus`后缀的模型名，以便在代码中清晰区分不同的API后端。
