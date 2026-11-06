# dsh-easyEDA-design-helper

嘉立创EDA专业版（EasyEDA Pro）的 AI 设计助手 skill —— **审原理图、审 PCB、给布局建议、查元件怎么用**。

它自己不访问 EDA，只做三件事：**约束调用方式、定义输出格式、守住证据边界**。所有几何与连接数据由 [`easyeda-agent`](https://github.com/zhoushoujianwork/easyeda-agent) 的 `easyeda` CLI 只读采集。

- 适用对象：DSH（DeepSeek Harness）/ Claude Code 等支持 `SKILL.md` 约定的 Agent 环境
- 实测环境：EasyEDA Pro **V4.1.78** + easyeda-agent **v1.16.0** + connector 1.16.0
- 审查**只报告不修改**：结论必须能被别人按同一命令复跑复核

## 功能

| 用户说什么 | 走哪个流程 | 产出 |
|---|---|---|
| 「检查一下我的板子」「帮我审查原理图和 PCB」「有没有问题」 | [审查](references/review.md) | 五列表：编号 / 严重度 / 问题内容 / 问题网络 / 所在层 |
| 「这块该怎么摆」「U1 放哪」「走线怎么走」「Layout 阶段」 | [布局建议](references/placement.md) | 主元件、相对位置、走线顺序建议（**不给坐标**） |
| 「AMS1117 怎么用」「这个芯片干嘛的」「帮我找 X 的用法」 | [元件用途](references/part_usage.md) | 引脚用途 + 典型接线；找不到手册就明说找不到 |

三个功能共用一套数据采集，区别只在判据和输出格式。

### 1. EDA 审查

同时审原理图与 PCB，输出固定五列（原理图「所在层」写 `—`，PCB 给 `Top Layer` / `Bottom Layer`），严重度三档：

- `ERROR` —— 电气/装配确定出问题（真短路、导线穿器件本体、关键网络悬空、敏感器件信号脚被异网铺铜覆盖、禁布区失效）
- `WARN` —— 规范缺陷，不影响当前功能但埋雷（禁布区只定义一层、零长线、引脚未打 NC、去耦离 IC 太远…）
- `COMMON` —— 建议性整洁度（位号重叠、标记重合、引脚出线方向），默认折叠成计数

三个必须做的归一化（原始工具输出不能直接进报告）：

1. **去重**：一处零长导线会以 ERROR 中文 + WARN 英文各报一次（实测 34 条 = 17 处）
2. **同类合并**：`pin-exit-direction` 一类动辄上百条，超过 6 条合并成一行
3. **误报裁决**：`wire-contact` 不可信（同一工程连跑两次网络对会变），必须用 `sch bridge-check` 的 `bridges` 计数裁决

### 2. PCB 布局建议

依据原理图给**相对位置与走线建议**：先找主元件（接口/晶振/MCU/电源），再按信号邻居强度排布卫星器件，明确**不给坐标**（坐标要结合板框和机械约束，属 Layout 阶段人工决策）。

### 3. 元件用途识别

数据手册三级查找，全部失败只回「**无法找到数据手册**」：

1. 本地工作目录下名称含 `数据` / `手册` / `datasheet` 的文件夹与文件
2. 嘉立创EDA 元件属性里的 `Datasheet` 超链接（`easyeda sch list` 的 `otherProperty.Datasheet`）
3. 用 `立创元件编号`（`supplierId`，如 `C8734`）到立创商城查

**没有明确指示时不得编造或按芯片类型/封装/引脚名推理引脚功能。**

## 依赖

### 必需

| 依赖 | 版本/要求 | 说明 |
|---|---|---|
| [`easyeda-agent`](https://github.com/zhoushoujianwork/easyeda-agent) | v1.16.0 实测通过 | 提供 `easyeda` CLI、本地 daemon 和连接器；**本 skill 不自带任何 EDA 访问能力** |
| EasyEDA Pro（嘉立创EDA专业版） | **V4**（实测 4.1.78） | 需装 `EDA Agent Connector` 扩展（.eext），并在扩展「配置」里开启**允许外部交互** |
| easyeda daemon | 端口 `60832` | `easyeda daemon start`（阻塞前台进程，应分离启动） |
| Python | 3.10+ | 脚本**只用标准库**，无需 pip 安装任何包 |

### 环境自检

```bash
easyeda health
```

必须看到 `status: found`、`windows` 数组非空、`capabilities` 含 `schematic.v1` 与 `pcb.v1`。`windows` 为空说明连接器未连上 —— 先停下让用户处理，不要盲试。

`easyeda` 不在 `PATH` 时，脚本会依次尝试 `EASYEDA_CLI` 环境变量 → `~/.easyeda-agent/bin` → `~/easyeda-agent/bin` → `PATH`。

### 可选

- 浏览器工具（web_fetch / dict 抓取）—— 仅功能三的第 3 级手册查找用；没有就按规矩回「无法找到数据手册」
- `easyeda-agent` skill 本体 —— 本 skill 的 `SKILL.md` 在采集细节上引用它

## 安装

把本仓库整个目录放进 Agent 的 skills 目录：

```bash
# Claude Code（Linux/macOS）
git clone https://github.com/skytide-chen/dsh-easyEDA-design-helper.git ~/.claude/skills/eda-design-helper

# 推荐：克隆到用户目录下的相对位置（不要写盘符绝对路径）
git clone https://github.com/skytide-chen/dsh-easyEDA-design-helper.git "$HOME/.claude/skills/eda-design-helper"

# Windows PowerShell 等价写法
git clone https://github.com/skytide-chen/dsh-easyEDA-design-helper.git "$env:USERPROFILE\.claude\skills\eda-design-helper"
```

装好后 Agent 的技能列表里应出现 `eda-design-helper`，`SKILL.md` 会被自动加载。

## 用法

### 通过 Agent 触发

直接说需求即可，例如：

- 「用 eda-design-helper 审一下工程 `XXX` 的 `YYY` 板子的 PCB 和原理图」
- 「我这个板子有没有问题？」
- 「U1 应该放哪，走线怎么走？」
- 「IP5310 的 VBUS 脚要接什么？」

### 直接跑脚本

```bash
# 原理图审查：sch check + bridge-check → 五列表（全部原理图页）
python scripts/review_check.py --project "工程名"
python scripts/review_check.py --project "工程名" --page <uuid> --page <uuid> --no-cross-page-merge
python scripts/review_check.py --project "工程名" --json > out.json     # 机读
python scripts/review_check.py --project "工程名" --all-warn --all-common  # 展开合并行

# PCB 铺铜/禁布区审查（layout-lint 和 drc 都不查铺铜，必须单独跑）
python scripts/pcb_copper_check.py --project "工程名" --doc <pcb-uuid>

# 铺铜检查前先自检几何解析器（bbox 对照平台真值 / 双 ARC 圆 / 指定器件逐层铜）
python scripts/selfcheck_geom.py --project "工程名" --doc <pcb-uuid> --ref X1

# 布局建议取证：主元件候选、机械约束件、信号邻居强度、PCB 阶段
python scripts/placement_brief.py --project "工程名" --ref U1
```

### 目录结构

```
SKILL.md                      触发路由 + 三条铁律 + 命令速查 + 输出约定
references/
  review.md                   功能一：五列表定义、六步审查流程、严重度分档、报告结构
  placement.md                功能二：主元件判据、摆放/走线原则、输出格式
  part_usage.md               功能三：数据手册三级查找、证据边界
  geometry.md                 EasyEDA source 几何语法（矩形 / ARC / 多轮廓 even-odd）
scripts/
  review_check.py             sch check + bridge-check → 五列表（去重、合并、误报裁决）
  pcb_copper_check.py         铺铜与禁布区审查（敏感器件焊盘逐层判铜）
  placement_brief.py          布局建议的事实采集
  selfcheck_geom.py           几何解析器自检（解析必须与平台 bbox 对上才可信）
  eda_geom.py                 几何解析库，被上面几个脚本 import
```

## 三条设计原则（踩坑总结）

1. **证据链**：每条「有问题」都必须能指回具体命令输出；做不到就降级成「未核实」，不要猜。
2. **跨页 + 逐层**：单页读时跨页网络一律显示 degree=1（像漏画）；禁布区**只拦同层的铺铜**，顶层有 `no-pours` 不代表底层没有铜 —— 判定必须逐层、且落到实际焊盘坐标上。
3. **知道工具不查什么**：`layout-lint` / `drc` **完全不读铺铜和禁布区**；「读不到」不等于「没发现」（`poured` 为空时正确输出是「没有铺铜可查，这不是检查通过」）。

## 已知限制

- **不报铺铜覆盖率百分比**：同一位置三种几何解读能算出 3.3% / 53.7% / 97.5% 三个数（环之间是并集还是异或无法确定），因此改为「焊盘下方有/无铜」的二值判定 —— 可复核，不会给出好看但可能错的数字。
- **受控阻抗查不了**：平台 API 读不到叠层的 Er、介质厚度、铜厚。
- **等长 / skew 只能部分做**，泪滴没有 typed API。
- **平台 DRC 会因 `Netlist Error`（原理图↔PCB 网表不一致）中止**，此时间距等规则结论缺失，报告里会显式说明。
- **一次只能跑一个采集流程**：`easyeda` 只有一个前台窗口，并发会报 `document.open failed: a write/document transition is being verified on this window`。审查一律串行：先原理图全部页，再 PCB。
- 审查**只报告不修改**；用户没明确要求不动工程文件。

## 实测验证

| 工程 | 规模 | 结果 |
|---|---|---|
| 烘干机主板（7 页原理图 / 103 个 PCB 器件 / 2 层板） | 59 器件 / 103 PCB 器件 | 原理图 353 处（ERROR 3 / WARN 96 / COMMON 254，原始 1063 条）；铺铜查出**无源晶振 X1 的 1、3 脚在底层被 GND 铺铜覆盖**（顶层有禁布区、底层漏配）；`bridge-check` 裁决 `wire-contact` 16 条为误报 |
| 排烟扇控制板 · V4-恒压MCU改良（2 页原理图） | 74 器件 / 43 网络 | 原理图 170 处（ERROR 3 / WARN 42 / COMMON 125）；PCB 查出 **3 个器件位号与原理图不一致导致平台 DRC `Netlist Error`**、47 处 via-in-pad、10 个去耦超距；并按板级规则（4.02/5.98 mil）把 4 处「间距违规」裁决为边界值误报 |

几何解析器自检：115 个禁布区 bbox 与平台真值全吻合、18 块静态填充全吻合、25 个双 ARC 圆偏差 < 1mil。

## 版本

- `1.0.0` —— 首个版本：三条铁律、四个脚本、两份实测工程验证

## 许可

[MIT](LICENSE) © 2026 skytide-chen
