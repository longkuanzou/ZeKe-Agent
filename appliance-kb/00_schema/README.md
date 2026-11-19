# 00_schema · 知识库数据契约

本目录定义"知识如何存放"。任何新增文件都必须遵守这里的三件事：**分层归属、字段形状、命名规范**。

---

## 1. 为什么要分层

知识库最容易烂掉的写法是把"型号参数 + 客服话术 + 保修政策"混在一篇文档里。那样写起来快，但三个月后无法维护，原因只有一个：**不同知识的变更频率差了一个数量级**。

| 层 | 目录 | 变更多快 | 谁维护 | 回答什么问题 |
|---|---|---|---|---|
| 事实层 | `03_product/` | 上新机型时线性增长，老机型基本冻结 | 资料/运营 | 这台机器**是什么**（参数、功能、安装条件、操作、故障现象） |
| 场景层 | `04_scenario/` | 每周都在改（话术、追问、转人工边界） | 客服运营 | 这句话**该怎么接**（问什么、怎么答、给不给方案） |
| 政策层 | `05_brand_ref/` `06_platform_policy/` | 季度级变动，一改全军生效 | 售后/法务 | **能不能、多少钱**（保修、退换、收费、拆封） |
| 编排层 | `07_dialogue/` | 跟场景层同步 | 产品/AI | 意图**路由到哪**、缺槽位怎么补 |
| 检索层 | `08_index/` | 由上面四层**自动生成** | 机器 | 切片、同义词、召回清单 |
| 评测层 | `09_eval/` | 随知识变更回归 | 测试 | 改完**有没有变傻** |

**硬约束**：场景层不许出现具体型号参数；事实层不许出现话术。两边的连接只发生在检索时（靠 `product_id` 拼装）。违反这条，参数一改就要回来改 30 篇话术。

---

## 2. 六个场景≠六个文档目录

同样是"六个场景"，每类知识的来源构成完全不同。这决定了每个场景目录里该放什么、不该放什么：

| 场景 | 事实层占比 | 场景层占比 | 政策层占比 | 外部工具 | 该场景真正的难点 |
|---|---|---|---|---|---|
| 售前咨询 | **高**（参数/功能/适配） | 中（对比与推荐逻辑） | 低（促销、以旧换新） | 商品库、价格库存 | 型号歧义：用户只说"空调"，要澄清到具体型号才能报参数 |
| 安装咨询 | **高**（安装条件） | 中（预约流程、上门前确认） | 中（辅材/高空/远程收费） | 安装预约、地址服务 | 收费项必须提前说清，事后争议高发 |
| 使用咨询 | **高**（操作/保养/注意） | **高**（分步引导） | 无 | 说明书配图 | 用户描述口语化，"滤网在哪"要能落到具体位置 |
| 故障排查 | **高**（现象/代码） | **高**（决策树 + 正常现象判别） | 中（是否在保修内） | 工单、远程诊断 | 边界：哪些能自查，哪些必须停机报修（涉及安全） |
| 售后政策 | 低（引用保修期） | 低 | **高**（退换/运费/二次销售） | 订单、物流 | 平台规则与厂家保修是两套体系，不能串答 |
| 报修建单 | 中（备件、型号） | 中（必填槽位） | 中（收费标准） | **工单系统 API** | 本质是表单填写，知识只负责"该问哪些字段" |

结论：`s6_service` 的 `ticket_fields.yaml` 比任何 FAQ 都重要；`s5_aftersale` 的答案基本全部来自 `06_platform_policy` + `05_brand_ref/warranty.yaml`，自身只留"怎么判断适用哪条政策"。

---

## 3. 字段契约

### 3.1 `03_product/{品类}/{品牌}/{型号}/product.yaml` — 身份卡
```yaml
product_id: midea-kfr-35gw-q2     # 必填，全局唯一，命名见 naming_and_ids.md
status: ready                     # 必填 ready | skeleton | deprecated
appliance: air_conditioner        # 必填 品类目录名
appliance_cn: 空调
brand: midea                      # 必填
brand_cn: 美的
model: KFR-35GW/Q2                # 必填 原始型号（含斜杠，照抄铭牌）
model_slug: KFR-35GW-Q2           # 必填 目录名，去掉斜杠
indoor_model: KFR-35G/Q2          # 选填 室内机型号
outdoor_model: KFR-35W/X258       # 选填 室外机型号
product_type: 分体挂壁式（变频）    # 必填 形态，用于"壁挂/柜机"类提问
series: null                      # 选填 系列
colloquial: "1.5匹"                # 选填 用户口头叫法，检索加权用
launch_year: null
source_docs:                      # 必填 资料来源
  - file: 美的-KFR-35GW-Q2-使用说明书.pdf
    pages: 38
```

### 3.2 `specs.yaml` — 参数（归一化 + 原始值双写）
```yaml
specs:
  cooling_capacity_w:   {value: 3510, min: 150, max: 5250, unit: W, source: p30}
  efficiency_apf:       {value: 5.32, unit: APF, source: p30}
  noise_indoor_db:      {min: 18, standard: 35, max: 41, unit: dB(A), source: p30}
  size_indoor_mm:       {w: 876, h: 305, d: 215, unit: mm, source: p30,
                         note: "说明书标注为(宽×高×深)，以铭牌复核"}
missing: [weight_indoor_kg]        # 说明书未给/仅"见铭牌"的项，进 backlog
```
规则：
- 所有键名来自 `spec_dictionary/{品类}.yaml`，**不许自创**；字典里没有就先加字典。
- 三段式数值（额定/最小/最大）必须拆成 `value/min/max`，不许塞成一个字符串。
- 尺寸一律拆 `w/h/d`。海尔按"宽×深×高"排、美的按"宽×高×深"排，只有拆成具名键才不会串位。
- 每条必须带 `source`（页码），便于客服核对、也便于回答时给"说明书第 30 页"。

**`source` 的三种锚点**（取决于这份资料是怎么进来的）：

| 资料形态 | source 写法 | 例子 |
|---|---|---|
| PDF 有正常文本层 | `02_ingest/{品类}/{品牌}/{型号}/text.md#p{页}` | `.../KFR-35GW-E1-1/text.md#p13` |
| PDF 全部/部分页是扫描件 | 同上，但页码来自 `ocr.md` 的锚点 | `.../F40H8093S/ocr.md#p3` |
| 纯图片资料（JPG/PNG） | `02_ingest/{品类}/{品牌}/{型号}/ocr.md#img:{文件名}` | `.../TG10K60/ocr.md#img:产品介绍.jpg` |

三条硬约束：

- **OCR 文本不得作为唯一依据直接落库**。OCR 会把 `0` 认成 `O`、`8` 认成 `B`、`1.5` 认成 `15`，
  回填参数前必须对照原图；对不上就进 `missing`，不要二选一猜。
- **文本层损坏（乱码）的 PDF 视为"无文本层"**。`ingest.py` 会做常用字比例检测并打 `text_garbled`，
  这类文件抽出的字符数看着正常，但全是乱码，必须整本 OCR。已发现：西门子 `WM12P2602W`、`WN54A1X42W`。
- `confidence: inferred` 的项必须同时写 `note` 说明怎么推出来的（表格错位、单位换行、跨页续接），
  否则三个月后没人能复核。

**`confidence` 的四个取值**（2026-10-23 增加 `web`）：

| 值 | 含义 | 能不能对外答 |
|---|---|---|
| `confirmed` | 说明书原文直接写明，已对照过原页/原图 | 可以，可报页码 |
| `inferred` | 由型号编码、水位线刻度、同平台机型推算；`note` 必须写清推理链 | 可以，但被追问依据时要承认是推断 |
| `web` | 品牌官网/电商详情页/官方服务资料，**非**本批说明书原文 | 可以，但被追问"说明书哪页"时要说明是网络值，并以铭牌为准 |
| 不写 | 与 `confirmed` 同义（老条目沿用） | 可以 |

`web` 值一律同时在 `specs.yaml` 末尾列进 `pending_verify:`，因为"说明书里查不到、网上查到了"
这类参数（电饭煲的尺寸/净重尤其典型）在退货、安放、安装纠纷里最容易被质疑，必须留一条待核销的账。

### 3.3 `faults.yaml` — 只放型号差异

故障**现象**的排查路径跨型号高度重合（十个型号的"不制冷"九成相同），所以现象主库放在
`04_scenario/s4_troubleshoot/symptoms.yaml`，型号目录只放这个型号特有的东西：

```yaml
codes:                        # 本型号是否有代码显示、代码类别、处置流程
  has_code_display: true
  code_prefixes: [E, F, P]
  rule: [关机断电, 等 10 分钟, 上电开机, 仍显示则报修]
protection:                   # 保护性停机的温度区间（各品牌阈值不同）
  cooling: {outdoor_max_c: 65, outdoor_min_c: -18, source: p31}
normal_phenomena: []          # 说明书明确"属正常"的现象，用于拦截假故障工单
model_specific_notes: []      # 功能有无导致的差异（如无自清洁则不能给自清洁步骤）
missing: []
```

现象主库的结构：
```yaml
- id: water_leak_indoor
  user_phrases: [漏水, 滴水, 地上有水]        # 召回用，覆盖口语
  severity: stop_and_call                    # normal | attention | stop_and_call
  triage: [...]                              # 判定链，命中即停
  self_help: []                              # 可引导的自查步骤（安全项必须为空）
  escalate_when: [立即]
  source: 海尔 p30
```
`severity` 直接决定答话姿态，也是 `handoff_rules` 的触发依据。**安全类现象（漏电、焦味、漏水、异响）必须 `stop_and_call`，不允许引导用户继续开机自检。**

代码表在 `05_brand_ref/{brand}/fault_codes.yaml`（品牌级，与型号无关）。本批两家说明书都没有逐码释义，所以该文件是空表 + 处置规则：用户报码一律不猜含义，直接建单。

### 3.4 `04_scenario/s*/faq.yaml` — 标准问答条目
```yaml
- id: faq-s4.003
  question: 空调显示 E1 是什么故障
  intent: troubleshoot.code
  scope: {appliance: air_conditioner, brand: [midea], model: []}   # 空=不限
  answer: |
    正文，≤120 字，先结论后步骤
  steps: [断电 10 分钟, 重新上电开机, 仍显示代码→报修]
  handoff: {needed: true, reason: code_persist}
  source: 05_brand_ref/midea/fault_codes.yaml#midea.ac.e1
  updated: 2026-10-21
```

### 3.5 `05_brand_ref` / `06_platform_policy` — 政策
```yaml
policy_id: platform.return.seven_days
applies_to: {channel: [自营, 平台], appliance: [air_conditioner]}
rule: "签收次日起 7 天内可申请无理由退货"
conditions: [不影响二次销售, 包装附件齐全]
exclusions: [已安装使用, 已拆封安装辅材]
fee: {return_shipping: 由责任方承担}
effective_from: 2026-01-01
source: "待法务确认"        # 政策类必须可溯源，未确认写"待确认"，禁止编造
```
**政策类字段禁止估算**。金额、期限、免责条款没有正式来源就写 `source: 待确认` + `verified: false`，检索层会把它降权并强制话术加"以页面/客服最终告知为准"。

### 3.6 `08_index/chunks/*.md` — 检索切片（自动生成）
```markdown
---
chunk_id: midea-kfr-35gw-q2@s1#002
product_id: midea-kfr-35gw-q2
appliance: air_conditioner
brand: midea
scenario: s1_presale
intent: presale.spec
doc_type: fact          # fact | faq | playbook | policy
source: 03_product/.../specs.yaml#efficiency_apf
source_page: 30
verified: true
---
制冷量 3510W（150~5250W），APF 5.32，能效等级 1 级……
```

---

## 4. 缺什么就往哪放

| 遇到的情况 | 放哪 |
|---|---|
| 新型号说明书 | `01_source/` 放原件 → 跑 `tools/ingest.py` → 建 `03_product/` 卡 |
| 品牌新增故障代码表 | `05_brand_ref/{brand}/fault_codes.yaml` |
| 平台改了退换规则 | `06_platform_policy/` + 改 `effective_from` |
| 客服发现一种新的用户说法 | `08_index/synonyms.yaml`（不改 FAQ 正文） |
| 一句新话术 | 对应 `s*/answers.md` |
| 说明书没写的参数 | `specs.yaml` 留 `null` + 进 `10_ops/backlog.md`，**不许估算** |
| 新增品类（洗衣机/冰箱…） | 见 `README.md` 第 4 节 |
