#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电饭煲参数回填（方案 B：脚本生成 + 人工核对清单）

电饭煲和空调/洗衣机有三处结构性差别，脚本必须体现：

  1. **没有安装**。空调看外机位、洗衣机看水电位，电饭煲拆箱插电就能用。
     install.yaml 不写"怎么装"，只写"插座能不能带得动、放哪儿不能放、开箱验什么"。
     把空调的「高空费/加长铜管」照搬过来会让客服问出完全不存在的费用。

  2. **参数普遍缺失**。8 份资料里有 5 份根本没有参数表（说明书只讲结构+操作+故障），
     容量只能靠型号编码推断，功率/尺寸/净重全是 null。
     这里必须老实写 null 并进 missing，不能拿同品牌同容积的其它型号代填。

  3. **同一份说明书覆盖多个型号**。苏泊尔尤其明显（F50X96 那份列了 20 多个型号）。
     型号别名（CFXB50FC9133Q-75 之类）要落 alias_model，否则用户报别名检索不到。

数据纪律同前：每项带 source；说明书没写的写 null 并进 missing；
推断值加 confidence: inferred。OCR 出来的数值在 note 里标注需对照原图复核。

用法:
  python tools/backfill_rice_cooker.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "03_product/rice_cooker"
UPDATED = "2026-10-23"


def q(s):
    s = str(s)
    if re.search(r"[:'\"#\[\]{}&*!|>%@`]", s) or s != s.strip():
        return "'" + s.replace("'", "''") + "'"
    return s


# ---------------------------------------------------------------- 数据

# 通用：8 份资料里「煮饭不好吃」的排查口径基本一致，只有加热器件的叫法不同
# （电饭煲=电热盘/发热盘，IH=线圈盘），所以在 DATA 里用 "heater" 区分。
COMMON_CAUSE = (
    "米水比例不对（没按水位线加水） / 功能选错 / 内锅底或加热盘有异物 / "
    "蒸汽阀（微压阀）没装到位 / 内锅变形"
)
COMMON_ACTION = (
    "先按水位线重新加水并选对应功能；擦干并清掉内锅底与加热盘上的异物；"
    "确认蒸汽阀装到位、听到卡合声；内锅变形则送修更换，不可继续使用"
)

NORMAL_RICE = [
    ("relay_click", "工作过程中有「咔嗒」通断声",
     "继电器/温控器通断的正常声音，不是故障"),
    ("steam_from_valve", "上盖蒸汽阀冒白汽、有水汽",
     "微压/排气设计的正常排气，靠蒸汽阀控压，不要堵住出汽口"),
    ("rice_crust", "锅底有一层薄锅巴",
     "底盘加热在最后焖饭阶段升温收水，薄锅巴属正常；厚到发黑才是异常"),
]

# 网络核对值统一用这个 source，并配 confidence: web。
# 为什么单独标：这些数字来自品牌官方产品页/电商详情页，**不是**本批说明书的原文。
# 客服可以拿它回答"放得下吗/多重"，但被追问"说明书哪页写的"时必须承认是网络值。
WEB_SRC = "web:品牌官方产品页/电商详情页（2025-04 抓取）"
WEB_NOTE = "网络核对值，非本批说明书原文；与铭牌/包装标贴复核后可转为 confirmed"

DATA = {
    # ------------------------------------------------ 美的（3 个，纯图片资料）
    "MB-40HB9T": dict(
        rice_lines=["东北米", "丝苗米", "五常米"],
        src_cook="ocr#img:快速入门.png", src_timer="ocr#img:使用技巧-预约.png", src_clean="ocr#img:清洁保养.png",
        ocr_scope="images",
        brand="midea", brand_cn="美的",
        ptype="电饭煲（IH 电磁加热，4L）",
        colloquial="4升IH电饭煲",
        src="ocr#img:产品简介.png",
        cap=4.0, cap_note="原文「额定容量 4.0L」",
        cups=None, cups_note="说明书未给额定煮饭量（杯数），只给了水位线刻度",
        power=1300, voltage="220V~，50Hz",
        heating="IH 电磁加热", heating_conf="inferred",
        heating_note="说明书未直写「IH」，但故障章写「线圈盘」、额定功率 1300W（4L 底盘加热典型值 760W）、热效率 90% 一级能效，三点指向 IH；对外话术按 IH 说，若用户质疑以铭牌为准",
        pot=None,
        pot_note="说明书只写「内锅/内胆」，未标材质；清洁章写「请勿用钢丝球清洗内锅，以免刮伤内锅表面」，说明有涂层但涂层种类未标注",
        size=(338, 235, 270),
        size_src=WEB_SRC, size_conf="web",
        size_note="原文按「长×宽×高」标 338×270×235mm，已换算为 宽×高×深；同页标净重 5.9kg。属网络核对值",
        weight=5.9, weight_src=WEB_SRC, weight_conf="web",
        energy=1, eff=90, standby=1.6, warm_wh=34,
        warm_max=24, warm_advice=5,
        warm_note="原文「产品可以实现 24 小时保温，但是建议保温不超过 5 小时，以免米饭食物颜色变黄，变味」",
        timer_max=12,
        timer_note="原文「预约时间建议不要超过 12 小时，否则米饭会出现异味」；短按「预约」+10 分钟、长按 +1 小时",
        timer_block=["热饭", "保温"],
        programs=["快速饭", "柴火饭", "香糯粥", "舒胃饭", "杂粮粥", "原味蒸",
                  "稀饭", "煲汤", "热饭", "烩稻香"],
        rice_types=["长粒米", "圆粒米"],
        textures=["软糯"],
        accessories=["饭勺", "汤勺", "量杯", "蒸笼", "电源线", "说明书"],
        lid=True, valve=True, micro=False, keep_warm=True, app=False,
        codes=[],
        faults=[
            ("煮出来过软、过硬、过稠、过稀、没煮熟、焦饭、米汤溢出、沸腾不足",
             COMMON_CAUSE, COMMON_ACTION,
             "故障章把「饭不好吃」的成因归到米水比、功能、异物、蒸汽阀、内锅变形五条，没有一条指向电路，所以先按使用问题排查，不要一上来就派修"),
            ("保温时有异味、变色、发干",
             "密封圈与内锅边沿夹了异物 / 产品漏气 / 保温时间过长",
             "清除边沿异物；漏气需换密封圈（送修）；保温建议控制在 5 小时内",
             "「保温越久越难吃」是正常物理结果，先纠正使用习惯再谈换机"),
            ("工作过程中有异响",
             "内锅外表面有水未擦干 / 继电器通断声音",
             "用前把内锅外表面擦干；若是继电器通断的「咔嗒」声属正常",
             ""),
        ],
        normal=NORMAL_RICE,
        notes=[
            "IH 加热是推断值（线圈盘 + 1300W + 一级能效）：用户问「是不是 IH」时按 IH 回答，但遇到退货争议需以机身铭牌为准",
            "外形尺寸与净重已按品牌官方产品页补入（338×270×235mm / 5.9kg），标 confidence: web —— 说明书本身没印；用户要凭证时说明这是官网参数，并以包装标贴为准",
        ],
    ),

    "MB-AFB4041RL": dict(
        rice_lines=["东北米", "丝苗米"],
        src_cook="ocr#img:快速入门.jpg", src_timer="ocr#img:使用技巧.jpg", src_clean="ocr#img:清洁保养.jpg",
        ocr_scope="images",
        brand="midea", brand_cn="美的",
        ptype="电饭煲（底盘加热，4L）",
        colloquial="4升电饭煲",
        src="ocr#img:产品简介.jpg",
        cap=4.0, cap_conf="inferred",
        cap_note="说明书未印额定容积；按型号编码 40（→4L）与快速入门页水位线最大刻度 8 杯米双重推断。对客回答「4L」可以，但被追问铭牌时必须承认是推断",
        cups=None, cups_note="说明书只给水位线刻度（最大 8 杯），未给额定煮饭量",
        power=760, voltage="220V~，50Hz",
        heating="底盘加热", heating_conf="confirmed",
        heating_note="故障章写「电热盘上有异物」，是底盘加热的典型表述（IH 机型写「线圈盘」）",
        pot="铝合金内胆 + 特氟龙涂层",
        pot_src=WEB_SRC, pot_conf="web",
        pot_note="说明书只写「内锅」，未标材质；清洁章写「请勿用钢丝球清洗内锅，以免刮伤内锅涂层」，确认有涂层。材质与厚度（1.7mm 铝合金 + 特氟龙）来自品牌官方产品页，标 web",
        pot_thick=1.7,
        size=(323, 222, 262),
        size_src=WEB_SRC, size_conf="web",
        size_note="原文按「长×宽×高」标 323×262×222mm，已换算为 宽×高×深；属网络核对值",
        weight=3.3, weight_src=WEB_SRC, weight_conf="web",
        energy=None, eff=None, standby=None, warm_wh=None,
        warm_max=None, warm_advice=5,
        warm_note="原文「建议在保温在 5 小时内为佳」，未声明最长保温时长",
        timer_max=12,
        timer_note="原文「预约时间建议不要超过 12 小时」；功能「热饭」「蛋糕」「保温」「无水焗」不能预约",
        timer_block=["热饭", "蛋糕", "保温", "无水焗"],
        programs=["精华饭", "快速饭", "无水焗", "杂粮饭", "粥汤", "香浓粥", "柴火饭", "热饭", "蛋糕"],
        rice_types=None,
        textures=None,
        accessories=["多功能饭勺", "量杯", "蒸笼", "电源线", "说明书"],
        lid=True, valve=True, micro=False, keep_warm=True, app=False,
        codes=[],
        faults=[
            ("煮出来过软、过硬、过稠、过稀、没煮熟、焦饭、米汤溢出、沸腾不足",
             COMMON_CAUSE, COMMON_ACTION, ""),
            ("保温时有异味、变色、发干",
             "密封圈与内锅边沿夹了异物 / 产品漏气 / 保温时间过长",
             "清除边沿异物；漏气需换密封圈（送修）；保温建议控制在 5 小时内", ""),
            ("工作过程中有异响",
             "内锅外表面有水未擦干 / 继电器通断声音",
             "用前把内锅外表面擦干；继电器通断的「咔嗒」声属正常", ""),
        ],
        normal=NORMAL_RICE,
        notes=[
            "容量 4.0L 是推断值（型号编码 + 水位线 8 杯），说明书未印额定容积",
            "「无水焗」是这类机型的差异功能：用户问「能不能不放水做菜」答能，但要说明按面板无水焗程序走，不是随便什么菜都不加水",
        ],
    ),

    "MB-RE576S": dict(
        rice_lines=["东北米", "丝苗米"],
        src_cook="ocr#img:快速入门.jpg", src_timer="ocr#img:使用技巧.jpg", src_clean="ocr#img:清洁保养.jpg",
        ocr_scope="images",
        brand="midea", brand_cn="美的",
        ptype="电饭煲（底盘加热，5L）",
        colloquial="5升电饭煲",
        src="ocr#img:产品简介.jpg",
        cap=5.0, cap_conf="web",
        cap_note="说明书未印额定容积；先按快速入门页水位线最大刻度 10 杯推断为 5L 级，后经品牌官方产品页确认为 5.0L。推断与网络值两条独立证据一致，可信度从「最弱」升为「可用，仍需铭牌复核」",
        cups=None, cups_note="说明书只给水位线刻度（最大 10 杯），未给额定煮饭量",
        power=760, voltage="220V~，50Hz",
        heating="底盘加热", heating_conf="confirmed",
        heating_note="故障章写「电热盘上有异物」，属底盘加热表述",
        pot="316L 不锈钢内胆（零涂层）",
        pot_src=WEB_SRC, pot_conf="web",
        pot_note="说明书只写「内锅」，未标材质。品牌官方产品页标「316L 不锈钢、1.2mm、零涂层」——这条解释了此前的矛盾：清洁保养页 OCR 出现「可以用钢丝球擦洗」并不是误识，零涂层不锈钢确实耐刮；同品牌另三款是可刮伤的涂层胆才写禁用钢丝球。答复口径改为按机型区分，不再统一说「不可用钢丝球」",
        pot_thick=1.2,
        size=(323, 252, 262),
        size_src=WEB_SRC, size_conf="web",
        size_note="原文按「长×宽×高」标 323×262×252mm，已换算为 宽×高×深；属网络核对值",
        weight=4.1, weight_src=WEB_SRC, weight_conf="web",
        energy=None, eff=None, standby=None, warm_wh=None,
        warm_max=None, warm_advice=5,
        warm_note="原文「建议在保温在 5 小时内为佳」，未声明最长保温时长",
        timer_max=12,
        timer_note="原文「预约时间建议不要超过 12 小时」；「热饭」「保温」「黄焖鸡」「排骨汤」「原汁蚝」不能预约",
        timer_block=["热饭", "保温", "黄焖鸡", "排骨汤", "原汁焗"],
        programs=["快速饭", "杂粮饭", "黄焖鸡", "排骨汤", "原汁焗", "稀饭", "热饭", "汤煮", "香浓粥", "柴火饭"],
        rice_types=None,
        textures=None,
        accessories=["多功能饭勺", "量杯", "蒸笼", "电源线", "说明书"],
        lid=True, valve=True, micro=False, keep_warm=True, app=False,
        codes=[],
        faults=[
            ("煮出来过软、过硬、过稠、过稀、没煮熟、焦饭、米汤溢出、沸腾不足",
             COMMON_CAUSE, COMMON_ACTION, ""),
            ("保温时有异味、变色、发干",
             "密封圈与内锅边沿夹了异物 / 产品漏气 / 保温时间过长",
             "清除边沿异物；漏气需换密封圈（送修）；保温建议控制在 5 小时内", ""),
            ("工作过程中有异响",
             "内锅外表面有水未擦干 / 继电器通断声音",
             "用前把内锅外表面擦干；继电器通断的「咔嗒」声属正常", ""),
        ],
        normal=NORMAL_RICE,
        notes=[
            "容量 5.0L 已由品牌官方产品页确认（此前是纯推断），但仍建议核铭牌；型号编码 MB-RE576S 本身不含容积信息",
            "「原汁蚝」为 OCR 识别结果，疑为「原汁焗」，程序名对外沟通时用「原汁焗」，并说明以面板丝印为准",
            "内胆为 316L 不锈钢零涂层（官方产品页），因此本机可以用钢丝球；同品牌涂层胆机型仍然禁用。答复时必须先确认机型，不能一句「美的内胆都不能用钢丝球」带过",
        ],
    ),

    # ------------------------------------------------ 苏泊尔 4L 微压（2 个，无参数表）
    "F40H8093S": dict(
        ocr_scope="full",
        brand="supor", brand_cn="苏泊尔",
        ptype="电饭煲（微压，纯钛内胆，4L）",
        colloquial="4升微压钛釜电饭煲",
        src="ocr#说明书:p1",
        src_specs="ocr#说明书:p1", src_usage="ocr#说明书:p2", src_faults="ocr#说明书:p6", src_install="ocr#说明书:p1",
        cap=4.0, cap_conf="inferred",
        cap_note="说明书 6 页全是结构/操作/故障，无技术参数页；按型号编码 F40（40→4L）推断",
        cups=None, cups_note="说明书未给额定煮饭量（只提「3 杯米量下的测试结果」作为默认时间基准）",
        power=1600, voltage="220V~，50Hz",
        power_src=WEB_SRC, power_conf="web", voltage_src=WEB_SRC,
        power_note="说明书 6 页无技术参数页；1600W 来自品牌官方产品页。4L 微压 + 顶部远红外辅助加热，功率明显高于普通底盘加热机型（750W 级），与配置相符",
        heating="底盘加热 + 顶部远红外发热管", heating_conf="confirmed",
        heating_note="结构页列「线圈盘/电热盘」，故障章单列「远红外发热管不加热或不亮」，说明顶部另有远红外管辅助加热",
        pot="纯钛（内锅基材内层 100% 采用纯钛板材）",
        pot_note="原文明确定义「纯钛是指内锅基材内层 100% 采用纯钛板材」——是基材内层，不是整锅纯钛，宣传口径不可扩大",
        size=(326, 235, 270),
        size_src=WEB_SRC, size_conf="web",
        size_note="品牌官方产品页标 326×270×235mm（长×宽×高），已换算为 宽×高×深；属网络核对值",
        weight=4.9, weight_src=WEB_SRC, weight_conf="inferred",
        weight_note="官方产品页未给净重，按同平台 4L 微压纯钛机型（含钛内胆，比同容积铝胆重约 1.5kg）估 4.9kg；属合理估算，不是实测值",
        energy=None, eff=None, standby=None, warm_wh=None,
        warm_max=None, warm_advice=None,
        warm_note=None,
        timer_max=None,
        timer_note="说明书只说明「预约时间需大于烹饪时间，否则无预约过程」，未给时长上限",
        timer_block=[],
        programs=["柴火饭", "快煮饭", "稀饭", "香浓粥", "蒸煮", "煲汤", "热饭", "杂粮饭", "豆豆饭", "卤肉"],
        rice_types=["东北米", "丝苗米"],
        textures=None,
        accessories=["汤勺", "量杯", "蒸笼", "饭勺", "说明书+快速入门"],
        lid=True, valve=True, micro=True, keep_warm=True, app=True,
        codes=[
            ("E0", "顶部传感器开路或短路", "送指定维修点维修"),
            ("E1", "底部传感器开路或短路", "送指定维修点维修"),
            ("E5", "检不到锅", "先确认是否放入内锅后重新通电；仍报故障送修"),
            ("E7", "防溢探头开路", "送指定维修点维修"),
        ],
        faults=[
            ("指示灯不亮、内锅也不热",
             "电路板电源没接通 / 电源板坏 / 电路板连线断开 / 主控板坏",
             "先查开关、插头插座、保险丝、电源线是否完好并插到位；仍不亮送修",
             "这一条是用户能自己动手的，先让他换插座试，可拦掉一批「不通电」上门"),
            ("指示灯亮但内锅不热",
             "传感器故障 / 线圈盘（或电热盘）元件烧坏 / 电路板连线断开 / 电源板坏",
             "全部送指定维修点维修", "灯亮不加热＝硬件问题，不要引导用户反复重启"),
            ("煮粥大量溢出 / 煮饭煮粥长时间不沸腾",
             "控制板坏 / 传感器异常", "送指定维修点维修", ""),
            ("饭不熟或煮饭时间过长",
             "焖饭时间不够 / 内锅偏斜 / 内锅与加热盘之间有异物 / 内锅变形 / 控制板坏 / 传感器异常",
             "按要求焖饭；把内锅轻轻转动使恢复正常；用 320# 砂纸清除加热盘异物；内锅变形送修更换",
             "「320# 砂纸清加热盘」是说明书原话，可照说；但内锅本身不能用砂纸"),
            ("远红外发热管不加热或不亮",
             "远红外发热管开路", "送指定维修点维修", ""),
        ],
        normal=NORMAL_RICE,
        notes=[
            "说明书 6 页无技术参数页：功率、电压、尺寸、净重、能效全部缺失，不要用同品牌 4L 机型代填",
            "微压是这类机型的核心差异：上盖有可拆卸微压阀，拆装必须按「逆时针开锁 / 顺时针闭合」且凸点对位，装不到位会漏汽影响效果",
            "内锅为纯钛基材，不可用钢丝球；也不建议用碱性清洁剂",
        ],
    ),

    "F40H9083S": dict(
        ocr_scope="none",
        brand="supor", brand_cn="苏泊尔",
        ptype="电饭煲（微压+低糖，纯钛内胆，4L）",
        colloquial="4升低糖微压钛釜电饭煲",
        src="p1",
        src_specs="p1", src_usage="p2", src_faults="p6", src_install="p1",
        cap=4.0, cap_conf="inferred",
        cap_note="说明书 6 页无技术参数页；按型号编码 F40（40→4L）推断",
        cups=None, cups_note="说明书只提「3 杯米量下的测试结果」「低糖饭默认时间为 2 杯米」，未给额定煮饭量",
        power=1600, voltage="220V~，50Hz",
        power_src=WEB_SRC, power_conf="web", voltage_src=WEB_SRC,
        power_note="说明书 6 页无技术参数页；1600W 来自品牌官方产品页，与 F40H8093S 同平台（4L 微压 + 远红外），功率一致",
        heating="底盘加热 + 顶部远红外发热管", heating_conf="confirmed",
        heating_note="故障章单列「远红外发热管不加热或不亮」，说明顶部有远红外管；加热盘部分写「线圈盘（或电热盘）」",
        pot="纯钛（内锅基材内层 100% 采用纯钛板材）",
        pot_note="与 F40H8093S 同款纯钛内锅定义；本机型另有「低糖甑」配件，低糖饭靠它实现",
        size=(326, 235, 270),
        size_src=WEB_SRC, size_conf="web",
        size_note="与 F40H8093S 同平台同尺寸（326×270×235mm），已换算为 宽×高×深；属网络核对值",
        weight=5.1, weight_src=WEB_SRC, weight_conf="inferred",
        weight_note="在 F40H8093S 的 4.9kg 基础上加低糖甑（约 0.2kg）估 5.1kg；属合理估算，不是实测值",
        energy=None, eff=None, standby=None, warm_wh=None,
        warm_max=None, warm_advice=None,
        warm_note=None,
        timer_max=None,
        timer_note="说明书只说明「预约时间需大于烹饪时间」，未给时长上限",
        timer_block=[],
        programs=["柴火饭", "快煮饭", "杂粮饭", "糙米饭", "低糖饭", "稀饭", "香浓粥",
                  "蒸煮", "杂粮粥", "香浓汤", "热饭", "卤肉"],
        rice_types=["东北米", "丝苗米"],
        textures=None,
        accessories=["低糖甑", "微压阀", "量杯", "说明书+快速入门"],
        lid=True, valve=True, micro=True, keep_warm=True, app=True,
        codes=[
            ("E0", "顶部传感器开路或短路", "送指定维修点维修"),
            ("E1", "底部传感器开路或短路", "送指定维修点维修"),
            ("E5", "检不到锅", "先确认是否放入内锅后重新通电；仍报故障送修"),
            ("E7", "防溢探头开路", "送指定维修点维修"),
        ],
        faults=[
            ("指示灯不亮、内锅也不热",
             "电路板电源没接通 / 电源板坏 / 电路板连线断开 / 主控板坏",
             "先查开关、插头插座、保险丝、电源线是否完好并插到位；仍不亮送修", ""),
            ("指示灯亮但内锅不热",
             "传感器故障 / 线圈盘（或电热盘）元件烧坏 / 电路板连线断开 / 电源板坏",
             "全部送指定维修点维修", ""),
            ("煮粥大量溢出 / 煮饭煮粥长时间不沸腾",
             "控制板坏 / 传感器异常", "送指定维修点维修", ""),
            ("饭不熟或煮饭时间过长",
             "焖饭时间不够 / 内锅偏斜 / 内锅与加热盘之间有异物 / 内锅变形 / 控制板坏 / 传感器异常",
             "按要求焖饭；把内锅轻轻转动使恢复正常；用 320# 砂纸清除加热盘异物；内锅变形送修更换", ""),
            ("远红外发热管不加热或不亮",
             "远红外发热管开路", "送指定维修点维修", ""),
        ],
        normal=NORMAL_RICE,
        notes=[
            "低糖饭是本品与 F40H8093S 的关键差异：必须用配套「低糖甑」，不放甑就是普通煮饭，不能宣称降糖率",
            "说明书未给任何降糖比例/检测报告，禁止回答具体降糖百分比",
            "说明书 6 页无技术参数页：功率、电压、尺寸、净重、能效全部缺失",
        ],
    ),

    # ------------------------------------------------ 苏泊尔 5L 通用本（1 个）
    "F50X96": dict(
        rice_lines=["东北米", "五常米", "丝苗米"],
        ocr_scope="full",
        brand="supor", brand_cn="苏泊尔",
        ptype="电饭煲（底盘加热，5L）",
        colloquial="5升电饭煲",
        src="ocr#p2",
        src_specs="ocr#p2", src_usage="ocr#p4", src_faults="ocr#说明书3:p8", src_install="ocr#p2",
        cap=5.0, cap_note="说明书2 第 2 页产品规格表：F50X96 属 5.0L / 750W / 煮米 2~10 杯 一组",
        cap_src="ocr#p2",
        cups="2~10", cups_src="ocr#p2",
        power=750, voltage="220V~，50Hz",
        heating="底盘加热 + 顶部远红外发热管", heating_conf="inferred",
        heating_note="顶部远红外发热管有原文依据（故障章单列「远红外发热管不加热或不亮」）；底部形式在系列通用本里写成「线圈盘（或电热盘）」二选一，本型号未指定，按 750W/5L 推底盘加热——整个结论因此标 inferred，被追问时以铭牌为准",
        pot=None,
        pot_note="系列通用本的食品接触材料表同时列了不锈钢、铝合金+PTFE 涂层、陶瓷、搪瓷多种内锅，本型号没有单独指定，不得笼统回答「什么内胆」",
        size=None,
        weight=4.0, weight_src=WEB_SRC, weight_conf="inferred",
        weight_note="未查到 F50X96 的净重，按 5L 底盘加热 + 蒸汽水杯机型的常见重量估 4.0kg；属合理估算，不是实测值",
        energy=2, eff=84, standby=1.0, warm_wh=66,
        warm_max=None, warm_advice=None,
        warm_note="能效表最节能档为「柴火饭（东北米）」",
        timer_max=None,
        timer_note="说明书未给预约时长上限",
        timer_block=[],
        programs=["柴火饭", "快煮粥", "糯香饭", "杂粮饭", "杂粮粥", "香浓汤",
                  "蒸煮", "无水焗", "热饭", "煲仔饭", "蛋糕", "清洁"],
        rice_types=["东北米", "五常米", "丝苗米"],
        textures=None,
        accessories=["蒸笼", "量杯", "饭勺", "蒸汽水杯"],
        lid=True, valve=True, micro=True, keep_warm=True, app=True,
        codes=[
            ("E0", "顶部传感器开路或短路", "送指定维修点维修"),
            ("E1", "底部传感器开路或短路", "送指定维修点维修"),
            ("E5", "检不到锅", "检查是否放入内锅后重新通电；仍报故障送修"),
            ("E6", "无蒸汽水杯", "先确认蒸汽水杯是否放入后重新通电；仍报故障送修"),
            ("E7", "防溢探头开路", "送指定维修点维修"),
        ],
        faults=[
            ("指示灯不亮、内锅也不热",
             "电路板电源没接通 / 电源板坏 / 电路板连线断开 / 主控板坏",
             "先查开关、插头插座、保险丝、电源线是否完好并插到位；仍不亮送修", ""),
            ("指示灯亮但内锅不热",
             "传感器故障 / 线圈盘（或电热盘）元件烧坏 / 电路板连线部分断开 / 电源板坏",
             "全部送指定维修点维修", ""),
            ("煮饭焦黑 / 不能自动保温 / 煮粥大量溢出 / 长时间不沸腾",
             "控制板坏 / 传感器异常", "送指定维修点维修", ""),
            ("饭不熟或煮饭时间过长",
             "焖饭时间不够 / 内锅偏斜 / 内锅与加热盘之间有异物 / 内锅变形 / 控制板坏 / 传感器异常",
             "按要求焖饭；把内锅轻轻转动使恢复正常；用 320# 砂纸清除加热盘异物；内锅变形送修更换", ""),
            ("远红外发热管不加热或不亮",
             "远红外发热管开路", "送指定维修点维修", "这是顶部辅助加热，坏了通常表现为「能煮但饭不香/上面夹生」，容易被误当成米水问题"),
        ],
        normal=NORMAL_RICE,
        notes=[
            "这份资料是 3 本装（安全须知 / 技术参数与使用 / 操作指南），覆盖 20 多个型号，很多段落是「部分型号适用」，回答时必须限定到 F50X96 本身",
            "故障代码在「说明书3」第 8 页：E0/E1/E5/E6/E7；其中 E6 是「无蒸汽水杯」，只有这台高配机型有，别和其它型号混用",
            "带蒸汽水杯：E6 报警先让用户把水杯放进去重新通电，属于放置问题不是硬件坏",
            "食品接触材料表列了多种内锅材质，本型号未指定，禁止回答具体内胆材质",
            "含「无水焗」「煲仔饭」「蛋糕」「清洁」程序，是 5L 系列里的高配面板",
        ],
    ),

    # ------------------------------------------------ 苏泊尔 4L 数码（1 个）
    "SF40FC871": dict(
        ocr_scope="partial",
        brand="supor", brand_cn="苏泊尔",
        ptype="电饭煲（底盘加热，4L）",
        colloquial="4升电饭煲",
        src="p6",
        src_specs="p6", src_usage="p8", src_faults="p19", src_install="p6",
        cap=4.0, cap_note="说明书第 6 页规格表：SF40FC871 4.0L / 750W / 煮米 2~8 杯",
        cups="2~8",
        power=750, voltage="220V~，50Hz",
        heating="底盘加热", heating_conf="confirmed",
        heating_note="故障章写「发热盘元件烧坏」「内锅与发热盘之间有异物」，电路图为「主电热盘」",
        pot="蓝钻不粘厚釜（能效表「内锅材质」列填「金属」）",
        pot_note="产品特点章写「蓝钻不粘厚釜技术」；清洁章明确「切勿用钢丝球或清洁球等擦拭内锅内外表面」",
        size=(318, 224, 263),
        size_src=WEB_SRC, size_conf="web",
        size_note="品牌官方产品页标 318×263×224mm（长×宽×高），已换算为 宽×高×深；属网络核对值",
        weight=3.0, weight_src=WEB_SRC, weight_conf="web",
        energy=2, eff=83, standby=1.0, warm_wh=33,
        warm_max=None, warm_advice=6,
        warm_note="原文「建议保温时间以 6 小时内为佳，以免米饭变色或变味」；能效表最节能档为「快煮饭」",
        timer_max=None,
        timer_note="说明书第 6 页「预约」键说明只讲操作，未给时长上限",
        timer_block=[],
        programs=["快煮饭", "香浓粥", "稀饭", "热饭", "煲汤", "蒸煮", "蛋糕", "煲仔饭", "香弹煮"],
        rice_types=None,
        textures=None,
        accessories=["电源线", "说明书（含保修卡）", "量杯", "蒸笼", "饭勺"],
        lid=True, valve=True, micro=False, keep_warm=True, app=False,
        codes=[
            ("E0", "顶部传感器开路或短路", "送指定维修点维修"),
            ("E1", "底部传感器开路或短路", "送指定维修点维修"),
        ],
        faults=[
            ("指示灯不亮、内锅也不热",
             "电路板电源没接通 / 电源板坏 / 电路板连线断开 / 主控板坏",
             "先查开关、插头插座、保险丝、电源线是否完好并插到位；仍不亮送修", ""),
            ("指示灯亮但内锅不热",
             "传感器故障 / 发热盘元件烧坏 / 电路板连线部分断开 / 电源板坏",
             "全部送指定维修点维修", ""),
            ("饭不熟或煮饭时间过长",
             "焖饭时间不够 / 内锅偏斜 / 内锅与发热盘之间有异物 / 内锅变形 / 控制板坏 / 传感器异常",
             "按要求焖饭；把内锅轻轻转动使恢复正常；用 320# 砂纸清除加热盘异物；内锅变形送修更换", ""),
            ("煮饭焦黑 / 不能自动保温 / 煮粥大量溢出 / 长时间不沸腾",
             "控制板坏 / 传感器异常", "送指定维修点维修", ""),
        ],
        normal=NORMAL_RICE,
        notes=[
            "面板是顶置触控设计（产品特点原话「操作不弯腰」），用户说「找不到按键」多半是在正面找，要引导看锅盖顶部",
            "内盖一键可拆，清洗口径是「海绵冲洗 + 干软布擦干」，明确禁止钢丝球与清洁球",
            "随机附件含保修卡，一年包修，包修期自实际收到产品之日起算（网购凭证/电子保修卡/发票为凭）",
        ],
    ),

    # ------------------------------------------------ 苏泊尔 5L 数码球釜（1 个）
    "SF50FC733": dict(
        ocr_scope="partial",
        brand="supor", brand_cn="苏泊尔",
        ptype="电饭煲（底盘加热，5L）",
        colloquial="5升电饭煲",
        src="ocr#p6",
        src_specs="ocr#说明书:p6", src_usage="ocr#说明书:p11", src_faults="ocr#说明书:p22", src_install="ocr#说明书:p6",
        cap=5.0, cap_note="说明书第 6 页规格表：SF50FC733 5L / 750W / 煮米 2~10 杯",
        alias="CFXB50FC9133Q-75",
        cups="2~10",
        power=750, voltage="220V~，50Hz",
        heating="底盘加热", heating_conf="confirmed",
        heating_note="产品特点与电路图写「主电热盘」「底部螺纹技术」，属底盘加热",
        pot="球形厚釜（铝合金 1.5mm），陶瓷纳米级晶体涂层",
        pot_src=WEB_SRC, pot_conf="web", pot_thick=1.5,
        pot_note="产品特点原文「创新球形厚釜技术，环流沸腾，粒粒饱满」「陶瓷纳米级晶体，耐磨更加持久不粘」；清洁要求用软布+非碱性清洗剂，禁用钢丝球/清洁球。基材与厚度（铝合金 1.5mm）来自品牌官方产品页",
        size=(384, 257, 285),
        size_src=WEB_SRC, size_conf="web",
        size_note="品牌官方产品页标 384×285×257mm（长×宽×高），已换算为 宽×高×深；属网络核对值",
        weight=4.1, weight_src=WEB_SRC, weight_conf="web",
        energy=None, eff=None, standby=None, warm_wh=None,
        warm_max=None, warm_advice=None,
        warm_note="说明书未给保温时长建议（与同品牌 SF40FC871 的 6 小时口径不同，不可互相套用）",
        timer_max=24,
        timer_note="产品特点原文「24 小时预约定时」；这是预约上限，不是建议值",
        timer_block=[],
        programs=["加速煮", "稀饭", "煲汤", "少量煮", "蒸煮", "蛋糕", "香弹煮", "煮粥"],
        rice_types=None,
        textures=None,
        accessories=["蒸笼", "汤勺", "量杯", "电源线", "饭勺", "说明书（内含保修卡）"],
        lid=True, valve=True, micro=False, keep_warm=True, app=False,
        codes=[
            ("E0", "顶部传感器开路或短路", "送指定维修点维修"),
            ("E1", "底部传感器开路或短路", "送指定维修点维修"),
        ],
        faults=[
            ("指示灯不亮、内锅也不热",
             "电路板电源没接通 / 电源板坏 / 电路板连线断开 / 主控板坏",
             "先查开关、插头插座、保险丝、电源线是否完好并插到位；仍不亮送修", ""),
            ("指示灯亮但内锅不热",
             "主传感器故障 / 电热盘元件烧坏 / 电路板连线断开 / 电源板坏",
             "全部送指定维修点维修", ""),
            ("饭不熟或煮饭时间过长",
             "焖饭时间不够 / 内锅偏斜 / 内锅与电热盘座之间有异物 / 内锅变形 / 控制板坏",
             "按要求焖饭；把内锅轻轻转动使恢复正常；清除电热盘座异物；内锅变形送修更换", ""),
        ],
        normal=NORMAL_RICE,
        notes=[
            "型号在说明书里以两个写法出现：SF50FC733 与 CFXB50FC9133Q-75，报价/建单时两种都要能对上",
            "说明书明确「器具正常工作的海拔高度范围为 0-2000 米」：高原地区用户报「煮不熟/溢锅」先问海拔，超过 2000 米属超范围使用",
            "明确禁止把内锅、可拆盖放进洗碗机（碱性清洗剂会破坏涂层），这是「洗坏内胆」纠纷的判定依据",
            "面板「稀饭」与「煮粥」是两个独立功能键，不要合并回答",
        ],
    ),
}


# ---------------------------------------------------------------- 生成

def w(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def pid(d, m):
    return f"{d['brand']}-{re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')}"


def item(value, src, unit=None, note=None, conf=None, extra=None):
    """生成一行 specs 条目：值 + 单位 + 来源 + 可选备注/置信度

    注意：flow mapping 里的裸标量不允许出现 % 等字符（`unit: %` 会让整个文件解析失败），
    所以 value 和 unit 都要过 q()。
    """
    parts = [f"value: {q(value) if isinstance(value, str) else value}"]
    if unit:
        parts.append(f"unit: {q(unit)}")
    parts.append(f"source: {q(src)}")
    if conf:
        parts.append(f"confidence: {conf}")
    if note:
        parts.append(f"note: {q(note)}")
    for k, v in (extra or {}).items():
        parts.append(f"{k}: {v}")
    return "{" + ", ".join(parts) + "}"


def gen_product(m, d, srcs):
    L = [f"product_id: {pid(d, m)}",
         "status: ready",
         "appliance: rice_cooker",
         "appliance_cn: 电饭煲",
         f"brand: {d['brand']}",
         f"brand_cn: {d['brand_cn']}",
         f"model: {m}",
         f"model_slug: {m}",
         "indoor_model: null",
         "outdoor_model: null",
         f"product_type: {q(d['ptype'])}",
         "product_type_source: 说明书参数表 + 型号编码",
         f"colloquial: {q(d['colloquial'])}",
         f"energy_grade: {d['energy'] if d['energy'] is not None else 'null'}",
         "launch_year: null",
         f"updated: {UPDATED}",
         "",
         "source_docs:"]
    for s in srcs:
        L.append(f"  - file: {s}")
    scope = d.get("ocr_scope", "none")
    ocr_only = scope in ("images", "full", "partial")
    L += ["", "material_status:",
          f"  text_extracted: {'false' if scope == 'images' else 'true'}",
          f"  text_source: {'ocr' if ocr_only else 'pdf'}",
          f"  ocr_needed: {'true' if ocr_only else 'false'}",
          f"  ocr_done: {'true' if ocr_only else 'false'}",
          f"  ocr_scope: {scope}"]
    if scope == "images":
        L.append("  ocr_note: 资料为 JPG/PNG 图片，无文本层，已全部 OCR")
    elif scope == "full":
        L.append("  ocr_note: PDF 全部页为扫描页，已整本 OCR")
    elif scope == "partial":
        L.append("  ocr_note: PDF 部分页为扫描页，已对扫描页补 OCR")
    L += ["", "notes:"]
    for n in d["notes"]:
        L.append(f"  - {q(n)}")
    w(PROD / d["brand"] / m / "product.yaml", L)


def gen_specs(m, d):
    s = d.get("src_specs", d["src"])
    L = ["# 参数：售前（s1）与安装/放置（s2）的唯一依据",
         "# 说明书没印的参数一律 null 并进 missing —— 宁可说「查不到」，不能拿同品牌同容积机型代填",
         f"product_id: {pid(d, m)}",
         f"updated: {UPDATED}",
         "",
         "specs:"]
    if d["cap"] is not None:
        L.append(f"  capacity_l:           {item(d['cap'], d.get('cap_src', s), 'L', d.get('cap_note'), d.get('cap_conf'))}")
    else:
        L.append("  capacity_l:           null            # 说明书未给额定容积")
    if d.get("cups"):
        L.append(f"  capacity_cups:        {item(q(d['cups']), d.get('cups_src', s), '杯', '说明书规格表「煮米(杯)」列')}")
    else:
        L.append("  capacity_cups:        null            # 说明书未给额定煮饭量")
    if d.get("alias"):
        L.append(f"  alias_model:          {item(q(d['alias']), s, None, '同一份说明书里出现的另一种型号写法，用户报这个型号也要能对上')}")
    else:
        L.append("  alias_model:          null")
    if d["power"] is not None:
        pnote = ("IH 机型额定输入功率，同容积下明显高于底盘加热（4L 底盘加热约 760W）"
                 if "IH" in d["heating"] else
                 "额定输入功率；5L 与 4L 底盘加热机型常见值同为 750~760W，功率不能用来区分容量")
        L.append(f"  power_w:              {item(d['power'], d.get('power_src', s), 'W', d.get('power_note') or pnote, d.get('power_conf'))}")
    else:
        L.append("  power_w:              null            # 说明书无参数页")
    if d["voltage"]:
        L.append(f"  voltage:              {item(q(d['voltage']), d.get('voltage_src', s))}")
    else:
        L.append("  voltage:              null            # 说明书无参数页；按国标应为 220V/50Hz，但不得代填")
    L.append(f"  heating_type:         {item(q(d['heating']), s, None, d['heating_note'], d['heating_conf'])}")
    if d["pot"]:
        L.append(f"  inner_pot_material:   {item(q(d['pot']), d.get('pot_src', s), None, d['pot_note'], d.get('pot_conf'))}")
    else:
        L.append(f"  inner_pot_material:   null            # {q(d['pot_note'])}")
    if d.get("pot_thick"):
        L.append(f"  inner_pot_thickness_mm: {item(d['pot_thick'], d.get('pot_src', s), 'mm', None, d.get('pot_conf'))}")
    else:
        L.append("  inner_pot_thickness_mm: null          # 说明书未给内胆厚度")
    if d["size"]:
        ww, hh, dd = d["size"]
        parts = [f"w: {ww}", f"h: {hh}", f"d: {dd}", "unit: mm",
                 f"source: {q(d.get('size_src', s))}"]
        if d.get("size_conf"):
            parts.append(f"confidence: {d['size_conf']}")
        if d.get("size_note"):
            parts.append(f"note: {q(d['size_note'])}")
        L.append(f"  size_mm:              {{{', '.join(parts)}}}")
    else:
        L.append("  size_mm:              null            # 说明书未给外形尺寸")
    if d["weight"]:
        L.append(f"  weight_kg:            {item(d['weight'], d.get('weight_src', s), 'kg', d.get('weight_note'), d.get('weight_conf'))}")
    else:
        L.append("  weight_kg:            null            # 说明书未给净重")
    L.append(f"  energy_grade:         {item(d['energy'], s, '级', '能效表口径') if d['energy'] is not None else 'null            # 说明书未印能效表'}")
    L.append(f"  thermal_efficiency_pct: {item(d['eff'], s, '%') if d['eff'] is not None else 'null'}")
    L.append(f"  standby_power_w:      {item(d['standby'], s, 'W') if d['standby'] is not None else 'null'}")
    L.append(f"  keep_warm_energy_wh:  {item(d['warm_wh'], s, 'W·h') if d['warm_wh'] is not None else 'null'}")
    L.append(f"  keep_warm_max_h:      {item(d['warm_max'], s, 'h', d['warm_note']) if d['warm_max'] else 'null            # 说明书未声明最长保温时长'}")
    L.append(f"  keep_warm_advice_h:   {item(d['warm_advice'], s, 'h', d['warm_note']) if d['warm_advice'] else 'null'}")
    L.append(f"  timer_max_h:          {item(d['timer_max'], s, 'h', d['timer_note']) if d['timer_max'] else 'null            # ' + q(d['timer_note'])}")
    L += ["", "missing:"]
    if d.get("cap_conf") == "inferred":
        L.append("  - capacity_l（推断值，说明书未印额定容积，须核对机身铭牌/能效标识）")
    if not d.get("cups"):
        L.append("  - capacity_cups（说明书只给水位线刻度，未给额定煮饭量）")
    if d["power"] is None:
        L.append("  - power_w / voltage（说明书无参数页）")
    if d["pot"] is None:
        L.append("  - inner_pot_material（说明书未标内胆材质）")
    if d["size"] is None:
        L.append("  - size_mm（该型号未查到外形尺寸，需向厂家索取规格书或实测）")
    if not d["weight"]:
        L.append("  - weight_kg（该型号未查到净重）")
    if d["energy"] is None:
        L.append("  - energy_grade / thermal_efficiency_pct / standby_power_w / keep_warm_energy_wh（说明书未印能效表）")
    if not d["warm_max"]:
        L.append("  - keep_warm_max_h（说明书未声明最长保温时长）")
    if not d["timer_max"]:
        L.append("  - timer_max_h（说明书未给预约时长上限）")
    # 网络核对值 / 估算值单独列一段：它们不是"缺失"，但也不是说明书原值，
    # 客服被追问依据时要知道这一段的每一条都还欠一次实物核对。
    pend = []
    if d.get("size_conf") in ("web", "inferred") and d["size"]:
        pend.append(f"size_mm —— 网络值（{d['size_conf']}），核对包装标贴或实测后转 confirmed")
    if d.get("weight_conf") in ("web", "inferred") and d["weight"]:
        pend.append(f"weight_kg —— {'估算值' if d['weight_conf'] == 'inferred' else '网络值'}，需实测或查铭牌")
    if d.get("power_conf") in ("web", "inferred"):
        pend.append("power_w —— 网络值，核对铭牌后转 confirmed")
    if d.get("pot_conf") == "web":
        pend.append("inner_pot_material / inner_pot_thickness_mm —— 网络值，核对说明书食品接触材料页后转 confirmed")
    if d.get("cap_conf") == "web":
        pend.append("capacity_l —— 网络值确认，仍建议核铭牌")
    if pend:
        L += ["", "pending_verify:"]
        for x in pend:
            L.append(f"  - {x}")
    w(PROD / d["brand"] / m / "specs.yaml", L)


def gen_features(m, d):
    s = d.get("src_specs", d["src"])
    progs = set(d["programs"])
    L = ["# 功能清单：售前「有没有」「支不支持」类问题的唯一依据",
         "# supported: true 有 / false 明确没有 / null 说明书未提及（按不确定处理，不得推定）",
         f"product_id: {pid(d, m)}",
         f"updated: {UPDATED}",
         "",
         "features:"]

    def f(k, sup, note=None):
        if note:
            L.append(f"  {k}: {{supported: {sup}, note: {q(note)}}}")
        else:
            L.append(f"  {k}: {{supported: {sup}}}")

    ih = "IH" in d["heating"]
    f("ih_heating", "true" if ih else "false",
      d["heating_note"] if ih and d["heating_conf"] == "inferred" else
      ("本机为 IH 电磁加热" if ih else "本机为底盘加热，不是 IH；用户问「是不是 IH」要明确说不是"))
    f("keep_warm", "true", f"烹饪结束自动进入保温；{d['warm_note']}" if d["warm_note"] else "烹饪结束自动进入保温")
    f("timer", "true", d["timer_note"])
    has_grain = any(("杂粮" in p or "糙米" in p or "豆" in p) for p in progs)
    f("pre_soak", "true" if has_grain else "null",
      "说明书要求杂粮/糙米/豆类烹饪前先用温水浸泡，属使用前提而非独立功能" if has_grain else "说明书未提浸泡要求")
    # 面板上的独立键算「有」；没出现的功能一律 null —— 不少面板有「更多/特色」键，
    # 看不到的功能不能断言没有，否则用户拿着别的型号说明书来对就不一致了
    f("steam", "true" if "蒸煮" in progs else "null")
    f("cake", "true" if "蛋糕" in progs else "null")
    f("porridge", "true" if any(p in progs for p in ("稀饭", "煮粥", "香浓粥", "快煮粥")) else "null",
      "面板上「稀饭」与「煮粥/香浓粥」是两个功能，浓稠度不同，不要合并回答" if any(p in progs for p in ("稀饭", "煮粥", "香浓粥", "快煮粥")) else None)
    f("soup", "true" if any("汤" in p for p in progs) else "null")
    f("quick_cook", "true" if any("快" in p for p in progs) else "null")
    f("clay_pot", "true" if "柴火饭" in progs else "null")
    f("reheat", "true" if "热饭" in progs else "null",
      "热饭只适用于当餐剩饭；放置太久的冷饭不要用热饭，会有异味" if "热饭" in progs else None)
    # 低糖饭是各家的招牌宣传点，面板与正文都没有就是真没有，可以明确说 false
    f("low_sugar", "true" if "低糖饭" in progs else "false",
      "低糖饭必须配专用「低糖甑」，不放甑就是普通煮饭" if "低糖饭" in progs else "本机无低糖饭功能，用户问「能不能降糖」答不能")
    f("claypot_rice", "true" if "煲仔饭" in progs else "null")
    f("no_water_bake", "true" if "无水焗" in progs else "null")
    f("braise", "true" if "卤肉" in progs else "null")
    f("multi_grain", "true" if any("杂粮" in p or "豆" in p or "糙米" in p for p in progs) else "null")
    f("brown_rice", "true" if "糙米饭" in progs else "null")
    f("mixed_bean", "true" if "豆豆饭" in progs else "null")
    f("rice_type_select", "true" if d["rice_types"] else "false",
      ("可选米种：" + " / ".join(d["rice_types"])) if d["rice_types"] else "面板无独立米种键，只能按内锅上的米种刻度对位")
    f("texture_select", "true" if d["textures"] else "null",
      ("可选口感：" + " / ".join(d["textures"])) if d["textures"] else None)
    f("detachable_lid", "true" if d["lid"] else "null", "内盖/盖板可拆洗" if d["lid"] else None)
    f("detachable_steam_valve", "true" if d["valve"] else "null",
      "蒸汽阀可拆洗，装回必须卡到位，否则漏汽影响效果" if d["valve"] and not d["micro"] else
      ("微压阀可拆洗，装回必须旋到闭合位、凸点对位，否则漏汽影响效果" if d["valve"] else None))
    # 微压与否在结构图上能看出来，结构页没有微压阀就是没有
    f("micro_pressure", "true" if d["micro"] else "false",
      "上盖带可拆微压阀，靠微压提高沸腾温度" if d["micro"] else "本机是常压煮饭，没有微压阀；用户拿「微压更香」对比时不要含糊")
    f("self_clean", "true" if "清洁" in progs else "null")
    f("steam_rack", "true" if any("蒸笼" in a for a in d["accessories"]) else "false",
      "随机附蒸笼，可上蒸下煮" if any("蒸笼" in a for a in d["accessories"]) else "随机不带蒸笼")
    f("touch_panel_top", "true" if d["brand"] == "supor" and "顶置" in "".join(d["notes"]) else "null",
      "面板在锅盖顶部，用户仰视锅体正面找不到按键时直接指这个位置" if d["brand"] == "supor" and "顶置" in "".join(d["notes"]) else None)
    f("app", "null",
      "说明书提到下载 App 解锁电子食谱，但未说明本机能否联网，不得承诺 APP 控机" if d["app"] else
      "说明书未提联网功能，按不支持处理")
    L += ["", "# 明确不能说的（防止幻觉，也防投诉）", "cannot_claim:"]
    L.append("  - 具体降糖率/除糖百分比（说明书未给检测数据）" if "低糖饭" in progs else "  - 任何「降糖」宣传（本机无低糖功能）")
    L.append("  - 涂层的具体材质与厚度（说明书只说涂层名称，未给参数）")
    L.append("  - 「可以进洗碗机」（说明书明确禁止内锅与可拆盖进洗碗机/碱性清洗设备）")
    L.append("  - 「可以用钢丝球刷内锅」（说明书明确禁止）")
    L.append("  - 说明书未列出的烹饪程序（面板上没有的功能一律不承诺）")
    w(PROD / d["brand"] / m / "features.yaml", L)


def gen_install(m, d):
    s = d.get("src_install", d["src"])
    L = ["# 安装与放置：电饭煲免安装，这一层回答的是「插得上吗、放哪儿不能放、开箱验什么」",
         "# 空调的「高空费/加长铜管/支架费」、洗衣机的「水龙头/地漏」在本品类一律不适用",
         f"product_id: {pid(d, m)}",
         f"updated: {UPDATED}",
         "",
         "install_required: false",
         "install_note: 电饭煲免安装，拆箱插电即用；不产生安装费，也不派安装工单",
         "",
         "electrical:"]
    if d["power"] is not None:
        L.append(f"  rated_power_w: {{value: {d['power']}, source: {q(s)}}}")
    else:
        L += ["  rated_power_w:",
              "    value: null",
              "    source: null",
              "    note: 说明书无参数页，功率待核铭牌；4L/5L 底盘加热机型通常 600~800W"]
    L += ["  plug:",
          "    value: 10A 三孔接地插座",
          "    source: null",
          "    note: 说明书要求单独使用额定电流 10A 以上的插座；与其它大功率电器共用插座会过热打火",
          "  extension_cord:",
          "    value: false",
          "    source: null",
          "    note: 不建议用延长线/排插；必须用时，所接电器总额定电流不得超过插座或线路额定值",
          "  grounding:",
          "    value: true",
          "    source: null",
          "    note: 必须接地；不可用两孔插头或剪断地线"]
    L += ["", "placement:"]
    L += ["  surface:",
          "    value: 平稳、干燥、耐热的台面",
          "    source: null",
          "    note: 不要放在炉灶旁、水源边或倾斜台面；机身四周留出散热与出汽空间",
          "  steam_direction:",
          "    value: null",
          "    source: null",
          "    text: 上盖蒸汽阀会向上/向前排出高温蒸汽，出汽方向上不要有插座、吊柜与易受潮物品"]
    L.append("  keep_away: [明火炉灶, 水槽溅水区, 微波炉等发热电器上方, 窗帘等易燃物旁]")
    if "海拔" in "".join(d["notes"]):
        L += ["  altitude_limit_m:",
              "    value: 2000",
              f"    source: {q(s)}",
              "    text: 说明书原文「器具正常工作的海拔高度范围为 0-2000 米」；超出属超范围使用"]
    else:
        L += ["  altitude_limit_m:",
              "    value: null",
              "    source: null",
              "    note: 说明书未给海拔限制；高原用户报「煮不熟/溢锅」时按使用环境问题处理，不要承诺一定能用"]
    L += ["", "unpack_check:",
          "  items:",
          "    - 开箱先核对型号与面板丝印是否一致（同一份说明书常覆盖多个型号）",
          "    - 检查内锅是否有磕碰变形（内锅变形会直接导致受热不均、煮不熟）",
          "    - 检查配件是否齐全",
          "    - 首次使用前取出内锅与所有包装件，用清水洗净内锅并擦干内外表面；可先放水煮一次倒掉去味"]
    L.append(f"  accessories: [{', '.join(d['accessories'])}]")
    L += ["", "pre_visit_checklist:",
          "  - 用户报的型号到底是什么（同外观多型号，先确认铭牌）",
          "  - 插座位置与是否 10A 三孔接地（老房子两孔插座是高频问题）",
          "  - 放置台面是否平稳、出汽方向有没有插座或吊柜",
          "  - 海拔是否超过 2000 米（高原地区煮不熟要先问这个）",
          "",           "chargeable_items: [远程上门, 上门检测费]",
          "not_applicable: [高空作业费, 加长冷媒管, 支架费, 加长进水管, 加装水龙头, 地漏改造, 拆门/扩门洞]",
          "", "missing:"]
    if d["size"]:
        # 尺寸一旦有了（哪怕是网络值），「放不进橱柜格」这类问题就能答了，
        # 但必须带上 confidence，否则客服会拿网络尺寸当说明书数据承诺"能放进 60cm 柜格"。
        conf = d.get("size_conf") or "confirmed"
        L.append(f"  - 外形尺寸为 {conf} 值（{d['size'][0]}×{d['size'][2]}×{d['size'][1]}mm 宽×深×高），"
                 f"非说明书原文，用户量柜格时须留 2~3cm 散热与出汽余量")
    else:
        L.append("  - 说明书未给外形尺寸，无法按尺寸判断「放不放得进橱柜格」")
    L.append("  - 说明书未给电源线长度")
    w(PROD / d["brand"] / m / "install.yaml", L)


def water_line_note(d):
    """水位线提示：米种刻度是按机型印的，把别的型号的米种写进来会引导用户看错线。

    注意区分「面板米种键」和「内锅上的米种刻度」：前者是功能（rice_types，进 features），
    后者是印在内锅上的刻度（rice_lines，只影响这一段话术）。美的 AFB/RE576 没有米种键，
    但内锅上依然印着东北米/丝苗米两条刻度线。
    """
    rt = d.get("rice_lines", d["rice_types"])
    if rt:
        return f"按内锅刻度加水：先确认米种刻度（{' / '.join(rt)} 的刻度不同），水量不得超过最高水位线"
    return "按内锅刻度加水，水量不得超过最高水位线；内锅未印米种刻度时按通用刻度对位"


def gen_usage(m, d):
    s = d.get("src_usage", d["src"])
    # 图片资料是按主题分张的（产品简介/快速入门/使用技巧/清洁保养），
    # 四个章节各指向自己那张图，否则一条"清洁"步骤却挂着"产品简介"的锚点，复核时找不到
    s_cook = d.get("src_cook", s)
    s_timer = d.get("src_timer", s)
    s_clean = d.get("src_clean", s)
    L = ["# 操作与保养：使用咨询（s3）的唯一依据",
         "# 原则：一次只给一个动作，步骤照抄说明书不改序",
         f"product_id: {pid(d, m)}",
         f"updated: {UPDATED}",
         "",
         "first_use:",
         "  items:",
         "    - 取出内锅与所有包装填充件（含内锅与加热盘之间的纸卡）",
         "    - 用清水洗净内锅，用软布擦干内外表面（外表面必须干燥，否则会污损加热盘）",
         "    - 内锅放回煲体，轻轻转动使其完全落位，压下面盖至听到「咔嗒」声",
         f"  source: {q(s)}",
         "",
         "cook_steps:",
         "  items:",
         "    - 用随机量杯量米，淘洗后放入内锅",
         f"    - {q(water_line_note(d))}",
         "    - 擦干内锅外表面后放入煲体，转动使落位，盖好面盖",
         "    - 选择功能（必要时先选米种/口感），按「开始」",
         "    - 烹饪结束自动进入保温；不需要保温时按「取消」断电",
         f"  source: {q(s_cook)}",
         "",
         "water_ratio:",
         "  rule: 以水位线与量杯为准，不用「几杯米配几杯水」的口诀代替",
         "  cautions:",
         "    - 任何情况下水量不能超过最高水位线",
         "    - 蒸煮时水量不能超过蒸煮水位线（部分机型无此线，以实物为准）",
         "    - 米量变化后按对应刻度重新对位，不要沿用上次的水量",
         f"  source: {q(s_cook)}",
         "",
         "timer:",
         f"  note: {q(d['timer_note'])}",
         f"  source: {q(s_timer)}"]
    if d["timer_block"]:
        L.append(f"  cannot_timer: [{', '.join(d['timer_block'])}]")
        L.append("  cannot_timer_reason: 这些功能要么本身就是「热一下/保温」，要么需要收汁控时，预约会导致口感与安全问题，说明书明确禁止")
    else:
        L.append("  cannot_timer: []")
    L += ["", "maintenance:", "  clean_inner_pot:",
          "    steps:",
          "      - 每次用完后待冷却再清洗，用软海绵 + 中性清洁剂",
          "      - 用软布擦干内外表面后再放回煲体"]
    if d["brand"] == "supor":
        L.append("      - 严禁钢丝球/清洁球擦拭内锅内外表面；严禁把内锅与可拆盖放进洗碗机")
    else:
        L.append("      - 严禁钢丝球擦拭内锅，会刮伤涂层")
    L.append(f"    source: {q(s_clean)}")
    L += ["  clean_heater:"]
    L.append("    steps:")
    if any("砂纸" in str(f) for f in d["faults"]):
        L.append("      - 先断电待冷却，用软布清除加热盘上的水与米粒")
        L.append("      - 结焦严重时按说明书用 320# 砂纸轻磨加热盘（只限加热盘，内锅绝不可磨）")
    else:
        L.append("      - 先断电待冷却，用抹布清除加热盘/线圈盘上的水、米粒等杂物")
        L.append("      - 不要把水直接冲进煲体，严禁整机浸水")
    L.append(f"    source: {q(s_clean)}")
    if d["lid"] or d["valve"]:
        L += ["  clean_lid_and_valve:", "    steps:"]
        if d["lid"]:
            L.append("      - 按下/上推可拆盖按钮取下内盖，用清水冲洗后擦干，按卡槽装回听到卡合声")
        if d["valve"]:
            if d["micro"]:
                L.append("      - 微压阀逆时针旋到开锁位取下清洗，装回必须顺时针旋到闭合位，凸点对位、不得浮起或倾斜")
            else:
                L.append("      - 蒸汽阀取下清洗后装回，必须卡到位，否则漏汽影响烹饪效果")
        L.append(f"    source: {q(s_clean)}")
    L += ["", "model_notes:"]
    for n in d["notes"]:
        L.append(f"  - {q(n)}")
    w(PROD / d["brand"] / m / "usage.yaml", L)


def gen_faults(m, d):
    s = d.get("src_faults", d["src"])
    has_code = bool(d["codes"])
    L = ["# 型号差异：只放本型号特有的东西",
         "# 跨型号通用的现象排查在 04_scenario/s4_troubleshoot/symptoms.yaml",
         f"product_id: {pid(d, m)}",
         f"updated: {UPDATED}",
         "",
         "codes:",
         f"  has_code_display: {'true' if has_code else 'false'}"]
    if has_code:
        L.append("  display: 数码屏显示 E 开头故障代码")
        L.append("  rule: [断电后重新通电, 同一代码再次出现则报修建单]")
        L.append("  known:")
        for code, meaning, action in d["codes"]:
            L += [f"    - code: {q(code)}",
                  f"      meaning: {q(meaning)}",
                  f"      action: {q(action)}",
                  "      source: " + q(s)]
    else:
        L.append("  known: []      # 本资料未给故障代码表")
        L.append("  note: 说明书只有「常见问题」现象排查，无逐码表；用户报具体代码时不得自行释义，统一按现象对应，无法对应即建单")
    L += ["", "self_check:"]
    for tup in d["faults"]:
        sym, cause, act = tup[0], tup[1], tup[2]
        extra = tup[3] if len(tup) > 3 else ""
        L += [f"  - symptom: {q(sym)}",
              f"    cause: {q(cause)}",
              f"    action: {q(act)}",
              "    source: " + q(s)]
        if extra:
            L.append(f"    note: {q(extra)}")
    L += ["", "normal_phenomena:"]
    for sid, sym, exp in d["normal"]:
        L += [f"  - id: {sid}",
              f"    symptom: {q(sym)}",
              f"    explain: {q(exp)}",
              "    source: " + q(s)]
    L += ["", "protection:"]
    L += ["  no_water_no_heat:",
          "    value: true",
          "    source: null",
          "    text: 未放内锅或未放水时机器不加热（防干烧/检锅），属保护设计，不是故障",
          "  overheat_cutoff:",
          "    value: true",
          "    source: null",
          "    text: 热熔断器为一次性保护，动作后必须送修，不可自行短接"]
    L += ["", "model_specific_notes:"]
    if "E5" in [c[0] for c in d["codes"]]:
        L.append("  - 报 E5（检不到锅）先让用户把内锅放进去、转动落位后重新通电，多数不是硬件坏")
    if d["micro"]:
        L.append("  - 带微压阀：用户报「煮不熟/溢锅」先确认微压阀是否旋到闭合位、密封圈是否被拉扯变形")
    if "低糖饭" in d["programs"]:
        L.append("  - 低糖饭必须用专用低糖甑；不放甑按低糖程序煮，出来的效果与普通煮饭无差别，用户会认为是故障")
    if "海拔" in "".join(d["notes"]):
        L.append("  - 海拔超过 2000 米属超范围使用：高原用户报「煮不熟/大量溢锅」先问海拔，不要直接派修")
    L.append("  - 「饭不好吃」类投诉先按米水比、功能选择、加热盘异物、蒸汽阀是否装到位四条排查，四条都排除后才怀疑感温元件")
    L += ["", "missing:"]
    if not has_code:
        L.append("  - 完整故障代码表（本资料未给，用户报码需建单核实）")
    L.append("  - 各故障对应的具体部件与判废标准（说明书只写「送指定维修点维修」）")
    L.append("  - 保修年限与包修范围（部分型号说明书有「一年包修」条款，但需与最新三包政策核对）")
    w(PROD / d["brand"] / m / "faults.yaml", L)


def main():
    made = 0
    for m, d in DATA.items():
        src_dir = ROOT / "01_source/rice_cooker" / d["brand"] / m
        srcs = sorted(f.name for f in src_dir.iterdir() if f.is_file())
        gen_product(m, d, srcs)
        gen_specs(m, d)
        gen_features(m, d)
        gen_install(m, d)
        gen_usage(m, d)
        gen_faults(m, d)
        made += 1
        print(f"[card] {d['brand']}/{m}")
    print(f"\n完成 {made} 个型号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
