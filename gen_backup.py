#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「缩进大纲」转换成「每日任务」App 可导入的备份 JSON。

用法：
    python gen_backup.py 大纲.txt -o 任务备份.json

大纲格式（缩进 = 层级，每个缩进单位可以是 2/4 个空格或 Tab，全用空格也行）：
    毕业论文
      文献综述
      方法论
        实验设计
        数据采集 | 2026-08-15
      论文撰写 | 2026-08-31 18:00

规则：
  - 每行一个任务，行首缩进决定它在树里的层级；
  - 可选的「 | 截止时间」跟在标题后，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM；
  - 空行、以 # 开头的行会被忽略；
  - 生成的 JSON 与 App 的「导出备份」格式一致，可直接在手机
    设置 → 导入备份 里还原（注意：导入会替换手机上的现有任务）。

也可配合 AI：把项目需求告诉 AI，让它输出上面的缩进大纲，再用本脚本生成文件。
"""
import argparse
import datetime as dt
import json
import sys


def parse_outline(text):
    """解析缩进大纲 → [(层级, 标题, 截止时间)]"""
    items = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        body = stripped
        title, deadline = body, ""
        if "|" in body:
            title, _, dl = body.partition("|")
            title, deadline = title.strip(), dl.strip()
        if not title:
            continue
        items.append((indent, title, deadline))
    return items


def build_tree(items):
    """按缩进层级生成 items 数组（id / parent_id 树）。"""
    nodes = []
    stack = []            # [(层级, id), ...]
    next_id = 1
    for level, title, deadline in items:
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        nodes.append({
            "id": next_id,
            "parent_id": parent_id,
            "title": title,
            "deadline": deadline,
            "done": 0,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        stack.append((level, next_id))
        next_id += 1
    return nodes


def main():
    ap = argparse.ArgumentParser(description="缩进大纲 → 每日任务备份 JSON")
    ap.add_argument("outline", help="缩进大纲文本文件路径")
    ap.add_argument("-o", "--output", default="backup_tasks.json",
                    help="输出 JSON 路径（默认 backup_tasks.json）")
    args = ap.parse_args()

    try:
        text = open(args.outline, encoding="utf-8").read()
    except OSError as e:
        sys.exit(f"无法读取大纲文件：{e}")

    items = parse_outline(text)
    if not items:
        sys.exit("大纲为空：没解析到任何任务行")

    data = {
        "app": "daily_tasks",
        "version": 1,
        "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
        "items": build_tree(items),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已生成 {args.output}：共 {len(data['items'])} 个任务（含子任务）")


if __name__ == "__main__":
    main()
