#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck_geom.py — 几何解析器自检。全部通过才能相信铺铜/禁布区的审查结论。

三组独立校验：
  1. 每个禁布区 / 静态填充的解析 bbox 必须等于 CLI 自带的 bbox（真值来自平台，不是自己）
  2. 两个 180° ARC 拼成的圆，展开后所有顶点到圆心的半径偏差必须 < 1mil
  3. 可选 --ref：打印指定器件的每个焊盘在各层"有没有铜"（二值判定回归样本）

用法:
  python selfcheck_geom.py --project "工程名" --doc <pcb uuid>
  python selfcheck_geom.py --project "工程名" --doc <uuid> --ref X1
"""
import argparse
import io
import json
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eda_geom import parse_contours, parse_contour, bbox_of, point_in_complex  # noqa: E402

FALLBACK_CLI_DIRS = (
    os.path.join(os.path.expanduser("~"), ".easyeda-agent", "bin"),
    os.path.join(os.path.expanduser("~"), "easyeda-agent", "bin"),
)
TOL = 0.6  # mil


def find_cli():
    env = os.environ.get("EASYEDA_CLI")
    if env and os.path.isfile(env):
        return env
    for d in FALLBACK_CLI_DIRS:
        exe = os.path.join(d, "easyeda.exe" if os.name == "nt" else "easyeda")
        if os.path.isfile(exe):
            return exe
    return "easyeda"


def run_cli(args, timeout=240):
    proc = subprocess.run([find_cli()] + args, capture_output=True, timeout=timeout)
    raw = proc.stdout or b""
    if not raw.strip():
        raise RuntimeError("%s 没有输出: %s" % (
            " ".join(args[:3]), (proc.stderr or b"")[:400].decode("utf-8", "replace")))
    text = None
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    text = text if text is not None else raw.decode("utf-8", "replace")
    start = text.find("{")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj.get("result", obj)


def bbox_diff(got, bb):
    want = (bb["minX"], bb["maxX"], bb["minY"], bb["maxY"])
    return max(abs(got[k] - want[i])
               for i, k in enumerate(("minX", "maxX", "minY", "maxY")))


def main():
    ap = argparse.ArgumentParser(description="几何解析器自检")
    ap.add_argument("--project", required=True)
    ap.add_argument("--doc", required=True, help="PCB 文档 uuid")
    ap.add_argument("--ref", help="额外打印这颗器件每个焊盘的逐层铜判定")
    args = ap.parse_args()

    dump = run_cli(["pcb", "dump", "--project", args.project, "--doc", args.doc,
                    "--include-copper", "--no-silk"])
    copper = dump.get("copper") or {}
    fails = []

    # 校验 1：bbox 对照平台真值
    for label, items in (("禁布区", copper.get("regions") or []),
                         ("静态填充", copper.get("fills") or [])):
        n = ok = 0
        for it in items:
            contours = parse_contours(it.get("source"))
            bb = it.get("bbox")
            if not contours:
                fails.append(("%s解析为空" % label, it.get("primitiveId")))
                continue
            if not bb:
                continue
            n += 1
            diff = bbox_diff(bbox_of(contours), bb)
            if diff > TOL:
                fails.append(("%s bbox 不符" % label, it.get("primitiveId"), round(diff, 2)))
            else:
                ok += 1
        print("校验1 %s %d 个：bbox 吻合 %d，不符 %d" % (label, n, ok, n - ok))

    # 校验 2：双 ARC 圆是否真的是圆
    rings = bad = 0
    for blk in copper.get("poured") or []:
        for fill in blk.get("fills") or []:
            src = fill.get("source")
            segs = src if isinstance(src, list) and src and isinstance(src[0], list) else [src]
            for seg in segs:
                if not (isinstance(seg, list) and len(seg) == 10
                        and seg[2] == "ARC" and seg[6] == "ARC"):
                    continue
                rings += 1
                pts = parse_contour(seg)
                cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2.0
                cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2.0
                rs = [math.hypot(p[0] - cx, p[1] - cy) for p in pts]
                if max(rs) - min(rs) > 1.0:
                    bad += 1
                    fails.append(("双 ARC 非圆", fill.get("id"), round(max(rs) - min(rs), 2)))
    print("校验2 双 ARC 圆 %d 个：非圆 %d" % (rings, bad))

    # 校验 3：指定器件的焊盘逐层铜判定（回归样本）
    if args.ref:
        by_layer = {}
        for blk in copper.get("poured") or []:
            cs = []
            for fill in blk.get("fills") or []:
                cs.extend(parse_contours(fill.get("source")))
            by_layer.setdefault(blk.get("layer"), []).extend(cs)
        target = [c for c in dump.get("components") or []
                  if c.get("designator") == args.ref]
        if not target:
            print("校验3 找不到位号 %s" % args.ref)
        else:
            print("校验3 %s（%d 脚）逐层铜判定：" % (
                args.ref, len(target[0].get("pads") or [])))
            for pad in target[0].get("pads") or []:
                line = "   pad%-3s %-10s" % (pad.get("padNumber"), pad.get("net") or "-")
                for lay in sorted(l for l in by_layer if isinstance(l, int)):
                    hit = point_in_complex((pad["x"], pad["y"]), by_layer[lay])
                    line += "  layer%d=%s" % (lay, "铜" if hit else "无")
                print(line)

    for p in dump.get("partial") or []:
        print("降级项：%s" % (p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)))

    if fails:
        print("\n!! 失败 %d 项：" % len(fails))
        for f in fails[:20]:
            print("   ", f)
        return 1
    print("\n全部校验通过 —— 可以相信铺铜/禁布区的判定结果")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
