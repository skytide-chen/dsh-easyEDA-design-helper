# 功能一：EDA 审查

审查原理图和 PCB，产出五列表格。只报告，不修改工程。

## 输出格式

**固定五列，不可增减：**

| 列 | 内容 | 示例 |
|---|---|---|
| 编号 | `S1`…（原理图）/ `P1`…（PCB）；同类合并时写 `S67–S90` | `S12` |
| 严重度 | `ERROR` / `WARN` / `COMMON` | `ERROR` |
| 问题内容 | **中文含义**（下面的规则表就是标准措辞）；自己发现的用一句话描述 | `零长度导线（首尾坐标相同，不连接任何东西）` |
| 问题网络 | 网络名 + 涉及的器件位号与引脚号 | `3V3（U1.22、R1.1）` |
| 所在层 | **仅审查 PCB 时给出**。原理图无层概念，写 `—` | `Bottom Layer` |

`scripts/review_check.py` 内置了规则表，中英文都按同一份表输出，直接用它生成表格，不要手写。

## 规则表（工具 type → 标准措辞与定档）

| type | 严重度 | 问题内容（标准措辞） | 判定依据 |
|---|---|---|---|
| `wire-through-body` | ERROR | 导线穿过器件本体 | 几何可判，实际压到器件 |
| `sensitive-under-copper` | ERROR | 敏感器件信号脚在 X 层下方被 Y 铜皮覆盖 | 由 `pcb_copper_check.py` 判 |
| `wire-contact` | COMMON | 异网导线存在端点/T 接/共线重叠的真实电气接触 | **不可信**，必须 bridge-check 裁决 |
| `pin-exit-direction` | COMMON | 引脚引出方向不符规范 | 工具标 ERROR，实测大量误判 |
| `titleblock-overlap` | COMMON | 图元压图签 | 不影响电气 |
| `marker-overlap` | COMMON | 网络标记重合 | 只影响观感 |
| `designator-overlap` | COMMON | 位号文字重叠 | 只影响观感 |
| `designator-wire-overlap` | COMMON | 位号压导线 | 只影响观感 |
| `reversed-net-flag` | WARN | 网络标志方向反了 | 影响可读性 |
| `zero-length-wire` | WARN | 零长度导线（首尾坐标相同，不连接任何东西） | 潜在漏画 |
| `floating-pin` | WARN | 引脚悬空（无导线连接，也没打 NC 标识） | 未用引脚应显式 NC |
| `polarity-convention-outlier` | WARN | 极性脚约定与同页多数不一致（电源侧接到 pin 1，同页多数是 pin 2） | 钽/电解反接有热失控风险，MLCC 可忽略 |
| `out-of-sheet` | WARN | 可见属性超出图纸边界（打印时会缺） | 交付缺陷 |
| `missing-partition` | WARN | 缺少功能分区框 | 交付规范 |
| `missing-titleblock` | WARN | 图签没填（图纸标题/设计者/说明） | 交付规范 |
| `dangling-net` | WARN | 网络只连了 1 个引脚 —— 跨页信号请确认另一页是否已接，否则就是漏画 | 由脚本跨页统计 |
| `region-not-effective` | ERROR | 禁布区声明禁铺铜，但区内仍有铜 | 由 `pcb_copper_check.py` 判 |
| `region-layer-gap` | WARN | 禁布区只定义了部分层，另一层同一位置仍有铺铜 | 由 `pcb_copper_check.py` 判 |
| `copper-unreadable` | WARN | 铺铜数据读不到（availability 非 available） | 采集降级 |
| `copper-not-materialized` | WARN | 有铺铜边界但铜皮为空，多半没跑 `easyeda pcb pour-rebuild` | 采集不完整 |

**工具的 `level` 字段大小写不统一**（`ERROR`/`error`/`warn` 混用），一律转大写再映射，不要拿字符串直接比。

## 三个必须做的归一化

原始 `sch check` 输出不能直接进报告，有三个坑：

1. **重复打标**：一处缺陷会同时以 ERROR 中文 message 和 WARN 英文 message 各报一次（零长度导线实测 34 条 = 17 处）。按 `(type, primitiveId|wirePrimitiveId, at, designator, nets)` 去重，**指纹里不要放 message**——重复打标正是 message 不同。
2. **同类刷屏**：`pin-exit-direction` 一类动辄上百条。同类超过 6 条合并成一行「（同类共 N 处）」+ 网络名去重汇总，编号写成 `S67–S90`。
3. **缺定位信息**：`wire-contact` 只有 `nets` 和 `message`，没有 `primitiveId`/`bbox`/`at`。这种"问题网络"列只能写网络名，后面补「本规则未给定位字段」。

## 审查流程

### 第一步：定位工程

```bash
python scripts/env_check.py                        # 环境自检；不通过就停下报错，等用户判断（不要自己乱试）
easyeda doc ls --project "工程名" --json            # 拿 uuid（不加 --json 是文本表格）
```

重名文档（本工程「强电」有 3 份、「单片机核心及输入」有 3 份）必须用 uuid，`--page 强电` 会报 ambiguous。**同名页多半是同一页的多份副本，选当前在用的那份**，否则 quantity/degree 统计会翻倍。

### 第二步：原理图采集

```bash
easyeda sch read  --project P --page U --no-check   # 每页一次
easyeda sch check --project P --page U --json       # 每页一次
easyeda sch nets  --project P                        # 单引脚网/同名网审计（只覆盖当前页）
easyeda sch list  --project P --page U               # 含 otherProperty（Value / Global Net Name / Datasheet）
```

从 `sch read` 拿：`components[]`（`designator`/`footprint`/`pins[{name,net,number}]`/`supplierId`）、`floatingPins`、`nets`。

**跨页判定必须合并所有页**：某引脚在 A 页看着悬空，但网络标志指向 B 页，B 页有器件接着——不是悬空。`review_check.py` 默认就是这么做的（用 `doc ls` 自动发现全部原理图页再合并），单页 `--page` 只是限制"检查哪一页的 findings"。

### 第三步：短路裁决（权威）

```bash
easyeda sch bridge-check --project P --doc D        # 注意是 --doc，传 --page 会报 unknown flag
```

返回 `result.summary = {trees, bridges, orphans, orphanFlags, orphanTrees, wireTreesTotal}` 和 `result.trees[]`。

- `bridges` > 0 → 同一棵导线树挂不同网名，**真短路，ERROR**。这时才把对应的 `wire-contact` 一起报（两者能对上）。
- `bridges` = 0 而 `wire-contact` 报了 N 条 → 误报，报告里写明"已交叉验证为误报"，不要闷声删掉——用户自己也看到过这些报错。

**两个容易报错的项（实测纠正）：**

- **`orphanFlags` 大多不是问题**。该规则只查"网络标志有没有压在导线上"，不考虑**网络标志直接贴在器件引脚上**这种合法画法。实测 20 条里 16 条带 `pins`（U1:1、U1:48、C13:2、LED1:1…），回读 `sch read` 确认这些引脚 net 都正常赋上了。**只有 `pins` 和 `nets` 都为空的才算真悬空。**
- **`orphanTrees` 要看 `pins`/`nets` 哪个空**。`nets` 非空而 `pins` 空 = 导线上标了网络名但整条支路没连到任何器件引脚（悬空走线），不是"没标网络"。

### 第四步：PCB 采集

```bash
easyeda pcb layers --project P --doc D        # 先拿层名映射：1=Top, 2=Bottom, 12=Multi
easyeda pcb list   --project P --doc D --include-bbox
```

器件 `layer` 是数字，用映射翻成名字再写进第五列。

### 第五步：PCB 检查

```bash
easyeda pcb layout-lint --project P --doc D
easyeda pcb drc       --project P --doc D
```

**这两条只覆盖器件几何，完全不读铺铜、不读禁布区。** 铺铜必须单独跑：

```bash
python scripts/selfcheck_geom.py --project P --doc D --ref X1   # 先自检几何解析对不对
python scripts/pcb_copper_check.py --project P --doc D
```

**换一块新板子先跑 `selfcheck_geom.py`**：它把解析结果和平台自带的 bbox 逐条对照
（本板 115 个禁布区 + 18 个静态填充全部吻合）、检查双 ARC 圆是否真的是圆、
再打印指定器件每个焊盘的逐层铜判定。全部通过才说明"焊盘下方有没有铜"这个结论可信。
自检不过就用 `--no-...` 之类绕过去，只会得到一堆假报（按错语法解析曾造出 57 条假报）。

它一次 `pcb dump --include-copper` 拿全量几何，做三类判定：

1. **敏感器件下方铺铜**：按**实际焊盘点**做二值判定（焊盘坐标有没有落在铜里），只报"信号脚被**其它网络**铺铜压住"。GND 脚下方有铜是正常的，同网铜也正常。
2. **禁布区是否生效**：区内仍有铜 → 禁布区没起作用。
3. **禁布区层覆盖缺口**：只在"禁布区 bbox 与敏感器件 bbox 相交"时判，且要求两层对照——顶层有 no-pours 而底层同位置照铺 = 只定义了一层。

不报覆盖率百分比。覆盖率在这个几何格式下不可靠：同一位置的三种解读能算出 3.3% / 53.7% / 97.5% 三个数（环之间是并集还是异或无法确定），而二值判定在三种方法下完全一致。**宁可给"焊盘下方有/无铜"这种可复核的结论，也不给一个好看但可能错的百分比。**

采集不可靠时脚本会分开报 `copper-unreadable` / `copper-not-materialized`。**"读不到"和"确实没有"是两件事**：`poured` 为空时输出是「这不是检查通过：没有铺铜可查」，不要当成板子干净。

### 第六步：重叠交叉验证

`layout-lint` 的 overlap 是 bbox 判定，实际器件轮廓常小于 bbox：

- 两颗 0603/0402 小贴片 bbox 相交但焊盘分开 → 标 WARN，注明"bbox 相交，建议目视确认"
- 涉及 IC body 与相邻电容 → 查具体焊盘坐标，可能真压了
- IC 之间（如 U1↔X1）→ bbox 相交但 body 不重叠常见，看 bbox 尺寸和封装外形

判不了就标 WARN 并在"未覆盖项"里说明。

## 严重度分档

| 档 | 判据 |
|---|---|
| `ERROR` | 电气/装配确定出问题：真短路（bridge-check）、导线穿器件本体、关键网络悬空、敏感器件信号脚被异网铺铜覆盖、禁布区失效 |
| `WARN` | 规范缺陷，不影响当前功能但埋雷：禁布区只定义一层、零长线、引脚未打 NC、网络标志方向反、图签未填、丝印压焊盘 |
| `COMMON` | 建议性洁癖项：位号重叠、标记重合、压图签、引脚出线方向 |

几个具体判断：

- **晶振下方铺铜**：无源晶振（晶体谐振器）对杂散电容敏感，下方铺铜会改变负载电容、影响起振和频偏 → **ERROR**。有源振荡器内部有电路，影响小得多 → WARN。
- **pin-exit-direction**：电气上连通，只是对维护和 ERC 是隐患。工具标 `ERROR`，实测大量误判（"outward rotation 270°"未必真的反了）。判不了就 COMMON 并注明。
- **受控阻抗 / 等长 / 泪滴**：查不了或只能部分做，一律写进"未覆盖项"，不要沉默。

## 报告结构

```
## 结论
一句话：最严重的问题是什么、要不要马上改。

## 问题清单（ERROR）
[五列表]

## 问题清单（WARN）
[五列表]

## 未覆盖项
- layout-lint 与 drc 不检查铺铜，铺铜由 pcb_copper_check.py 单独核查
- 受控阻抗无法验证（平台读不到叠层 Er/介质厚/铜厚）
- 等长/时序 skew 未做全量验证

## 疑似误报
[工具报了但交叉验证后不成立的，说明理由和裁决依据]
```

COMMON 放折叠附录，按规则类型汇总计数，不占正文。

## 常见踩坑

- findings 字段是 `type`/`level`/`nets`/`message`，**不是** `code`/`severity`
- `bridge-check` 用 `--doc`，`sch check` / `sch read` 用 `--page`
- `pcb.components.list` 必须指到 PCB 文档，指到原理图会报 `needs a pcb document`
- `poured-list` 是铺完后的实际铜岛（有 `fills`），`pour-list` 是可编辑的铺铜定义（有 `source`）。**判有没有铜用 `poured-list`**，判能不能改用 `pour-list`
- 一次全页扫描 7 页约 2.5 分钟，超过前台超时会转后台 job，记得用 job_output 收
- 脚本输出含中文，Windows 终端按 GBK 显示会乱码：`PYTHONIOENCODING=utf-8 python ... > out.txt` 再看文件
