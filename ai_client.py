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
    {
        "id": "study_help",
        "name": "课堂速解",
        "kind": "chat",
        "description": "上课遇到没听过的词或概念，快速讲清楚",
        "system": (
            "你是一个耐心的课堂助教。用户在上课时遇到没听过的词、概念或术语。"
            "先用一两句大白话直接回答它是什么意思，再给一个贴近生活的例子，"
            "最后给出一个容易记住的记忆方法或一句话总结。"
            "如果用户追问，继续用更具体的例子解释，不要绕弯子。"
        ),
    },
    {
        "id": "roleplay",
        "name": "角色扮演",
        "kind": "chat",
        "description": "导入角色卡，建立长期陪伴对话",
        "system": (
            "你是用户导入的角色。严格按角色卡设定扮演，保持人设、语气和关系。"
            "如果用户提到之前聊过的事，要自然记得并接上（历史对话会提供给你）。"
            "回复要像真实对话，有情绪、有回应，不要机械化列点。"
            "不要主动跳出角色，除非用户明确要求。"
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


# ---------------------------------------------------------------------------
# 角色卡解析 / 动态状态 / 按需加载
# ---------------------------------------------------------------------------
ROLE_SECTIONS = ["核心", "背景", "说话风格", "关系", "扩展",
                 "记忆", "当前情绪", "好感度"]


def parse_role_card(content: str) -> dict:
    """按 [段名] 解析角色卡，返回 {段名: 内容}。未知段名保留。"""
    sections = {}
    current = None
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            current = line[1:-1].strip()
            sections[current] = []
            continue
        if current:
            sections[current].append(raw)
    return {k: "\n".join(v).strip() for k, v in sections.items() if v}


def parse_state_block(text: str):
    """把 ---STATE--- 状态块从回复中拆出来。

    返回 (clean_text, state_dict)。找不到状态块时 state_dict 为空。
    """
    marker = "---STATE---"
    idx = text.find(marker)
    if idx == -1:
        return text, {}
    body = text[idx + len(marker):].strip()
    state = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key:
            state[key] = val
    clean = text[:idx].rstrip()
    return clean, state


def extract_load_requests(text: str):
    """找出 AI 请求加载的角色卡段，并从回复文本中移除 @load 指令。"""
    lines = []
    loads = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("@load:") or stripped.startswith("@load："):
            loads.append(stripped.split(":", 1)[1].strip())
            continue
        lines.append(line)
    return "\n".join(lines).strip(), loads


def build_role_system(card_content: str, state: dict, loaded=None) -> str:
    """生成角色扮演 system：
    - 始终带 [核心] 和动态状态
    - 其他静态段只给一句话摘要，需要时 AI 用 @load:段名 请求全文
    - 已加载段直接带全文
    """
    sections = parse_role_card(card_content)
    loaded = set(loaded or [])
    core = sections.get("核心", "")
    memory = state.get("记忆", "")
    emotion = state.get("当前情绪", "")
    affection = state.get("好感度", "")
    important = state.get("重要记忆", "")

    parts = [
        SKILL_BY_ID["roleplay"]["system"],
        "以下是角色卡信息：",
    ]
    if core:
        parts.append(f"[核心]\n{core}")
    if affection:
        parts.append(f"[好感度]\n{affection}")
    if emotion:
        parts.append(f"[当前情绪]\n{emotion}")
    if important:
        parts.append(f"[重要记忆]\n{important}")
    if memory:
        parts.append(f"[记忆摘要]\n{memory}")

    # 其他静态段：未加载时给摘要，已加载时给全文
    for section in ("背景", "说话风格", "关系", "扩展"):
        content = sections.get(section, "")
        if not content:
            continue
        if section in loaded:
            parts.append(f"[{section}] 全文：\n{content}")
        else:
            summary = content.splitlines()[0][:100] if content else ""
            parts.append(
                f"[{section}] 摘要：{summary}\n"
                f"如果你需要完整 {section} 细节，请输出一行 @load:{section}"
            )

    parts.append(
        "规则：\n"
        "- 保持角色人设，用口语化、短句、有情绪的回复，不要出现“作为AI/很高兴帮助你”等套话。\n"
        "- 如果用户说“一定要记得/记住/别忘了/很重要”，请把它写入状态块。\n"
        "- 如果回答需要某个段的完整细节，只输出 @load:段名，不要编造细节。\n"
        "- 回复末尾可以输出 ---STATE--- 状态块（格式：键=值，每行一个），"
        "用于更新 好感度/当前情绪/记忆/重要记忆；不需要更新时可不输出。"
    )
    return "\n\n".join(parts)
