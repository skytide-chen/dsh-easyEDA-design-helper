#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""placement_brief.py — 功能二的取证步骤：把原理图的连接关系整理成「谁围着谁转」。

不给坐标、不替用户摆位；只输出可复核的事实：
  - 每颗器件的引脚数、跨页后的网络邻居（共享 net 越多越该靠近）
  - 候选主元件（引脚多 + 邻居多），以及机械约束器件（连接器/按键/指示灯/继电器）
  - PCB 现状（有没有摆、有没有画线）

用法:
  python placement_brief.py --project "工程名"                 # 全局摘要
  python placement_brief.py --project "工程名" --ref U1        # 看 U1 的邻居，给布局建议用
  python placement_brief.py --project "工程名" --json
"""
import argparse
import collections
import io
import json
import os
import subprocess
import sys

FALLBACK_CLI_DIRS = (
    os.path.join(os.path.expanduser("~"), ".easyeda-agent", "bin"),
    os.path.join(os.path.expanduser("~"), "easyeda-agent", "bin"),
)

MECH_PREFIX = ("CN", "J", "SW", "KEY", "LED", "K", "RL", "BT", "USB")
MECH_HINTS = ("connector", "header", "switch", "button", "relay", "led")


def find_cli():
    env = os.environ.get("EASYEDA_CLI")
    if env and os.path.isfile(env):
        return env
    for d in FALLBACK_CLI_DIRS:
        exe = os.path.join(d, "easyeda.exe" if os.name == "nt" else "easyeda")
        if os.path.isfile(exe):
            return exe
    return "easyeda"


CLI = find_cli()


def run_cli(args, timeout=180):
    proc = subprocess.run([CLI] + args, capture_output=True, timeout=timeout, shell=False)
    raw = proc.stdout or b""
    if not raw.strip():
        err = (proc.stderr or b"")[:600].decode("utf-8", "replace")
        raise RuntimeError("%s 没有输出。stderr=%s" % (" ".join(args[:3]), err))
    text = None
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
    start = text.find("{")
    if start < 0:
        raise RuntimeError("输出里没有 JSON: %r" % text[:200])
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(text[start:])
    return obj.get("result", obj)


def list_pages(project):
    data = run_cli(["doc", "ls", "--project", project, "--json"])
    docs = data.get("documents") or data.get("docs") or []
    return [d for d in docs if (d.get("type") or "").lower() == "schematic"]


def read_page(project, page_uuid):
    return run_cli(["sch", "read", "--project", project, "--page", page_uuid,
                    "--no-check"], timeout=240)


def collect(project):
    pages = list_pages(project)
    comps = {}
    net_members = collections.defaultdict(set)
    net_pages = collections.defaultdict(set)
    for p in pages:
        uuid = p["uuid"]
        data = read_page(project, uuid)
        for c in data.get("components") or []:
            if c.get("componentType") != "part":
                continue
            ref = c.get("designator")
            if not ref:
                continue
            entry = comps.setdefault(ref, {
                "ref": ref, "value": None, "device": None, "footprint": None,
                "pins": 0, "nets": set(), "pages": set(),
            })
            entry["pins"] = max(entry["pins"], len(c.get("pins") or []))
            dev = c.get("manufacturerId") or c.get("device") or c.get("name")
            if isinstance(dev, str) and not dev.startswith("="):
                entry["device"] = entry["device"] or dev
            fp = c.get("footprint")
            if isinstance(fp, dict):
                entry["footprint"] = entry["footprint"] or fp.get("name")
            entry["pages"].add(p.get("name") or uuid)
            for pin in c.get("pins") or []:
                net = (pin.get("net") or "").strip()
                if not net:
                    continue
                entry["nets"].add(net)
                net_members[net].add(ref)
                net_pages[net].add(p.get("name") or uuid)
    # 邻居强度：共享 net 数（跨页已合并）
    # 电源/地这类挂了几十颗器件的 net 不算"该靠近"的信号，剔除（阈值 12）
    neighbors = collections.defaultdict(collections.Counter)
    pair_nets = collections.defaultdict(set)
    for net, refs in net_members.items():
        if len(refs) < 2 or len(refs) > 12:
            continue
        for a in refs:
            for b in refs:
                if a != b:
                    neighbors[a][b] += 1
                    pair_nets[(a, b)].add(net)
    return pages, comps, net_members, neighbors, pair_nets


def is_mech(ref, comp):
    up = ref.upper()
    if any(up.startswith(p) for p in MECH_PREFIX):
        return True
    blob = ("%s %s" % (comp.get("footprint") or "", comp.get("device") or "")).lower()
    return any(h in blob for h in MECH_HINTS)


def pcb_state(project):
    try:
        data = run_cli(["doc", "ls", "--project", project, "--json"])
        docs = data.get("documents") or data.get("docs") or []
        pcbs = [d for d in docs if (d.get("type") or "").lower() == "pcb"]
    except RuntimeError:
        return None
    out = []
    for d in pcbs:
        try:
            comps = run_cli(["pcb", "list", "--project", project, "--doc", d["uuid"],
                             "--include-bbox"], timeout=240)
            tracks = run_cli(["pcb", "track-list", "--project", project, "--doc", d["uuid"]],
                             timeout=240)
            out.append({"name": d.get("name"), "uuid": d["uuid"],
                        "components": len(comps.get("components") or []),
                        "tracks": tracks.get("count", 0)})
        except RuntimeError as exc:
            out.append({"name": d.get("name"), "uuid": d["uuid"], "error": str(exc)[:120]})
    return out


def main():
    ap = argparse.ArgumentParser(description="PCB 布局建议的取证摘要（不给坐标）")
    ap.add_argument("--project", required=True)
    ap.add_argument("--ref", help="重点看这颗器件的邻居")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    pages, comps, net_members, neighbors, pair_nets = collect(args.project)
    dup = collections.Counter(p.get("name") for p in pages)
    dup = {k: v for k, v in dup.items() if v > 1}

    ranked = sorted(comps.values(),
                    key=lambda c: (len(neighbors.get(c["ref"], {})), c["pins"]),
                    reverse=True)
    mech = [c for c in comps.values() if is_mech(c["ref"], c)]
    pcb = pcb_state(args.project)

    if args.json:
        payload = {
            "pages": [{"name": p.get("name"), "uuid": p["uuid"]} for p in pages],
            "duplicate_page_names": dup,
            "components": [
                {"ref": c["ref"], "pins": c["pins"], "nets": len(c["nets"]),
                 "neighbors": len(neighbors.get(c["ref"], {})),
                 "device": c.get("device"), "footprint": c.get("footprint"),
                 "mechanical": is_mech(c["ref"], c)}
                for c in sorted(comps.values(), key=lambda x: x["ref"])
            ],
            "candidates_main": [
                {"ref": c["ref"], "pins": c["pins"],
                 "neighbors": len(neighbors.get(c["ref"], {})),
                 "device": c.get("device")}
                for c in ranked[:5]
            ],
            "neighborhood": (
                {args.ref: neighbors.get(args.ref, {}).most_common()} if args.ref else None
            ),
            "pcb": pcb,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("## 工程概况")
    print("- 原理图页 %d 个：%s" % (
        len(pages), "、".join("%s(%s)" % (p.get("name"), p["uuid"][:8]) for p in pages)))
    if dup:
        print("- ⚠ 同名页：%s —— 多半是副本，统计已按 ref 合并，但确认主元件时留意"
              % "、".join("%s×%d" % kv for kv in dup.items()))
    parts = [c for c in comps.values()]
    print("- 器件 %d 颗（part 类型）" % len(parts))
    if pcb:
        print("\n## PCB 现状")
        for p in pcb:
            if "error" in p:
                print("- %s：读不到（%s）" % (p["name"], p["error"]))
            else:
                stage = "还没摆器件" if p["components"] == 0 else (
                    "摆了没画线" if p["tracks"] == 0 else "已有走线")
                print("- %s：%d 颗器件 / %d 条走线 → %s"
                      % (p["name"], p["components"], p["tracks"], stage))

    print("\n## 候选主元件（按「邻居数 + 引脚数」排）")
    for c in ranked[:5]:
        print("- %s：%d 脚 / %d 个邻居网络%s" % (
            c["ref"], c["pins"], len(neighbors.get(c["ref"], {})),
            "（%s）" % c["device"] if c.get("device") else ""))

    print("\n## 机械约束器件（位置由外壳决定，优先定死）")
    if mech:
        for c in sorted(mech, key=lambda x: x["ref"]):
            print("- %s%s" % (c["ref"], "（%s）" % c["device"] if c.get("device") else ""))
    else:
        print("- 没识别到连接器/按键/指示灯类器件")

    if args.ref:
        ref = args.ref
        if ref not in comps:
            print("\n!! 找不到位号 %s" % ref)
            return 2
        print("\n## %s 的连接强度（共享 net 数 → 该靠多近）" % ref)
        nb = neighbors.get(ref, {})
        if not nb:
            print("- 没有强关联邻居（只连电源/地或独立网络）")
        for other, n in nb.most_common(15):
            shared = sorted(pair_nets.get((ref, other), ()))
            print("- %s：共享 %d 个信号 net（%s）%s" % (
                other, n, "、".join(shared[:6]),
                " — 电源/地已剔除" if n < len(shared) else ""))
        own = sorted(n for n in comps[ref]["nets"])
        print("\n%s 自己的网络：%s" % (ref, "、".join(own[:20]) or "无"))
        print("\n> 摆放与走线原则见 references/placement.md；**不给坐标**。")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
