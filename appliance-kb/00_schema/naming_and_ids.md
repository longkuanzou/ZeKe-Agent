# 命名与 ID 规范

## 目录

```
01_source/{品类}/{品牌}/{型号目录}/
03_product/{品类}/{品牌}/{型号目录}/     # 前三级与 01_source 完全同名，靠路径即可对齐
```
- 品类、品牌目录名：小写 ASCII，下划线分词 → `air_conditioner` `washer` `refrigerator` `rice_cooker` `microwave`
- 型号目录名：即为 `model_slug`，大写字母、数字保留，`/` → `-`，`/.` → `-`
  - 原始 `KFR-35GW/Q2` → 目录 `KFR-35GW-Q2`
  - 原始 `KFR-35GW/E1-1Plus` → 目录 `KFR-35GW-E1-1Plus`
- 以 `_` 开头的目录/文件是模板或报告，索引脚本跳过：`_manifest.yaml` `_template/`

## ID

| 对象 | 格式 | 例 |
|---|---|---|
| 型号 | `{brand}-{model_slug}` 全小写 | `midea-kfr-35gw-q2`、`haier-kfr-35gw-e1-1` |
| 事实条目 | `{product_id}.{组}.{键}` | `midea-kfr-35gw-q2.specs.efficiency_apf` |
| FAQ | `faq-{场景号}.{三位序号}` | `faq-s4.003` |
| 故障码 | `{brand}.{品类缩写}.{code}` 小写 | `midea.ac.e1` |
| 政策 | `{层级}.{主题}.{子项}` | `platform.return.seven_days`、`brand.haier.warranty` |
| 切片 | `{product_id 或 global}@{场景}#{序号}` | `midea-kfr-35gw-q2@s1#002` |
| 意图 | `{场景}.{动作}` 全小写 | `presale.spec`、`troubleshoot.code`、`service.create_ticket` |

品类缩写：`ac` 空调 / `wm` 洗衣机 / `rf` 冰箱 / `rc` 电饭煲 / `mw` 微波炉

## 单位与数值（强约束）

1. 一律 SI 基本单位，不写"匹""度""公分"：`W` `mm` `kg` `m³/h` `dB(A)` `℃` `元`。
   匹数只作为 `colloquial` 里的检索别名，不参与任何计算或对比。
2. 区间必须拆 `min / value(额定) / max`，禁止 `"3510(150~5250)"` 这种整串存法。
3. 尺寸必须拆 `w / h / d` 三个具名键。不同品牌标注顺序不一致（海尔"宽×深×高"、美的"宽×高×深"），整串存必错位。
4. 缺失写 `null` 并列入 `missing`，禁止估算、禁止从同系列机型"借用"。
5. 金额/期限类政策字段必须同时带 `effective_from` 与 `source`；无正式来源写 `source: 待确认` + `verified: false`。

## 更新与生效

- 事实层文件头部保留 `updated: YYYY-MM-DD`。
- 政策层不覆盖旧值，改为新增一条带 `effective_from` 的记录；检索层只召回 `effective_from <= 今天` 的最新一条。
- 每次批量改动记 `10_ops/changelog.md`，测试集回归结果记 `09_eval/report/`。
