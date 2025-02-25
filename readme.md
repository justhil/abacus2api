# Abacus2Api

<div align="center">

![Abacus2Api Logo](https://via.placeholder.com/200x200?text=Abacus2Api)

[![FastAPI](https://img.shields.io/badge/FastAPI-0.68.0-009688.svg?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/Python-3.8+-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

_✨ 一个API代理服务 ✨_

</div>

## 📖 项目介绍

Abacus2Api是一个基于FastAPI构建的API代理服务，它提供了一个统一的接口来与各种大型语言模型(LLM)进行流式交互。该服务完全兼容OpenAI API格式，让您可以轻松地将现有应用与不同的AI模型集成。

### 主要特性

- 🚀 兼容OpenAI API格式
- 🌊 原生支持流式(Stream)响应
- 🔄 统一接口访问多种LLM模型
- 🛡️ 内置错误处理和异常管理
- 🌐 支持CORS，方便前端集成

## 🔧 系统架构

```mermaid
flowchart TD
    A[客户端应用] -->|API请求| B
    B[Abacus2Api] -->|处理请求| C
    C[消息格式化] --> D
    D[流式处理] --> E
    E[响应生成] --> F
    F[返回客户端]
    
    style B fill:#1e88e5,color:white
    style D fill:#43a047,color:white
    style F fill:#e53935,color:white
```

## 💻 安装指南

### 前置条件

- Python 3.8+
- pip (Python包管理工具)

### 安装步骤

1. 克隆仓库

```bash
git clone https://github.com/yourusername/abacus2api.git
cd abacus2api
```

2. 创建并激活虚拟环境（可选但推荐）

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python -m venv venv
source venv/bin/activate
```

3. 安装依赖

```bash
pip install -r requirements.txt
```

## 🚀 使用方法

### 启动服务

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

服务将在 `http://localhost:8000` 上运行。

### API端点

#### 健康检查

```
GET /
```

#### 获取可用模型列表

```
GET /v1/models
```

#### 创建聊天完成（流式响应）

```
POST /v1/chat/completions
```

请求体示例:

```json
{
  "messages": [
    {
      "role": "system",
      "content": "你是一个有用的AI助手。"
    },
    {
      "role": "user",
      "content": "你好，请介绍一下自己。"
    }
  ],
  "model": "gpt-3.5-turbo",
  "stream": true
}
```

## 📊 请求流程

```mermaid
sequenceDiagram
    participant C as 客户端
    participant A as Abacus2Api
    participant L as 语言模型
    
    C->>A: 发送请求
    A->>A: 处理请求
    A->>L: 转发请求
    L-->>A: 流式响应
    loop 响应块
        A-->>C: 发送事件
    end
```

## 🔍 故障排除

常见问题:

1. **连接超时**
   - 检查网络连接
   - 确认LLM服务端点是否可访问

2. **认证错误**
   - 验证API密钥是否有效
   - 确保Authorization格式正确

3. **请求格式错误**
   - 遵循API文档中的请求格式
   - 检查必填字段

## 📄 许可证

本项目基于MIT许可证发布 - 详情请查看[LICENSE](LICENSE)文件。

---

<div align="center">
Made with ❤️ by Abacus Team
</div>
