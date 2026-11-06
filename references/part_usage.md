# 功能三：元件用途识别

用户问"AMS1117 怎么接线"、"这个 TLP521 干嘛用的"、"帮我找 ESP32 的用法"。

## 铁律：证据不足就不答

**这是本功能最重要的一条。** 用户明确要求：没有查到数据手册时，直接回答「无法找到数据手册」，不得编造，也不得按芯片类型、封装、引脚名去推理。

原因很实在：EDA 设计里一个引脚接错就是废板。LM358 是双运放，但它是单电源还是双电源应用、哪个脚是同相端，不同厂家不同批次的 datasheet 在细节上就有差异；TLP521 是光耦，但 CTR 范围决定能不能直接用，猜不出来。**宁可说"不知道"，也不能给一个看起来合理的错误答案。**

这条规则的边界要想清楚：

- ✅ 能答：从数据手册里读到的引脚定义、典型应用电路、参数
- ✅ 能答：用户自己工程里的实际连接（`sch read` 读出来的 `pins[].net`）
- ❌ 不能答：凭"这类芯片一般怎么接"给的通用建议
- ❌ 不能答：凭引脚名叫 VCC/GND 就推断出的电源接法
- ❌ 不能答：凭封装是 SOT-23-5 就说"应该是 LDO"

当用户问的是「**我的板子上这个芯片实际怎么接的**」，那不需要数据手册——读原理图就能答，这属于证据充分的情况。要分清这两类问题。

## 数据手册三级查找

按顺序找，找到就停，不能跳过前面的直接跳后面。

### 第一级：本地文件夹

在工作目录下找名称包含「数据」「手册」「datasheet」等关键词的文件夹，看里面有没有这个器件的文档。

```bash
ls -d */ | grep -iE '数据|手册|datasheet|doc'
```

找到文件夹后按器件型号（`manufacturerId` 或 `supplierId`）搜文件名。注意型号的写法差异：`STM32F103C8T6` 可能被存成 `STM32F103C8`、`stm32f103c8t6.pdf`、`STM32F103C8T6_数据手册.pdf`。用忽略大小写、忽略分隔符的方式匹配。

搜到 PDF 后要**真的读**，不要看到文件名就假设内容。读 PDF 时重点看 Pinout、Pin Description、Typical Application 三节。

### 第二级：嘉立创EDA 元件属性里的「数据手册」超链接

`sch read` **不返回**自定义属性，要改用 `sch list`：

```bash
easyeda sch list --project P --page U        # 注意：没有 --json 参数，但输出本身就是 JSON
```

`result.components[].otherProperty` 里带库里的全部自定义属性。找这几个键：

| 键 | 内容 | 例 |
|---|---|---|
| `Datasheet` | 数据手册 URL | `https://item.szlcsc.com/datasheet/STM32F103C8T6/9243.html` |
| `LCSC Part Name` | 立创对这颗料的描述 | `主流增强型ARM Cortex-M3 MCU，具有64 KB Flash、72 MHz CPU` |
| `Supplier Footprint` | 立创封装名 | `LQFP-48(7x7)` |
| `Value` | 值（阻容才有） | `10k` |
| `Global Net Name` | 全局网名 | `GND` |

`sch list` 里**没有 designator 字段**，只有 `subPartName`（形如 `STM32F103C8T6.1`）和 `primitiveId`。要和 `sch read` 的 `components[]` 按 **`primitiveId` join** 才能对上位号。

**覆盖率不高**：本板 59 个器件里只有 6 个带 `Datasheet`（U1、两颗 AMS1117、AT24C02、8P 排针、蓝牙模块）。没有这个键就是没填，直接退第三级，不要在这里反复试。

拿到 URL 就用 web_fetch 取。**实测这个 `item.szlcsc.com/datasheet/...` 页面是 JS 渲染的，web_fetch 只能拿到标题，取不到 PDF 正文和引脚表**——这种情况下：

1. 有浏览器工具就用浏览器打开该 URL 读内容
2. 没有浏览器工具 → 视为"取不到"，退第三级
3. 三级全失败 → 按下面的规矩回「无法找到数据手册」

链接失效、403、需要登录同样退第三级。

### 第三级：用立创编号去立创商城查

```bash
# supplierId 就是立创元件编号，形如 C8734
```

有了 `supplierId`（`sch read` 里就有，如 `C8734`），去立创商城搜这个编号：

```bash
# 站内搜索（页面是 JS 渲染，web_fetch 只能拿到空壳，要用浏览器工具打开）
https://so.szlcsc.com/global.html?k=C347256
```

**实测结论：`item.szlcsc.com` 的商品页和 datasheet 页都是 JS 渲染，`web_fetch` 拿不到内容**（datasheet 页只回标题，商品页 `https://item.szlcsc.com/<C号>.html` 直接 404）。所以第三级实际上**要求有浏览器工具**：

- 有浏览器 → 打开搜索页，进商品页，读「数据手册」链接
- 没有浏览器 → 第三级视为失败，按下面的规矩回「无法找到数据手册」

不要因为"我打不开这个页面"就把自己记得的芯片知识当成手册内容——那还是编造。

也可以先用 CLI 反查器件身份，再顺藤摸瓜：

```bash
easyeda lib by-lcsc --lcsc C8734     # → libraryUuid + uuid
easyeda lib search --help            # 按型号/封装搜器件库
```

注意：`supplierId` 是**立创元件编号**（C 开头），`manufacturerId` 是**厂家型号**，两者不一样。在商城里要按前者搜。

`sch read` 里 `name` 常是 `={Manufacturer Part}` 这类占位符，真实型号看 `manufacturerId`（PCB 侧可见）或 `sch list` 的 `subPartName`。

### 全部失败

原样回复：

> 无法找到数据手册。

然后可以补一句"我可以根据你原理图里现有的连接告诉你这块板现在怎么接的"，把主动权交回用户。**不要**在这句话后面接着给推测性的接线建议。

## 数据手册里读什么

拿到了手册，按用户的问题去找，不要通读全文。

| 用户问 | 查手册的哪里 |
|---|---|
| 怎么接线 | Pin Configuration + Pin Description + Typical Application Circuit |
| 这个脚干嘛的 | Pin Description 表里对应那一行 |
| 供电范围 | Absolute Maximum Ratings + Recommended Operating Conditions |
| 输出能带多大负载 | Electrical Characteristics 里的 Iout / 驱动能力 |
| 外围元件怎么选 | Application Information 里的计算例子 |
| 和别的芯片能不能互换 | 对比 Pinout 和 Electrical Characteristics |

**引用时给出页码或章节号**，让用户能自己核对。比如"见第 12 页 Pin Description 表"。

## 结合用户的实际电路

用户问"这个芯片在我板子上怎么用的"，要读原理图：

```bash
easyeda sch read --project P --page U --no-check
```

找到器件的 `pins[]`，每个引脚有 `name`/`number`/`net`。整理成：

| 引脚号 | 引脚名 | 接的网络 |
|---|---|---|
| 1 | VIN | 24V |
| 2 | GND | GND |
| 3 | VOUT | 5V |

然后结合数据手册解释每个网络是干嘛的。这种"实际连接 + 手册对照"的回答最有价值，因为它同时给了事实和解释。

## 输出格式

**查到了手册：**

```
## 一句话
AMS1117-3.3 是 800mA 低压差线性稳压器，固定 3.3V 输出。

## 引脚与接法
| 引脚 | 名称 | 接法 | 依据 |
|---|---|---|---|
| 1 | GND | 接地 | datasheet p.3 Pin Description |
| 2 | VOUT | 3.3V 输出，配 10µF 输出电容 | p.4 Typical Application |

## 你这块板上是怎么接的
[列实际网络]

## 注意事项
- 输入输出压差大时发热明显，800mA 下压差 5V 就有 4W 功耗
```

**没查到手册：**

```
无法找到数据手册。
```

一句话，不加推测。

## 常见坑

- `name` 字段常常是 `={Manufacturer Part}` 或 `={Value}` 这样的占位符，**不是真实型号**。真实型号看 `manufacturerId`，立创编号看 `supplierId`
- 读自定义属性要用 `sch list`，`sch read` 里没有 `otherProperty`
- 同一个器件在不同工程里的属性名可能不一样，找不到 datasheet 字段时把所有 attributes 打出来看
- 有多个同名器件（比如两个 AMS1117）时要问用户是哪一个，或者按位号都列出来
- 手册是英文的很正常，回答用中文，术语保留英文
- 不要把"我记得这个芯片大概是这样"当成数据手册——这就是编造，铁律禁止
