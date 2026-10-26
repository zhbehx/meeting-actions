#!/usr/bin/env python3
"""检查会议纪要：决策/行动项/待定三节是否齐全，行动项三要素是否完整。

行动项识别：Markdown 表格行（| 责任人 | 做什么 | 期限 |）或 `- 张三：做X，11/3 前` 形式。

用法：
    python scripts/check_actions.py note.md
    python scripts/check_actions.py note.md --strict
    python scripts/check_actions.py note.md --json

退出码：0 正常；--strict 下三要素不齐或缺少决策节时为 1；文件错误 2。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VAGUE_DEADLINE = ["尽快", "下周", "回头", "稍后", "看情况", "本周内", "TBD", "待定", "之后", "后续", "近期"]
VAGUE_VERB = ["跟进", "推进", "关注", "了解一下", "对接", "沟通", "协调", "同步一下", "看看", "支持"]
GROUP_OWNER = ["大家", "各位", "产品组", "技术组", "相关同学", "所有人", "全员", "团队", "我们"]
DATE_RE = re.compile(r"\d{1,2}\s*[/月.-]\s*\d{1,2}|\d{1,2}月\d{1,2}[日号]?|周[一二三四五六日天]|Q[1-4]|本周[一二三四五六日天]|下月")
SECTION_DECISION = re.compile(r"^#+\s*.*决策|^#+\s*决定")
SECTION_ACTION = re.compile(r"^#+\s*.*行动|^#+\s*.*待办|^#+\s*.*to-?do", re.I)
SECTION_OPEN = re.compile(r"^#+\s*.*待定|^#+\s*.*未定|^#+\s*.*open")

ONE_SCREEN_LINES = 45


def parse_actions(lines: list[str]) -> list[dict]:
    """从表格行与 `- 姓名：内容，日期` 行里提取行动项。"""
    actions = []
    section = "other"  # action / open / other
    header_cols: list[str] = []

    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if SECTION_OPEN.match(line):
                section = "open"
            elif SECTION_ACTION.match(line):
                section = "action"
            else:
                section = "other"
            header_cols = []
            continue

        # 「待定」节下的条目不是行动项，不参与三要素检查
        if section == "open":
            continue
        in_action_section = section == "action"

        # Markdown 表格
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if not header_cols:
                header_cols = cells
                continue
            row = dict(zip(header_cols, cells))

            def pick(*keys: str) -> str:
                """表头允许带括号说明，如「做什么（含产物）」，用包含匹配。"""
                for k in keys:
                    for hk, val in row.items():
                        if k in hk:
                            return val
                return ""

            owner = pick("责任人", "负责", "Owner", "owner")
            what = pick("做什么", "事项", "内容", "动作", "任务")
            when = pick("期限", "时间", "Deadline", "deadline", "何时")
            if owner or what or when:
                actions.append({"line": i, "owner": owner, "what": what, "when": when,
                                "raw": line[:100], "source": "table"})
            continue

        # 列表式行动项：- 张三：做X，11/3 前
        m = re.match(r"^[-*•·]\s*(?:\[[ xX]\]\s*)?([^：:\s]{1,10})[：:]\s*(.+)$", line)
        if m and (in_action_section or DATE_RE.search(line) or "前" in line):
            owner, body = m.group(1), m.group(2)
            dm = DATE_RE.search(body)
            actions.append({
                "line": i,
                "owner": owner,
                "what": body[:80],
                "when": dm.group(0) if dm else "",
                "raw": line[:100],
                "source": "list",
            })
            continue

        # 没有责任人的裸条目：- 跟进下小客户的问题（缺责任人也算行动项，要报出来）
        if line.startswith(("-", "*", "•", "·")) and not line.startswith("---"):
            body = re.sub(r"^[-*•·]\s*", "", line).strip()
            if body and (in_action_section or any(v in body for v in VAGUE_VERB) or DATE_RE.search(body)):
                dm = DATE_RE.search(body)
                actions.append({
                    "line": i,
                    "owner": "",
                    "what": body[:80],
                    "when": dm.group(0) if dm else "",
                    "raw": line[:100],
                    "source": "bare",
                })

    return actions


def analyze(text: str) -> dict:
    lines = text.splitlines()
    nonempty = [l for l in lines if l.strip()]

    has_decision = any(SECTION_DECISION.match(l.strip()) for l in lines)
    has_open = any(SECTION_OPEN.match(l.strip()) for l in lines)
    has_action_sec = any(SECTION_ACTION.match(l.strip()) for l in lines)

    actions = parse_actions(lines)
    problems = []
    for a in actions:
        miss = []
        if not a["owner"] or any(g in a["owner"] for g in GROUP_OWNER):
            miss.append("责任人")
        if not a["what"] or any(v in a["what"] for v in VAGUE_VERB) and len(a["what"]) < 14:
            miss.append("可验收产物")
        if not a["when"] or any(v in a["when"] for v in VAGUE_DEADLINE):
            miss.append("具体期限")
        if miss:
            problems.append({"line": a["line"], "missing": miss, "raw": a["raw"]})

    unclaimed = [a["raw"] for a in actions if "待认领" in a["raw"]]

    critical = []
    if not has_decision:
        critical.append("没有「决策」节（会上定了什么，必须写出来）")
    if not actions:
        critical.append("没有识别到行动项（格式：表格 责任人/做什么/期限，或 `- 张三：做X，11/3 前`）")
    if problems:
        critical.append(f"{len(problems)} 条行动项三要素不全")

    score = 100
    score -= 25 if not has_decision else 0
    score -= 20 if not has_action_sec else 0
    score -= min(30, len(problems) * 10)
    score -= 10 if not has_open else 0
    score -= 5 if len(nonempty) > ONE_SCREEN_LINES else 0
    score = max(0, score)

    return {
        "has_decision_section": has_decision,
        "has_action_section": has_action_sec,
        "has_open_section": has_open,
        "action_count": len(actions),
        "incomplete_actions": problems,
        "unclaimed": unclaimed,
        "nonempty_lines": len(nonempty),
        "too_long": len(nonempty) > ONE_SCREEN_LINES,
        "critical": critical,
        "score": score,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="检查会议纪要质量")
    ap.add_argument("file", help="纪要文件")
    ap.add_argument("--strict", action="store_true", help="有严重问题时退出码 1")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    p = Path(args.file)
    if not p.is_file():
        print(f"找不到文件：{p}", file=sys.stderr)
        return 2

    r = analyze(p.read_text(encoding="utf-8-sig"))

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(f"决策节    ：{'有' if r['has_decision_section'] else '缺'}")
        print(f"行动项节  ：{'有' if r['has_action_section'] else '缺'}")
        print(f"待定节    ：{'有' if r['has_open_section'] else '缺（会上没定的事也要写）'}")
        print(f"行动项    ：{r['action_count']} 条（三要素不全 {len(r['incomplete_actions'])} 条）")
        print(f"篇幅      ：{r['nonempty_lines']} 行{'（超过一屏）' if r['too_long'] else ''}")
        print(f"评分      ：{r['score']}/100")

        if r["critical"]:
            print("\n必须改：")
            for c in r["critical"]:
                print(f"  - {c}")
        if r["incomplete_actions"]:
            print("\n三要素不全的行动项（责任人 = 一个人 / 产物 = 可验收 / 期限 = 具体日期）：")
            for x in r["incomplete_actions"]:
                print(f"  第 {x['line']} 行 缺：{'、'.join(x['missing'])}")
                print(f"    {x['raw']}")
        if r["unclaimed"]:
            print("\n待认领（发出时点名确认）：")
            for u in r["unclaimed"]:
                print(f"  - {u}")

    if args.strict and r["critical"]:
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1:]))
