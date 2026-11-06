#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐层铺铜 / 禁布区核查。

`pcb layout-lint` 和 `pcb drc` 都不读铺铜与禁布区（只做器件 bbox 相交、间距、
飞线交叉、可布性），这个脚本补上这块盲区。

用法：
    python pcb_copper_check.py --project P --doc D [--json]

三类判定：
  1. sensitive-under-copper  敏感器件（晶振/振荡器/天线/电感）的信号脚在某一层
     被**其它网络**的铺铜压住 —— 无源晶振的信号脚被对地铜皮包住会改变寄生电容，
     进而拉偏振荡频率。GND 脚下方有铜是正常的，不算问题。
  2. region-not-effective    禁布区声明禁铺铜，但该层对应位置仍有铜皮。
  3. region-layer-gap        某层有 no-pours 禁布区保护，另一层同位置却在铺铜且
     没有对应禁布区 —— 这是最常犯的疏漏（只给顶层加了禁布区，忘了底层）。

几何判定用 even-odd 填充规则（铺铜的复杂多边形里带挖空），
并用 CLI 自带 bbox 做独立校验，详见 eda_geom.py。

依赖：easyeda CLI 在 PATH 里，且 easyeda health 显示已连上 EasyEDA。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eda_geom import parse_contours, point_in_complex, bbox_of  # noqa: E402

LAYER_NAMES = {1: "Top Layer", 2: "Bottom Layer", 12: "Multi-Layer"}
MULTI_LAYER = 12

SENSITIVE_PREFIX = {
    "X": "crystal",      # 无源晶振
    "Y": "oscillator",   # 有源振荡器
    "L": "inductor",
    "E": "antenna",
}
SENSITIVE_FOOTPRINT_HINTS = {
    "CRYSTAL": "crystal", "XTAL": "crystal", "X3225": "crystal",
    "OSC": "oscillator", "ANT": "antenna", "IND": "inductor",
}
# 无源晶振对下方铜最敏感（两个信号脚的对地电容直接决定振荡频率），有源振荡器次之
SEVERITY = {"crystal": "ERROR", "antenna": "ERROR", "oscillator": "WARN", "inductor": "WARN"}

FALLBACK_CLI_DIRS = (
    os.path.join(os.path.expanduser("~"), ".easyeda-agent", "bin"),
    os.path.join(os.path.expanduser("~"), "easyeda-agent", "bin"),
)


def find_cli():
    """定位 easyeda 可执行文件。

    Git Bash / MSYS 下 PATH 里的 easyeda 可能是无扩展名 shim，Windows 的
    CreateProcess 认不出来，所以优先找 .exe。也支持 EASYEDA_CLI 显式指定。
    """
    env = os.environ.get("EASYEDA_CLI")
    if env and os.path.isfile(env):
        return env
    for name in ("easyeda.exe", "easyeda.cmd", "easyeda.bat"):
        found = shutil.which(name)
        if found:
            return found
    for d in FALLBACK_CLI_DIRS:
        for name in ("easyeda.exe", "easyeda.cmd", "easyeda.bat"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return cand
    found = shutil.which("easyeda")
    if found:
        return found
    raise RuntimeError(
        "找不到 easyeda CLI。请确认已安装 easyeda-agent 并加进 PATH，"
        "或设置环境变量 EASYEDA_CLI 指向可执行文件。")


def run_cli(args_list):
    """跑 easyeda 子命令，返回解析后的 JSON。中文输出编码不稳定，逐级回退。"""
    cmd = [find_cli()] + args_list
    proc = subprocess.run(cmd, capture_output=True)
    raw = proc.stdout
    if not raw.strip():
        raise RuntimeError("%s 没有输出。stderr=%r" % (" ".join(cmd), proc.stderr[:500]))
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    s = raw.decode("utf-8", errors="replace")
    i = s.find("{")
    if i >= 0:
        try:
            return json.loads(s[i:])
        except json.JSONDecodeError:
            pass
    raise RuntimeError("无法解析 %s 的输出（前 300 字节）：%r" % (" ".join(cmd), raw[:300]))


def classify(designator, device, footprint):
    """判断器件是否属于「对下方铺铜敏感」的类别。

    ⚠ 只靠位号前缀会误判：LED1 以 L 开头，会被当成电感。所以 L/E 前缀
      必须再要求 device/footprint 里有对应线索才认。
    """
    des = (designator or "").upper()
    texts = " ".join(str(x).upper() for x in (device, footprint) if x)

    # 明确的排除项先行
    if des.startswith("LED") or "LED" in texts:
        return None

    # 晶振/振荡器：位号前缀 X / Y 已经足够可靠，且有封装名佐证时优先
    if "CRYSTAL" in texts or "XTAL" in texts:
        return "crystal"
    if "OSC" in texts:
        return "oscillator"
    if des[:1] == "X":
        return "crystal"
    if des[:1] == "Y":
        return "oscillator"

    # 天线：必须有封装/型号线索
    if "ANT" in texts or "ANTENNA" in texts:
        return "antenna"

    # 电感：位号 L 前缀 + 封装/型号线索，缺一不认
    if des[:1] == "L" and any(h in texts for h in ("IND", "L_", "UH", "MH", "NH")):
        return "inductor"
    return None


def layer_cn(lay):
    return LAYER_NAMES.get(lay, "Layer %s" % lay)


def main():
    ap = argparse.ArgumentParser(description="逐层铺铜/禁布区核查")
    ap.add_argument("--project", required=True)
    ap.add_argument("--doc", required=True, help="PCB 文档 uuid")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    ap.add_argument("--arc-steps", type=int, default=32, help="圆弧插值段数，默认 32")
    args = ap.parse_args()

    p, d = args.project, args.doc

    # 一次 pcb dump 就够：器件（含焊盘）、真实铜皮、禁布区都在里面。
    # 不加 --out 是纯 stdout 模式，不落临时文件。
    dump = run_cli(["pcb", "dump", "--project", p, "--doc", d,
                    "--include-copper", "--no-silk"])
    if isinstance(dump.get("result"), dict):
        dump = dump["result"]
    if not dump:
        raise RuntimeError("pcb dump 没有返回数据")

    comps = dump.get("components") or []
    copper = dump.get("copper") or {}
    regions = copper.get("regions") or []
    poured = copper.get("poured") or []
    degraded = dump.get("partial") or []

    # 按层收集铜皮轮廓（even-odd 复合多边形）
    copper_by_layer = {}
    fills_by_layer = {}
    for blk in poured:
        lay = blk.get("layer")
        for f in blk.get("fills", []):
            cs = parse_contours(f.get("source"), args.arc_steps)
            if not cs:
                degraded.append("poured fill %s 无法解析几何" % f.get("id"))
                continue
            copper_by_layer.setdefault(lay, []).extend(cs)
            fills_by_layer.setdefault(lay, []).append({
                "id": f.get("id"), "net": blk.get("net"),
                "contours": cs, "bbox": bbox_of(cs),
            })

    layers = sorted(l for l in copper_by_layer if l != MULTI_LAYER)

    # 「读不到」和「确实没有」必须分开。dump 的 availability 会标明每类观测是否
    # 成功；空数组只有在 available 时才等价于「板上没有」。铺铜边界存在但没有
    # 实体化铜（没 rebuild）时，本脚本判不了，必须明确说出来而不是报干净。
    avail = copper.get("availability") or {}
    pours = copper.get("pours") or []
    unreadable = [k for k, v in avail.items() if v not in ("available", None)]
    not_materialized = bool(pours) and not poured

    def copper_at(pt, lay):
        """该点在该层是否是铜；是则返回该层铜皮所属网名（多网重叠时取第一个）。"""
        items = fills_by_layer.get(lay, [])
        if not items:
            return None
        for it in items:
            bb = it["bbox"]
            if not (bb["minX"] <= pt[0] <= bb["maxX"] and bb["minY"] <= pt[1] <= bb["maxY"]):
                continue
            if point_in_complex(pt, it["contours"]):
                return it["net"]
        # 复合多边形按层整体判定（even-odd 跨 fill 合并）
        if point_in_complex(pt, copper_by_layer[lay]):
            for it in items:
                if point_in_complex(pt, it["contours"]):
                    return it["net"]
        return None

    findings = []

    # ---------- 前置：无法判定的情况必须先报出来 ----------
    for k in unreadable:
        findings.append({
            "check": "copper-unreadable",
            "severity": "WARN",
            "field": k,
            "message": "铺铜观测项 %s 的可用性为 %s（不是 available），本项检查不完整" % (
                k, avail.get(k)),
        })
    if not_materialized:
        findings.append({
            "check": "copper-not-materialized",
            "severity": "WARN",
            "pours": len(pours),
            "message": "板上有 %d 个铺铜边界，但 poured-list 没有返回实体化铜 —— "
                       "多半是改过布局后没执行 `easyeda pcb pour-rebuild`。"
                       "此时无法判断铺铜是否压到敏感器件，不代表没有问题" % len(pours),
        })

    # ---------- 检查 1：敏感器件信号脚被其它网络的铺铜压住 ----------
    for c in comps:
        des = c.get("designator") or ""
        kind = classify(des, c.get("device"), c.get("footprint"))
        if not kind:
            continue
        pads = c.get("pads") or []
        if pads:
            for pad in pads:
                pad_net = (pad.get("net") or "").strip()
                pt = (pad.get("x"), pad.get("y"))
                if pt[0] is None or pt[1] is None:
                    continue
                for lay in layers:
                    net = copper_at(pt, lay)
                    if not net:
                        continue
                    if pad_net.upper().startswith("GND"):
                        continue          # GND 脚脚下的铜就是它自己，正常
                    if net == pad_net:
                        continue          # 同网铜皮是出线/散热，正常
                    findings.append({
                        "check": "sensitive-under-copper",
                        "severity": SEVERITY.get(kind, "WARN"),
                        "designator": des,
                        "kind": kind,
                        "pad": pad.get("padNumber"),
                        "pad_net": pad_net,
                        "copper_net": net,
                        "layer": lay,
                        "layer_name": layer_cn(lay),
                        "message": "%s（%s）的 %s 脚（%s）在 %s 下方被 %s 铜皮覆盖" % (
                            des, kind, pad.get("padNumber"), pad_net,
                            layer_cn(lay), net),
                    })
        else:
            bb = c.get("bbox")
            if not bb:
                continue
            pt = ((bb["minX"] + bb["maxX"]) / 2.0, (bb["minY"] + bb["maxY"]) / 2.0)
            for lay in layers:
                net = copper_at(pt, lay)
                if net:
                    findings.append({
                        "check": "sensitive-under-copper",
                        "severity": SEVERITY.get(kind, "WARN"),
                        "designator": des,
                        "kind": kind,
                        "copper_net": net,
                        "layer": lay,
                        "layer_name": layer_cn(lay),
                        "note": "焊盘坐标不可用，按器件本体中心判定",
                        "message": "%s（%s）本体中心在 %s 下方被 %s 铜皮覆盖" % (
                            des, kind, layer_cn(lay), net),
                    })

    # ---------- 检查 2：禁布区是否真的生效 ----------
    for r in regions:
        lay = r.get("layer")
        if lay == MULTI_LAYER:
            continue
        rules = [str(x) for x in (r.get("ruleTypeNames") or [])]
        if "no-pours" not in rules and "no-fills" not in rules:
            continue
        cs = parse_contours(r.get("source"), args.arc_steps)
        if not cs:
            continue
        bb = r["bbox"]
        pts = []
        for i in range(5):
            for j in range(5):
                pts.append((bb["minX"] + (bb["maxX"] - bb["minX"]) * i / 4.0,
                            bb["minY"] + (bb["maxY"] - bb["minY"]) * j / 4.0))
        pts = [q for q in pts if point_in_complex(q, cs)]
        if not pts:
            continue
        hit = sum(1 for q in pts if copper_at(q, lay))
        if hit:
            findings.append({
                "check": "region-not-effective",
                "severity": "ERROR",
                "region": r.get("primitiveId"),
                "rules": rules,
                "layer": lay,
                "layer_name": layer_cn(lay),
                "hit_samples": hit,
                "total_samples": len(pts),
                "message": "禁布区 %s（%s，%s）声明禁止铺铜，但 %d/%d 个采样点仍有铜皮" % (
                    r.get("primitiveId"), "/".join(rules),
                    layer_cn(lay), hit, len(pts)),
            })

    # ---------- 检查 3：禁布区只盖住了部分层 ----------
    # 只针对敏感器件所在区域报这一条。全板报会出几十条噪音（板上有 115 个
    # 禁布区，顶层有禁布区而底层照铺是常态）；但「给晶振加了顶层禁布区、
    # 忘了扩展到覆盖整机的底层铺铜」正是最容易犯且最难肉眼发现的错。
    no_pours = [r for r in regions
                if "no-pours" in [str(x) for x in (r.get("ruleTypeNames") or [])]]

    def region_covers(r, pt):
        bb = r.get("bbox")
        if not bb:
            return False
        if not (bb["minX"] <= pt[0] <= bb["maxX"] and bb["minY"] <= pt[1] <= bb["maxY"]):
            return False
        cs = parse_contours(r.get("source"), args.arc_steps)
        return point_in_complex(pt, cs) if cs else True

    def bbox_overlap(a, b):
        return not (a["maxX"] < b["minX"] or b["maxX"] < a["minX"]
                    or a["maxY"] < b["minY"] or b["maxY"] < a["minY"])

    sensitive_boxes = []
    for c in comps:
        des = c.get("designator") or ""
        kind = classify(des, c.get("device"), c.get("footprint"))
        if kind and c.get("bbox"):
            sensitive_boxes.append((des, kind, c["bbox"]))

    for r in no_pours:
        lay = r.get("layer")
        if lay == MULTI_LAYER:
            continue
        bb = r["bbox"]
        hosts = [s for s in sensitive_boxes if bbox_overlap(bb, s[2])]
        if not hosts:
            continue
        cx = (bb["minX"] + bb["maxX"]) / 2.0
        cy = (bb["minY"] + bb["maxY"]) / 2.0
        for other in layers:
            if other == lay:
                continue
            other_net = copper_at((cx, cy), other)
            if not other_net:
                continue
            if any(r2.get("layer") in (other, MULTI_LAYER) and region_covers(r2, (cx, cy))
                   for r2 in no_pours):
                continue
            host_txt = "、".join("%s(%s)" % (h[0], h[1]) for h in hosts[:3])
            findings.append({
                "check": "region-layer-gap",
                "severity": "WARN",
                "region": r.get("primitiveId"),
                "protected_layer": lay,
                "protected_layer_name": layer_cn(lay),
                "copper_layer": other,
                "copper_layer_name": layer_cn(other),
                "copper_net": other_net,
                "sensitive": host_txt,
                "at": {"x": round(cx, 2), "y": round(cy, 2)},
                "message": "%s 的 no-pours 禁布区 %s 保护着 %s，但 %s 同一位置仍有 %s "
                           "铺铜，且 %s 没有对应的 no-pours 禁布区 —— 禁布区只定义了部分层" % (
                               layer_cn(lay), r.get("primitiveId"), host_txt,
                               layer_cn(other), other_net, layer_cn(other)),
            })

    # ---------- 输出 ----------
    if args.json:
        print(json.dumps({"findings": findings,
                          "copper_layers": layers,
                          "degraded": degraded,
                          "note": "判定为点级二值（有铜/无铜），铺铜按 even-odd 复合多边形解析"},
                         ensure_ascii=False, indent=2))
        return 0

    if not findings:
        if not poured:
            print("板上没有实体化铺铜（poured 为空，availability=%s）。"
                  % (avail.get("poured") or "available"))
            print("这不是「检查通过」：没有铺铜可查，敏感器件下方铺铜一项无从判断。")
            return 0
        print("未发现铺铜/禁布区问题。")
        print("（检查了 %s 的铺铜，共 %d 个铜岛、%d 个禁布区）" % (
            "、".join(layer_cn(l) for l in layers), len(poured), len(regions)))
        return 0

    order = {"ERROR": 0, "WARN": 1, "COMMON": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["check"]))
    print("%-6s %-24s %s" % ("严重度", "检查项", "问题内容"))
    print("-" * 110)
    for f in findings:
        print("%-6s %-24s %s" % (f["severity"], f["check"], f["message"]))
    print()
    print("共 %d 项（ERROR %d / WARN %d）" % (
        len(findings),
        sum(1 for f in findings if f["severity"] == "ERROR"),
        sum(1 for f in findings if f["severity"] == "WARN")))
    if degraded:
        print("降级项 %d：%s" % (len(degraded), "; ".join(str(x) for x in degraded[:3])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
