# 单一事实来源（Single Source of Truth）

> 路径约定：本文所有路径相对 `appliance-kb/`，脚本引用相对仓库根目录。

## 为什么要有这份契约

同一个事实如果在多层各写一遍，检索会同时召回多份「都正确但不同名」的文档：

- 售后评测 15 次命中里只有 2 次排第 1；
- 排在正确文档之前的 171 个候选里，`faq` 84 个、`playbook` 53 个；
- 政策 `platform.return` 曾被按品类展开成 5 个切片，规则文本完全相同。

这不是检索算法坏了，而是**没有唯一权威**。任何一次改写都可能只改了一份，另一份继续被检索命中。

## 权威层矩阵（唯一事实来源）

| 事实类型 | 唯一权威 | 其他层只许写 | 机器判据 |
| --- | --- | --- | --- |
| 型号参数（容量/能效/尺寸/重量/噪音/冷媒/电源…） | `03_product/**/specs.yaml` → 派生**型号参数卡**（每型 1 篇，`doc_type=fact`） | `faq` 只写「结论一句 + 见参数卡/用 `get_product`」；`playbook` 不写参数数值 | 参数卡覆盖 `00_schema/spec_dictionary/<appliance>.yaml` 的全部已填键 |
| 品牌政策（保修/收费/故障码/备件） | `05_brand_ref/<brand>/*.yaml`，**每个 `policy_id` 只产生 1 个切片** | `faq` 给结论 + `policy_id` 指针；`playbook` 只写「怎么用」 | 切片清单里 `policy_id` 不得重复 |
| 平台政策（退换/运费/拆封认定/安装收费） | `06_platform_policy/*.yaml`，每个 `policy_id` 1 个切片 | 同上 | 同上；运费口径只在 `platform.shipping` 定义 |
| 症状与处置 | 「现象 + 用户说法 + 解释」→ `04_scenario/s4_troubleshoot/symptoms.yaml`；「处置流程」→ `playbook`（**两类不同事实，可并存**） | `faq` 只给结论 + 指针 | 症状切片不得复制政策数值；FAQ 不得整段复述症状解释 |
| 内部作业规则（判断链/必填字段/红线） | `04_scenario/*/playbook.md` | 不进用户答案候选（生成包打 `acl_tags: [kb:internal]`） | 生成包中 playbook 必须带该标签 |

### 两条推论（容易踩）

1. **多品类 ≠ 多副本**。一条政策适用于 5 个品类，应写成一份 `applies_to.appliance: [5 个]`，由 `build_index.py` 产出**一个**切片、`appliance=null`（无归属的通用文档在过滤时始终保留）；**不要**按品类展开成 5 个 `@appliance` 切片。
2. **指针必须唯一**。FAQ 写「详见 `platform.return`」之前，`platform.return` 必须已经只剩一个切片；否则指针无处可指。

## FAQ 的定位（入口，不是答案）

- 每条 FAQ 的 `answer` 只允许：**一句结论 + 一个指针**（`详见 <policy_id>` 或 `见 <doc 相对路径>#<锚点>`）。
- 不再复述政策数值（期限、金额、条件清单）。原因：复述层与权威层一旦漂移，用户会同时拿到两套说法。
- 多品牌/多品类必须拆成多条单一口径（`scope: {appliance: washer, brand: [haier]}` 这种列表只允许在**结论完全相同**时使用）。

## 变更顺序（不可打乱）

1. 先合并副本（改 `build_index.py` 的展开逻辑），让每个 `policy_id` 只剩一个切片；
2. 再改 FAQ 为指针（`04_scenario/*/faq.yaml`）；
3. 再生成参数卡（`tools/build_cards.py`），gold 改指向卡；
4. 最后重建索引与评测集。

顺序反了会出现：FAQ 指向的 `policy_id` 还有多份；参数卡的 `doc_id` 与旧 gold 同时存在。

## 校验

```bash
python appliance-kb/tools/validate.py --strict      # 契约 + 单一来源规则
python appliance-kb/tools/build_index.py            # 重建切片清单（08_index 是生成物，不手改）
```

`validate.py` 的单一来源规则：

| 规则 | 级别 |
| --- | --- |
| 同一 `policy_id` 在切片清单里出现多次 | error |
| FAQ `answer` 不含指针 / 长度超过阈值 | error（迁移完成后） |
| `playbook` 正文出现参数或政策数值 | error（迁移完成后） |
| 参数卡未覆盖 `spec_dictionary` 中已填键 | error（迁移完成后） |

> 迁移期间后三条先以 warn 输出，迁移完成后改为 error；改级别时必须同步更新本文。
