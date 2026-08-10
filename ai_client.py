# -*- coding: utf-8 -*-
"""
AI 客户端：OpenAI 兼容接口（默认 DeepSeek），只用标准库 urllib。

- AI_SKILLS：内置三个技能（拷问拆解 / 快速拆解 / 通用问答），各自带系统提示词。
- chat_completion()：阻塞式 POST /chat/completions，返回 (ok, 文本或错误说明)。
- extract_tasks()：从回复里解析 ---TASKS--- 标记后的大纲，返回 [(level, title, deadline)]。
"""
import json
import urllib.error
import urllib.request

from models import parse_deadline

AI_SKILLS = [
    {
        "id": "grill",
        "name": "AI 拷问拆解",
        "desc": "像教练一样一次只问一个问题，根据回答继续追问，最后给出任务大纲",
        "icon": "question",
        "system": (
            "你是一名严格的规划教练，帮用户把一个目标拆成可执行的任务。"
            "你一次只问一个问题（目标是什么、最难在哪、截止什么时候、现有资源等），"
            "根据用户的回答继续追问，把关键环节问清楚。"
            "当用户说「好了 / 继续 / 开始拆解」，或你觉得信息已经足够时，"
            "停止提问，直接输出任务大纲，格式如下：\n"
            "---TASKS---\n"
            "任务A\n"
            "  子任务A1\n"
            "  子任务A2\n"
            "任务B | 2026-08-15\n"
            "缩进两个空格表示一层；需要截止时间的行在末尾加「 | YYYY-MM-DD」或「 | YYYY-MM-DD HH:MM」。"
        ),
    },
    {
        "id": "quick",
        "name": "快速拆解",
        "desc": "根据项目标题 / 截止时间 / 已有子任务，直接生成任务大纲",
        "icon": "bolt",
        "system": (
            "你是任务拆解助手。根据用户提供的项目信息，直接给出一份可执行的任务大纲。"
            "要求：5~10 条，用缩进表达层级，需要截止的关键步骤在行尾写清日期。"
            "只输出大纲本身，不要解释，格式如下：\n"
            "---TASKS---\n"
            "任务A\n"
            "  子任务A1\n"
            "  子任务A2\n"
            "任务B | 2026-08-15\n"
            "缩进两个空格表示一层；截止日期用「 | YYYY-MM-DD」或「 | YYYY-MM-DD HH:MM」写在行尾。"
        ),
    },
    {
        "id": "chat",
        "name": "通用问答",
        "desc": "普通对话：提问、建议、答疑，答案可复制 / 分享 / 存为任务备注",
        "icon": "forum",
        "system": "你是一个中文生活与学习助手。回答要简洁、直接、有条理；除非用户明确要求，否则不要啰嗦。",
    },
]

SKILL_BY_ID = {s["id"]: s for s in AI_SKILLS}


def chat_completion(base_url, model, api_key, messages, timeout=45):
    """调用 OpenAI 兼容的 /chat/completions。

    参数：
      base_url  —— 例如 https://api.deepseek.com
      model     —— 例如 deepseek-chat
      api_key   —— 用户的密钥（只用于本次请求，不落盘）
      messages  —— [{"role": "user|assistant|system", "content": "..."}, ...]
   返回：
      (True, 回复文本) 成功；
      (False, 中文错误说明) 失败（Key 未配 / 网络 / 鉴权 / 格式异常等）。
    """
    if not (api_key or "").strip():
        return False, "还没配置 API Key，请到「设置 → AI 设置」填写"
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return False, "还没配置 Base URL"
    payload = json.dumps(
        {"model": model or "deepseek-chat", "messages": messages, "stream": False}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key.strip()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        if e.code in (401, 403):
            return False, "鉴权失败：API Key 无效或没有权限"
        if e.code == 429:
            return False, "请求太频繁，请稍后再试"
        return False, f"服务返回错误 {e.code}：{body[:120]}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, TimeoutError):
            return False, "请求超时，请检查网络或重试"
        return False, f"无法连接服务器：{reason}"
    except TimeoutError:
        return False, "请求超时，请检查网络或重试"
    except Exception as e:
        return False, f"请求失败：{e}"
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return False, f"返回数据格式异常：{str(data)[:120]}"
    return True, content


def extract_tasks(text):
    """从 AI 回复中提取 ---TASKS--- 之后的大纲，返回 [(level, title, deadline), ...]。

    层级 = 行首空格数 ÷ 2（向下取整）；行尾「 | 日期」为截止时间。
    标记出现后的所有非空行都按任务行收集（预览步骤再人工确认）。
    """
    rows = []
    if not text:
        return rows
    in_tasks = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("---TASKS---"):
            in_tasks = True
            continue
        if not in_tasks or not stripped:
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        level = max(0, round(indent / 2))
        deadline = ""
        if "|" in stripped:
            left, right = stripped.split("|", 1)
            left = left.strip()
            right = right.strip()
            if parse_deadline(right) is not None:
                stripped = left
                deadline = right
        if not stripped:
            continue
        rows.append((level, stripped, deadline))
    return rows
