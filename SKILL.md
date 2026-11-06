---
name: eda-design-helper
description: "审查嘉立创EDA专业版（EasyEDA Pro）工程的原理图与 PCB、给出 PCB 布局建议、识别元件用途与接线方法。当用户要求检查/审查/复核电路设计、问板子有没有问题、想看布局该怎么摆、问某个器件（如 AMS1117、TLP521、LM358）怎么接线或有什么用途，或者贴出 BOM/位号让分析时使用。依赖 easyeda-agent skill 提供的 easyeda CLI 与连接器。"
metadata:
  author: fattywhale
  version: "1.0.0"
  license: MIT
---

# EDA Design Helper

三个功能共用一套数据采集（`easyeda` CLI），区别只在判据和输出格式：

| 用户说什么 | 走哪个流程 |
|---|---|
| "检查一下我的板子" / "帮我审查原理图和 PCB" / "有没有问题" | [审查](references/review.md) |
| "这块该怎么摆" / "U1 放哪" / "走线怎么走" / 还在 Layout 阶段 | [布局建议](references/placement.md) |
| "AMS1117 怎么用" / "这个芯片干嘛的" / "帮我找 X 的用法" | [元件用途](references/part_usage.md) |

配套参考：[几何格式](references/geometry.md)（铺铜/禁布区 `source` 语法，写脚本或手工核几何时必读）

分不清时先问一句。用户在 Layout 阶段问"这样摆行不行"，那是功能二；问"画完的板子有没有问题"，那是功能一。

## 前置条件

本 skill 不自带任何 EDA 访问能力，全部依赖 `easyeda-agent` skill：

```bash
easyeda health     # 必须看到 windows 数组非空、capabilities 含 schematic.v1 / pcb.v1
```

`windows` 为空说明连接器没连上，**先停下**让用户处理（见 easyeda-agent 的 `references/environment-setup.md`），不要盲试。常见原因是 EasyEDA 里没开「允许外部交互」，或 daemon 没启动（`easyeda daemon start`）。

## 三条铁律

这三条是踩过坑总结出来的，违反任何一条都会产出看似合理但错误的报告。

### 一、证据链：先取证，再下结论

任何一条"有问题"都必须能指回一个具体的命令输出。写报告时，每条结论心里要能补上一句"证据来自 `xxx` 命令的哪个字段"。做不到就降级成"未核实"，而不是猜。

工具说 ERROR 不等于真问题，需要用不同机制交叉验证：

- 短路类：`sch check` 的 `wire-contact` **不可靠**——同一工程连跑两次，网络对会变，而且这条规则不提供任何定位字段。必须用 `sch bridge-check` 的 `bridges` 计数裁决（几何并查集，同一条导线上挂两个不同网名才算真短路）。
- 同理，`bridge-check` 的 `orphanFlags` 也不能照单全收：网络标志**直接贴在器件引脚上**是合法画法，该规则不认识，20 条里 16 条是这样。只有 `pins` 和 `nets` 都为空的才是真悬空。
- 重叠类：`layout-lint` 的 overlap 用 bbox 判定，实际器件轮廓常小于 bbox，报 ERROR 的几对也可能是误报。要结合封装的实际焊盘位置判断是否真的干涉装配。

反过来，工具没报也不代表没问题——见铁律三。

### 二、跨页与逐层：网络和铜都不是平面结构

跨页网络：单页读 `sch read --page` 时，凡是指向其他页的网络标志，在任何单页里看都只有 degree=1，像漏画。**必须把所有相关页都读一遍**再判断悬空；`sch read` 不带 `--page` 时读的是当前前台页，同样不够。本工程就有一批 `CYCLE_FAN_MOS` / `MOTOR_DRIVE1` 在某页看着是悬空，实际接在「强电」页上。

逐层铜：`poured-list` 的每个铜岛自带 `layer`，`region list` 的禁布区也自带 `layer`。**禁布区只拦同层的铺铜**，只有 `layer=12`（Multi-Layer）才是多层通吃。见到顶层有「no-pours」禁布区就当"晶振下方无铜"，是踩过的错误——底层铺铜照样铺满整颗晶振。**判定必须逐层、且落到实际焊盘坐标上**，不要只看 bbox 或中心点。

### 三、知道工具不查什么

`layout-lint` 和 `drc` 只做器件 bbox 相交、间距、飞线交叉、可布性。**它们完全不读铺铜、不读禁布区。** 铺铜相关的问题（晶振/电感/天线下方铺铜、禁布区没生效、禁布区只定义一层）必须单独跑 `scripts/pcb_copper_check.py`。

另一个方向也要守住：**"读不到"不等于"没发现"**。`poured` 为空、`availability` 非 available 时，正确输出是「没有铺铜可查，这不是检查通过」，不是「未发现问题」。

其它已知盲区：
- 受控阻抗查不了——平台 API 读不到叠层的 Er、介质厚度、铜厚
- 等长/时序 skew 只能部分做
- 泪滴没有 typed API

写报告时这些盲区要在"未覆盖项"里显式列出，别让用户以为检查过了。

## 命令速查

统一先看 `easyeda <命令> --help`，参数以当前版本为准。`--doc` / `--page` 用 uuid 最稳，重名页会报 ambiguous。

```bash
# 工程与文档
easyeda health
easyeda project ls
easyeda doc ls --project "工程名"          # 拿 schematic/pcb 文档 uuid
easyeda pcb layers --project P --doc D     # 层 id→名字映射（1=Top, 2=Bottom, 12=Multi）

# 原理图
easyeda sch read --project P --page U --no-check   # components / nets / floatingPins
easyeda sch check --project P --page U --json      # findings[{type,level,nets,message}]
easyeda sch bridge-check --project P --doc D       # 权威短路判据（--doc，不是 --page）

# PCB 几何
easyeda pcb list --project P --doc D --include-bbox
easyeda pcb track-list --project P --doc D
easyeda pcb pour-list --project P --doc D          # 可编辑铺铜外轮廓
easyeda pcb poured-list --project P --doc D --net GND   # 实体化铜岛（带 layer）
easyeda pcb region list --project P --doc D        # 禁布区（带 layer 和 ruleType）

# PCB 检查
easyeda pcb layout-lint --project P --doc D
easyeda pcb drc --project P --doc D
```

## 脚本

| 脚本 | 用途 |
|---|---|
| `scripts/review_check.py` | 原理图审查：跑 `sch check` + `bridge-check`，归一化成五列表（去重、合并同类、裁决误报） |
| `scripts/pcb_copper_check.py` | PCB 铺铜/禁布区审查：敏感器件焊盘下方铺铜、禁布区是否生效、禁布区层覆盖缺口 |
| `scripts/placement_brief.py` | 布局建议取证：候选主元件、机械约束器件、每颗器件的信号邻居强度、PCB 阶段 |
| `scripts/selfcheck_geom.py` | 几何解析器自检：bbox 对照平台真值、双 ARC 圆、指定器件焊盘逐层铜判定 |
| `scripts/eda_geom.py` | `source` 几何解析库（矩形/圆弧/多轮廓 even-odd），被上面几个脚本 import |

用法：

```bash
python scripts/review_check.py --project P                 # 全部页
python scripts/review_check.py --project P --page U        # 指定页
python scripts/pcb_copper_check.py --project P --doc D
python scripts/placement_brief.py --project P --ref U1   # 布局建议前先跑这个拿事实
python scripts/selfcheck_geom.py --project P --doc D --ref X1   # 铺铜检查前先自检几何解析
```

这几个脚本都先跑 `--help` 看参数。`review_check.py` 的 `--all-warn` / `--all-common` 展开被合并的行，`--json` 出结构化结果。

**几何格式（`source` 里的矩形和圆弧语法）必须按 [geometry.md](references/geometry.md) 理解，不要凭直觉解析——按"ARC 后是圆心"解析会算出虚大几倍的包围盒，按"环拼成一条路径"会填死内部挖空。**

### 一次只能跑一个采集流程

`easyeda` 只有**一个前台窗口**。两个进程同时跑（或一个在后台跑全页扫描、另一个插进来读 PCB）会撞车，报：

```
document.open failed: a write/document transition is being verified on this window
— wait for its readback; do not interleave another write or document switch
```

看到这个就**等前一个流程跑完再重试**，不要重试风暴。审查一律串行：先原理图全部页，再 PCB。

全页扫描耗时以分钟计（7 页约 2.5 分钟），前台超时会转后台 job，用 `job_output` 收结果，期间不要发起新的 CLI 调用。

### 读 CLI 输出时的编码坑

Windows 控制台会把 CLI 的中文输出按 GBK 二次解码成乱码。抓取输出时按 `utf-8` → `gbk` → `gb18030` 依次尝试，别硬解。`sch check` 的文本输出在 JSON 前面还有一行人类可读摘要，切 JSON 要从第一个 `{` 开始。

## 输出约定

- 全中文，EDA 术语保留英文原词（net、via、DRC、keepout 等）
- 结论先行：一句话说清最严重的问题，再展开
- "问题内容"用中文标准措辞（`scripts/review_check.py` 内置规则表），不要照抄工具英文 message
- 严重度三档：`ERROR`（电气/装配确定出问题）/ `WARN`（规范缺陷，埋雷但不影响当前功能）/ `COMMON`（建议性）
- 只报告不修改。用户没明确要求时不要动工程文件——审查结果要能被别人复核，改完就复不了核了
