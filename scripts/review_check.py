#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原理图审查：把 `sch check` 的原始 findings 归一化成五列表格。

`sch check` 直接输出的东西不能拿来当审查报告，原因有四个，全部实测确认过：

  1. 字段名和层级不统一：`level` 大小写混用（ERROR / error / warn 同时出现），
     各条规则带的定位字段差异极大 —— marker-overlap 带 primitiveId+bbox+重叠面积，
     wire-contact 只有 nets 和 message，没有任何定位信息。
  2. 会重复打标：一条零长度导线同时以 ERROR 和 WARN 各报一次（本工程 34 条
     finding 实际只有 17 处问题）。
  3. 有已知误报规则：wire-contact 只看单根导线是否承载多个网络名，实测同一页
     连跑两次有一半网络对会变，必须用 `sch bridge-check`（按共享顶点建并查集）
     做交叉验证；bridge-check 说 0 short 时，wire-contact 不能当短路报。
  4. 没有"哪一层"和"哪个元件哪个引脚"这两个用户要的列，需要从各规则的
     异构字段里抽。

所以本脚本做三件事：**去重 → 交叉验证 → 填出五列**。

用法：
    python review_check.py --project "烘干机主板" --page <uuid> [--page <uuid> ...]
    python review_check.py --project "烘干机主板"          # 自动取所有原理图页
    python review_check.py --project P --page U --json     # 机读

输出列：编号 | 严重度 | 问题内容 | 问题网络 | 所在层
（原理图没有层概念，所在层固定为 "—"；PCB 侧用 pcb_copper_check.py 和 layout-lint）
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pcb_copper_check import find_cli, run_cli  # noqa: E402

# ---------------------------------------------------------------- 规则知识表
# severity : 本 skill 的定档（不是照抄工具的 level，见下方注释）
# meaning  : 问题内容列的措辞（中文标准表述），保持前后一致
# evidence : 该规则能不能单独下结论。False 表示必须交叉验证，否则只能报"待复核"
RULES = {
    "pin-exit-direction": dict(
        severity="COMMON", meaning="引脚引出方向不符规范",
        evidence=True,
        note="工具标 ERROR，但实测 78 条里绝大多数是极性判断误判，按整洁度处理"),
    "titleblock-overlap": dict(
        severity="COMMON", meaning="图元压图签", evidence=True, note=""),
    "reversed-net-flag": dict(
        severity="WARN", meaning="网络标志方向反了", evidence=True,
        note="方向反不影响网络连接（逻辑等价），但影响可读性"),
    "zero-length-wire": dict(
        severity="WARN", meaning="零长度导线（首尾坐标相同，不连接任何东西）",
        evidence=True, note="工具会对同一根线打两遍标（ERROR+WARN），已去重"),
    "wire-contact": dict(
        severity="COMMON", meaning="异网导线存在端点/T 接/共线重叠的真实电气接触",
        evidence=False,
        note="规则无任何定位字段，实测两次运行结果会变；须由 bridge-check 裁判"),
    "marker-overlap": dict(
        severity="COMMON", meaning="网络标记重合", evidence=True, note=""),
    "designator-overlap": dict(
        severity="COMMON", meaning="位号文字重叠", evidence=True, note=""),
    "designator-wire-overlap": dict(
        severity="COMMON", meaning="位号压导线", evidence=True, note=""),
    "wire-through-body": dict(
        severity="ERROR", meaning="导线穿过器件本体", evidence=True,
        note="带 designator+primitiveId+at+bbox，可定位，能直接下结论"),
    "missing-partition": dict(
        severity="WARN", meaning="缺少功能分区框", evidence=True,
        note="交付规范要求，不影响电气"),
    "missing-titleblock": dict(
        severity="WARN", meaning="图签没填（图纸标题/设计者/说明）", evidence=True,
        note="交付规范要求，不影响电气"),
    "floating-pin": dict(
        severity="WARN", meaning="引脚悬空（无导线连接，也没打 NC 标识）",
        evidence=True,
        note="未使用的引脚应显式打 NC，否则后续 ERC 和装配判断都会漏"),
    "polarity-convention-outlier": dict(
        severity="WARN",
        meaning="极性脚约定与同页多数不一致（电源侧接到 pin 1，同页多数是 pin 2）",
        evidence=True,
        note="钽电容/电解电容反接有热失控风险，务必核对；MLCC 无极性可忽略"),
    "out-of-sheet": dict(
        severity="WARN", meaning="可见属性超出图纸边界（打印时会缺）",
        evidence=True, note=""),
}

DEFAULT_RULE = dict(severity="WARN", meaning=None, evidence=True,
                    note="未登记的新规则，按工具 level 折算")

ORDER = {"ERROR": 0, "WARN": 1, "COMMON": 2}

# 同类 WARN 超过这个条数就合并成一行（见下方 group()）
GROUP_MIN = 6


def layer_norm(level):
    """工具 level 大小写混用，统一成 ERROR / WARN。"""
    return "ERROR" if str(level or "").strip().upper() == "ERROR" else "WARN"


def pick(obj, *keys):
    """从异构 finding 里取第一个非空值。"""
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def ref_of(d):
    """把 designator + pins 拼成 'U1.27' / 'U1.27,U1.28' 这种形式。"""
    if not d:
        return None
    if isinstance(d, str):
        return d
    des = d.get("designator")
    pins = d.get("pins") or d.get("pin") or d.get("padNumber")
    if des and pins:
        if isinstance(pins, (list, tuple)):
            return ",".join("%s.%s" % (des, p) for p in pins)
        return "%s.%s" % (des, pins)
    return des or None


def problem_net(f):
    """填「问题网络」列：网络名 + 哪个元件哪个引脚。

    各规则字段不同，按可用信息从强到弱拼：
      有 net 有元件 → 'NET (U1.27)'
      只有 net      → 'NET'（并注明无定位，审查者要自己找）
      只有元件      → 'U1.27（未给网络名）'
    """
    nets = f.get("nets") or pick(f, "net", "markerNet")
    if isinstance(nets, str):
        nets = [nets]
    nets = [n for n in (nets or []) if n]

    refs = []
    for key in ("designator", "refs", "components"):
        v = f.get(key)
        if isinstance(v, list):
            refs += [ref_of(x) for x in v]
        elif v:
            # floating-pin 这类规则：designator 是字符串，引脚号单独放在 pins 里
            r = ref_of({"designator": v, "pins": f.get("pins")}
                       if isinstance(v, str) and f.get("pins") else v)
            if r:
                refs.append(r)
    other = f.get("other")
    if isinstance(other, dict) and other.get("designator"):
        refs.append(other["designator"])
    refs = [r for r in refs if r]

    if len(refs) > 6:
        refs = refs[:6] + ["…等 %d 个" % len(refs)]
    if nets and refs:
        return "%s (%s)" % ("、".join(nets), "、".join(refs))
    if nets:
        return "、".join(nets)
    if refs:
        at = f.get("at") or {}
        if at.get("x") is not None:
            return "%s（本规则未给网络名；位置 %g,%g）" % (
                "、".join(refs), at["x"], at["y"])
        return "%s（本规则未给网络名）" % "、".join(refs)
    at = f.get("at") or {}
    if at.get("x") is not None:
        return "位置 %g,%g（本规则未给网络名）" % (at["x"], at["y"])
    return "—（本规则无定位字段）"


def cross_page_nets(project, pages):
    """合并所有页的「引脚-网络」关系，返回 {net: [designator.pin, ...]}。

    为什么必须合并：单页读的时候，跨页信号只在本页露一个引脚，看起来像
    degree=1 的漏画。之前就因为只看单页，把 CYCLE_FAN_MOS / MOTOR_DRIVE1 等
    判成漏画，切到「强电」页才发现接的是 R23.1 / U22.3。
    只有把所有页合并后统计引脚数，degree 才可信。
    """
    net_pins = {}
    for page in pages:
        res = run_cli(["sch", "read", "--project", project,
                       "--page", page, "--no-check"])
        r = res.get("result") or res
        for c in r.get("components") or []:
            if c.get("componentType") not in (None, "part"):
                continue
            for pin in c.get("pins") or []:
                net = pin.get("net")
                if not net:
                    continue
                ref = "%s.%s" % (c.get("designator"), pin.get("number"))
                bucket = net_pins.setdefault(net, [])
                # 工程里常有同名页的副本（本工程"单片机核心及输入"就有 3 份），
                # 不按 ref 去重的话同一引脚会被数 3 次，degree 虚高。
                if ref not in bucket:
                    bucket.append(ref)
    return net_pins


def dedupe(findings):
    """同一处问题被重复报时只留一条。

    实测的重复打标有两种形态，共同点是 **message 不同、定位信息相同**：
      · 零长度导线：同一根线（同 wirePrimitiveId、同 at）先以 ERROR 报
        "wire contains a zero-length segment"，再以 warn 报中文
        "零长度导线(首尾坐标相同...)"，34 条 finding 实际只有 17 处问题。
      · 位号重叠：同一个 primitiveId 被报了两次以上。
    所以指纹只用定位信息、不含 message。

    反过来，wire-contact 这类**没有任何定位字段**的规则，两条完全一样的记录
    代表两处真实接触点，不能折叠 —— 没有 id 和坐标时保留全部。
    """
    seen = {}
    out = []
    for f in findings:
        at = f.get("at") or {}
        pid = pick(f, "primitiveId", "wirePrimitiveId")
        des = pick(f, "designator")
        nets = tuple(sorted(f.get("nets") or []))
        loc = (round(at.get("x", 0), 1), round(at.get("y", 0), 1)) if at else None
        fp = (f.get("type"), pid, loc, des, nets)
        discriminating = bool(pid or loc or des or nets)
        if discriminating and fp in seen:
            seen[fp]["_dups"] = seen[fp].get("_dups", 1) + 1
            continue
        if discriminating:
            seen[fp] = f
        out.append(f)
    return out


def bridge_check(project, page):
    """跑权威的导线树检测，返回 (summary, trees)。

    ⚠ 这个子命令的定位参数是 `--doc`，不是 `--page`（跟 `sch check` 不一样），
      传 --page 会直接报 `unknown flag: --page`。
    bridge-check 用并查集把一页的导线按共享顶点聚成导线树：
      · 一棵树上有多个网络名 → BRIDGE，真实短路
      · 树不挂任何网络名但碰到器件引脚 → ORPHAN_TREE，悬空支路
      · 网络标记没落在任何导线上 → ORPHAN_FLAG
    """
    res = run_cli(["sch", "bridge-check", "--project", project,
                   "--doc", page, "--json"])
    r = res.get("result") or res
    summary = r.get("summary") or {}
    trees = r.get("trees") or []
    return summary, trees


def main():
    ap = argparse.ArgumentParser(description="原理图审查五列归一化")
    ap.add_argument("--project", required=True)
    ap.add_argument("--page", action="append", default=[],
                    help="原理图页 uuid，可重复；不给则自动取全部原理图页")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON")
    ap.add_argument("--all-common", action="store_true",
                    help="COMMON 条目逐条列出（默认只按规则类型汇总计数）")
    ap.add_argument("--no-cross-page-merge", action="store_true",
                    help="跨页统计只合并 --page 指定的页（默认合并工程内所有原理图页）")
    ap.add_argument("--no-cross-page", action="store_true",
                    help="跳过跨页网络完整性检查（默认开启：合并所有页统计引脚数）")
    ap.add_argument("--all-warn", action="store_true",
                    help="WARN 条目逐条列出（默认同类超过 6 条合并成一行）")
    args = ap.parse_args()

    pages = args.page
    if not pages:
        docs = run_cli(["doc", "ls", "--project", args.project, "--json"])
        items = (docs.get("result") or docs).get("documents") or []
        pages = [d["uuid"] for d in items if d.get("type") == "schematic"]
        if not pages:
            print("找不到原理图页，请用 --page 显式指定 uuid。", file=sys.stderr)
            return 2
        sch = [d for d in items if d.get("type") == "schematic"]
        print("自动发现 %d 个原理图页：%s\n" % (
            len(pages), "、".join("%s(%s)" % (d.get("name"), d["uuid"]) for d in sch)))
        names = {}
        for d in sch:
            names.setdefault(d.get("name"), []).append(d["uuid"])
        dupes = {n: v for n, v in names.items() if len(v) > 1}
        if dupes:
            print("⚠ 检测到同名页 %s —— 多半是同一页的多份副本（旧版本/备份）。"
                  "本脚本会把内容相同的发现合并成一条，但若要审的只是当前版本，"
                  "请用 --page 指定真正在用的 uuid。\n" % (
                      "、".join("%s×%d" % (n, len(v)) for n, v in dupes.items())))
    else:
        pages = list(pages)

    raw, per_page = [], []
    for page in pages:
        res = run_cli(["sch", "check", "--project", args.project,
                       "--page", page, "--json"])
        r = res.get("result") or res
        found = r.get("findings") or []
        per_page.append((page, r, len(found)))
        for f in found:
            g = dict(f)
            g["_page"] = page
            raw.append(g)

    deduped = dedupe(raw)
    bc, bc_trees = bridge_check(args.project, pages[0])
    confirmed_short = (bc.get("bridges") or 0) > 0

    rows = []
    for i, f in enumerate(deduped, 1):
        t = f.get("type") or "unknown"
        rule = RULES.get(t, DEFAULT_RULE)
        sev = rule["severity"]
        content = rule["meaning"] or f.get("message") or t
        note = rule["note"]

        if not rule["evidence"]:
            if sev == "ERROR" and not confirmed_short:
                sev = "COMMON"
            if not confirmed_short:
                content = "%s（复核为**规则误报**：bridge-check 确认 0 真实短路）" % content
            else:
                content = "%s（bridge-check 确认存在真实短路，需人工定位）" % content
        elif t == "pin-exit-direction":
            content = "%s；%s" % (content, note)
        rows.append({
            "id": "S%d" % i,
            "severity": sev,
            "content": content,
            "net": problem_net(f),
            "layer": "—",
            "type": t,
            "page": f["_page"],
            "dups": f.get("_dups", 1),
            "note": note,
        })

    # bridge-check 的独立发现也要进表：sch check 看不到"整棵树不挂网络名"
    # 这种问题（单根导线没毛病，是整条支路悬空），而这正是最常见的一类。
    next_id = len(rows) + 1
    for tr in bc_trees:
        kind = tr.get("type") or ""
        if kind == "bridge":
            rows.append({
                "id": "S%d" % next_id, "severity": "ERROR",
                "content": "导线树上有多个网络名，真实短路（bridge-check 确认）",
                "net": "%s（导线 %s）" % (
                    "、".join(tr.get("nets") or []) or "—",
                    "、".join(tr.get("wireIds") or []) or "—"),
                "layer": "—", "type": "bridge", "page": pages[0], "dups": 1, "note": "",
            })
            next_id += 1
        elif kind == "orphan-tree":
            # 树上有网络名、但不接触任何引脚 → 这条画了却没人用的悬空走线。
            # 反过来 pins 非空 nets 空才是"接了引脚却没标网络"。
            pins = tr.get("pins") or []
            nets = tr.get("nets") or []
            if pins and not nets:
                desc = "导线连到了 %s，但整条支路没标网络名，网络悬空" % "、".join(pins)
            elif nets and not pins:
                desc = "导线上标着 %s，但整条支路没有连到任何器件引脚，是悬空走线" % "、".join(nets)
            else:
                desc = "导线树既不接引脚也不挂网络名，是孤立的悬空走线"
            rows.append({
                "id": "S%d" % next_id, "severity": "WARN",
                "content": desc,
                "net": "%s（导线 %s）" % (
                    "、".join(nets) or "无网络",
                    "、".join(tr.get("wireIds") or []) or "—"),
                "layer": "—", "type": "orphan-tree", "page": pages[0], "dups": 1, "note": "",
            })
            next_id += 1
        elif kind == "orphan-flag":
            # ⚠ 这条规则最容易误报：它只查"网络标志有没有压在导线上"，没考虑
            # **网络标志直接贴在器件引脚上**这种完全合法的画法。实测本页 20 条
            # orphan-flag 里 16 条都带着引脚（U1:1、C13:2、R34:1…），回读
            # sch read 确认这些引脚的 net 都正常赋上了 3V3 / GND / AGND。
            # 所以只有 pins 和 nets 都为空的才是真悬空标记。
            if tr.get("pins"):
                continue
            rows.append({
                "id": "S%d" % next_id, "severity": "WARN",
                "content": "网络标志悬空：既不贴器件引脚也不压导线，不连接任何东西",
                "net": "（标志 %s）" % ("、".join(tr.get("flagIds") or []) or "—"),
                "layer": "—", "type": "orphan-flag", "page": pages[0], "dups": 1, "note": "",
            })
            next_id += 1

    # ---------- 跨页网络完整性：合并所有页后仍只有 1 个引脚的网络 ----------
    dangling = {}
    if not args.no_cross_page:
        # 单页检查时也必须把所有页合起来统计，否则跨页信号一律显示 degree=1
        merge_pages = pages
        if not args.no_cross_page_merge:
            try:
                docs = run_cli(["doc", "ls", "--project", args.project, "--json"])
                items = (docs.get("result") or docs).get("documents") or []
                all_pages = [d["uuid"] for d in items if d.get("type") == "schematic"]
                if all_pages:
                    merge_pages = all_pages
            except RuntimeError:
                pass
        net_pins = cross_page_nets(args.project, merge_pages)
        dangling = {n: v for n, v in net_pins.items() if len(v) == 1}
        for net, pins in sorted(dangling.items()):
            next_id += 1
            rows.append({
                "id": "S%d" % next_id, "severity": "WARN",
                "content": "网络只连了 1 个引脚 —— 跨页信号请确认另一页是否已接，"
                           "否则就是漏画或孤立网络",
                "net": "%s（%s）" % (net, pins[0]),
                "layer": "—", "type": "dangling-net", "page": pages[0],
                "dups": 1, "note": "",
            })

    rows.sort(key=lambda r: (ORDER.get(r["severity"], 9), r["type"]))

    if args.json:
        print(json.dumps({
            "rows": rows,
            "pages": [{"page": p, "findings": n,
                       "passed": (r or {}).get("passed")} for p, r, n in per_page],
            "raw_findings": len(raw),
            "deduped_findings": len(deduped),
            "bridge_check": {"confirmed_short": confirmed_short,
                             "summary": bc, "trees": bc_trees},
        }, ensure_ascii=False, indent=2))
        return 0

    counts = {}
    for r in rows:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    errs = [r for r in rows if r["severity"] == "ERROR"]
    warns = [r for r in rows if r["severity"] == "WARN"]
    commons = [r for r in rows if r["severity"] == "COMMON"]

    print("## 结论")
    if not rows:
        print("未发现问题。")
        return 0
    print("共 %d 处（ERROR %d / WARN %d / COMMON %d），原始 finding %d 条经去重与交叉验证后得到。%s" % (
        len(rows), counts.get("ERROR", 0), counts.get("WARN", 0), counts.get("COMMON", 0),
        len(raw),
        "" if confirmed_short else "导线树检测（bridge-check）确认本工程 **0 处真实短路**。"))
    if errs:
        print("\n**必须先处理的是 %d 处 ERROR**，见下表。" % len(errs))
    else:
        print("\n没有 ERROR 级问题。")

    def table(rs, title, count=True):
        if not rs:
            return
        if count:
            print("\n### %s（%d 处）\n" % (title, len(rs)))
        else:
            print("\n### %s\n" % title)
        print("| 编号 | 严重度 | 问题内容 | 问题网络 | 所在层 |")
        print("|---|---|---|---|---|")
        for r in rs:
            print("| %s | %s | %s | %s | %s |" % (
                r["id"], r["severity"], r["content"], r["net"], r["layer"]))

    # 同类问题成批出现时（本工程 reversed-net-flag 24 条、zero-length-wire 17 条），
    # 逐条铺开会把真正要看的东西挤出屏幕。超过 GROUP_MIN 条的同类合并成一行，
    # 网络名去重后汇总；要逐条看加 --all-warn。
    def group(rs):
        if args.all_warn or len(rs) <= GROUP_MIN:
            return rs
        groups = {}
        for r in rs:
            groups.setdefault(r["type"], []).append(r)
        merged = []
        for t, items in groups.items():
            if len(items) <= GROUP_MIN:
                merged.extend(items)
                continue
            nets = []
            for it in items:
                n = it["net"].split("（")[0]
                if n and n not in nets and "无定位" not in n:
                    nets.append(n)
            net_txt = "、".join(nets[:8]) + ("…" if len(nets) > 8 else "") if nets else "—（无定位字段）"
            merged.append({
                "id": "%s–%s" % (items[0]["id"], items[-1]["id"]),
                "severity": items[0]["severity"],
                "content": "%s（同类共 %d 处）" % (items[0]["content"], len(items)),
                "net": net_txt,
                "layer": "—",
                "type": t,
                "page": items[0]["page"],
                "dups": 1,
                "note": items[0]["note"],
            })
        return merged

    table(errs, "ERROR")
    gw = group(warns)
    if len(gw) == len(warns):
        table(gw, "WARN")
    else:
        table(gw, "WARN（共 %d 处，同类合并后 %d 行）" % (len(warns), len(gw)),
              count=False)

    # COMMON 是"整洁度建议"，全列会把表撑到看不出重点（本工程 154 条），
    # 默认按规则类型折叠成计数；要逐条看加 --all-common。
    if commons:
        groups = {}
        for r in commons:
            groups.setdefault(r["type"], []).append(r)
        print("\n### COMMON 附录（%d 处，按规则类型汇总）\n" % len(commons))
        print("| 规则 | 数量 | 含义 |")
        print("|---|---|---|")
        for t, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print("| %s | %d | %s |" % (
                t, len(rs), RULES.get(t, DEFAULT_RULE)["meaning"] or rs[0]["content"]))
        if args.all_common:
            print("")
            table(commons, "COMMON 逐条")

    print("\n> 所在层：原理图无层概念，固定为 —；PCB 侧的层信息由 pcb_copper_check.py "
          "与 `pcb layout-lint` 提供。")
    if not confirmed_short and any(r["type"] == "wire-contact" for r in rows):
        print("> wire-contact 条目已按 bridge-check 结论标注为误报，"
              "不要据此判定短路。")
    flag_total = bc.get("orphanFlags") or 0
    flag_skipped = sum(1 for t in bc_trees
                       if t.get("type") == "orphan-flag" and t.get("pins"))
    if flag_total:
        print("> bridge-check 报了 %s 个\"悬空网络标记\"，其中 %s 个是网络标志直接贴在"
              "器件引脚上（合法画法，已排除），%s 个既不贴引脚也不压导线，已按 WARN 计入上表。"
              % (flag_total, flag_skipped, flag_total - flag_skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
