"""
系统提示词构建 + ReAct 输出解析。
=================================
本地模型 (llama.cpp) 不原生支持 OpenAI 函数调用, 因此采用"文本 ReAct"协议:
模型每轮只输出一个 JSON 对象:
  - 需要工具:  {"thought": "...", "action": "工具名", "action_input": {参数}}
  - 任务完成:  {"thought": "...", "final_answer": "最终答复"}
"""
from __future__ import annotations

import json
import re
from typing import Optional


def build_system_prompt(goal: str, tools_block: str) -> str:
    """组装系统提示词。tools_block 由 ToolRegistry.prompt_block() 生成。"""
    return (
        "你是一个运行在 Windows 上的电脑自动化助手, 通过调用工具完成任务。\n"
        f"你的任务目标: {goal}\n\n"
        "你可以使用以下工具 (工具名: 说明 | 参数):\n"
        f"{tools_block}\n"
        "规则:\n"
        "1. 每轮只能输出一个 JSON 对象, 不要输出 JSON 以外的任何文字、解释或 Markdown 代码块。\n"
        "2. 需要调用工具时, 严格输出:\n"
        '   {"thought": "简短思考", "action": "工具名", "action_input": {"参数": "值"}}\n'
        "3. 目标已完成、或确认无法完成时, 严格输出:\n"
        '   {"thought": "简短总结", "final_answer": "给用户的最终答复"}\n'
        "4. 一次只调用一个工具, 等工具返回结果后再决定下一步。\n"
        '5. action_input 中的参数名必须与工具说明完全一致。'
    )


def parse_llm_json(text: str) -> Optional[dict]:
    """从模型输出中解析出 JSON 对象; 解析失败返回 None。

    容错: 容忍 Markdown 围栏 (```json ... ```)、前后杂文、多个花括号。
    """
    if not text or not text.strip():
        return None
    t = text.strip()

    # 去掉 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S)
    if m:
        t = m.group(1).strip()

    # 提取第一个 { 到最后一个 }
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(t[start : end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None
