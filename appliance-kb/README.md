# 家电客服知识问答库 · appliance-kb

基于现有说明书构建的客服问答知识底座。

已接入三个品类、5 个品牌（海尔 / 美的 / 小天鹅 / 西门子 / 苏泊尔）、28 个型号：

| 品类 | 品牌 | 型号数 | 状态 |
|---|---|---|---|
| 空调 | 海尔 5 / 美的 5 | 10 | **全部 ready**（五件套齐；23 项推断值待核，见 `10_ops/confirm_queue.md`） |
| 洗衣机 | 小天鹅 5 / 西门子 5 | 10 | **全部 ready**（小天鹅 4 个 + 西门子 5 个由回填脚本生成） |
| 电饭煲 | 美的 3 / 苏泊尔 5 | 8 | **全部 ready**（参数缺口较多，见下表说明） |

28 个型号 × 5 个文件（specs / features / install / usage / faults）全部齐备，
资料形态涵盖纯图片、扫描页、以及两份字体损坏的乱码 PDF，均已 OCR 成可检索文本。

**ready 不等于参数齐全**。洗衣机与电饭煲的参数缺口集中在两类：

| 缺口类型 | 涉及型号 | 原因 |
|---|---|---|
| 尺寸 / 净重全缺 | 电饭煲 8 个全部 | 5 份资料根本没有参数页，只讲结构+操作+故障 |
| 容量为推断值 | 电饭煲 7 个（仅 MB-40HB9T 是原值） | 靠型号编码或水位线最大刻度推断，标 `confidence: inferred` |
| 噪声 / 干衣容量 | 西门子 WM12P2602W、WN54A1X42W 等 | 技术数据页本身没给 |

这些 null 是**故意留的**：缺就写 null 并进 `missing`，绝不拿同品牌同容积机型代填——
尺寸代填错了，直接就是"放不进去"的退换货。

结构上按品类可扩展到冰箱、微波炉。

设计目标只有一个：**让机器答得对，且答错的时候能查出来是谁的错**。

---

## 1. 目录总览

```
appliance-kb/
├── 00_schema/                数据契约：分层规则、字段形状、命名规范
│   ├── README.md             字段契约总表（先读这个）
│   ├── naming_and_ids.md     ID / 目录 / 单位规范
│   ├── single_source.md      单一事实来源规则（一条事实只存一处，切片怎么切）
│   ├── aliases.yaml          用户口语别名：政策 / 参数字段 / 安装小节（生成切片时拼进正文）
│   └── spec_dictionary/      参数标准字典（跨品牌对齐的关键）
│       ├── air_conditioner.yaml   空调 40+ 个标准键 + 说明书原文别名映射
│       ├── washer.yaml            洗衣机 26 个键（重点在尺寸/门宽/排水方式）
│       ├── rice_cooker.yaml       电饭煲 18 个键（容积/加热方式/内胆）
│       └── _template.yaml         新品类模板（含冰箱/微波炉最小键集）
│
├── 01_source/                原始资料（只读，不改）
│   ├── _manifest.yaml        型号清单（脚本生成）
│   └── {品类}/{品牌}/{型号}/  说明书 PDF / 拍照版 JPG
│
├── 02_ingest/                抽取中间产物（可重跑、可丢弃）
│   └── {品类}/{品牌}/{型号}/
│       ├── text.md           PDF 文本（带 <a id="pN"> 页码锚点）
│       ├── ocr.md            OCR 结果（图片资料 / 扫描页 / 乱码 PDF，带 img:/pN 锚点）
│       ├── ocr/*.txt         单文件 OCR 缓存，重跑不重复识别
│       └── report.json       抽取质量：页数、字符数、扫描页、是否乱码
│
├── 03_product/               ① 事实层：型号级，唯一事实来源
│   └── {品类}/{品牌}/{型号}/
│       ├── product.yaml      身份卡（状态 ready / skeleton）
│       ├── specs.yaml        参数（归一化键 + 原始值 + 页码）
│       ├── features.yaml     功能有无（true/false/null 三态）
│       ├── install.yaml      安装条件（电/位/管 三查）
│       ├── usage.yaml        操作与保养
│       └── faults.yaml       型号差异（代码规则、保护温度、特殊现象）
│
├── 04_scenario/              ② 场景层：跨型号，剧本 + 问答 + 槽位
│   ├── _shared/              全场景共用
│   │   ├── intent_map.yaml       意图 → 场景 → 必填槽位
│   │   ├── handoff_rules.yaml    转人工/转工单的硬性触发条件
│   │   ├── safety_redlines.md    安全红线（优先级高于一切话术）
│   │   └── answer_style.md       回答骨架与措辞替换表
│   ├── s1_presale/           售前：playbook / faq / slots / compare
│   ├── s2_install/           安装：playbook / faq / slots
│   │   └── by_appliance/     品类差异剧本（washer.md：电/水/排水/门 四查）
│   ├── s3_usage/             使用：playbook / faq / slots
│   ├── s4_troubleshoot/      故障：playbook / faq / slots / symptoms（现象主库）
│   ├── s5_aftersale/         售后政策：playbook / faq / slots
│   └── s6_service/           报修：playbook / faq / slots / ticket_fields
│
├── 05_brand_ref/             ③ 政策层（品牌级）：一次维护、全型号生效
│   └── {品牌}/ brand / warranty / fee_schedule / fault_codes / parts_catalog
├── 06_platform_policy/       ③ 政策层（平台级）：退换 / 拆封 / 运费
│
├── 07_dialogue/              ④ 编排层
│   ├── routing_matrix.yaml   意图 → 知识源 → 工具（防止全库瞎搜）
│   ├── slot_schema.yaml      槽位字典（按品类打标签，新品类只加槽位）
│   └── disambiguation.yaml   型号澄清（"空调" → 哪个型号）
│
├── 08_index/                 ⑤ 检索层（全部由脚本生成，不手改）
│   ├── manifest.jsonl        切片清单
│   ├── chunks/{fact,faq,playbook,policy,identity}/
│   ├── synonyms.yaml         检索期同义词（查询时归一，按品类分区）
│   ├── glossary.yaml         术语表（带白话解释）
│   └── _build/report.md      构建报告 + 未验证切片清单
│
├── 09_eval/                  ⑥ 评测集（198 条，由脚本生成，勿手改）
│   ├── questions.jsonl           三品类 × 六场景 + 21 条对抗样本
│   ├── 答案待确认清单.md          按答案凭据等级分组的人读版（需要业务方逐条核）
│   ├── 答案确认页.html          自包含确认工作台（逐条勾 + 改 + 导出 JSON）
│   └── _audit.json              答案审计报告（audit_eval.py 输出）
├── 10_ops/                   运维：coverage（生成）/ backlog / changelog / confirm_queue
└── tools/
    ├── ingest.py             PDF/JPG → text.md + report.json（含乱码检测、型号清单）
    ├── ocr.py                图片/扫描页/乱码 PDF → ocr.md（带缓存，可断点续跑）
    ├── scaffold_products.py  按 01_source 批量补型号身份卡（参数一律不猜）
    ├── validate.py           契约校验 + 生成覆盖度报告
    ├── build_index.py        切片 + manifest（政策一 policy_id 一片、一型号一张参数卡、
    │                         FAQ 指针化、`slots.yaml` 不建切片——见第 2 节末）
    ├── dump_compare.py       按 compare.yaml 声明的组打印当前参数（改完 specs 用来对表）
    ├── gen_eval.py           生成评测集 + 答案待确认清单（--dry 只统计）
    ├── check_eval.py         评测集自检 9 项（E6「禁止项不得出现在标准答案里」最容易踩）
    ├── audit_eval.py         标准答案审计 6 项（查答案对不对，不查自洽）
    ├── build_review_html.py  生成 09_eval/答案确认页.html（单文件、零外部依赖）
    └── backfill_*.py         参数回填脚本，DATA 字典即数据源：
        ├── backfill_ac.py              空调 10 个（首选模板）
        ├── backfill_washer_sw.py       小天鹅 4 个
        ├── backfill_washer_siemens.py  西门子 5 个
        └── backfill_rice_cooker.py     电饭煲 8 个
```

两个评测工具分工明确，别混用：

| 工具 | 查什么 | 典型输出 |
|---|---|---|
| `check_eval.py` | 评测集**自身自洽**（id 唯一、出处文件存在、必须项在答案里、禁止项不在答案里） | 117 → 0 error（E6 那次最典型） |
| `audit_eval.py` | 标准答案**对不对**（把答案拿回源文件核对数值溯源、依据等级、品类匹配） | 6 处真错（凭空数字、出处张冠李戴、默认值映射错） |

参数回填都用 `tools/backfill_*.py` 而不是手写 YAML：**数据留在脚本里，改一个值重跑就生效**，
`source` 锚点与 `missing` 清单由代码统一生成，不会出现"某个型号忘了写 source"这种漏项。
新增型号的做法是往 `DATA` 里加一个键，不是复制目录再手改。

---

## 2. 为什么这样分层

知识库最常见的烂法是：一篇文档里既有"制冷量 3510W"，又有"建议您先清洗滤网"，还有"七天无理由需不影响二次销售"。写起来快，三个月后无法维护。

根本原因不是文档写得乱，是**这三类知识的变更频率差了一个数量级**：

| 层 | 变更频率 | 谁维护 | 冲突时以谁为准 |
|---|---|---|---|
| 事实层 `03_product` | 上新机型时线性增长，老机型冻结 | 资料/运营 | 说明书页码 |
| 场景层 `04_scenario` | 每周改（话术、追问、边界） | 客服运营 | 安全红线 > 场景剧本 |
| 政策层 `05`/`06` | 季度级，一改全军生效 | 售后/法务 | 品牌条款 > 平台规则 > 话术 |

**硬约束**：场景层不写具体型号参数，事实层不写话术。两边只在检索时靠 `product_id` 拼装。违反这条，改一次参数就要回头改三十篇话术。

### 切片形态（`build_index.py` 的落地方式）

1622 篇源文件被压成 **684 条切片**，规则都在 `00_schema/single_source.md`：

| 源 | 切片 | 理由 |
| --- | --- | --- |
| `05`/`06` 政策 | 一条 `policy_id` 一片（不再按品类/品牌展开副本） | 同一条退换规则展开成 N 份会互相竞争，top-5 被自己的副本占满 |
| `03_product/**/specs+features` | 一型号**一张参数卡**，按字段切子块 | 用户问的是「这台多重」，必须落到具体型号，不能跨型号串参数 |
| `04_scenario/s1,s5/faq` | **不建切片**，问法拼进参数卡与政策的「用户常问」 | 售前/售后 FAQ 只是问法，答案在卡/政策里 |
| `04_scenario/s2,s3,s4,s6/faq`（指针型） | **不建切片**，问法拼进它指向的 fact 切片 | 答案在 `usage.yaml#maintenance` 这类权威文档里 |
| `04_scenario/*/slots.yaml` | **不建切片** | 它是追问顺序与话术约束，不是可检索的事实（见 `docs/KNOWN_ISSUES.md` M28） |
| `04_scenario/*/playbook` | 按 `## ` 小节切 | 内部剧本，`knowledge/meta.yaml` 派生 `acl_tags: [internal]`，检索默认不可见 |

副产物是体积：1622 → 684 片，同一事实不再有 3–4 份副本。

---

## 3. 六个场景：不是六个文件夹，是六种知识配比

用户给的六个场景，知识来源构成完全不同。这决定了每个目录里该放什么：

| 场景 | 事实层 | 场景层 | 政策层 | 外部工具 | 真正的难点 | 本库的应对 |
|---|---|---|---|---|---|---|
| 售前咨询 | 高 | 中 | 低 | 商品/价格 | 型号歧义 | `disambiguation.yaml` 强制先收型号；无型号只给档位 |
| 安装咨询 | 高 | 中 | 中 | 预约系统 | 收费争议 | 三查（电/位/管）+ 只报项目不报总价 |
| 使用咨询 | 高 | 高 | 无 | 说明书配图 | 步骤太密 | 一次只给一个动作，步骤照抄不改序 |
| 故障排查 | 高 | 高 | 中 | 工单/远程诊断 | 假故障与安全边界 | `symptoms.yaml` 现象主库 + severity 分诊 + 五步法 |
| 售后政策 | 低 | 低 | **高** | 订单/物流 | 两套规则串答 | 渠道优先判定；平台规则与品牌保修分开存 |
| 报修建单 | 中 | 中 | 中 | **工单 API** | 表单字段缺失 | `ticket_fields.yaml` 八项必填 + 去重 + 加急标记 |

所以 `s6_service` 里最重要的是 `ticket_fields.yaml` 而不是 FAQ；`s5_aftersale` 的回答几乎全部来自政策层，自身只负责"判断适用哪条"。

---

## 4. 一次问答怎么走完

以"空调不制冷"为例：

```
用户: 空调不制冷
  ↓ 07_dialogue/disambiguation.yaml      无型号 → 先问型号（或机型标签照片）
  ↓ 04_scenario/_shared/intent_map.yaml  归类 troubleshoot.symptom
  ↓ 07_dialogue/routing_matrix.yaml      路由到 s4，检索范围限定 s4 + 该型号
  ↓ 04_scenario/s4_troubleshoot/symptoms.yaml#not_cooling
      ① 安全判定：不是红线
      ② 正常现象过滤：不制冷不在"正常"清单
      ③ 环境/设置：模式？温度？滤网？
  ↓ 03_product/{model}/usage.yaml        取该型号的滤网清洗步骤
  ↓ 03_product/{model}/faults.yaml       取该型号的保护温度（判断是否工况外）
  → 回答：三步自查 + 下一步（仍未解决则建单）
  ↓ 命中"两轮无效" → s6 建单，把已排查项带进工单
```

每一步都能落到一个具体文件和字段上——这是"答错能查是谁的错"的前提。

---

## 5. 快速开始

```bash
# 依赖（pymupdf / pyyaml 必需；rapidocr-onnxruntime 只在需要 OCR 时装）
# 注意：脚本用了 yaml/fitz，跑之前先确认解释器装对了，别用系统 python
PY=~/.workbuddy/binaries/python/envs/default/Scripts/python.exe
$PY -m pip install -i https://mirrors.aliyun.com/pypi/simple/ pymupdf pyyaml rapidocr-onnxruntime

# 1. 新增说明书后：抽取（PDF → 带页码文本 + 抽取报告 + 更新型号清单）
$PY tools/ingest.py

# 2. 补型号身份卡（01_source 里有、03_product 里没有的，自动建 skeleton 卡）
$PY tools/scaffold_products.py
#    已 ready 的卡不会被覆盖（--force 也拦），避免手填内容被脚本冲掉

# 3. 需要时补 OCR（图片资料 / 扫描页 / 文本层乱码的 PDF）
$PY tools/ocr.py                    # 全量，带缓存，可断点续跑
$PY tools/ocr.py --model TG10K60    # 单个型号

# 4. 改完知识后：校验契约（参数必须带 source、政策必须声明 verified、ID 唯一…）
$PY tools/validate.py --strict

# 5. 重建检索切片与清单
$PY tools/build_index.py
#    重构过切法（如政策/FAQ 合并）后，旧切片会变成孤儿：默认只告警不删（Windows 一次删
#    几百个文件会被安全策略拦），确认无误后加 `--prune` 清一次，否则 chunks/ 会越堆越多

# 6. 生成/更新评测集（改了知识库之后必跑，答案跟着库走）
$PY tools/gen_eval.py               # 写 questions.jsonl + 答案待确认清单.md

# 7. 评测集自检：必须项要在答案里、禁止项不能在答案里、出处文件要真实存在、型号要全覆盖
$PY tools/check_eval.py --strict

# 8. 答案审计：把每条标准答案拿回源文件核对（查数值溯源/依据等级/品类匹配）
$PY tools/audit_eval.py            # 报告写到 09_eval/_audit.json

# 9. 生成确认工作台（给业务方逐条勾改用，单文件可邮件转发）
$PY tools/build_review_html.py     # → 09_eval/答案确认页.html

# 10. 回归评测集（在仓库根目录跑，检索指标见 docs/progress/p8.md）
#     逐条跑 09_eval/questions.jsonl，检查 must_include / must_not_include / handoff
zkagent --industry homeapp rag eval-retrieval --k 5 --verbose
```

当前状态：`validate ERROR 0 / WARN 0`、`check_eval ERROR 0 / WARN 0`、`audit_eval 0 error / 2 warn`，
28 个型号（28 ready / 0 skeleton），**684 条切片**，**198 条评测题**。

> 目录结构能不能建客服？见 `10_ops/backlog.md` 的「运行时缺口」一节——
> 结论是**知识层够了，运行时不够**：知识层已可直接支撑 RAG 检索，
> 但还缺接口契约层、会话/多轮上下文、评测执行器这三项 P0 资产。

> 解释器踩坑：Windows 上 `python` 可能指向系统 Python 或另一个版本，缺 `yaml`/`fitz` 会直接报
> `ModuleNotFoundError`。脚本本身不关心装在哪，但**必须用同一个解释器跑完 ingest / validate / build_index**，
> 否则会出现"ingest 抽了但 validate 读不到"的假故障。

---

## 6. 扩品类：实际走一遍之后的六步

洗衣机、电饭煲是按下面这套流程真接进来的，不是设想：

1. **放资料**：`01_source/{washer}/{brand}/{model}/`
2. **建字典**：复制 `_template.yaml` → `washer.yaml`，按该品类说明书参数表增删键。
   **先建字典再抽参数**——顺序反了就会出现"抽到一堆没地方放的数"。
3. **跑抽取**：`$PY tools/ingest.py`，看 `report.json` 的 `ocr_needed` / `text_garbled`
4. **补型号卡**：`$PY tools/scaffold_products.py`（只建身份卡，参数不猜）
5. **补 OCR**：`$PY tools/ocr.py`，然后把 OCR 结果**对照原图**回填 `specs/features/usage/faults/install`，填完把 `status` 改 `ready`
6. **补场景差异**：
   - `s2_install/by_appliance/{品类}.md` 写该品类的"查什么"（洗衣机是电/水/排水/门，不是电/位/管）
   - `04_scenario/s4_troubleshoot/symptoms.yaml` 增加该品类现象
   - `07_dialogue/disambiguation.yaml` 增加该品类的型号编码规则（各品类编码毫无共性）
   - `08_index/synonyms.yaml` 增加该品类的口语别名与易错归一

场景剧本的**骨架**（先判安全 → 再判是否正常现象 → 再给一步动作 → 再决定转人工）跨品类通用，变的只是"查什么"和槽位。

---

## 7. 八条踩坑得出的设计决策

1. **尺寸必须拆 w/h/d**。海尔按"宽×深×高"标、美的按"宽×高×深"标，整串存必然串位（海尔 E1-1 室内机 910×210×316，若按美的顺序读会把深度当高度）。
2. **断码不跨品牌推断**。E1 在美的、海尔、格力的含义并不通用。本库两家说明书都没有逐码释义，所以 `fault_codes.yaml` 是空表 + 处置规则，用户报码一律建单，不猜。
3. **"说明书没写"是一等公民**。`features.yaml` 用 true/false/null 三态，null 意思是"未标注"，不是"没有"。海尔 E1-1 的语音能力就是 null。
4. **政策必须带 verified**。金额、期限没有正式来源就 `verified: false`，检索层会降权且强制加免责话术。`validate.py` 会拦"verified=false 却写了具体价格"。
5. **正常现象要单列并拦截**。说明书里"流水声""噼啪声""关机不能立即启动"都是正常现象。不拦这一层，工单量会虚高，师傅白跑。
6. **最小房间面积不是推荐面积**。4㎡ 是 R32 冷媒的安全下限，不是"适合 4㎡ 的房间"。推荐面积只能按 150~200W/㎡ 估算，且必须标注"按经验估算"。
7. **故障现象放共享库，不放型号目录**。十个型号的"不制冷"排查路径九成相同，抄十遍必然漂移。型号目录只留差异。
8. **报修建单是一次问全，不是挤牙膏**。反复问同一字段是投诉主因；`slots.yaml` 设了 `batch_collect: true`。
9. **"抽到字"不等于"抽到内容"**。西门子 `WM12P2602W`/`WN54A1X42W` 的 PDF 字体没嵌 ToUnicode 表，PyMuPDF 抽出 2.2 万字全是乱码（`䊺墖䟂伩橵䥌`），字符数指标完全正常。`ingest.py` 现在用"常用字比例"检测：真实中文说明书每千字至少 20 个"的了是在不和有请使用安装"，乱码接近 0，低于 5 判为损坏并整本转 OCR。**只看字符数的质检，一定会漏这类资料。**
10. **型号卡脚本必须能拒绝覆盖**。`scaffold_products.py` 遇到 `status: ready` 的卡即使加 `--force` 也不动——脚本的价值是"补齐"不是"统一"，一旦它能覆盖人填的内容，就没人敢再手填。
11. **品类差异写差异文件，不写进通用剧本**。`s2_install/by_appliance/washer.md` 独立于 `playbook.md`，一是检索时能按 `appliance` 过滤，二是改空调的剧本不会碰坏洗衣机。

---

## 8. 维护节奏

| 频率 | 动作 | 责任人 |
|---|---|---|
| 上新机型 | 资料入库 → 建卡 → 补 compare_group | 资料 |
| 每周 | 从真实会话补 10 条同义词；把新出现的问题补成 FAQ | 客服运营 |
| 每季度 | 核对品牌保修/收费政策（改 `effective_from`，不覆盖旧值） | 售后 |
| 每次改动 | `validate --strict` + 重建索引 + 跑评测集 | 谁改谁跑 |

### 已知缺口
知识侧只剩 4 项硬缺口（小天鹅包修分档的分界年份、平台书面退换规则、西门子收费表列错位、MB-40HB9T 内胆材质）；
更重要的是**运行时缺口 8 项**——缺接口契约层、会话与多轮上下文、评测执行器，见 `10_ops/backlog.md` 的「运行时缺口」一节。

---

## 9. 版本管理

整个 `客服知识问答库/`（含 `appliance-kb/` 与 `.workbuddy/memory/`）是一个 git 仓库，基线提交见 `git log`。

### 什么入库、什么不入库

| 内容 | 入库 | 理由 |
|---|---|---|
| `01_source/**` 原始说明书 PDF/JPG（227MB） | ✅ | **一手资料，不可再生**。丢了整个知识库就失去溯源能力 |
| `02_ingest/**/*.md` 带页码抽取文本 | ✅ | 是「答案 → 切片 → 说明书页码」溯源链的中间一环 |
| `02_ingest/**/_pages/` PDF 渲染 PNG（264 个 / 73MB） | ❌ | OCR 中间物，`tools/ocr.py` 可随时重新渲染 |
| `08_index/**` 检索切片（约 7MB） | ✅ | 体量小，clone 下来不用装依赖、不用跑脚本就能直接检索 |
| `09_eval/**` 评测集与确认页 | ✅ | 评测基线要能 diff，"哪次改动让通过率掉了"才有据可查 |
| `.workbuddy/memory/**` 工作记忆 | ✅ | 记录每轮为什么这么改，比 commit message 更细 |

### 三条硬约定

1. **换行符统一 LF**。`.gitattributes` 里 `* text=auto eol=lf`，Windows 上也不会混进 CRLF。
   已存在的 CRLF 文件在下次 `git add` 时会被规范化。
2. **PDF/JPG 标记 `binary`**。不参与 diff、不计语言统计，避免 git 试图按文本合并。
3. **生成物标记 `linguist-generated`**。切片、评测集、审计报告、确认页、覆盖度报告在 diff 里折叠，
   不淹没真正手写的改动。

### 日常流程

```bash
cd 客服知识问答库

# 改完知识后：先跑校验，绿了再提交
PY=~/.workbuddy/binaries/python/envs/default/Scripts/python.exe
$PY appliance-kb/tools/validate.py --strict
$PY appliance-kb/tools/build_index.py          # 改了事实层/场景层才需要重建切片
$PY appliance-kb/tools/gen_eval.py             # 评测答案跟着库走
$PY appliance-kb/tools/check_eval.py --strict
$PY appliance-kb/tools/audit_eval.py

# 提交
git add -A
git commit -m "fix(midea): 柜机风量按核定改为 1760，说明书旧值降 cross_check_alt"
```

### commit message 约定

`<类型>(<范围>): <一句话>`

| 类型 | 用于 |
|---|---|
| `feat(<品类/品牌>)` | 新增型号、新增品牌、新增场景 |
| `fix(<层>)` | 改错值、改错出处、改错话术 |
| `data(<层>)` | 业务方补料入库（不改结构，只加数据） |
| `eval` | 评测集增删改、审计修正 |
| `tool` | 脚本改动 |
| `docs` | README / changelog / backlog |

范围用具体到能定位的名词：`midea-kfr-72lw` / `s5_aftersale` / `audit_eval` / `backlog`。

### 为什么不把切片排除、又为什么不用 LFS

- **切片入库**：约 7MB 换「clone 即可用」很值。而且切片是知识的一种呈现形式，
  改动它往往意味着内容真的变了，值得看一眼 diff。
- **暂不上 LFS**：最大单文件 42MB（苏泊尔 SF40FC871 说明书），未超 GitHub 100MB 单文件硬限；
  仓库总量 235MB 偏大但可推。**将来要推远端时**再考虑 `git lfs track "*.pdf"`——
  那时必须重写历史（`git lfs migrate import`），不是简单加一行配置。
