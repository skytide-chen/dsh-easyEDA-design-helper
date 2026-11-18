#!/usr/bin/env python3
"""环境自检：确认 easyeda CLI + daemon + 连接器都在位。

这是 skill 唯一允许的"探测"动作。只读 `easyeda health`，不启动、不重启、
不重装任何东西。判据不满足时按 SKILL.md 的约定输出报告块并退出非零，
把诊断交给用户判断 —— 不许自己想办法绕过。

用法:
    python scripts/env_check.py              # 现场跑 easyeda health
    python scripts/env_check.py --json        # 机器可读
    python scripts/env_check.py --from-file saved_health.json   # 调试：读已保存的 health 输出

退出码:
    0  环境就绪
    2  环境不足（daemon 未找到 / windows 为空 / capabilities 缺项 / 版本门禁不通过）
    4  找不到 easyeda CLI
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REQUIRED_CAPS = ("schematic.v1", "pcb.v1")

COMMON_CAUSES = (
    "EasyEDA 里没开「设置 → 扩展 → 已安装 → EDA Agent Connector → 允许外部交互」",
    "daemon 没启动，或启动后随终端/job 一起被关掉",
    "连接器版本与 CLI 版本不匹配",
    "当前打开的不是 EasyEDA 专业版 V4，或窗口被最小化 / 未激活",
    "端口被其他进程占用",
)


def find_cli():
    """复用共享实现，失败时返回 None 而不是抛错（本脚本要自己出报告）。"""
    try:
        from pcb_copper_check import find_cli as _find

        return _find()
    except Exception:  # noqa: BLE001 - CLI 缺失时由调用方给出可读报告
        return None


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_health(text: str):
    """health 的 stdout 在 JSON 前后可能夹人类可读文本，从第一个 { 起 raw_decode。"""
    start = text.find("{")
    if start < 0:
        raise ValueError("health 输出里没有 JSON")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    return data


def run_health(cli: str) -> str:
    proc = subprocess.run(
        [cli, "health"],
        capture_output=True,
        timeout=60,
    )
    return decode(proc.stdout) + "\n" + decode(proc.stderr)


def summarize(data: dict) -> dict:
    found = data.get("found") or {}
    raw = found.get("raw") or {}
    windows = raw.get("windows") or []
    gate = data.get("versionGate") or {}
    host = data.get("hostCompatibility") or {}

    problems = []
    status = data.get("status")
    if status != "found":
        problems.append(f"daemon 未找到（status={status or '未知'}）")
    if not windows:
        problems.append("没有 EasyEDA 窗口连上连接器（windows 为空）")

    win_rows = []
    for win in windows:
        caps = win.get("capabilities") or []
        missing = [c for c in REQUIRED_CAPS if c not in caps]
        if missing:
            problems.append(
                "窗口 %s 的 capabilities 缺 %s"
                % (str(win.get("windowId"))[:8], "/".join(missing))
            )
        if win.get("connectorVersionOk") is False:
            problems.append("窗口 %s 的连接器版本与 CLI 不匹配" % str(win.get("windowId"))[:8])
        win_rows.append(
            {
                "windowId": win.get("windowId"),
                "connectorVersion": win.get("connectorVersion"),
                "easyedaVersion": win.get("easyedaVersion"),
                "capabilities": caps,
                "connectorVersionOk": win.get("connectorVersionOk"),
                "pageVisibility": (win.get("context") or {}).get("pageVisibility"),
            }
        )

    if gate.get("verdict") not in (None, "ok"):
        problems.append("版本门禁 verdict=%s" % gate.get("verdict"))
    if host.get("verdict") not in (None, "ok"):
        problems.append("EasyEDA 宿主兼容性 verdict=%s" % host.get("verdict"))

    return {
        "ok": not problems,
        "problems": problems,
        "status": status,
        "port": found.get("port"),
        "pid": raw.get("pid"),
        "daemonVersion": raw.get("version"),
        "windows": win_rows,
        "versionGate": {
            "cli": gate.get("cli"),
            "daemon": gate.get("daemon"),
            "connectors": gate.get("connectors"),
            "verdict": gate.get("verdict"),
        },
        "hostCompatibility": {
            "baseline": host.get("baseline"),
            "recommended": host.get("recommended"),
            "verdict": host.get("verdict"),
        },
        "checks": data.get("checks") or [],
    }


def render_fail(summary: dict, cli: str | None, from_file: bool = False) -> str:
    cli_label = cli or ("未找到" if not from_file else "(来自 --from-file，未现场调用)")
    lines = [
        "环境自检未通过：连不上 easyeda 连接器，已停止，等待你的判断。",
        "",
        "- easyeda CLI：%s" % cli_label,
        "- daemon(127.0.0.1:60832)：%s" % (summary.get("status") or "未知"),
        "- EasyEDA 窗口(windows)：%s"
        % (
            ("%d 个" % len(summary["windows"]))
            if summary["windows"]
            else "空"
        ),
        "- capabilities：%s"
        % (
            "; ".join(
                "%s=%s" % (str(w["windowId"])[:8], ",".join(w["capabilities"]) or "无")
                for w in summary["windows"]
            )
            or "无（没有窗口）"
        ),
        "- 版本门禁：%s（cli=%s / daemon=%s）"
        % (
            summary["versionGate"]["verdict"] or "未知",
            summary["versionGate"]["cli"] or "?",
            summary["versionGate"]["daemon"] or "?",
        ),
    ]
    if summary["problems"]:
        lines.append("")
        lines.append("具体不满足的判据：")
        lines += ["%d. %s" % (i + 1, p) for i, p in enumerate(summary["problems"])]
    lines += ["", "常见原因（供你判断，我不自行处置）："]
    lines += ["%d. %s" % (i + 1, c) for i, c in enumerate(COMMON_CAUSES)]
    lines += [
        "",
        "请确认是哪种情况，或处理好后回我一句，我再重跑 easyeda health，通过后继续。",
    ]
    return "\n".join(lines)


def render_pass(summary: dict) -> str:
    win = summary["windows"][0]
    lines = [
        "环境自检通过：",
        "- daemon：pid %s，端口 %s，版本 %s"
        % (summary["pid"], summary["port"], summary["daemonVersion"]),
        "- 窗口：%d 个已连接（连接器 %s / EasyEDA %s / 可见性 %s）"
        % (
            len(summary["windows"]),
            win["connectorVersion"],
            win["easyedaVersion"],
            win["pageVisibility"],
        ),
        "- capabilities：%s" % ", ".join(win["capabilities"]),
        "- 版本门禁：%s；EasyEDA 宿主：%s"
        % (
            summary["versionGate"]["verdict"] or "未知",
            summary["hostCompatibility"]["verdict"] or "未知",
        ),
    ]
    for check in summary["checks"]:
        if check.get("status") not in ("ok", None):
            lines.append(
                "- 提示：%s — %s" % (check.get("name"), check.get("detail"))
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="easyeda 环境自检：连不上连接器就报错并停下，不自行处置"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--from-file",
        metavar="PATH",
        help="调试用：读取已保存的 health 输出，而不是现场调用 CLI",
    )
    args = parser.parse_args(argv)

    cli = None
    if args.from_file:
        with open(args.from_file, "rb") as handle:
            text = decode(handle.read())
    else:
        cli = find_cli()
        if not cli:
            payload = {
                "ok": False,
                "problems": ["找不到 easyeda CLI"],
                "cli": None,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    "\n".join(
                        [
                            "环境自检未通过：找不到 easyeda CLI，已停止，等待你的判断。",
                            "",
                            "常见原因（供你判断，我不自行处置）：",
                            "1. easyeda-agent 没安装，或没加进 PATH",
                            "2. 想用自定义位置，可设 EASYEDA_CLI（指向可执行文件）或 EASYEDA_HOME（安装根目录）",
                            "",
                            "请确认是哪种情况，或处理好后回我一句，我再重跑环境自检。",
                        ]
                    )
                )
            return 4
        try:
            text = run_health(cli)
        except Exception as exc:  # noqa: BLE001 - 调用失败同样属于环境不足
            print("环境自检未通过：调用 `easyeda health` 失败，已停止，等待你的判断。")
            print("")
            print("- CLI：%s" % cli)
            print("- 错误：%s" % exc)
            print("")
            print("常见原因（供你判断，我不自行处置）：")
            print("1. CLI 损坏或被杀软拦截")
            print("2. daemon 端口被占用 / 网络栈异常")
            print("")
            print("请确认原因或处理好后回我一句，我再重跑环境自检。")
            return 2

    try:
        data = parse_health(text)
    except ValueError as exc:
        if args.json:
            print(json.dumps({"ok": False, "problems": [str(exc)]}, ensure_ascii=False))
        else:
            print("环境自检未通过：health 输出无法解析，已停止，等待你的判断。")
            print("")
            print("- CLI：%s" % (cli or "(来自文件)"))
            print("- 原因：%s" % exc)
            print("- 原始输出片段：%s" % text.strip()[:300])
            print("")
            print("常见原因（供你判断，我不自行处置）：")
            print("1. CLI 与 daemon 版本差异导致输出格式变化")
            print("2. health 被其他输出污染")
            print("")
            print("请确认原因或处理好后回我一句，我再重跑环境自检。")
        return 2

    summary = summarize(data)
    summary["cli"] = cli

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif summary["ok"]:
        print(render_pass(summary))
    else:
        print(render_fail(summary, cli, from_file=bool(args.from_file)))

    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
