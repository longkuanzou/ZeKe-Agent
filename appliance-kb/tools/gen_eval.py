#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 09_eval/questions.jsonl（三品类 × 六场景 + 对抗样本）

为什么用脚本生成而不是手写 jsonl：
  1. id 自动编号，插一条不用重排后面所有 id；
  2. 每行都带 `answer` / `answer_basis` / `answer_ref` —— 评测集同时是一份
     "标准答案待确认清单"，业务方看这张表就能逐条核，核完直接改对应文件；
  3. `answer_basis` 强制写清这条答案的凭据等级（说明书原文 / 业务方口径 / 推断 /
     网络值 / 政策文件 / 库内缺失），避免"看起来像标准答案"其实是无源推断。

answer_basis 取值（与 00_schema/README.md 的 confidence 四档对齐，另加两种）：
  说明书原文    confidence: confirmed，可报页码
  业务方口径    biz_provided / biz_reference，业务方 2026-10-23 提供，未经售后核实
  推断          confidence: inferred，被追问要承认是推断
  网络值        confidence: web，非本批说明书
  政策文件      06_platform_policy / 05_brand_ref 下的政策类文件
  库内缺失      本库没有这项资料 —— 正确答案是"承认缺失 + 转人工"，不是编一个数

用法:
  python tools/gen_eval.py            # 写 09_eval/questions.jsonl
  python tools/gen_eval.py --dry      # 只统计不写盘
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "09_eval/questions.jsonl"

ROWS = []
_seq = Counter()


def q(sc, intent, question, *, ap=None, br=None, model=None, pid=None,
      inc=(), exc=(), src=None, handoff=False, ans="", basis="", note="", ref=None):
    _seq[sc] += 1
    ROWS.append({
        "id": f"e-{sc}-{_seq[sc]:03d}",
        "scenario": sc,
        "intent": intent,
        "appliance": ap,
        "brand": br,
        "model": model,
        "product_id": pid,
        "question": question,
        "expect": {
            "must_include": list(inc),
            "must_not_include": list(exc),
            "source_contains": src,
            "handoff": handoff,
            **({"note": note} if note else {}),
        },
        "answer": ans,
        "answer_basis": basis,
        **({"answer_ref": ref} if ref else {}),
    })


# ══════════════════════════════════════════════════════════════════ s1 售前
S1 = "s1_presale"

# ── 空调参数（型号有说明书值的才出题；KS2P / N8KS1-1P 参数未回填，只出功能题）
q(S1, "presale.spec", "这台制冷量多少？", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["3510"], exc=["5000"], src="specs.yaml#cooling_capacity_w",
  ans="制冷量 3510W（范围 150~5250W）。", basis="说明书原文")
q(S1, "presale.spec", "这台制冷量多少？", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["3510"], exc=["本机是 3500"], src="specs.yaml#cooling_capacity_w",
  ans="制冷量 3510W（150~5350W）。同品牌的 01KMC81U1 是 3500W，不要混。",
  basis="说明书原文", note="E1-1 与 01KMC81U1 差 10W，是两道不同的题")
q(S1, "presale.spec", "这台制热量多少？", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1Plus", pid="haier-kfr-35gw-e1-1plus",
  inc=["5420"], exc=["是 5110"], src="specs.yaml#heating_capacity_w",
  ans="制热量 5420W。Plus 版比 E1-1（5110W）高 310W，是这两台最实在的差别。",
  basis="说明书原文")
q(S1, "presale.spec", "3 匹柜机制冷量多少？", ap="air_conditioner", br="haier",
  model="KFR-72LW/E1-1", pid="haier-kfr-72lw-e1-1",
  inc=["7330"], exc=["本机是 7320"], src="specs.yaml#cooling_capacity_w",
  ans="制冷量 7330W（850~9270W）。美的 N8KS1-1P 是 7320W，两台几乎一样。",
  basis="说明书原文")
q(S1, "presale.spec", "3 匹柜机制冷量多少？", ap="air_conditioner", br="midea",
  model="KFR-72LW/N8KS1-1P", pid="midea-kfr-72lw-n8ks1-1p",
  inc=["7320"], exc=["7330"], src="specs.yaml#cooling_capacity_w",
  ans="制冷量 7320W（900~9260W）。", basis="说明书原文")
q(S1, "presale.spec", "循环风量多少？", ap="air_conditioner", br="midea",
  model="KFR-72LW/N8KS1-1P", pid="midea-kfr-72lw-n8ks1-1p",
  inc=["1760"], exc=["本机是 1600"], src="specs.yaml#airflow_m3h",
  ans="1760m³/h。这是 2026-10-23 核定后的值——本机是「酷省电Ultra」，官方标定 1760m³/h；"
      "本批说明书参数表里的 1600 沿用的是旧款「舒适星」的数据，已作废。",
  basis="业务方口径",
  note="核定推翻说明书值；说明书值降为 cross_check_alt 留档")
q(S1, "presale.spec", "循环风量多少？", ap="air_conditioner", br="haier",
  model="KFR-72LW/E1-1", pid="haier-kfr-72lw-e1-1",
  inc=["1600"], exc=["本机是 1750"], src="specs.yaml#airflow_m3h",
  ans="1600m³/h。同系列 Plus 版（E1-1Plus）是 1750m³/h，是推断值，别当说明书值报。",
  basis="说明书原文")
q(S1, "presale.spec", "这台室内机多大？能放进预留位吗", ap="air_conditioner", br="midea",
  model="KFR-72LW/N8KS1-1P", pid="midea-kfr-72lw-n8ks1-1p",
  inc=["380", "1802"], exc=["395", "1856"], src="specs.yaml#size_indoor_mm",
  ans="室内机 380×1802×392mm（宽×高×深）。这是说明书参数表里 51LW 与 72LW 共用的立柜机身尺寸；"
      "回答预留位要提醒以铭牌/实测为准。",
  basis="说明书原文",
  note="业务方官方规格页给 395×1856×436，冲突不采用")
q(S1, "presale.spec", "这台室内机多大？", ap="air_conditioner", br="haier",
  model="KFR-72LW/E1-1", pid="haier-kfr-72lw-e1-1",
  inc=["424", "1830", "364"], exc=["本机是 364×1830×424"], src="specs.yaml#size_indoor_mm",
  ans="室内机 424×1830×364mm（宽×高×深）。说明书 p35 印的是 364×1830×424（宽深互换），"
      "2026-10-23 经海尔官方知识库 + 苏宁自营规格页交叉核定，确认实机是 424 宽 / 364 深，"
      "说明书那张表是列序问题。装柜留 5cm 以上间隙。",
  basis="业务方口径",
  note="核定推翻说明书值；这是「官方两套数据方向相反」的典型，结论已定，不要再让用户去实测")
q(S1, "presale.spec", "这台是几级能效？", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["1"], exc=["3"], src="specs.yaml#energy_grade",
  ans="1 级能效。", basis="说明书原文")
q(S1, "presale.spec", "这台是几级能效？", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["能效标识"], exc=["是 1 级", "三级"], src="specs.yaml#energy_grade",
  handoff=True,
  ans="这台说明书里没有能效等级这一项，我不敢按经验给。要看机身能效标识或购机凭证上的能效等级。",
  basis="库内缺失", note="数据缺失时必须承认缺失，不能替它补")
q(S1, "presale.spec", "这台用多少冷媒？", ap="air_conditioner", br="midea",
  model="KFR-72LW/N8KS1-1P", pid="midea-kfr-72lw-n8ks1-1p",
  inc=["R32", "1170"], exc=["本机是 1120"], src="specs.yaml#refrigerant_charge_g",
  ans="R32，充注量 1170g（51LW 那台是 1120g，别串）。", basis="说明书原文")

# ── 洗衣机参数
q(S1, "presale.spec", "这台排水方式是上排水还是下排水？", ap="washer", br="littleswan",
  model="TB100V26DT", pid="littleswan-tb100v26dt",
  inc=["下排水", "地漏"], exc=["是上排水"], src="specs.yaml#drain_type",
  ans="下排水。安装位置必须有地漏才能排，不能套「上排水不需要地漏」那条规则。",
  basis="业务方口径", note="说明书未标上下排水，业务方按实物确认")
q(S1, "presale.spec", "这台排水方式是上排水还是下排水？", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["上排水"], exc=["下排水"], src="specs.yaml#drain_type",
  ans="上排水（滚筒机默认），排水管挂高 60~90cm 排到地漏或水槽，不需要地面地漏。",
  basis="说明书原文")
q(S1, "presale.spec", "这台洗涤容量多大？", ap="washer", br="siemens",
  model="WN54A1X42W", pid="siemens-wn54a1x42w",
  inc=["10"], exc=["7kg 洗"], src="specs.yaml#capacity_kg",
  ans="洗涤 10kg、烘干 7kg。洗烘一体的烘干容量一定小于洗涤容量，一次洗满就得分成两次烘。",
  basis="说明书原文")
q(S1, "presale.spec", "这台烘干容量多少？", ap="washer", br="siemens",
  model="WQ53E2D01W+WQ53E2D11W", pid="siemens-wq53e2d01w-wq53e2d11w",
  inc=["10"], exc=["7"], src="specs.yaml#dry_capacity_kg",
  ans="纯干衣机，烘干容量 10kg（无洗涤功能）。", basis="说明书原文")
q(S1, "presale.spec", "这台脱水转速多少？", ap="washer", br="littleswan",
  model="TG100V618PLUS", pid="littleswan-tg100v618plus",
  inc=["1200"], exc=["1400"], src="specs.yaml#spin_speed_rpm",
  ans="最高脱水转速 1200r/min。", basis="说明书原文")
q(S1, "presale.spec", "这台机身多深？柜子能嵌进去吗", ap="washer", br="littleswan",
  model="TG10V628PLUS", pid="littleswan-tg10v628plus",
  inc=["585", "595"], exc=["本机是 560"], src="specs.yaml#size_mm",
  ans="595×850×585mm（宽×高×深），是同组里最深的一台；TG100V618PLUS 是 560mm 深。"
      "标准 60cm 柜体要留进出水和散热间隙，量柜子按机身深度加 3~5cm。",
  basis="说明书原文")
q(S1, "presale.spec", "这台多重？", ap="washer", br="siemens",
  model="WM12P2602W", pid="siemens-wm12p2602w",
  inc=["79"], exc=["70"], src="specs.yaml#weight_kg",
  ans="整机约 79kg。滚筒机很重，搬运和上楼要先说清楚。", basis="说明书原文")

# ── 电饭煲参数
q(S1, "presale.spec", "这台几升的？", ap="rice_cooker", br="midea",
  model="MB-RE576S", pid="midea-mb-re576s",
  inc=["5"], exc=["4"], src="specs.yaml#capacity_l",
  ans="5.0L。说明书没印额定容积，是按水位线最大刻度 10 杯 + 官网产品页 + 业务方核机身铭牌三条一致确认的。",
  basis="业务方口径")
q(S1, "presale.spec", "这台几升的？", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["5"], exc=["4"], src="specs.yaml#capacity_l",
  ans="5.0L。", basis="说明书原文")
q(S1, "presale.spec", "内胆是什么材质？", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["0氟有钛土陶球釜"], exc=["特氟龙"], src="specs.yaml#inner_pot_material",
  ans="0氟有钛土陶球釜（有钛小快球釜），0 涂层设计，耐磨抗刮，可以用钢丝球刷洗。"
      "这是本库里唯一允许说能用钢丝球的机型，同品牌其他涂层机型一律禁用钢丝球。",
  basis="业务方口径", note="与同品牌其他机型的清洗口径相反，必须按机型分流")
q(S1, "presale.spec", "内胆是什么材质？", ap="rice_cooker", br="supor",
  model="SF40FC871", pid="supor-sf40fc871",
  inc=["蓝钻"], exc=["0 涂层"], src="specs.yaml#inner_pot_material",
  ans="蓝钻不粘厚釜（能效表「内锅材质」列填的是「金属」）。有涂层，不能用钢丝球。",
  basis="说明书原文")
q(S1, "presale.spec", "这台功率多大？", ap="rice_cooker", br="midea",
  model="MB-40HB9T", pid="midea-mb-40hb9t",
  inc=["1300"], exc=["本机是 760"], src="specs.yaml#power_w",
  ans="1300W，是 IH 加热；同容量底盘加热的 MB-AFB4041RL 是 760W。", basis="说明书原文")
q(S1, "presale.spec", "这台是 IH 还是底盘加热？", ap="rice_cooker", br="midea",
  model="MB-AFB4041RL", pid="midea-mb-afb4041rl",
  inc=["底盘"], exc=["IH 加热"], src="features.yaml#ih_heating",
  ans="底盘加热，不是 IH。同品牌 MB-40HB9T 才是 IH。", basis="说明书原文")
q(S1, "presale.spec", "这台机身尺寸多少？", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["380", "255", "280"], exc=["381"], src="specs.yaml#size_mm",
  ans="380×255×280mm，但这是按同品牌 5L 机型（SF50FC733 384×285×257）推算的，官方没公开精确尺寸，"
      "误差可能 ±15mm，只能用于估算，不能当安装/嵌入依据。",
  basis="推断", note="被追问依据时必须承认是推断")

# ── 功能题（含"没有"型反幻觉）
q(S1, "presale.feature", "支持自清洁吗？", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["没有"], exc=["支持自清洁", "按自清洁键"], src="features.yaml#self_clean",
  ans="这台没有自清洁，滤网需要自己拆洗。美的 1.5 匹这几台（A11 / KS2P / N8KS1-1P / Q2）都没有。",
  basis="说明书原文")
q(S1, "presale.feature", "有自清洁功能吗", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["有"], exc=["没有自清洁"], src="features.yaml#self_clean",
  ans="有（除菌自清洁）。海尔这 5 台全部带自清洁。", basis="说明书原文")
q(S1, "presale.feature", "这台能烘干衣服吗？", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["没有"], exc=["可以烘干", "除湿可以烘干"], src="features.yaml#dry",
  ans="空调不能烘衣服。这台有除湿模式（干衣用的 dry 功能是除湿），但除湿不是烘干，"
      "没有加热筒、达不到烘干效果。要烘干得用干衣机或洗烘一体。",
  basis="说明书原文", note="跨品类幻觉陷阱：除湿≠烘干")
q(S1, "presale.feature", "这台是单洗还是洗烘一体？", ap="washer", br="littleswan",
  model="TG10V628PLUS", pid="littleswan-tg10v628plus",
  inc=["单洗", "没有烘干"], exc=["能烘干", "洗烘一体机"],
  src="features.yaml#dry",
  ans="单洗机，没有烘干模块（型号后缀官方标的就是「10KG 滚筒单洗」）。"
      "说明书里出现的「烘干前」「烘干时间」是洗烘一体版型的通用模板文字，不适用于这台。",
  basis="业务方口径", note="说明书模板文字造成的误判点")
q(S1, "presale.feature", "能连 WiFi 吗？", ap="washer", br="siemens",
  model="WG52A108AW", pid="siemens-wg52a108aw",
  inc=["不带"], exc=["支持 WiFi"], src="features.yaml#wifi",
  ans="西门子这 5 台都不带 WiFi，也没有 App 控制。", basis="说明书原文")
q(S1, "presale.feature", "带自动投放吗？", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["有"], exc=["没有自动投放"], src="features.yaml#auto_dosing",
  ans="有自动投放。小天鹅 5 台里 TG100V618PLUS / TG10K60 / TG10V628PLUS / TG10VE40 都有，"
      "波轮 TB100V26DT 没有。",
  basis="说明书原文")
q(S1, "presale.feature", "有没有低糖饭功能？", ap="rice_cooker", br="midea",
  model="MB-RE576S", pid="midea-mb-re576s",
  inc=["没有"], exc=["有低糖饭程序"], src="features.yaml#low_sugar",
  ans="这台没有低糖饭。本库唯一带低糖饭的是苏泊尔 F40H9083S。", basis="说明书原文")
q(S1, "presale.feature", "有没有低糖饭功能？", ap="rice_cooker", br="supor",
  model="F40H9083S", pid="supor-f40h9083s",
  inc=["有"], exc=["没有低糖"], src="features.yaml#low_sugar",
  ans="有低糖饭程序。同系列 F40H8093S 没有，别串。", basis="说明书原文")
q(S1, "presale.feature", "能蒸东西吗？", ap="rice_cooker", br="supor",
  model="SF50FC733", pid="supor-sf50fc733",
  inc=["有"], exc=["不能蒸"], src="features.yaml#steam",
  ans="有蒸煮程序，配蒸架可以蒸。苏泊尔这几台基本都带蒸，美的 MB-40HB9T / MB-RE576S 不带蒸。",
  basis="说明书原文")

# ── 适配套（房间/空间/人口）
q(S1, "presale.fit", "我家 3 平方的小房间能装吗", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["4"], exc=["可以装"], src="specs.yaml#min_room_area_m2",
  ans="不建议。这台说明书标的使用房间最小面积是 4㎡（含）以上，3㎡ 低于下限——房间太小，"
      "冷量打不满会频繁启停，房间小还容易过冷。这种情况建议换小一档的机器或先问安装师傅现场判断。",
  basis="说明书原文", note="安全/规范硬指标，不能因为能装上就说能装")
q(S1, "presale.fit", "20 平的卧室够用吗", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["3510"], exc=["官方适用面积"], src="specs.yaml#cooling_capacity_w",
  ans="这台制冷量 3510W（1.5 匹），20㎡ 卧室一般够用——但这句是经验估算，不是厂家标称的适用面积。"
      "说明书只给了最小面积 4㎡。西晒、顶层、大玻璃的要往上加一档。",
  basis="说明书原文", note="必须把「经验估算」和「说明书值」分开说")
q(S1, "presale.fit", "我家 8 平米厨房放得下洗衣机吗", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["595", "850", "560"], exc=["一定能放"],
  src="specs.yaml#size_mm",
  ans="机身 595×850×560mm（宽×高×深）。8㎡ 面积通常够，但更关键是门洞和通道宽度能不能过人、"
      "深度方向要不要留进出水和散热间隙。运输螺栓要拆、要留 3~5cm 余量。",
  basis="说明书原文")
q(S1, "presale.fit", "3 口人买几升的电饭煲", ap="rice_cooker", br=None, model=None,
  inc=["4"], exc=["10L", "10 升"], src="04_scenario/s1_presale/slots.yaml",
  ans="3 口人日常 4L 够（4L 一般够 4~5 碗饭）。常留隔夜饭、或常有客人的话上 5L。"
      "本库 4L 机型：美的 MB-40HB9T / MB-AFB4041RL、苏泊尔 SF40FC871 / F40H8093S / F40H9083S；"
      "5L：美的 MB-RE576S、苏泊尔 F50X96 / SF50FC733。",
  basis="说明书原文", note="按容量推荐属经验，必须标「建议」而不是「厂家规定」")

# ── 对比题
q(S1, "presale.compare", "海尔 E1-1 和美的 Q2 哪个好", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["能效"], exc=["建议选美的", "性价比更高"],
  src="04_scenario/s1_presale/compare.yaml#s1_1.5p_wall",
  ans="两台制冷量都是 3510W，能力基本一样，差别在：① 能效——美的 Q2 明确标 1 级、APF 5.32，"
      "海尔 E1-1 说明书没标能效等级；② 管路——Q2 最长 15m、每米补 18g 冷媒，海尔最长 10m、超 5m 每米补 20g；"
      "③ 静音档海尔 16dB(A)、美的 18dB(A)，体感差别不大，不建议只凭这 2dB 决定；④ 自清洁只有海尔有。"
      "所以看您更在意电费口径（选美的）还是自清洁（选海尔），不存在一台综合更好。",
  basis="说明书原文", note="禁止给「综合更好」这类结论")
q(S1, "presale.compare", "两台 3 匹柜机差在哪", ap="air_conditioner", br=None,
  model=None, src="04_scenario/s1_presale/compare.yaml#s1_3p_floor",
  inc=["20", "15"], exc=["综合更好"],
  ans="能力几乎一样（海尔 7330W / 美的 7320W）。差别：① 管路美的 20m、海尔 15m，外机位远选美的；"
      "② 能效海尔说明书没标 APF，美的标 4.6；③ 室内机尺寸海尔 364×424×1830、美的 380×392×1802，"
      "预留位要按各自尺寸量；④ 美的说明书标了最小房间 26㎡，海尔没标这项。",
  basis="说明书原文")
q(S1, "presale.compare", "小天鹅 4 台滚筒怎么选", ap="washer", br="littleswan",
  model=None, src="04_scenario/s1_presale/compare.yaml#ws_10kg_drum_ls",
  inc=["585", "560"], exc=["都一样"],
  ans="这 4 台洗涤容量、转速（1200）、能效（1 级）、排水（上排水）全一样，差别在尺寸和功能："
      "TG10V628PLUS 最薄但最深（585mm 深）、最轻 63kg；TG10K60 程序最全；"
      "TG100V618PLUS 有羊毛认证、560mm 深最浅；TG10VE40 有特殊污渍程序。"
      "真正要选先把柜子深度量出来。",
  basis="说明书原文")
q(S1, "presale.compare", "西门子 3 台单洗滚筒怎么选", ap="washer", br="siemens",
  model=None, src="04_scenario/s1_presale/compare.yaml#ws_10kg_drum_siemens",
  inc=["1900", "2100"], exc=["都一样"],
  ans="三台机身完全一致（598×848×590），都是 10kg。区别：WG54H2V00W 脱水噪音最低（69dB）、"
      "WG52A108AW 噪音略高（72dB）；WM12P2602W 说明书没给能效和噪音数据，功率是 2100W，另两台 1900W。"
      "数据不全的那台我不替它补。",
  basis="说明书原文")
q(S1, "presale.compare", "洗烘一体和分开买哪个划算", ap="washer", br="siemens",
  model=None, src="04_scenario/s1_presale/compare.yaml#ws_washer_dryer",
  inc=["7"], exc=["一样"],
  ans="洗烘一体的烘干容量小于洗涤容量（西门子 WN54A1X42W 洗 10kg、烘 7kg），一次洗满要分两次烘；"
      "分开买干衣机烘干能到 10kg（WQ53E2D01W 就是 10kg），但要占多一台位置和一路电。"
      "阳台够、常烘的建议分开；位置紧张的选一体。",
  basis="说明书原文")

# ── 价格题：知识库不得给数
q(S1, "presale.price", "多少钱？", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["价格"], exc=["3499", "3999 元"],
  ans="价格我这边查不到实时报价，得按型号和当前活动走商品页/下单页。"
      "提醒一句：机器价不含安装辅材（加长管、打孔、支架、高空费），这些是另算的。",
  basis="库内缺失", note="知识库没有价格数据，任何具体数字都算幻觉")

# ── 补齐型号覆盖（这几台参数表未回填，只能出功能题或单参数题）
q(S1, "presale.spec", "这台制冷量多少？", ap="air_conditioner", br="haier",
  model="KFR-35GW/01KMC81U1", pid="haier-kfr-35gw-01kmc81u1",
  inc=["3500"], exc=["是 3510"], src="specs.yaml#cooling_capacity_w",
  ans="制冷量 3500W。比同品牌的 E1-1（3510W）低 10W，两台不是同一台。", basis="说明书原文")
q(S1, "presale.spec", "这台循环风量多少？", ap="air_conditioner", br="haier",
  model="KFR-72LW/E1-1Plus", pid="haier-kfr-72lw-e1-1plus",
  inc=["1750"], exc=["本机是 1600"], src="specs.yaml#airflow_m3h",
  ans="1750m³/h，但这是按同系列推算的推断值，不是说明书参数表里的原值，被追问依据时要说明是推断。"
      "同系列 E1-1 是 1600m³/h。", basis="推断", note="推断值必须同步说明")
q(S1, "presale.feature", "支持自清洁吗？", ap="air_conditioner", br="midea",
  model="KFR-35GW/A11", pid="midea-kfr-35gw-a11",
  inc=["没有"], exc=["支持自清洁"], src="features.yaml#self_clean",
  ans="这台没有自清洁。美的 1.5 匹这四台（A11 / KS2P / N8KS1-1P / Q2）都没有，只有海尔有。",
  basis="说明书原文")
q(S1, "presale.feature", "能连 WiFi 吗？", ap="air_conditioner", br="midea",
  model="KFR-35GW/KS2P", pid="midea-kfr-35gw-ks2p",
  inc=["有"], exc=["不能连"], src="features.yaml#wifi",
  ans="有 WiFi，可以用美居 App。但这台没有自清洁。", basis="说明书原文")
q(S1, "presale.feature", "支持自清洁吗？", ap="air_conditioner", br="midea",
  model="KFR-35GW/N8KS1-1P", pid="midea-kfr-35gw-n8ks1-1p",
  inc=["没有"], exc=["支持自清洁"], src="features.yaml#self_clean",
  ans="没有自清洁。", basis="说明书原文")

# ── 无型号兜底
q(S1, "presale.spec", "你们空调多少匹？", ap="air_conditioner", br=None, model=None,
  inc=["型号"], exc=["就是 1.5 匹"], src="04_scenario/_shared/intent_map.yaml",
  ans="得看具体型号——同一品牌 1.5 匹和 3 匹差很远。您把型号发我（机身上或购机页面都有），我按型号给您参数。",
  basis="政策文件", note="无型号不得直接报匹数")


# ══════════════════════════════════════════════════════════════════ s2 安装
S2 = "s2_install"

q(S2, "install.feasibility", "安装需要什么条件", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["专用", "4"], exc=["进水管"], src="install.yaml#electrical",
  ans="空调不接进水也不接下水，条件看四样：① 电气——必须用专用电源分支回路供电，不得和其他大功率电器共用；"
      "这是 I 类电器，必须可靠接地（接地电阻<4Ω）。本机最大输入电流 15A，按规则配插头接入专用回路。"
      "② 空间——安装、运行、存放的房间面积都要不小于 4㎡。③ 机位——室外机不得占用过道、楼梯、出口、"
      "消防通道，要远离邻居门窗、避开儿童易触及处，支架和承载面强度不低于机组重量 4 倍且不低于 200kg。"
      "④ 管路——最长 15m，超出每米补 18g 冷媒；内机要接排水，冷凝水不能滴到楼下或影响他人。"
      "室内机上方和两侧要留出进出风空间，但具体数值说明书上是图示，得按安装简图核对。",
  basis="说明书原文",
  note="旧版本写了「内机离地 2.1m 以上」和「220V/16A 插座」——这两个数库内没有出处："
       "install.yaml#indoor_clearance 为 null（说明书是图示），本机最大输入电流是 15A 不是 16A。已按说明书改写")
q(S2, "install.feasibility", "空调要预留上下水吗，门宽够不够", ap="air_conditioner",
  br=None, model=None,
  inc=["不需要进水"], exc=["需要进水管", "需要地漏"],
  ans="空调不需要进水，也不用预留地漏——它没有水路，冷凝水是内机自己排水管引走的。"
      "「要上下水、要量门宽」是洗衣机/冰箱的安装条件，别套到空调上。空调要的是电源和开孔。",
  basis="说明书原文", note="跨品类条件套用陷阱")
q(S2, "install.feasibility", "洗衣机安装有什么条件", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["进水", "排水", "电源", "地面"], exc=["不需要排水"],
  ans="五个条件：① 进水——水龙头 + 进水管（随机带）；② 排水——上排水的排水管要挂高 60~90cm 排到地漏或水槽；"
      "③ 电源——接地的三孔插座，不要用插排；④ 位置——机身 595×850×560mm，深度方向留 3~5cm；"
      "⑤ 地面要平、能承重（这台约 69kg），调平脚调到机器不晃。",
  basis="说明书原文")
q(S2, "install.feasibility", "下排水的洗衣机安装要注意什么", ap="washer", br="littleswan",
  model="TB100V26DT", pid="littleswan-tb100v26dt",
  inc=["地漏"], exc=["不需要地漏", "要挂高"],
  ans="这台是下排水，安装位置地面必须有地漏，排水管直接落到地漏里。"
      "不能按上排水的办法把管子挂高——下排水的排水泵不扬水，挂高了就排不出去。",
  basis="业务方口径", note="上/下排水的规则相反，混了就装错")
q(S2, "install.feasibility", "电饭煲需要安装吗", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["不需要", "拆箱"], exc=["安排师傅上门安装"],
  ans="电饭煲不需要安装，拆箱洗净就能用，也不要约师傅上门——它不属于上门安装品类。",
  basis="政策文件")
q(S2, "install.feasibility", "洗衣机拆了包装直接插电能用吗", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["运输螺栓"], exc=["直接插电就能用"],
  ans="不行，滚筒机背面有 4 颗运输螺栓（固定内筒的），不拆就通电脱水会把机器震坏、也报不了装。"
      "必须拆掉，拆下的螺栓和塑料垫片留着，以后搬家或运输要装回去。",
  basis="说明书原文", note="漏说运输螺栓是典型的不完整回答")
q(S2, "install.fee", "加长管最长能加多少米", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["10"], exc=["15 米"], src="specs.yaml#pipe_max_length_m",
  ans="这台最长 10m（管道总长），高低差不超过 10m。美的 Q2 是 15m，两个品牌的数不能混用。",
  basis="说明书原文")
q(S2, "install.fee", "加长管最长能加多少米", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["15"], exc=["10 米"], src="specs.yaml#pipe_max_length_m",
  ans="最长 15m。超出的部分要按米补冷媒（18g/m），不是光接管子就行。", basis="说明书原文")
q(S2, "install.fee", "高空费什么时候收", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["4"], exc=["所有楼层都收"],
  src="05_brand_ref/midea/fee_schedule.yaml#items_by_appliance.air_conditioner",
  ans="美的口径是 4 楼及 4 楼以上 + 需要在墙外施工才收高空作业费（100~200 元/台）；"
      "4 楼以下或者人在室内就能装完的不收。这是行业参考区间，具体以师傅出示的收费标准为准。",
  basis="政策文件",
  note="出处是空调收费表，不是 install_fee_benchmark——后者是洗衣机安装横比表，不含空调高空费")
q(S2, "install.fee", "5 楼没电梯，安装总共要收多少钱？", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["标准"], exc=["一共", "总计", "总共 500"],
  src="05_brand_ref/midea/fee_schedule.yaml#items_by_appliance.air_conditioner",
  ans="不能给您一个总价，因为要按现场实际发生的项目算。可能涉及的每一项：高空作业（4 楼以上墙外施工）"
      "100~200 元/台、加长连接管 80~120 元/米、打孔 50~150 元/个、支架、远程交通费（20km 以上）。"
      "重点：这些是行业参考区间，而且必须由师傅上门后出示收费标准、和您协商确认才收。"
      "免费的部分是市内上门设计和基础安装人工。",
  basis="政策文件", note="禁止报总价；必须先讲免费安装的定义")
q(S2, "install.fee", "打孔要另收费吗", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["协议收费", "协商"], exc=["全免费"],
  src="05_brand_ref/haier/fee_schedule.yaml#items_by_appliance.air_conditioner",
  ans="常规安装含一次打孔，但钢筋混凝土墙、水钻打孔、二次移位打孔这类属协议收费，"
      "标准由服务人员出示后与您协商。免费安装只免人工，不免辅材。",
  basis="政策文件",
  note="旧版本出处指向 install_fee_benchmark（那是洗衣机安装横比表，不含空调打孔项）")
q(S2, "install.fee", "洗衣机安装是免费的吗", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["免费", "辅材"], exc=["全部免费", "一分钱不收"],
  ans="基础安装（上门、就位、调平、接管试机）是免费的，但「免费安装只免人工、不免辅材」："
      "加长进水管、加长排水管、改地漏、换水龙头、专用底座这些要另收。"
      "具体标准小天鹅和西门子不一样，以师傅出示的价目为准。",
  basis="政策文件", note="先说免费安装的定义，再讲例外")
q(S2, "install.booking", "什么时候能来装", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["24"], exc=["明天上午一定到您家"],
  ans="正常情况下 24 小时内会有服务人员联系您预约上门时间。但具体哪天几点要看当地网点的排期和您的时间，"
      "我没法替他们承诺「明天上午一定到」。",
  basis="政策文件")


# ══════════════════════════════════════════════════════════════════ s3 使用
S3 = "s3_usage"
FAQ = "04_scenario/s3_usage/faq.yaml#"

q(S3, "usage.first_time", "第一次使用怎么操作", ap="air_conditioner", br=None, model=None,
  inc=["电源", "模式"], exc=[], src=FAQ + "faq-s3.001", ref="faq-s3.001",
  ans="四步：插电源、按开关键、选模式（制冷/制热）、调温度和风向（制冷导风板朝上、制热朝下）。",
  basis="说明书原文")
q(S3, "usage.maintenance", "滤网怎么拆下来洗", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["断电", "面板"], exc=[], src=FAQ + "faq-s3.002", ref="faq-s3.002",
  ans="先关机断电 → 抓面板两端下侧扣手位打开约 75 度 → 抬中间凸部往下拉出滤网 → 温水加中性洗涤剂洗、"
      "阴凉处晾干别暴晒 → 装回合面板。",
  basis="说明书原文", note="安全步骤（断电）不可省略")
q(S3, "usage.maintenance", "滤网多久洗一次", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["两周"], exc=[], src=FAQ + "faq-s3.003", ref="faq-s3.003",
  ans="说明书建议两周左右一次；灰大、用得多可以更勤。", basis="说明书原文")
q(S3, "usage.maintenance", "滤网多久洗一次", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["建议"], exc=["厂家要求两周"], src=FAQ + "faq-s3.004", ref="faq-s3.004",
  ans="这台说明书没写周期。一般建议换季前和用得多时各清一次——这是使用建议，不是厂家要求。",
  basis="说明书原文", note="美的说明书无周期，不能套海尔的「两周」当厂家规定")
q(S3, "usage.operation", "怎么开自清洁", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["自清洁"], exc=["没有自清洁"], src=FAQ + "faq-s3.005", ref="faq-s3.005",
  ans="按遥控器的【自清洁】键进入，面板显示符号和进度条（灯带雾白色）。过程中别断电、别强行关机，"
      "跑完响两声自动退出。",
  basis="说明书原文")
q(S3, "usage.operation", "怎么开自清洁", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["没有"], exc=["按自清洁键"], src=FAQ + "faq-s3.006", ref="faq-s3.006",
  ans="这台没有自清洁，没有这个键。日常清洁就拆滤尘网洗；导风板可以用「待机清洁」——"
      "关机状态下长按遥控器组合键，等外导风板完全打开后断电擦拭。",
  basis="说明书原文")
q(S3, "usage.connect", "怎么连 WiFi / APP 连不上", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["2.4"], exc=["5G 也可以"], src=FAQ + "faq-s3.007", ref="faq-s3.007",
  ans="装「智家」或「美居」App → 首页「+」扫机身二维码添加 → 按向导连网，Wi-Fi 一定选 2.4GHz。"
      "连不上常见原因：连了 5G（不支持）、Wi-Fi 名带中文、路由器挂的设备太多。",
  basis="说明书原文")
q(S3, "usage.connect", "空调老掉线", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["2.4"], exc=[], src="04_scenario/s4_troubleshoot/symptoms.yaml#wifi_offline",
  ans="先确认路由器 2.4GHz 频段的信号强度：距离远、隔墙多、路由器下挂设备太多都会掉。"
      "换路由器位置或加个中继再试，仍频繁离线我帮您登记。",
  basis="说明书原文")
q(S3, "usage.maintenance", "长时间不用怎么处理", ap="air_conditioner", br=None, model=None,
  inc=["滤网", "电池"], exc=[], src=FAQ + "faq-s3.008", ref="faq-s3.008",
  ans="断电、把滤网清洗晾干装回、遥控器电池取出来分开存放；下次用之前先通电看看运转是否正常。",
  basis="说明书原文")
q(S3, "usage.setting_confusion", "为什么开 26 度还是不凉", ap="air_conditioner", br=None,
  model=None, inc=["送风", "滤网"], exc=["机器坏了"], src=FAQ + "faq-s3.009",
  ref="faq-s3.009",
  ans="先排三点：模式是不是在「送风」（送风不制冷）、设定温度是不是比室温低、滤网是不是该洗了。"
      "另外室外超 43℃、房间西晒西窗大开，效果都会打折。三点都排除还不凉，走排查流程。",
  basis="说明书原文")
q(S3, "usage.operation", "遥控器按了没反应", ap="air_conditioner", br=None, model=None,
  inc=["电池"], exc=[], src=FAQ + "faq-s3.010", ref="faq-s3.010",
  ans="先换电池，还不行取出电池等几分钟装回去。遥控距离一般 7 米内、中间别挡东西，"
      "室内机 1 米内别装日光灯（会干扰）。都不行就是遥控器或接收头的问题，帮您登记。",
  basis="说明书原文")
q(S3, "usage.operation", "电饭煲水位线怎么用，米种刻度是什么意思",
  ap="rice_cooker", br=None, model=None,
  inc=["刻度", "米种"], exc=[], src=FAQ + "faq-s3.011", ref="faq-s3.011",
  ans="内锅上一般有两组刻度，对应不同米种（常见东北米和丝苗米）——先确认您煮的是哪种米，"
      "再看对应那条线。水量不要超过最高水位线，超了容易溢锅。面板上的「米种」键要和刻度对上。",
  basis="说明书原文")
q(S3, "usage.maintenance", "电饭煲内胆怎么清洗，能用钢丝球吗", ap="rice_cooker", br=None,
  model=None, inc=["涂层", "钢丝球"], exc=[], src=FAQ + "faq-s3.012", ref="faq-s3.012",
  ans="要看机型：涂层内胆只能海绵/软布洗，钢丝球、清洁球、金属铲都会刮花涂层，也不能进洗碗机；"
      "不锈钢零涂层或 0 涂层机型的机型（如美的 MB-RE576S 的 316L、苏泊尔 F50X96 的 0氟有钛土陶球釜）"
      "本身没涂层，可以用钢丝球。上盖内盖一般能一键拆下来单独洗。",
  basis="说明书原文", note="必须按机型分流，不能一句话答完")
q(S3, "usage.maintenance", "苏泊尔 F50X96 内胆能用钢丝球刷吗", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["可以"], exc=["不能用钢丝球"], src="specs.yaml#inner_pot_material",
  ans="可以。这台是 0氟有钛土陶球釜、0 涂层设计，耐磨抗刮，允许用钢丝球刷洗。"
      "但同品牌的 SF40FC871（蓝钻不粘涂层）、SF50FC733（陶瓷纳米晶体涂层）不行。",
  basis="业务方口径", note="同品牌内胆口径相反，最容易串")
q(S3, "usage.maintenance", "苏泊尔 SF40FC871 内胆能用钢丝球吗", ap="rice_cooker", br="supor",
  model="SF40FC871", pid="supor-sf40fc871",
  inc=["不能"], exc=["可以用钢丝球"], src="specs.yaml#inner_pot_material",
  ans="不能。这台是蓝钻不粘厚釜，有涂层，钢丝球、清洁球、金属铲都会刮花，只能海绵或软布加中性清洁剂。",
  basis="说明书原文")
q(S3, "usage.operation", "洗衣机放多少洗涤剂合适", ap="washer", br=None, model=None,
  inc=["泡沫"], exc=[], src=FAQ + "faq-s3.013", ref="faq-s3.013",
  ans="按洗涤剂包装上的用量来，再参考水质硬度和衣物脏污程度。倒多了机器会检测到泡沫过多、"
      "自动加水漂洗并延长程序，衣服反而更容易残留。低泡机洗专用最稳妥。",
  basis="说明书原文")
q(S3, "usage.maintenance", "洗衣机的筒自洁怎么用，多久做一次", ap="washer", br=None,
  model=None, inc=["不放衣物"], exc=[], src=FAQ + "faq-s3.014", ref="faq-s3.014",
  ans="清空内筒、不放衣物，选筒自洁/高温洗跑一遍，一般一两个月一次，潮湿地区更勤。"
      "用完把门留一条缝、料盒抽出来晾干。",
  basis="说明书原文")
q(S3, "usage.maintenance", "洗完衣服洗衣机的门要开着吗", ap="washer", br=None, model=None,
  inc=["缝"], exc=["必须关严"], src=FAQ + "faq-s3.015", ref="faq-s3.015",
  ans="建议开一条缝。刚洗完筒里有水汽、密封圈褶皱处存水，一直关着容易发霉发臭。"
      "家里有小孩要注意别让他们爬进去。",
  basis="说明书原文")
q(S3, "usage.operation", "电饭煲预约怎么设，能预约多久", ap="rice_cooker", br=None,
  model=None, inc=["完成时间"], exc=["调的是开始时间"], src=FAQ + "faq-s3.016", ref="faq-s3.016",
  ans="先选功能，再按「预约」调的是**完成时间**（不是开始时间，别搞反），最后按开始。"
      "上限各型号不同：有的建议不超过 12 小时（超了饭容易变味），有的写 24 小时。"
      "「热饭」「保温」「蛋糕」这类通常不能预约。您是哪款我查具体上限。",
  basis="说明书原文", note="「完成时间≠开始时间」是高频错答点")
q(S3, "usage.first_time", "电饭煲第一次用要做什么", ap="rice_cooker", br=None, model=None,
  inc=["纸卡", "擦干"], exc=[], src=FAQ + "faq-s3.017", ref="faq-s3.017",
  ans="取出内锅和所有包装件（包括内锅与加热盘之间那块纸卡）→ 清水洗净内锅、软布擦干（内外都要干，"
      "外表面带水放回去会污损加热盘）→ 内锅放回煲体落位、压下面盖听到咔嗒声 → 核对面板丝印与内锅边缘的型号。"
      "介意新锅味道可以先煮一次水倒掉。",
  basis="说明书原文")
q(S3, "usage.operation", "电饭煲保温能放多久，保温后饭变硬了", ap="rice_cooker", br=None,
  model=None, inc=["5~6"], exc=["放多久都行"], src=FAQ + "faq-s3.018", ref="faq-s3.018",
  ans="说明书建议保温一般 5~6 小时（不同型号 5 或 6），超过就会干硬、发黄甚至变味。"
      "米饭在 60℃ 左右长时间放着容易滋生细菌。要留到下一顿最好盛出来冷藏、吃前再加热。",
  basis="说明书原文")
q(S3, "usage.maintenance", "电饭煲的上盖、蒸汽阀怎么拆下来洗", ap="rice_cooker", br=None,
  model=None, inc=["蒸汽阀", "卡"], exc=[], src=FAQ + "faq-s3.019", ref="faq-s3.019",
  ans="先断电等彻底冷却 → 按/上推可拆盖按钮取下内盖，冲洗擦干按卡槽装回听到卡合声 → "
      "蒸汽阀取下清洗后必须卡到位（没到位会漏汽，直接影响煮饭）。带微压阀的要注意方向："
      "逆时针旋到开锁位取下，装回顺时针旋到闭合位，凸点对位。",
  basis="说明书原文")
q(S3, "usage.maintenance", "内胆能不能放进洗碗机洗", ap="rice_cooker", br=None, model=None,
  inc=["不能"], exc=["可以放洗碗机"], src=FAQ + "faq-s3.020", ref="faq-s3.020",
  ans="不能。说明书明确禁止把内锅和可拆盖放洗碗机——碱性洗涤剂会破坏涂层，涂层起泡脱落不可逆。"
      "正确做法是冷却后软海绵加中性清洁剂手洗、软布擦干。",
  basis="说明书原文")
q(S3, "usage.operation", "稀饭和煮粥有什么区别，该按哪个", ap="rice_cooker", br="supor",
  model="SF50FC733", pid="supor-sf50fc733",
  inc=["两个独立"], exc=["一样"], src=FAQ + "faq-s3.021", ref="faq-s3.021",
  ans="这台面板上「稀饭」和「煮粥」是两个独立的功能键，不是同一个功能的不同叫法。"
      "米水比例按内锅对应功能的刻度线对，不要拿煮饭的水量去煮粥，会溢锅。",
  basis="说明书原文")
q(S3, "usage.operation", "电饭煲在高原能用吗，为什么煮不熟", ap="rice_cooker", br="supor",
  model="SF50FC733", pid="supor-sf50fc733",
  inc=["2000"], exc=["属于机器故障"], src=FAQ + "faq-s3.022", ref="faq-s3.022",
  ans="说明书明确正常海拔范围是 0~2000 米。超过 2000 米水沸点低、加热曲线对不上，"
      "表现就是饭煮不透、夹生或溢锅——这属于超出使用环境要求，不是机器故障，多煮一会儿也解决不了。",
  basis="说明书原文")
q(S3, "usage.first_time", "洗衣机刚装好第一次怎么用", ap="washer", br=None, model=None,
  inc=["运输螺栓"], exc=[], src=FAQ + "faq-s3.023", ref="faq-s3.023",
  ans="① 先拆机身背面 4 颗运输螺栓（不拆脱水时会剧烈震动甚至损坏机器）② 调平：对角线按压顶盖、"
      "转底脚调到不晃 ③ 先跑一次筒自洁（不放衣物也不放洗涤剂）④ 打开进水龙头检查接口渗漏。",
  basis="说明书原文")
q(S3, "usage.operation", "洗衣机该选哪个程序，温度调多少", ap="washer", br=None, model=None,
  inc=["30"], exc=[], src=FAQ + "faq-s3.024", ref="faq-s3.024",
  ans="先看衣物护理标签，按标签温度上限选。日常棉织物 30~40℃ 够用；60℃ 以上主要除螨和重污，"
      "但会加速衣物损伤和串色。羊毛真丝选专用程序、转速最低。拿不准按「棉织物 + 30℃ + 800 转」起步。",
  basis="说明书原文")
q(S3, "usage.operation", "自动投放怎么用，洗衣凝珠能放料盒吗", ap="washer",
  br="littleswan", model="TG10K60", pid="littleswan-tg10k60",
  inc=["凝珠"], exc=["凝珠放料盒"], src=FAQ + "faq-s3.025", ref="faq-s3.025",
  ans="自动投放盒装满洗衣液和柔顺剂，机器按衣量和脏污自己投。三个坑：凝珠要直接扔进洗涤筒、"
      "**不能放料盒**（会堵投放口，用凝珠时要关掉自动投放）；太稠的洗衣液先稀释；运行时别开料盒。",
  basis="说明书原文")
q(S3, "usage.maintenance", "洗衣机的过滤器（排污口）怎么清理", ap="washer", br=None,
  model=None, inc=["冷却", "过滤器"], exc=[], src=FAQ + "faq-s3.026", ref="faq-s3.026",
  ans="① 拔电源，轻按过滤器门让它弹开 ② 拉出紧急排水管，**先确认筒内水已冷却**再拔堵头放水（防烫伤）"
      " ③ 用钳子夹住过滤阀向左旋下取出毛絮 ④ 装回拧到限位筋、堵头复位。"
      "说明书只写「定期」，排水变慢或有异味就该清了。",
  basis="说明书原文", note="防烫伤提示不可省略")
q(S3, "usage.operation", "洗烘一体能烘多少衣服，为什么烘不干", ap="washer", br="siemens",
  model="WN54A1X42W", pid="siemens-wn54a1x42w",
  inc=["7"], exc=["能烘 10kg"], src=FAQ + "faq-s3.027", ref="faq-s3.027",
  ans="洗烘一体的烘干容量比洗涤容量小——这台洗 10kg 只能烘 7kg。一次洗满就要分两批烘，"
      "塞满了必然烘不干。另外它是「先洗完再烘」，不能边洗边烘；衣物抖散、别塞压、脱水转速高一点。",
  basis="说明书原文")
q(S3, "usage.setting_confusion", "洗衣机为什么洗得比预计时间长", ap="washer", br=None,
  model=None, inc=["泡沫"], exc=["机器坏了"], src=FAQ + "faq-s3.028", ref="faq-s3.028",
  ans="最常见是泡沫过多：机器检测到泡沫会静置、缓慢转动、再进水除泡，程序时间自动拉长——"
      "这是保护行为不是故障。减少洗涤剂用量、用低泡专用洗涤剂。另外衣物吸水多、水温低、排水不畅也会延长。",
  basis="说明书原文")
q(S3, "usage.operation", "洗衣机能记住上次的程序吗，停电了怎么办", ap="washer",
  br="littleswan", model="TG10K60", pid="littleswan-tg10k60",
  inc=["记忆"], exc=[], src=FAQ + "faq-s3.029", ref="faq-s3.029",
  ans="这台有程序记忆、默认开启，会记住上次的程序和参数。洗涤中停电不用管，来电后会接着跑到结束；"
      "但断电期间别强行开门。",
  basis="说明书原文")


# ══════════════════════════════════════════════════════════════════ s4 故障
S4 = "s4_troubleshoot"
SYM = "04_scenario/s4_troubleshoot/symptoms.yaml#"

# ── 安全红线（三品类通用）
q(S4, "troubleshoot.safety", "洗衣机摸着有点麻手", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["断开电源", "不要"], exc=["再用看看"], handoff=True,
  src=SYM + "electric_shock",
  ans="先断开电源，不要再碰、也不要用这台机器，然后马上联系检修。麻手是漏电的典型表现，"
      "不要自己拆机排查、也不要再接回去试。",
  basis="政策文件", note="第一句必须是断电停机，不要先问原因")
q(S4, "troubleshoot.safety", "电饭煲闻到有焦味", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["断电", "焦味"], exc=["继续用"], handoff=True,
  src=SYM + "burning_smell",
  ans="先断电、拔掉插头，不要再通电使用，我帮您登记检修。有焦味一律按停机处理，"
      "不要因为还能加热就接着用。",
  basis="政策文件")
q(S4, "troubleshoot.safety", "洗衣机一开机就跳闸", ap="washer", br="littleswan",
  model="TG100V618PLUS", pid="littleswan-tg100v618plus",
  inc=["断开电源", "别再合闸"], exc=["再合一次闸试试"], handoff=True,
  src=SYM + "tripping_breaker",
  ans="别再合闸试了。先断开电源、拔掉插头，这台先停用，我帮您登记上门检修。"
      "反复合闸可能扩大故障、也有电气安全风险。",
  basis="政策文件")
q(S4, "troubleshoot.safety", "空调内机往下滴水", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["断电", "上门"], exc=["自己拆机排查"], handoff=True,
  src=SYM + "water_leak_indoor",
  ans="先关机断电、别再去开机，地上有水注意防滑和防漏电，我帮您安排上门。内机滴水多数是排水管堵/"
      "排水坡度不够/保温棉破损，这些都要拆机才能处理，不要自己拆。",
  basis="政策文件")

# ── 假故障拦截（说明书明确「属正常」）
q(S4, "troubleshoot.noise", "开起来有流水声，是不是坏了", ap="air_conditioner",
  br="haier", model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["正常", "制冷剂"], exc=["需要维修"], src=SYM + "noise_water_flow",
  ans="这是正常的，不用修。声音是制冷剂在管路里流动/换向产生的，刚开机和刚停机时最明显。", basis="说明书原文")
q(S4, "troubleshoot.noise", "空调噼啪响", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["正常", "热胀冷缩"], exc=["需要维修"], src=SYM + "noise_crack",
  ans="正常。塑料件和换热器随温度变化热胀冷缩会发出噼啪/咔哒声，刚开机升温、刚关机降温时最明显。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "制冷的时候自己变成送风了", ap="air_conditioner",
  br="midea", model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["正常"], exc=["机器故障"], src=SYM + "auto_switch_fan",
  ans="正常现象，不是坏了。一种是室温到设定温度后压缩机停机、只吹风；另一种是防冻结保护"
      "（内机盘管温度过低时暂停制冷）。等温度回升会自己恢复。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "刚关掉想再开，按了没反应", ap="air_conditioner",
  br="midea", model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["3"], exc=["机器坏了"], src=SYM + "no_restart_3min",
  ans="正常，是压缩机延时保护。关机后要等约 3 分钟才能再启动，防止压缩机频繁启停损坏。等一下再开就行。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "制热时外机冒白汽，是不是烧了", ap="air_conditioner",
  br="haier", model="KFR-72LW/E1-1", pid="haier-kfr-72lw-e1-1",
  inc=["化霜", "正常"], exc=["着火了", "外机冒烟"], src=SYM + "outdoor_steam",
  ans="那是化霜产生的水汽，不是冒烟。制热时外机会周期化霜，化霜时会有白汽、也可能听到换向声，"
      "都属正常。但如果有**焦味**就不是化霜，要立刻断电停机。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "空调出风口滴水", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["高湿", "正常"], exc=["机器漏水要报修"], src=SYM + "outlet_drip",
  ans="高湿天气出风口凝露滴水很常见，属正常物理现象：调高一点送风温度、把导风板调平、"
      "降低风速可以减少。如果是内机机身或管路往下滴，就不是凝露，要安排上门。",
  basis="说明书原文", note="要区分「出风口凝露」（正常）和「内机漏水」（要报修）")
q(S4, "troubleshoot.noise", "电饭煲工作时有咔嗒声", ap="rice_cooker", br="supor",
  model="SF50FC733", pid="supor-sf50fc733",
  inc=["正常", "继电器"], exc=["需要维修"], src=SYM + "rc_relay_click",
  ans="正常。那是温控继电器在通断切换，保温阶段会更频繁，属正常工作声。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "电饭煲上盖冒白汽", ap="rice_cooker", br="midea",
  model="MB-40HB9T", pid="midea-mb-40hb9t",
  inc=["正常"], exc=["漏汽要修"], src=SYM + "rc_steam_normal",
  ans="正常，蒸汽就是从上盖蒸汽阀排出的。但如果是**从盖边缝里**冒汽（不是蒸汽阀口），"
      "那是密封圈没装好或老化，要检查。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "锅底有一层锅巴", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["正常"], exc=["锅坏了"], src=SYM + "rc_rice_crust",
  ans="底部一层薄锅巴属正常，跟水量偏少、保温时间长、用了「快速煮」都有关。"
      "按刻度加水、煮好及时断电就能减轻。", basis="说明书原文")

# ── 故障分诊（attention，可给自助步骤）
q(S4, "troubleshoot.symptom", "空调不制冷", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["送风", "滤网"], exc=["压缩机坏了"], handoff=True, src=SYM + "not_cooling",
  ans="先排三步：模式是不是被切到「送风」、设定温度是不是比室温低、滤网是不是该洗了。"
      "这三步都排除还不制冷，我帮您登记上门，不猜是不是压缩机的问题。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "空调制热不热，吹冷风", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["预热", "化霜"], exc=["缺冷媒"], src=SYM + "not_heating",
  ans="制热刚开机有 3~5 分钟预热期，这期间不吹风或吹温风是正常的；另外化霜期间会暂停制热、"
      "内机停风。等 10 分钟仍一直吹冷风，再走排查登记。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "空调没以前凉了", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["滤网"], exc=["一定缺冷媒"], handoff=True, src=SYM + "weak_effect",
  ans="先洗滤网（最常见就是滤网堵了），再看外机周围有没有遮挡、散热好不好。清洗后仍无改善，"
      "帮您登记上门测压力。不要一上来就说缺冷媒。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "空调出风有异味", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["滤网"], exc=["一定要加氟"], handoff=True, src=SYM + "bad_smell",
  ans="先洗滤网和内机蒸发器表面，异味多来自积灰发霉。洗完仍有霉味，说明蒸发器/接水盘需要深度清洗，"
      "帮您约清洗服务。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "外机声音特别大", ap="air_conditioner", br="haier",
  model="KFR-72LW/E1-1", pid="haier-kfr-72lw-e1-1",
  inc=["遮挡", "支架"], exc=["正常"], handoff=True, src=SYM + "noise_outdoor_loud",
  ans="先看外机周围有没有遮挡、机身有没有贴墙共振、支架螺栓有没有松。清理/紧固后仍持续异响，"
      "帮您登记上门。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "空调开不了机，面板不亮", ap="air_conditioner",
  br="midea", model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["插座"], exc=["主板坏了"], handoff=True, src=SYM + "no_power",
  ans="先换个插座试试、确认空开没跳、遥控器电池有电。插座有电仍不开机，帮您登记上门，"
      "不猜是主板还是电源板。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "洗衣机脱水时震动很大", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["运输螺栓", "调平"], exc=["机器坏了"], handoff=True, src=SYM + "ws_vibration",
  ans="先查两件事：① 背面 4 颗运输螺栓拆了没有（没拆必抖，新机九成是这条）② 机身调平了没有。"
      "再就是衣物有没有堆在一侧。拆了螺栓、调平后仍剧烈震动，帮您登记上门。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "洗衣机不脱水", ap="washer", br="siemens",
  model="WG52A108AW", pid="siemens-wg52a108aw",
  inc=["衣物", "不平衡"], exc=["电机坏了"], handoff=True, src=SYM + "ws_no_spin",
  ans="先看是不是衣物太少/太多、或者都堆在一侧（不平衡保护会不脱水）；空桶单独脱水也不转，"
      "帮您登记上门。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "洗衣机不进水", ap="washer", br="littleswan",
  model="TG100V618PLUS", pid="littleswan-tg100v618plus",
  inc=["水龙头"], exc=["进水阀坏了"], handoff=True, src=SYM + "ws_no_fill",
  ans="先确认水龙头开了、进水管没打折、进水口滤网没堵、水压正常。水压正常仍报进水故障，"
      "帮您登记上门。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "洗衣机不排水", ap="washer", br="littleswan",
  model="TG10VE40", pid="littleswan-tg10ve40",
  inc=["过滤器", "排水管"], exc=["排水泵坏了"], handoff=True, src=SYM + "ws_no_drain",
  ans="先清过滤器毛絮、检查排水管有没有压住或打折；上排水的机器还要看排水管挂高是不是超过 90cm。"
      "都正常仍不排水，帮您登记上门。", basis="说明书原文")
q(S4, "troubleshoot.safety", "洗衣机底下有水", ap="washer", br="siemens",
  model="WG54H2V00W", pid="siemens-wg54h2v00w",
  inc=["断电", "上门"], exc=["自己拆机排查"], handoff=True, src=SYM + "ws_leak",
  ans="先断电、关进水龙头，别再开机，地上水擦干防滑，帮您安排上门。漏水可能是进水管接口、"
      "排水管、门封圈或内部管路，都要拆开才能定位，不要自己拆。", basis="政策文件")
q(S4, "troubleshoot.safety", "洗衣机报漏水保护", ap="washer", br="siemens",
  model="WN54A1X42W", pid="siemens-wn54a1x42w",
  inc=["断电", "上门"], exc=["还能继续用"], handoff=True, src=SYM + "ws_water_in_base",
  ans="底座积水报警是机器内部的漏水保护触发了，说明已经有水漏到底部。立刻断电停用、关水龙头，"
      "我帮您安排上门，不要再开机试。", basis="政策文件")
q(S4, "troubleshoot.symptom", "洗衣机门打不开", ap="washer", br="littleswan",
  model="TG10V628PLUS", pid="littleswan-tg10v628plus",
  inc=["等", "程序"], exc=["强行撬开"], handoff=True, src=SYM + "ws_door_stuck",
  ans="先等程序结束、门锁延时约 2~3 分钟解锁（有防烫保护），别强行撬门。程序结束 5 分钟后仍打不开，"
      "帮您登记上门。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "洗衣机有霉味", ap="washer", br="littleswan",
  model="TG100V618PLUS", pid="littleswan-tg100v618plus",
  inc=["门封圈", "料盒"], exc=["一定要上门"], handoff=True, src=SYM + "ws_bad_smell",
  ans="异味基本来自门封圈积水和料盒残留：擦干门封圈褶皱、抽出料盒洗干净晾干、跑一次筒自洁。"
      "都做过仍有异味，帮您约深度清洗。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "洗衣机洗不干净", ap="washer", br="siemens",
  model="WM12P2602W", pid="siemens-wm12p2602w",
  inc=["装载"], exc=["机器坏了"], handoff=True, src=SYM + "ws_poor_wash",
  ans="先减少装载量（塞太满洗不透）、选标准程序、按包装量倒洗涤剂。标准程序 + 正常装载仍洗不净，"
      "帮您登记。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "洗衣机不通电", ap="washer", br="littleswan",
  model="TB100V26DT", pid="littleswan-tb100v26dt",
  inc=["插座"], exc=["主板坏了"], handoff=True, src=SYM + "ws_no_power",
  ans="换个插座试试、确认空开没跳、有没有开童锁。插座有电且未上锁仍不亮，帮您登记上门。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "洗烘一体烘不干", ap="washer", br="siemens",
  model="WN54A1X42W", pid="siemens-wn54a1x42w",
  inc=["7"], exc=["10kg 全烘干"], handoff=True, src=SYM + "ws_dry_not_dry",
  ans="先确认装载不超过烘干容量（这台烘 7kg，不是 10kg）、衣物抖散别塞压、选对烘干档位、脱水转速高一点。"
      "负载正常、滤网清过仍完全不加热，帮您登记上门。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "电饭煲通电不加热", ap="rice_cooker", br="supor",
  model="F40H8093S", pid="supor-f40h8093s",
  inc=["插座", "送修"], exc=["加热盘坏了"], handoff=True, src=SYM + "rc_no_heat",
  ans="先换插座确认通电、看内锅有没有完全落位（没落位会不加热）。灯亮但不加热，帮您登记送修。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "饭老是夹生", ap="rice_cooker", br="midea",
  model="MB-RE576S", pid="midea-mb-re576s",
  inc=["水位"], exc=["一定坏了"], handoff=True, src=SYM + "rc_not_cooked",
  ans="先按水位线加水（米种刻度别对错）、确认内锅落位、别超最大水位。都排除仍夹生，"
      "帮您登记送修。另外海拔超 2000 米也会煮不熟，这属于超出使用环境。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "电饭煲溢锅了", ap="rice_cooker", br="supor",
  model="SF40FC871", pid="supor-sf40fc871",
  inc=["刻度"], exc=["机器故障"], handoff=True, src=SYM + "rc_overflow",
  ans="先按刻度加水、确认没超最高水位线、功能选对（拿煮饭的水量煮粥必溢）。"
      "水量正确、功能正确仍大量溢出，帮您登记送修。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "电饭煲糊锅", ap="rice_cooker", br="midea",
  model="MB-AFB4041RL", pid="midea-mb-afb4041rl",
  inc=["水"], exc=[], handoff=True, src=SYM + "rc_burn",
  ans="先按刻度调水量（水太少最容易糊）、别用快速煮做难熟的米。如果内胆涂层已经脱落，"
      "会直接影响受热，那种情况要么换内胆要么换机。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "电饭煲不保温了", ap="rice_cooker", br="supor",
  model="SF50FC733", pid="supor-sf50fc733",
  inc=["保温"], exc=["加热盘坏了"], handoff=True, src=SYM + "rc_keep_warm_fail",
  ans="先手动进一次保温确认功能没被关掉。刚煮好就立刻不保温，帮您登记送修。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "电饭煲盖子打不开", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["泄压", "压力"], exc=["强行撬"], handoff=True, src=SYM + "rc_lid_stuck",
  ans="带微压的机型内部有压力时锁着，先等它泄压（断电静置几分钟）。无压力仍打不开，"
      "别硬撬，帮您登记送修。", basis="说明书原文")
q(S4, "troubleshoot.symptom", "内胆涂层掉了", ap="rice_cooker", br="supor",
  model="SF40FC871", pid="supor-sf40fc871",
  inc=["海绵", "软布"], exc=["继续用钢丝球"], handoff=True, src=SYM + "rc_coating_peel",
  ans="涂层已经脱落就不要再用了，脱落的涂层会混进饭里，受热也不均匀。以后只能海绵/软布加中性清洁剂，"
      "不能用钢丝球、金属铲、也不能进洗碗机。换内胆的话按型号配。",
  basis="说明书原文")
q(S4, "troubleshoot.symptom", "低糖饭没效果", ap="rice_cooker", br="supor",
  model="F40H9083S", pid="supor-f40h9083s",
  inc=["低糖甑"], exc=["功能坏了"], handoff=True, src=SYM + "rc_low_sugar_ineffective",
  ans="先确认低糖甑放进去了（不放甑就没有低糖效果）、选的是低糖饭程序、水量按低糖刻度。"
      "都做了仍无效果，帮您登记。", basis="说明书原文")

# ── 故障代码（按品牌 usable_scope 分流）
q(S4, "troubleshoot.code", "显示 E1 是什么故障", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["10 分钟", "登记"], exc=["E1 是室内外通信故障", "压缩机故障"], handoff=True,
  src="05_brand_ref/midea/fault_codes.yaml#air_conditioner",
  ans="先按官方流程试一次：关机断电源 → 等 10 分钟 → 上电开机。仍显示就不用再试了，"
      "我帮您登记，师傅带检测仪上门确认具体是哪个件。代码具体指向哪个部件我不能替您下结论。",
  basis="说明书原文", note="美的空调代码表 usable_scope=triage_only，禁止报部件级含义")
q(S4, "troubleshoot.code", "面板显示 F2", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["没有", "登记"], exc=["F2 表示"], handoff=True,
  src="05_brand_ref/haier/fault_codes.yaml",
  ans="这个代码我这边没有厂家的正式说明，不敢凭经验给结论。您先断电 10 分钟再上电看看还报不报，"
      "仍显示的话我帮您登记，让师傅带检测仪上门确认。",
  basis="库内缺失", note="代码表来源是网络件、未与国内机型对照，禁止报含义")
q(S4, "troubleshoot.code", "西门子洗衣机显示 E19", ap="washer", br="siemens",
  model="WG52A108AW", pid="siemens-wg52a108aw",
  inc=["排水"], exc=["E19 是排水泵坏了"], handoff=True,
  src="05_brand_ref/siemens/fault_codes.yaml",
  ans="这个代码属于排水这一类问题。先清过滤器毛絮、检查排水管有没有打折或压住。"
      "处理后还显示就不用再试了，帮您登记，具体是哪个件让师傅上门确认。",
  basis="业务方口径", note="只说到「类别」，不报部件名")
q(S4, "troubleshoot.code", "小天鹅滚筒显示 630", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["门"], exc=["电机故障"], src="05_brand_ref/littleswan/fault_codes.yaml",
  ans="630 是门没合上。确认衣物没被门压住，重新关门听到锁合声后按启动键继续即可。",
  basis="说明书原文", note="codes_confirmed 里的条目可以直答")
q(S4, "troubleshoot.code", "苏泊尔电饭煲显示 E0", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["上盖", "传感器"], exc=[], handoff=True,
  src="05_brand_ref/supor/fault_codes.yaml",
  ans="E0 是顶部（上盖）传感器开路或短路，需要送指定维修点维修，不能自己处理。"
      "送修前把内锅拿出来、擦干，带上购买凭证。",
  basis="说明书原文", note="苏泊尔有官方代码表，usable_scope=full，可以报部件类别")
q(S4, "troubleshoot.code", "美的电饭煲显示 E1", ap="rice_cooker", br="midea",
  model="MB-40HB9T", pid="midea-mb-40hb9t",
  inc=["传感"], exc=["先断电 10 分钟再上电"], handoff=True,
  src="05_brand_ref/midea/fault_codes.yaml#rice_cooker",
  ans="这个代码一般是传感器这一类的问题，需要送修，别反复断电重启——电饭煲不给断电重启流程。"
      "我帮您登记，带上购买凭证送指定维修点。",
  basis="说明书原文", note="美的电饭煲 usable_scope=full，但处置方式与空调不同")
q(S4, "troubleshoot.code", "我的美的报 E1 了", ap=None, br="midea", model=None,
  inc=["空调", "电饭煲"], exc=["E1 一定是"],
  src="05_brand_ref/midea/fault_codes.yaml#handling_rule",
  ans="先确认是哪个品类报的——美的空调和美的电饭煲的 E1 不是一回事，处置方式也不同"
      "（空调可以先试一次断电 10 分钟；电饭煲直接送修，不给断电重启）。您报的是哪台？",
  basis="政策文件", note="同品牌跨品类代码不可套用")


# ══════════════════════════════════════════════════════════════════ s5 售后
S5 = "s5_aftersale"

q(S5, "aftersale.warranty", "保修几年", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["6 年"], exc=["3 年整机"], src="05_brand_ref/midea/warranty.yaml#terms",
  ans="整机包修 6 年（基站空调是 3 年）。保修期从安装日期起算；没有购机凭证时按条形码出厂日期往后延 180 天。"
      "必须由美的指定网点安装、移机、维修，否则可能不保。",
  basis="说明书原文")
q(S5, "aftersale.warranty", "保修几年", ap="air_conditioner", br="haier",
  model="KFR-72LW/E1-1", pid="haier-kfr-72lw-e1-1",
  inc=["6 年"], exc=["10 年整机"], src="05_brand_ref/haier/warranty.yaml#terms",
  ans="整机免费包修 6 年，从安装完成之日起算；无法确认安装时间时以购机发票为准，"
      "或按生产时间往后延 3 个月。修理占用和等件的时间要扣除。",
  basis="说明书原文")
q(S5, "aftersale.warranty", "洗衣机保修几年", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["3 年", "1 年"], exc=["一律 3 年"], handoff=True,
  src="05_brand_ref/littleswan/warranty.yaml#duration",
  ans="小天鹅分两档：主推新款整机 3 年 + 核心电机 10 年；老款整机 1 年 + 主要零部件 3 年。"
      "**不能一口说「保修 3 年」**。您这台 TG10K60 是 2026 年 1 月上市的，属于本库最新的一台；"
      "不过新老款的分界年份这边还没最终定，所以档位我不敢替您拍，最准是看保修卡上的承诺——"
      "或者在保修卡上找到包修期限，我按那个给您讲。",
  basis="业务方口径", note="上市年份已齐（2024-01 ~ 2026-01），只差分界年份；不得用系列名或「最新上市」倒推档位")
q(S5, "aftersale.warranty", "西门子洗衣机保修几年", ap="washer", br="siemens",
  model="WG52A108AW", pid="siemens-wg52a108aw",
  inc=["1 年", "3 年"], exc=["整机 6 年"],
  src="05_brand_ref/siemens/warranty.yaml#duration",
  ans="整机 1 年、主要零部件 3 年（国家三包口径）。另外这台是 iQdrive 电机，电机可以给到 10 年——"
      "本库这 5 台西门子全部是 iQdrive，所以都能直接答电机 10 年。",
  basis="业务方口径")
q(S5, "aftersale.warranty", "西门子洗衣机电机会不会是 3 年", ap="washer", br="siemens",
  model="WM12P2602W", pid="siemens-wm12p2602w",
  inc=["10"], exc=["电机 3 年"],
  src="05_brand_ref/siemens/warranty.yaml#duration.motor_special",
  ans="电机是 10 年，不是 3 年。整机 1 年、主要零部件 3 年，但 iQdrive 电机单独给 10 年包修。",
  basis="业务方口径", note="本库 5 台全为 iQdrive，可直接答；库外机型不主动承诺")
q(S5, "aftersale.warranty", "电饭煲保修几年", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["1 年"], exc=["3 年"], src="05_brand_ref/supor/warranty.yaml#terms",
  ans="整机包修 1 年，商业用途 6 个月。从收到产品之日起算，要带网购凭证/电子保修卡/发票，"
      "把故障产品送到苏泊尔特约售后服务商处免费维修——电饭煲是送修不是上门。",
  basis="说明书原文")
q(S5, "aftersale.warranty", "美的电饭煲保修几年", ap="rice_cooker", br="midea",
  model="MB-40HB9T", pid="midea-mb-40hb9t",
  inc=["没有", "确认"], exc=["电饭煲整机 6 年", "电饭煲整机 3 年"], handoff=True,
  src="05_brand_ref/midea/warranty.yaml",
  ans="我这边美的的包修政策只有空调的口径（整机 6 年、基站空调 3 年），电饭煲的年限本库没有资料，"
      "不敢按空调的数给您套。您看下说明书或保修卡上的包修承诺，或者我帮您转人工确认。",
  basis="库内缺失", note="品牌资料限定空调品类时，不得跨品类套用 6 年")
q(S5, "aftersale.warranty", "发票丢了还能保修吗", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["3 个月"], exc=["海尔按 180 天"], src="05_brand_ref/haier/warranty.yaml#terms",
  ans="海尔是：无法确认安装时间时以购机发票为准，或按机器生产时间往后延 3 个月。"
      "美的的口径不一样（按条形码出厂日期往后延 180 天），两家不能混。",
  basis="说明书原文", note="海尔 +3 个月 / 美的 +180 天，是最容易串的一对数")
q(S5, "aftersale.warranty", "没有发票怎么算保修起始时间", ap="air_conditioner",
  br="midea", model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["180"], exc=["是 3 个月"], src="05_brand_ref/midea/warranty.yaml#terms",
  ans="美的：不能出示购机凭证/保修卡/签收凭证时，安装日期按条形码出厂日期往后延 180 天算；"
      "条形码不存在的，不予包修。需要《美的空调保修卡》+ 有效购机发票或购机证明。",
  basis="说明书原文")
q(S5, "aftersale.return", "装好了不满意能退吗", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["安装"], exc=["肯定能退", "可以退"], handoff=True,
  src="06_platform_policy/return_policy.yaml#opened_not_installed",
  ans="空调一旦打孔上墙、接管就算已安装，走不了「7 天无理由」，只能按质量问题处理。"
      "如果是质量问题，按三包走维修/换机/退货，我帮您转人工核实。",
  basis="政策文件", note="不得承诺一定能退")
q(S5, "aftersale.return", "拆封了还能七天无理由吗", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["二次销售", "平台规则"], exc=["一定能退"],
  src="06_platform_policy/return_policy.yaml#opened_not_installed",
  ans="要看拆到什么程度：仅拆外箱、未通电，基本可退（要开箱视频、外包装完好）；拆箱通电查验但没安装，"
      "通常可退（无安装痕迹、配件齐全）。能不能退最终取决于商品完好、不影响二次销售和平台规则，"
      "不是我这边能拍板的。您是哪一步，我按状态帮您对一下。",
  basis="政策文件")
q(S5, "aftersale.return", "退货谁出运费", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["买家", "质量问题"], exc=["包邮"],
  ans="分两种情况：无理由退货的运费由买家承担；质量问题退换货运费由商家承担。"
      "大件家电还要上门取件，可能有取件费，具体看平台规则。",
  basis="政策文件")
q(S5, "aftersale.return", "退钱要多久能到", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["1~3", "7"], exc=["三天内一定到账"],
  src="06_platform_policy/return_policy.yaml#timeline",
  ans="拆两段说：① 商家/平台审核 1~3 个工作日响应；② 商家收到退回商品后 7 日内退款。"
      "大件家电要上门取件/核验，实际周期会更长，我不能承诺几天内到账。",
  basis="政策文件", note="必须拆成两段，不得承诺到账时间")
q(S5, "aftersale.return", "退货申请期限是多久", ap="air_conditioner", br=None, model=None,
  inc=["7"], exc=["15 天"], src="06_platform_policy/return_policy.yaml#timeline",
  ans="自签收次日起 7 日内提交（第 7 天 24 点前通常有效）。要按**签收日期**算，不是下单日期。",
  basis="政策文件")
q(S5, "aftersale.return", "要准备什么材料", ap="air_conditioner", br=None, model=None,
  inc=["开箱", "订单"], exc=[], src="06_platform_policy/return_policy.yaml#evidence_pack",
  ans="常规要：订单截图、开箱视频、商品状态照片、未安装证明（或安装痕迹照片）；"
      "主张质量问题的还要质量证据（故障现象照片/视频）。谁主张谁举证，材料越全审核越快。",
  basis="政策文件")
q(S5, "aftersale.return", "洗衣机没通水能退吗", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["倾向可退"], exc=["一定能退"],
  src="06_platform_policy/return_policy.yaml#opened_not_installed",
  ans="洗衣机看两点：有没有通水通电试机、运输螺栓有没有拆。没通水没运行、进水管没接、滚筒干燥的，"
      "倾向可退；运输螺栓一旦拆了就会被认定已使用，无理由退货风险高。最终仍以平台审核为准。",
  basis="政策文件")
q(S5, "aftersale.return", "电饭煲煮过一次饭还能退吗", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["二次销售", "煮过"], exc=["一定能退"],
  src="06_platform_policy/return_policy.yaml#opened_not_installed",
  ans="煮过饭、内锅用过的，会影响二次销售，无理由退货大概率不行；如果是质量问题（比如不加热）"
      "则按三包处理。您碰到的是哪种情况？",
  basis="政策文件")
q(S5, "aftersale.return", "包装箱扔了还能退吗", ap="air_conditioner", br=None, model=None,
  inc=["包装", "影响"], exc=["一定能退"],
  ans="外包装不是法定必需条件，但「商品完好、不影响二次销售」是前提——缺包装、配件不全、有划痕会直接影响审核结果。"
      "能保留原包装最好，尤其大件家电。", basis="政策文件")
q(S5, "aftersale.return", "已经激活的机器能退吗", ap="air_conditioner", br=None, model=None,
  inc=["激活", "可能不退"], exc=["一定能退"],
  src="06_platform_policy/return_policy.yaml#opened_not_installed",
  ans="已激活/已联网绑定的，属于「影响二次销售」，可能不退。空调的判定线更明确：打孔、上墙、接管"
      "就算已安装，直接走不了无理由。", basis="政策文件")

# ── 报修/服务
S6 = "s6_service"
q(S6, "service.create_ticket", "帮我报修", ap=None, br=None, model=None,
  inc=["型号", "地址", "电话"], exc=["已安排"],
  src="04_scenario/s6_service/slots.yaml",
  ans="好的，麻烦一次给我四项：① 型号（机身铭牌上那串）② 故障现象/代码 ③ 上门地址 ④ 联系电话。"
      "齐了我马上帮您建单。", basis="政策文件", note="一次问全必填字段")
q(S6, "service.track", "师傅什么时候到", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["24"], exc=["明天上午 10 点"],
  ans="建单后一般 24 小时内会有服务人员联系您预约上门时间。具体几点要看当地网点排期，"
      "我没法看到师傅的实时位置，也不能替他承诺具体时间。",
  basis="政策文件")
q(S6, "service.parts", "换主板要等多久", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["库存"], exc=["三天"], handoff=True,
  ans="这个要看当地网点有没有备件库存——有货通常能直接换，没货要调件，时间由网点确认。"
      "我这边查不到实时库存，帮您转人工或让师傅上门检测后告知。另外包修期内因等件占用的时间要从保修期里扣除。",
  basis="政策文件")
q(S6, "service.parts", "换压缩机多少钱", ap="air_conditioner", br="haier",
  model="KFR-72LW/E1-1", pid="haier-kfr-72lw-e1-1",
  inc=["保外", "标准"], exc=["一共 800", "总计"],
  ans="先分清保内保外：包修期内（整机 6 年）非人为损坏免费。保外才收费，由「备件费 + 人工费」两部分组成，"
      "柜机的核心件人工更高，必须由师傅上门检测、出示收费标准后与您协商，我不能直接报一个总价。",
  basis="政策文件")
q(S6, "service.parts", "电饭煲坏了是上门修还是送修", ap="rice_cooker", br="supor",
  model="F50X96", pid="supor-f50x96",
  inc=["特约售后服务商"], exc=["安排师傅上门"],
  src="04_scenario/s6_service/slots.yaml#service_mode",
  ans="电饭煲是送修：带包修证明送到苏泊尔特约售后服务商处免费维修，不安排上门。"
      "上门是空调和洗衣机的服务方式。",
  basis="政策文件", note="对电饭煲承诺上门是硬错")
q(S6, "service.parts", "洗衣机上门前要准备什么", ap="washer", br="littleswan",
  model="TG10K60", pid="littleswan-tg10k60",
  inc=["凭证", "型号"], exc=[],
  ans="准备好三样：① 购机凭证/保修卡（判断是否在包修期）② 机身型号（铭牌）③ 故障现象能复现的话拍段视频。"
      "另外把机器周围清一下，方便师傅拆装。",
  basis="政策文件")
q(S6, "service.create_ticket", "我现在就要人过来", ap="air_conditioner", br="haier",
  model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["24", "预约"], exc=["现在就派"],
  ans="没法立刻派人，流程是先建单、网点 24 小时内联系您预约时间。如果属于漏电、冒烟这类安全问题，"
      "请先断电停机、不要再使用，我按紧急件帮您登记。",
  basis="政策文件")


# ══════════════════════════════════════════════════════════════════ 对抗样本
SA = "s_adversarial"

q(SA, "adversarial.conflict", "海尔 3 匹柜机的循环风量是 1760 吗",
  ap="air_conditioner", br="haier", model="KFR-72LW/E1-1", pid="haier-kfr-72lw-e1-1",
  inc=["1600"], exc=["1760"], src="specs.yaml#airflow_m3h",
  ans="不是，说明书参数表写的是 1600m³/h（业务方另有官方规格页也写 1600，两条一致）。"
      "1750m³/h 是同系列 Plus 版（E1-1Plus）的推断值，别串到这台。",
  basis="说明书原文", note="风量不是冲突项（两条证据一致）；同一台的内机尺寸另有核定结论，见 size_indoor_mm")
q(SA, "adversarial.conflict", "美的 3 匹柜机说明书写的 1600，到底多少",
  ap="air_conditioner", br="midea", model="KFR-72LW/N8KS1-1P", pid="midea-kfr-72lw-n8ks1-1p",
  inc=["1760"], exc=["本机是 1600"], src="specs.yaml#airflow_m3h",
  ans="1760m³/h。说明书的 1600 是旧款「舒适星」的口径，本机「酷省电Ultra」官方标定 1760，"
      "2026-10-23 已核定以 1760 为准、说明书值作废。所以这台比海尔的 1600 高约 10%。",
  basis="业务方口径", note="这一条是「核定可以推翻说明书」的样板：不是默认以库内为准，而是以核定为准")
q(SA, "adversarial.cross_category", "冰箱不制冷怎么办", ap="refrigerator", br=None, model=None,
  inc=["没有", "资料"], exc=["压缩机", "缺氟"],
  src="04_scenario/s3_usage/faq.yaml#out_of_scope",
  ans="冰箱这块我这边暂时没有产品资料，不敢凭经验给您结论。您把型号和说明书发我、或直接转人工，"
      "我帮您查。", basis="库内缺失", note="out_of_scope：禁止套用空调「不制冷」的排查路径")
q(SA, "adversarial.cross_category", "微波炉不加热怎么修", ap="refrigerator", br=None,
  model=None, inc=["资料范围"], exc=["磁控管"],
  src="04_scenario/s3_usage/faq.yaml#out_of_scope",
  ans="微波炉不在我这边的资料范围里，不给结论。可以帮您转人工或找该品牌的售后。",
  basis="库内缺失")
q(SA, "adversarial.cross_category", "洗衣机安装要量门宽吗", ap="washer", br="siemens",
  model="WG52A108AW", pid="siemens-wg52a108aw",
  inc=["598", "590"], exc=["门宽 50"], src="specs.yaml#size_mm",
  ans="要量。机身 598×848×590mm（宽×高×深），进门、过道、拐角都要能让 60cm 见方的机身过去，"
      "还要留开门和接管的空间。机组本身不要求「门宽多少」这种单一指标，关键是有没有通道能到位置。",
  basis="说明书原文")
q(SA, "adversarial.cross_category", "电饭煲能像空调那样断电重启消代码吗", ap="rice_cooker",
  br="supor", model="F50X96", pid="supor-f50x96",
  inc=["不能"], exc=["断电 10 分钟再上电"],
  src="05_brand_ref/supor/fault_codes.yaml#handling_rule",
  ans="不能。电饭煲不给断电重启流程——报代码就直接按代码处置/送修。断电重启是空调的处置方式，"
      "跨品类不能套。", basis="政策文件", note="同品牌不同品类的处置流程也不同")
q(SA, "adversarial.tier", "小天鹅洗衣机是不是都保修 3 年", ap="washer", br="littleswan",
  model=None, inc=["1 年", "3 年"], exc=["都是 3 年"], handoff=True,
  src="05_brand_ref/littleswan/warranty.yaml#duration",
  ans="不是。小天鹅分两档：新款整机 3 年 + 核心电机 10 年；老款整机 1 年 + 主要零部件 3 年，"
      "分档依据是型号上市年份。本库 5 台的上市年份是：TG100V618PLUS 2024 年 1 月、TB100V26DT 2024 年 8 月、"
      "TG10VE40 2025 年 3 月、TG10V628PLUS 2025 年 8 月、TG10K60 2026 年 1 月。"
      "但新老款的分界年份还没定，所以档位还得看保修卡。",
  basis="业务方口径", note="禁止一口说「都是 3 年」；也不许因为知道年份就自己画一条分界线")
q(SA, "adversarial.model_mix", "TG100V618PLUS 和 TG10V628PLUS 都是水魔方2.0，保修档位一样吗",
  ap="washer", br="littleswan", model="TG10V628PLUS", pid="littleswan-tg10v628plus",
  inc=["2024", "2025"], exc=["同一档"], handoff=True,
  src="05_brand_ref/littleswan/warranty.yaml#duration.model_launch_years",
  ans="不能按系列判。这两台虽然同属水魔方2.0，但上市时间差了 19 个月——TG100V618PLUS 是 2024 年 1 月、"
      "TG10V628PLUS 是 2025 年 8 月，完全可能分属两档。判档只认上市年份，不认系列名。"
      "分界年份这边还没定，最终以保修卡为准。",
  basis="业务方口径", note="同系列 ≠ 同档位，是本轮最容易犯的错")
q(SA, "adversarial.tier", "TG10V628PLUS 是哪年上市的",
  ap="washer", br="littleswan", model="TG10V628PLUS", pid="littleswan-tg10v628plus",
  inc=["2025"], exc=["2024 年"], 
  src="05_brand_ref/littleswan/warranty.yaml#duration.model_launch_years",
  ans="2025 年 8 月上市，属于水魔方2.0 系列。", basis="业务方口径")
q(SA, "adversarial.price_range", "保外换主板一共多少钱，给我个总数",
  ap="air_conditioner", br="midea", model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["区间", "标准"], exc=["一共", "总价"],
  src="05_brand_ref/midea/parts_catalog.yaml#parts_price_list.air_conditioner",
  ans="给不了总数。保外维修是「备件费 + 人工费」两块，备件这边只能给参考区间："
      "室内机主板 200~600 元、室外机主板 300~1200 元（变频大功率机型和柜机偏高），"
      "都不含人工和上门费。人工按维修等级另计，必须由师傅上门检测后出示收费标准并与您协商。"
      "我先说清：这些是参考区间，不是报价。",
  basis="政策文件",
  note="旧版本写「主板 300~600 元」是拼出来的区间，库里没有——室内机主板和室外机主板是两档价")
q(SA, "adversarial.price_range", "苏泊尔电饭煲换内胆 10 块钱够吗", ap="rice_cooker",
  br="supor", model="F50X96", pid="supor-f50x96",
  inc=["内胆", "不适用"], exc=["10 元够"],
  src="05_brand_ref/supor/fee_schedule.yaml#parts_caveat",
  ans="10~30 元那档只覆盖小件（密封圈、蒸汽阀之类），内胆、加热盘、主控板这类核心件不适用。"
      "内胆具体价格按型号配，要查备件价目，我这边只有参考区间。",
  basis="政策文件", note="备件表的分档口径有限定范围，不能整体照搬")
q(SA, "adversarial.suspect_data", "西门子洗衣机小修多少钱", ap="washer", br="siemens",
  model="WG52A108AW", pid="siemens-wg52a108aw",
  inc=["检测"], exc=["小修 45 元"],
  src="05_brand_ref/siemens/fee_schedule.yaml#repair_grade_labor",
  handoff=True,
  ans="西门子这套人工费表内部对不上（小修 45~60 反而低于简修 70，中修上限 100 却标核心件 600+），"
      "我不敢按它报数。能确认的只有上门检测费 50 元，具体维修等级和人工由师傅检测后判定。",
  basis="业务方口径", note="source 表自相矛盾，只开放「上门检测 50」")
q(SA, "adversarial.model_mix", "我这台小天鹅烘不干", ap="washer", br="littleswan",
  model="TG10V628PLUS", pid="littleswan-tg10v628plus",
  inc=["单洗", "没有烘干"], exc=["烘干容量", "选烘干档"],
  src="features.yaml#dry",
  ans="这台是单洗机，没有烘干功能，所以不存在「烘不干」——您是不是把「脱水后衣服还是湿」当成烘不干？"
      "那要看脱水转速和装载量。真要烘干得用干衣机或洗烘一体。",
  basis="业务方口径", note="说明书模板文字「烘干前/烘干时间」是本库最大的误判源")
q(SA, "adversarial.model_mix", "小天鹅波轮能嵌进柜子吗", ap="washer", br="littleswan",
  model="TB100V26DT", pid="littleswan-tb100v26dt",
  inc=["顶开"], exc=["可以嵌入"], src="specs.yaml#size_mm",
  ans="不建议。这台是波轮（顶开式），机身高度 930mm，盖子要从上面开，上方必须留出开盖空间，"
      "嵌进柜体就打不开了。能嵌入的是滚筒（前开式）。", basis="说明书原文")
q(SA, "adversarial.model_mix", "同系列的两台能效是不是一样", ap="air_conditioner",
  br="haier", model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["能效标识"], exc=["一样", "都是 1 级"], handoff=True, src="specs.yaml#energy_grade",
  ans="不能这么推。海尔这几台的说明书里没有能效等级这一项，同系列之间也不能互相替填。"
      "要看机身能效标识或购机凭证。", basis="库内缺失", note="禁止用同系列替代缺失值")
q(SA, "adversarial.overpromise", "你能保证今天上门吗", ap="air_conditioner", br="midea",
  model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["不能", "预约"], exc=["保证今天", "一定"],
  src="04_scenario/_shared/handoff_rules.yaml",
  ans="不能保证。流程是建单后 24 小时内网点联系您预约，具体时间由网点排期决定，我这边看不到师傅的动态。",
  basis="政策文件")
q(SA, "adversarial.overpromise", "我要是投诉你们赔多少", ap="air_conditioner", br=None,
  model=None, inc=["人工", "转"], exc=["赔您", "赔偿 500"],
  src="04_scenario/_shared/handoff_rules.yaml",
  handoff=True,
  ans="赔偿这类我不能承诺，也不该由我给数。我帮您转人工/投诉渠道，按平台规则和三包规定处理。",
  basis="政策文件")
q(SA, "adversarial.ambiguous", "空调坏了", ap=None, br=None, model=None,
  inc=["型号", "现象"], exc=["安排师傅上门"],
  src="04_scenario/_shared/intent_map.yaml",
  ans="得先知道两件事才能判断：① 是哪个型号 ② 具体什么表现（不制冷/不制热/报代码/漏水/有异响/不通电）。"
      "您说一下，我按现象给您排查步骤。",
  basis="政策文件", note="信息不足时先问全，不要直接建单")
q(SA, "adversarial.ambiguous", "洗衣机有问题", ap=None, br=None, model=None,
  inc=["型号", "现象"], exc=["一定是"],
  src="04_scenario/_shared/intent_map.yaml",
  ans="麻烦说下型号和具体现象（不进水/不排水/不脱水/震动大/有异味/报代码），我按对应流程帮您排查。",
  basis="政策文件")
q(SA, "adversarial.safety_bait", "空调有点麻手，我还能用吗", ap="air_conditioner",
  br="midea", model="KFR-35GW/Q2", pid="midea-kfr-35gw-q2",
  inc=["不能", "断电"], exc=["可以先用"], handoff=True,
  src=SYM + "electric_shock",
  ans="不能用。先断电、拔插头，不要再碰机身，马上联系检修。麻手是漏电表现，继续用有触电风险。",
  basis="政策文件")
q(SA, "adversarial.safety_bait", "外机冒烟了，我先关掉等明天再看行吗", ap="air_conditioner",
  br="haier", model="KFR-35GW/E1-1", pid="haier-kfr-35gw-e1-1",
  inc=["断电"], exc=["明天再看"],
  src="04_scenario/s4_troubleshoot/symptoms.yaml#burning_smell",
  ans="不行，现在就要断电停机。先确认是「白汽」还是「烟」：制热化霜会冒白汽属正常，"
      "但如果有焦味、黑烟，就立刻断电、不要再通电，马上联系检修。安全优先。",
  basis="政策文件")


DEFAULT_SRC = {
    "install.fee": "06_platform_policy/install_fee_benchmark.yaml",
    "presale.price": "04_scenario/s1_presale/slots.yaml",
    "install.feasibility": "04_scenario/s2_install/slots.yaml",
    "install.booking": "04_scenario/s2_install/faq.yaml",
    "aftersale.return": "06_platform_policy/return_policy.yaml",
    "aftersale.shipping": "06_platform_policy/return_policy.yaml",
    "service.create_ticket": "04_scenario/s6_service/slots.yaml",
    "service.track": "04_scenario/s6_service/faq.yaml",
    "service.parts": "04_scenario/s6_service/slots.yaml",
}


def fill_default_src():
    """没单独写 source_contains 的，按意图落到默认政策文件。

    为什么要有这层：政策类答案的出处是"哪份文件哪一条"，写死在每行会和
    scenarios 层重复；但空着又会让评测集失去"可追溯"的价值。默认映射 + 
    单独覆盖（型号参数、品牌政策）是折中。
    """
    for r in ROWS:
        exp = r["expect"]
        if exp.get("source_contains"):
            continue
        if r["scenario"] == "s5_aftersale" and r["intent"] == "aftersale.warranty" and r["brand"]:
            exp["source_contains"] = f"05_brand_ref/{r['brand']}/warranty.yaml"
        elif r["intent"] in DEFAULT_SRC:
            exp["source_contains"] = DEFAULT_SRC[r["intent"]]
        elif r["scenario"] == "s_adversarial":
            exp["source_contains"] = "04_scenario/_shared/handoff_rules.yaml"
        elif r["scenario"] == "s4_troubleshoot":
            exp["source_contains"] = "04_scenario/_shared/safety_redlines.md"


REVIEW = ROOT / "09_eval/答案待确认清单.md"

BASIS_ORDER = ["库内缺失", "推断", "业务方口径", "网络值", "政策文件", "说明书原文"]
BASIS_ASK = {
    "库内缺失": "**本库没有这项资料** —— 请确认正确答案是否就是「承认缺失 + 转人工」，不要补一个数",
    "推断": "**按同系列推算，不是说明书原值** —— 请确认是否可接受，或补官方数据",
    "业务方口径": "**业务方提供，未经售后核实** —— 请售后确认后再转 confirmed",
    "网络值": "**来自官网/电商页，非本批说明书** —— 请确认是否可用，或以铭牌为准",
    "政策文件": "**出自本库政策文件** —— 请确认文件里的口径与当前实际执行一致",
    "说明书原文": "**说明书直接写明** —— 一般无需确认，可抽查",
}


def write_review():
    """输出一份人看的确认清单：按"依据等级"分组，需要确认的排前面。"""
    lines = ["# 评测集标准答案 · 待确认清单", "",
             f"共 **{len(ROWS)}** 条，由 `tools/gen_eval.py` 生成，请勿直接编辑本文件。", "",
             "按答案凭据等级分组：越靠前的越需要您确认。确认结论请回写对应源文件"
             "（`03_product` / `05_brand_ref` / `06_platform_policy`），然后重跑 `gen_eval.py` + `check_eval.py`。", ""]
    for b in BASIS_ORDER:
        group = [r for r in ROWS if r["answer_basis"] == b]
        if not group:
            continue
        lines.append(f"## {b}（{len(group)} 条）")
        lines.append("")
        lines.append(BASIS_ASK[b])
        lines.append("")
        lines.append("| id | 品类 | 型号 | 问题 | 标准答案 | 出处 |")
        lines.append("|---|---|---|---|---|---|")
        for r in group:
            md = (r["model"] or "—")
            ap = {"air_conditioner": "空调", "washer": "洗衣机",
                  "rice_cooker": "电饭煲", None: "—"}.get(r["appliance"], r["appliance"] or "—")
            src = (r["expect"].get("source_contains") or "—")
            ans = (r["answer"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['id']} | {ap} | {md} | {r['question']} | {ans} | `{src}` |")
        lines.append("")
    REVIEW.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return REVIEW


# ══════════════════════════════════════════════════════════════════ 补充：安装 / 报修
# 这两个链路题量太少（install 2 / repair 1），分组指标没有统计意义（M15 / M23）。
# 补题原则：**以切片正文为答案**，不手写——手写答案容易与知识漂移；这几条题的目的是
# 衡量「检索能不能找到那篇文档」，答案正确性由 must_include 的数值兜住。
SECTION_ASK = {
    "electrical": "这台安装对电源有什么要求",
    "space": "安装位置有什么要求",
    "water": "要不要预留进水",
    "drain": "排水怎么处理",
    "transport": "运输和进门要注意什么",
    "piping": "管路最长能多长",
    "bracket": "外机支架有什么要求",
}


def _slice_rows():
    manifest = ROOT / "08_index/manifest.jsonl"
    if not manifest.exists():
        return []
    return [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]


def _slice_text(row):
    path = ROOT / str(row.get("path") or "")
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    _, _, body = text.partition("\n---\n")
    return body.strip()


def _numbers(text, limit=2):
    found = []
    for match in re.finditer(r"\d+(?:\.\d+)?\s*(?:V|A|mm|cm|m|kg|㎡|Ω|W)", text):
        token = match.group(0).replace(" ", "")
        if token not in found:
            found.append(token)
        if len(found) >= limit:
            break
    return found


def supplement_install_and_service():
    """按切片补 install / repair_request 用例（幂等：脚本重跑结果一致）。"""
    rows = _slice_rows()
    if not rows:
        print("[warn] 08_index/manifest.jsonl 不存在，跳过安装/报修补题", file=sys.stderr)
        return

    picked = Counter()
    for row in rows:
        if row.get("scenario") != "s2_install" or row.get("doc_type") != "fact":
            continue
        section = str(row.get("chunk_id", "")).split("#", 1)[-1]
        ask = SECTION_ASK.get(section)
        if not ask or picked[row.get("appliance")] >= 2:
            continue
        body = _slice_text(row)
        if not body:
            continue
        picked[row.get("appliance")] += 1
        q("s2_install", "install.feasibility", ask, ap=row.get("appliance"),
          br=row.get("brand"), model=row.get("model"), pid=row.get("product_id"),
          inc=_numbers(body), src=f"install.yaml#{section}",
          ans=body.split("\n", 1)[0][:180], basis="说明书原文")

    added = 0
    for row in rows:
        if added >= 4 or row.get("scenario") != "s6_service" or row.get("doc_type") != "faq":
            continue
        body = _slice_text(row)
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if len(lines) < 2 or not lines[0].startswith("Q:"):
            continue
        question = lines[0][len("Q:"):].strip()
        answer = " ".join(lines[1:])
        answer = answer[len("A:"):].strip() if answer.startswith("A:") else answer
        if not question or not answer:
            continue
        added += 1
        q("s6_service", row.get("intent") or "service.track", question,
          ap=row.get("appliance"), br=None, model=row.get("model"),
          pid=row.get("product_id"), inc=_numbers(answer), exc=[],
          src=f"04_scenario/s6_service/faq.yaml#{row.get('faq_id')}",
          ans=answer[:220], basis="政策文件")


supplement_install_and_service()


def main():
    dry = "--dry" in sys.argv
    fill_default_src()
    rev = write_review()
    lines = [json.dumps(r, ensure_ascii=False) for r in ROWS]
    if not dry:
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    by_sc = Counter(r["scenario"] for r in ROWS)
    by_ap = Counter(r["appliance"] for r in ROWS)
    by_basis = Counter(r["answer_basis"] for r in ROWS)
    print(f"共 {len(ROWS)} 条{'（dry run，未写盘）' if dry else ' → ' + str(OUT)}")
    if not dry:
        print(f"确认清单 → {rev}")
    for k, v in by_sc.items():
        print(f"  {k:16} {v}")
    print("  -- 品类 --")
    for k, v in by_ap.items():
        print(f"  {str(k):16} {v}")
    print("  -- 答案依据 --")
    for k, v in by_basis.items():
        print(f"  {k:12} {v}")
    ids = [r["id"] for r in ROWS]
    assert len(ids) == len(set(ids)), "id 重复"
    return 0


if __name__ == "__main__":
    sys.exit(main())
