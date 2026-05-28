#!/usr/bin/env python3
"""
测试阿里云百炼 API 是否正常工作的简单脚本
"""

import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# 获取配置
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL")
model = os.getenv("LA_MODEL", "qwen-turbo")

print("=" * 50)
print("阿里云百炼 API 测试")
print("=" * 50)
print(f"\n配置信息:")
print(f"  API Key: {api_key[:10]}...{api_key[-4:]}")
print(f"  Base URL: {base_url}")
print(f"  Model: {model}")

# 测试 API 调用
try:
    from openai import OpenAI
    
    print(f"\n正在测试 API 调用...")
    
    client = OpenAI(
        api_key=api_key,
        base_url=base_url
    )
    
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是一个友好的助手。"},
            {"role": "user", "content": "你好！请简单介绍一下你自己。"}
        ],
        temperature=0.7,
        max_tokens=200
    )
    
    print("\n" + "=" * 50)
    print("API 调用成功!")
    print("=" * 50)
    print(f"\n模型回复:")
    print(f"  {response.choices[0].message.content}")
    print(f"\n使用统计:")
    print(f"  Prompt tokens: {response.usage.prompt_tokens}")
    print(f"  Completion tokens: {response.usage.completion_tokens}")
    print(f"  Total tokens: {response.usage.total_tokens}")
    
except ImportError as e:
    print(f"\n错误: 缺少必要的库 - {e}")
    print("请安装依赖: pip install openai python-dotenv")
    
except Exception as e:
    print(f"\n" + "=" * 50)
    print("API 调用失败!")
    print("=" * 50)
    print(f"\n错误信息: {e}")
    print(f"\n可能的解决方案:")
    print("  1. 检查 API Key 是否正确")
    print("  2. 确认阿里云账号已完成实名认证")
    print("  3. 检查网络连接是否正常")
    print("  4. 确认模型名称是否正确")

print("\n" + "=" * 50)
