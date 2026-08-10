# -*- coding: utf-8 -*-
"""AI 客户端：OpenAI 兼容接口 + 内置技能 + 任务大纲解析。

只使用 Python 标准库，方便随 Flet 打包进 Android，不引入额外网络依赖。
"""
import json
import re
import urllib.error
import urllib.request


# ---------------------------------------------------------------------------
# 内置技能
# ---------------------------------------------------------------------------
AI_SKILLS = [
    {
        "id": "grill_decompose",
        "name": "AI 拷问拆解",
        "kind": "decompose",
        "description": "像 grill-me 一样逐题追问，把大项目磨清楚后生成任务树",
        "system": (
            "你是一个耐心、尖锐的项目拷问者。用户会先给出一个大项目。"
            "你一次只问一个问题，根据用户的回答决定下一个问题，重点挖掘：目标、"
            "截止时间、依赖、风险和可执行步骤。每次最多问一个问题。"
            "当信息足够拆解任务时，先简短总结，然后输出严格的大纲（缩进层级即父子关系），"
            "以 ---TASKS--- 单独一行开始，之后每行一个任务。"
            "大纲格式示例：\n"
            "---TASKS---\n"
            "项目名\n"
            "  阶段一\n"
            "    任务1\n"
            "    任务2\n"
            "  阶段二\n"
            "使用两个空格表示一层缩进。不要在大纲中出现空行。"
        ),
    },
    {
        "id": "quick_decompose",
        "name": "快速拆解",
        "kind": "decompose",
        "description": "直接基于现有信息生成任务树，不追问",
        "system": (
            "你是一个务实的项目拆解助手。用户会给出项目标题、截止时间和已有子任务。"
            "直接输出可执行的任务大纲：缩进层级即父子关系，每行一个任务，"
            "以 ---TASKS--- 单独一行开始，之后每行一个任务。"
            "使用两个空格表示一层缩进。不要在大纲中出现空行。"
        ),
    },
    {
        "id": "general_chat",
        "name": "通用问答",
        "kind": "chat",
        "description": "总结、起草、头脑风暴等日常 AI 对话",
        "system": (
            "你是一个清晰、简洁的 AI 助手。回答用户的问题，"
            "必要时分点、给例子、给出可执行的建议。不要编造事实。"
        ),
    },
]

SKILL_BY_ID = {s["id"]: s for s in AI_SKILLS}


# ---------------------------------------------------------------------------
# OpenAI 兼容请求（阻塞实现，调用方用 asyncio.to_thread 包装）
# ---------------------------------------------------------------------------
def chat_completion(base_url, api_key, model, messages, timeout=45) -> str:
    """发送 OpenAI 兼容 chat/completions 请求，返回助手文本。"""
    if not api_key:
        raise ValueError("尚未配置 API Key")
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.6,
            "max_tokens": 3000,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            raise RuntimeError("API Key 无效或没有权限") from e
        if e.code == 429:
            raise RuntimeError("请求过于频繁或配额不足") from e
        raise RuntimeError(f"AI 接口错误 ({e.code})：{body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 AI 接口：{e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError("请求超时，请稍后重试") from e

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("AI 接口返回为空")
    return (choices[0].get("message") or {}).get("content") or ""


# ---------------------------------------------------------------------------
# 任务大纲解析
# ---------------------------------------------------------------------------
def extract_tasks(text: str):
    """从 AI 回复中提取 ---TASKS--- 之后的大纲，解析成缩进树列表。

    返回 [(level, title), ...]，level 0 为根。找不到标记时返回 []。
    """
    marker = re.search(r"^\s*---TASKS---\s*$", text, flags=re.MULTILINE)
    if not marker:
        return []
    body = text[marker.end():].strip()
    rows = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[-\*•]+\s*", "", line)
        if not line:
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        level = indent // 2 if indent else 0
        rows.append((level, line))
    if not rows:
        return []
    # 归一化：根层至少是 0，且不能跳过层级（例如 0,2 -> 0,1）
    base = min(l for l, _ in rows)
    normalized = []
    prev = 0
    for level, title in rows:
        lvl = max(0, level - base)
        lvl = min(lvl, prev + 1)
        normalized.append((lvl, title))
        prev = lvl
    return normalized
