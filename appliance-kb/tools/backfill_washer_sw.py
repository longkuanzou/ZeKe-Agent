#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小天鹅洗衣机参数回填（方案 B：脚本生成 + 人工核对清单）

为什么用脚本而不是手写 20 个文件：
  1. 同一系列 5 个型号，差异只在参数表几个数字和程序清单，手写必然漂移；
  2. 结构固定，脚本能保证"每个型号该有的键一个不少"，validate 一次过；
  3. 数据从 OCR 逐项核对后写进 DATA，来源锚点自动带上，不会漏 source。

数据纪律：
  - 每个值都标 `ocr#<图片名>`，回填前已对照 OCR 原文；OCR 本身可能认错数字，
    所以 90% 的项写 note 说明取自哪张图，便于二次复核。
  - 说明书没写的，一律 null 并进 missing，**不用型号编码或"同类机型大概"推断**。
  - TG10K60 不在此脚本内：它是手写的打样板，用来对照脚本产出是否等质量。

用法:
  python tools/backfill_washer_sw.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "03_product/washer/littleswan"
UPDATED = "2026-10-22"


def q(s):
    """YAML 标量引用：含 YAML 特殊字符时用单引号包，单引号内转义只需 '' """
    s = str(s)
    if re.search(r"[:'\"#\[\]{}&*!|>%@`]", s) or s != s.strip():
        return "'" + s.replace("'", "''") + "'"
    return s


# ── 数据：全部来自 02_ingest/washer/littleswan/{model}/ocr.md ──────────
DATA = {
    "TB100V26DT": {
        "ptype": "波轮机（顶开式，单洗）",
        "ptype_src": "型号编码 TB + 说明书「产品介绍」页",
        "colloquial": "10公斤波轮",
        "capacity": 10.0,
        "power": 390,
        "current": None,
        "noise": (56, 66),
        "noise_note": "说明书标注「洗涤脱水噪音dB(A) 56/66」，未注明声功率级还是声压级，与其他型号口径可能不同",
        "size": (550, 930, 565),   # 原文 宽×厚×高 550×565×930
        "size_order": "原文「外形尺寸(宽X厚X高mm) 550X565X930」，已按 宽/厚/高 拆成 w/d/h——波轮标的是「厚」不是「深」",
        "size_max": None,
        "weight": 33,
        "spin": None,
        "drain": None,
        "drain_note": "说明书未标注上排水还是下排水；波轮机多为下排水，但不得据此回答，需按实物或铭牌确认",
        "energy": 1,
        "loading": "波轮（顶开式）",
        "programs_builtin": ["标准", "快洗", "柔洗", "超净洗", "速洗", "单脱", "除螨"],
        "programs_app": [],
        "features": {
            "dry": (False, None, "波轮机，说明书无烘干相关内容"),
            "steam": (None, None, "说明书未提蒸汽/除菌程序；程序表有「除螨」但未说明是否高温"),
            "self_clean": (None, None, "波轮说明书无筒自洁程序，仅有内桶清洁保养指引"),
            "wifi": (None, None, "说明书未见联网指引"),
            "child_lock": (None, None, "说明书未提童锁"),
            "auto_dosing": (False, None, "波轮机型为手动投放洗涤剂"),
            "auto_weight": (None, None),
            "water_level_manual": (True, "使用技巧.jpg", "水位键可手动设定水位"),
            "wash_strength": (True, "使用技巧.jpg", "长按强度键调 P1/P2/P3，洗涤强度逐步提高，标准/快洗/速洗可用"),
            "delay_start": (True, "使用技巧.jpg", "长按 3 秒预约，预约时间到达后开始运行"),
            "one_key_smart": (True, "使用技巧.jpg", "一键智洗"),
            "quick_program": (True, "使用技巧.jpg", "快洗 / 速洗"),
            "mite_removal": (True, "使用技巧.jpg", "除螨程序"),
        },
        "codes_note": "说明书有 E1~E4 报警，明确「E1-E4 报警解除需关闭门盖，按启动键」；逐条含义未列",
        "codes_known": [],
        "transport_bolts": None,
        "form": "top",
    },
    "TG100V618PLUS": {
        "ptype": "滚筒洗衣机（前开式，单洗）",
        "ptype_src": "型号编码 TG + 说明书「产品介绍」页",
        "colloquial": "10公斤滚筒",
        "capacity": 10.0,
        "power": 1800,
        "current": 10,
        "noise": (48, 69),
        "noise_note": "声功率级",
        "size": (595, 850, 560),   # 原文 宽×深×高 595×560×850
        "size_order": "原文「外形尺寸(WXD1XH) 595X560X850」，已按 W/D1/H 拆成 w/d/h；D1 为机身深度",
        "size_max": (595, 850, 632),
        "weight": 64,
        "spin": 1200,
        "drain": "上排水",
        "drain_note": None,
        "energy": 1,
        "loading": "滚筒（前开式）",
        "programs_builtin": ["混合洗", "冷水洗", "除螨洗", "节能", "浸泡洗", "漂+脱",
                             "单脱水", "筒自洁", "大件", "棉麻", "羊毛", "羽绒服",
                             "速洗30'", "快洗15"],
        "programs_app": ["智能洗", "牛仔", "衬衫", "深度筒清洁", "新衣洗", "消毒洗", "运动服"],
        "features": {
            "dry": (False, None, "单洗机型，说明书无烘干程序与干衣容量"),
            "steam": (None, None, "说明书写「持续高温消毒洗涤」的消毒洗程序需 App 下载，未明确蒸汽"),
            "self_clean": (True, "使用说明-程序介绍.jpg", "筒自洁程序，可调冷水/高温"),
            "wifi": (True, "联网指引.jpg", "美的美居 App；部分程序需下载后使用"),
            "child_lock": (True, "使用说明-控制面板介绍.jpg", "童锁"),
            "auto_dosing": (True, "使用说明书-特色功能介绍.jpg", "自动投放洗衣液/柔顺剂"),
            "auto_weight": (None, None),
            "water_cube": (True, "使用说明书-特色功能介绍.jpg", "水魔方"),
            "power_off_memory": (True, "使用说明书-特色功能介绍.jpg", "断电记忆"),
            "foam_sensor": (True, "使用说明书-特色功能介绍.jpg", "泡沫感知检测"),
            "drum_light": (True, "使用说明书-特色功能介绍.jpg", "筒灯"),
            "delay_start": (True, "使用说明-控制面板介绍.jpg", "预约"),
            "wool_certified": (True, "使用说明书-程序介绍.jpg", "羊毛洗获 Woolmark 认证，授权编号 M1906"),
        },
        "codes_note": "常见问题页有信息代码表，OCR 数字部分可读（630/610/612/621），含义需对照原图确认",
        "codes_known": [
            ("630", "门未合上", "确认衣物没有被门压住，重新关门后按启动键继续", "confirmed"),
            ("610", "进水超时", "确认水龙头已打开、水压正常、进水管未弯折、进水阀过滤网无堵塞", "partial"),
            ("612", "排水超时", "清理排水泵杂物，排水管最高处不超过 100cm", "partial"),
        ],
        "transport_bolts": 4,
        "form": "front",
    },
    "TG10V628PLUS": {
        "ptype": "滚筒洗衣机（前开式，单洗）",
        "ptype_src": "型号编码 TG + 说明书「产品介绍」页",
        "colloquial": "10公斤滚筒",
        "capacity": 10.0,
        "power": 1800,
        "current": None,
        "noise": (48, 69),
        "noise_note": "声功率级",
        "size": (595, 850, 585),   # 原文 宽×深×高 595×585×850
        "size_order": "原文「外形尺寸(WXD1XH) 595X585X850」，已按 W/D1/H 拆成 w/d/h；D1 为机身深度",
        "size_max": (595, 850, 620),
        "weight": 63,
        "spin": 1200,
        "drain": "上排水",
        "drain_note": None,
        "energy": 1,
        "loading": "滚筒（前开式）",
        "programs_builtin": ["混合", "冷水洗", "除螨洗", "节能洗", "浸泡洗", "漂+脱",
                             "单脱水", "筒自洁", "大件", "棉麻", "羽绒服", "速洗30",
                             "快洗15", "春秋洗", "夏日洗", "冬季洗", "婴儿服", "冲锋衣"],
        "programs_app": ["新衣洗", "牛仔", "衬衫", "运动服"],
        "features": {
            "dry": (None, None,
                    "说明书按系列编写，出现「进水/烘干前」「烘干时间」等烘干相关描述，"
                    "但程序表无烘干程序，也无干衣容量。是否带烘干必须按实物面板确认，不得推定"),
            "steam": (None, None),
            "self_clean": (True, "使用说明书-程序介绍.jpg", "筒自洁程序"),
            "wifi": (True, "联网指引.jpg", "美的美居 App"),
            "child_lock": (True, "使用说明-控制面板介绍.jpg", "童锁"),
            "auto_dosing": (True, "使用说明书-特色功能介绍.jpg", "自动投放洗衣液/柔顺剂"),
            "auto_weight": (True, "使用说明书-特色功能介绍.jpg", "自动称重，按干/湿布重量设定水量与投放量"),
            "water_cube": (True, "使用说明书-特色功能介绍.jpg", "水魔方"),
            "power_off_memory": (True, "使用说明书-特色功能介绍.jpg", "断电记忆"),
            "foam_sensor": (True, "使用说明书-特色功能介绍.jpg", "泡沫感知检测"),
            "drum_light": (True, "使用说明书-特色功能介绍.jpg", "筒灯"),
            "forget_take_reminder": (True, "使用说明书-特色功能介绍.jpg", "忘取无忧"),
            "unbalance_sensor": (True, "使用说明书-特色功能介绍.jpg", "脱水不平衡检测，显示 Ub"),
            "add_clothes_midway": (True, "使用说明书-特色功能介绍.jpg", "中途添衣"),
        },
        "codes_note": "常见问题页有信息代码表，OCR 数字粘连严重（如「016 3」「112」），未采信任何数字",
        "codes_known": [],
        "transport_bolts": 4,
        "form": "front",
    },
    "TG10VE40": {
        "ptype": "滚筒洗衣机（前开式，单洗）",
        "ptype_src": "型号编码 TG + 说明书「产品介绍」页",
        "colloquial": "10公斤滚筒",
        "capacity": 10.0,
        "power": 1800,
        "current": 10,
        "noise": (46, 68),
        "noise_note": "声功率级",
        "size": (595, 850, 565),   # 原文 宽×深×高 595×565×850
        "size_order": "原文「外形尺寸(WXD1XH) 595X565X850」，已按 W/D1/H 拆成 w/d/h；D1 为机身深度",
        "size_max": (595, 850, 582),
        "weight": 68,
        "spin": 1200,
        "drain": "上排水",
        "drain_note": None,
        "energy": 1,
        "loading": "滚筒（前开式）",
        "programs_builtin": ["混合", "特渍洗", "冷水洗", "棉麻", "羽绒服", "速洗30",
                             "快洗15", "漂＋脱", "单脱水", "大件", "春秋洗", "夏日洗",
                             "冬季洗", "婴儿服", "冲锋衣", "户外服", "蒸汽消毒洗"],
        "programs_app": ["新衣洗", "牛仔", "衬衫", "运动服"],
        "features": {
            "dry": (False, None, "单洗机型，说明书无烘干程序与干衣容量"),
            "steam": (True, "产品介绍.jpg",
                      "本机除菌除螨测试程序为「蒸汽消毒洗」程序；另有同名独立程序，持续高温消毒洗涤"),
            "self_clean": (True, "使用说明-程序介绍.jpg", "筒自洁程序"),
            "wifi": (True, "联网指引.jpg", "美的美居 App"),
            "child_lock": (True, "使用说明-控制面板介绍.jpg", "童锁"),
            "auto_dosing": (True, "使用说明-特色功能介绍.jpg", "自动投放洗衣液/柔顺剂"),
            "auto_weight": (None, None),
            "micro_bubble": (True, "使用说明-特色功能介绍.jpg", "超微净泡"),
            "water_cube": (True, "使用说明-特色功能介绍.jpg", "水魔方"),
            "power_off_memory": (True, "使用说明-特色功能介绍.jpg", "断电记忆"),
            "foam_sensor": (True, "使用说明-特色功能介绍.jpg", "泡沫感知检测"),
            "drum_light": (True, "使用说明-特色功能介绍.jpg", "筒灯"),
            "forget_take_reminder": (True, "使用说明-特色功能介绍.jpg", "忘取无忧"),
            "special_stain": (True, "使用说明-程序介绍.jpg",
                              "特渍洗，经第三方按 QMY-J070.0012 检测符合特渍洗 A 等级；更多污渍类别需在美的美居 App 选择"),
        },
        "codes_note": "常见问题页有信息代码表，OCR 仅「610」可读，其余数字粘连，未采信",
        "codes_known": [
            ("610", "进水超时", "确认水龙头已打开、水压正常、进水管未弯折、进水阀过滤网无堵塞", "partial"),
        ],
        "transport_bolts": 4,
        "form": "front",
    },
}

# 说明书明确写出的通用"现象→原因→处理"，同系列各型号基本一致
SELF_CHECK = [
    ("洗衣机无法启动", "机门没关紧 / 水龙头没开 / 没按启动键",
     "重新关门；确认打开水龙头；按启动/暂停键"),
    ("机门打不开", "机器安全保护设计，正在工作",
     "关闭电源重新开机，等待机器安全后开门"),
    ("漏水", "进水管或排水管连接不牢", "检查并紧固水管，清理排水管"),
    ("料盒中有残留洗衣粉", "洗衣粉受潮、结块", "清洁并擦干料盒"),
    ("洗涤效果不佳", "衣物太脏 / 洗涤剂剂量不足",
     "选择合适的洗衣程序；选用专用机洗低泡洗衣粉"),
    ("指示灯或显示屏不亮", "断电 / 电脑板、线束故障",
     "确认未停电，以及电源插头正确插接；仍不亮则报修"),
    ("震动过大、噪音较大", "运输螺栓未拆除 / 未装在坚固水平地面",
     "拆除背面 4 颗运输螺栓；调整底脚使机器平稳"),
    ("脱水延时或不脱水", "衣物过少或过多触发偏心保护 / 泡沫过多等待消泡",
     "调整衣物数量并均匀分布后重新运行；泡沫情况等待程序自行完成"),
]

NORMAL = [
    ("spin_delay_low_load", "衣物较少时脱水延时或不脱水",
     "偏心保护会先低速检测并重新分布衣物；增加部分衣物均匀分布后按启动键继续"),
    ("foam_extra_time", "洗涤中程序时间变长、迟迟不脱水",
     "检测到泡沫过多会进入静置或缓慢转动、进水除泡，属正常"),
    ("power_off_resume", "停电后机器停了，来电后没有继续",
     "断电记忆在来电后继续运行；若断电期间按了电源键关机，则需重新开始"),
    ("ub_prompt", "面板显示 Ub",
     "衣物不平衡保护提示，不是硬件故障；取出抖散或拧掉部分水分后重新放入即可"),
    ("door_locked_running", "运行中机门打不开",
     "安全保护设计，运行中门锁不放行；需关机后等待安全时间再开"),
]


def w(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def gen_product(m, d, srcs):
    L = [f"product_id: littleswan-{m.lower()}",
         "status: ready",
         "appliance: washer",
         "appliance_cn: 洗衣机",
         "brand: littleswan",
         "brand_cn: 小天鹅",
         f"model: {m}",
         f"model_slug: {m}",
         "indoor_model: null",
         "outdoor_model: null",
         f"product_type: {q(d['ptype'])}",
         f"product_type_source: {q(d['ptype_src'])}",
         f"colloquial: {q(d['colloquial'])}",
         f"energy_grade: {d['energy']}",
         "launch_year: null",
         f"updated: {UPDATED}",
         "",
         "source_docs:"]
    for s in srcs:
        L.append(f"  - file: {s}")
    L += ["  - note: 资料主体为 JPG 图片，文本由 tools/ocr.py 识别，见 02_ingest/washer/littleswan/" + m + "/ocr.md",
          "",
          "material_status:",
          "  text_extracted: true",
          "  text_source: ocr",
          "  ocr_needed: true",
          "  ocr_done: true",
          "  ocr_scope: images",
          "",
          "notes:",
          "  - 参数与功能来自 OCR，数值已对照 OCR 原文；涉及报价/合同前请再核原图"]
    if d["form"] == "top":
        L.append("  - 波轮机：程序为固定 01~07 档，水位与洗涤强度手动可调，与滚筒的操作逻辑不同")
    w(PROD / m / "product.yaml", L)


def gen_specs(m, d):
    L = [f"# 参数来源：小天鹅 {m}「产品介绍」页「规格及技术参数」表（图片资料 → OCR）",
         "# 键名受 00_schema/spec_dictionary/washer.yaml 约束",
         f"product_id: littleswan-{m.lower()}",
         f"updated: {UPDATED}",
         "",
         "specs:",
         f"  capacity_kg:          {{value: {d['capacity']}, unit: kg, source: \"ocr#产品介绍.jpg\"}}"]
    L.append("  dry_capacity_kg:      {value: null, unit: kg, source: \"ocr#产品介绍.jpg\","
             " note: 说明书未给干衣容量}")
    if d["spin"]:
        L.append(f"  spin_speed_rpm:       {{value: {d['spin']}, unit: r/min, source: \"ocr#产品介绍.jpg\","
                 " note: 面板最高可调转速}")
    else:
        L.append("  spin_speed_rpm:       null            # 波轮说明书未给脱水转速")
    L.append(f"  energy_grade:         {{value: {d['energy']}, unit: 级, source: \"ocr#产品介绍.jpg\"}}")
    L.append("  water_consumption_l:  null            # 说明书未给单次耗水量")
    L.append(f"  power_w:              {{value: {d['power']}, unit: W, source: \"ocr#产品介绍.jpg\"}}")
    L.append('  power_supply:         {value: "220V~/50Hz", source: "ocr#产品介绍.jpg"}')
    if d["current"]:
        L.append(f"  rated_current_a:      {{value: {d['current']}, unit: A, source: \"ocr#产品介绍.jpg\"}}")
    w0, h0, dp0 = d["size"]
    L.append(f"  size_mm:              {{w: {w0}, h: {h0}, d: {dp0}, unit: mm, source: \"ocr#产品介绍.jpg\",")
    L.append(f"                         note: {q(d['size_order'])}}}")
    if d["size_max"]:
        w1, h1, dp1 = d["size_max"]
        L.append(f"  size_max_mm:          {{w: {w1}, h: {h1}, d: {dp1}, unit: mm, source: \"ocr#产品介绍.jpg\",")
        L.append("                         note: 最大外形尺寸，含凸出件；搬运与嵌入柜体按此算}")
    else:
        L.append("  size_max_mm:          null            # 波轮说明书只给一组外形尺寸")
    L.append(f"  weight_kg:            {{value: {d['weight']}, unit: kg, source: \"ocr#产品介绍.jpg\", note: 机器净重}}")
    L.append("  door_width_min_mm:    null            # 说明书只给使用预留空间，未给搬运最小门宽")
    L.append(f"  loading_type:         {{value: {q(d['loading'])}, source: \"ocr#产品介绍.jpg\"}}")
    L.append('  needs_water_inlet:    {value: true, source: "ocr#产品介绍.jpg"}')
    L.append('  needs_drain:          {value: true, source: "ocr#产品介绍.jpg"}')
    if d["drain"]:
        L.append(f"  drain_type:           {{value: {d['drain']}, source: \"ocr#产品介绍.jpg\"}}")
    else:
        L.append(f"  drain_type:           {{value: null, source: \"ocr#产品介绍.jpg\", note: {q(d['drain_note'])}}}")
    L.append('  water_pressure_mpa:   {min: 0.03, max: 1.0, unit: MPa, source: "ocr#产品介绍.jpg"}')
    L.append('  needs_level_ground:   {value: true, source: "ocr#常见问题.jpg",')
    L.append("                         note: 说明书要求装在坚固水平地面并调平}")
    nw, ns = d["noise"]
    L.append(f"  noise_wash_db:        {{value: {nw}, unit: dB(A), source: \"ocr#产品介绍.jpg\",")
    L.append(f"                         note: {q(d['noise_note'])}}}")
    L.append(f"  noise_spin_db:        {{value: {ns}, unit: dB(A), source: \"ocr#产品介绍.jpg\",")
    L.append(f"                         note: {q(d['noise_note'])}}}")
    L += ["", "missing:"]
    miss = ["water_consumption_l", "door_width_min_mm"]
    if not d["spin"]:
        miss.append("spin_speed_rpm（说明书未给脱水转速）")
    if not d["drain"]:
        miss.append("drain_type（说明书未标注上/下排水，需按实物确认）")
    if not d["size_max"]:
        miss.append("size_max_mm（说明书只有一组外形尺寸）")
    for x in miss:
        L.append(f"  - {x}")
    w(PROD / m / "specs.yaml", L)


def gen_features(m, d):
    L = [f"# 功能清单：售前「有没有」「支不支持」类问题的唯一依据",
         "# supported: true 有 / false 明确没有 / null 说明书未提及（按不确定处理，不得推定）",
         f"product_id: littleswan-{m.lower()}",
         f"updated: {UPDATED}",
         "",
         "features:"]
    for k, tup in d["features"].items():
        sup, src, note = (list(tup) + [None, None])[:3]
        v = {True: "true", False: "false", None: "null"}[sup]
        body = f"  {k}: {{supported: {v}"
        if src:
            body += f", source: \"ocr#{src}\""
        if note:
            body += f", note: {q(note)}"
        body += "}"
        L.append(body)
    prog = d["programs_builtin"]
    if prog:
        L += ["", "# 说明书程序表逐项列出的程序（可在面板直接选）",
              "builtin_programs:"]
        for p in prog:
            L.append(f"  - {q(p)}")
    if d["programs_app"]:
        L += ["", "# 需下载安装后在「美的美居」App 内使用，面板上选不到，回答时必须说清这一点",
              "app_only_programs:"]
        for p in d["programs_app"]:
            L.append(f"  - {q(p)}")
    L += ["", "cannot_claim:"]
    if d["features"].get("dry", (None,))[0] is False:
        L.append("  - 烘干 / 洗烘一体（本机无烘干）")
    elif d["features"].get("dry", (None,))[0] is None:
        L.append("  - 烘干 / 洗烘一体（说明书未明确本型号是否带烘干，不得承诺）")
    L.append("  - 除菌率 / 除螨率的具体百分比（说明书未给检测数据）")
    L.append("  - 具体耗水量（说明书未给）")
    w(PROD / m / "features.yaml", L)


def gen_install(m, d):
    L = ["# 安装条件：安装咨询（s2）与上门前确认的唯一依据",
         "# 洗衣机看的是 电/水/排水/门，不是空调的 电/位/管",
         f"product_id: littleswan-{m.lower()}",
         f"updated: {UPDATED}",
         "",
         "electrical:",
         '  supply: {value: "220V~/50Hz", source: "ocr#产品介绍.jpg"}',
         f"  rated_power_w: {{value: {d['power']}, source: \"ocr#产品介绍.jpg\"}}"]
    if d["current"]:
        L.append(f"  rated_current_a: {{value: {d['current']}, source: \"ocr#产品介绍.jpg\"}}")
    L.append('  dedicated_circuit: {value: null, source: null,')
    L.append("    note: 说明书未强制要求专用回路；带加热洗的机型功率大，建议独立回路加漏电保护（建议口径，非厂家强制）}")
    L.append('  grounding: {value: true, source: null,')
    L.append("    note: 说明书未单列接地章节，按 I 类电器通用要求必须可靠接地}")
    L += ["", "water:",
          '  inlet_required: {value: true, source: "ocr#产品介绍.jpg"}',
          '  pressure_mpa: {min: 0.03, max: 1.0, source: "ocr#产品介绍.jpg",',
          "    text: 使用水压 0.03~1.0MPa；低于下限表现为进水超时，高于上限需加装减压阀}"]
    if d["form"] == "front":
        L.append('  inlet_filter: {source: "ocr#清洁保养.jpg",')
        L.append("    text: 进水阀处有过滤网，进水变慢时清洗；清洗前先关进水龙头}")
    L += ["", "drain:"]
    if d["form"] == "front":
        L.append(f"  type: {{value: {q(d['drain'])}, source: \"ocr#产品介绍.jpg\"}}")
        L.append('  sink_required: {value: false, source: "ocr#产品介绍.jpg",')
        L.append("    text: 上排水机型不需要地漏，可排入水槽或专用排水接口；不能按「必须留地漏」回答}")
        L.append('  max_hose_height_mm: {value: 1000, source: "ocr#常见问题.jpg",')
        L.append("    text: 排水管最高处请勿超过 100cm，超了排水泵顶不上去，会报排水超时}")
    else:
        L.append('  type: {value: null, source: "ocr#产品介绍.jpg",')
        L.append(f"    text: {q(d['drain_note'])}}}")
        L.append('  hose_setup: {source: "ocr#快速入门.jpg",')
        L.append("    text: 洗衣前需放下排水管；排水管不得高于机器排水口，否则排不出去}")
    L += ["", "space:"]
    if d["form"] == "front":
        L.append('  front_clearance_mm: {value: 900, source: "ocr#产品介绍.jpg",')
        L.append("    text: 机器正面需预留 900mm 以上使用空间（开门、取放衣物），不是搬运通过尺寸}")
        L.append('  embed_gap: {source: "ocr#产品介绍.jpg",')
        L.append("    text: 嵌入式安装时左右后上最小间隙，见说明书图示；只用于散热与维护，不含开门空间}")
    else:
        L.append('  top_clearance_mm: {value: null, source: null,')
        L.append("    text: 波轮为顶开盖，上方必须留出开盖空间；说明书未给具体数值，需按现场确认}")
    L.append('  level_ground: {value: true, source: "ocr#常见问题.jpg",')
    L.append("    text: 必须放在坚固水平地面并调平；不平会直接表现为脱水震动大、噪音大、机器移位}")
    L += ["", "transport:"]
    if d["form"] == "front":
        L.append(f"  transport_bolts: {{value: {d['transport_bolts']}, source: \"ocr#常见问题.jpg\",")
        L.append("    text: 机身背面 4 颗运输螺栓，安装后必须拆除；不拆就脱水会剧烈震动甚至损坏}")
        if d["size_max"]:
            w1, h1, dp1 = d["size_max"]
            L.append(f"  max_size_mm: {{w: {w1}, h: {h1}, d: {dp1}, source: \"ocr#产品介绍.jpg\",")
            L.append("    text: 搬运/过门按最大外形尺寸（含凸出件）计算}")
    else:
        L.append("  note: 波轮说明书未提运输螺栓；搬运时按外形尺寸与整机重量评估")
    L.append(f"  net_weight_kg: {{value: {d['weight']}, source: \"ocr#产品介绍.jpg\"}}")
    L += ["", "pre_visit_checklist:"]
    chk = ["放置位置是否有进水龙头（普通 3/4 螺纹）",
           "插座位置与是否带地线，会不会被机身挡住"]
    if d["form"] == "front":
        chk.insert(1, "是否有地漏（上排水机型不是必须，但用户常误以为必须）")
        chk.append("是否需要叠放/嵌入柜体（决定预留间隙）")
    else:
        chk.insert(1, "排水往哪里排（说明书未标排水方式，需现场确认）")
        chk.append("机器上方是否有足够开盖空间")
    chk.append("从楼下到房间最窄门口宽度、楼梯转角")
    chk.append("楼层与是否有电梯（影响上楼费）")
    for c in chk:
        L.append(f"  - {q(c)}")
    L += ["", "chargeable_items: [上楼费, 远程上门, 加长进水管, 拆门/扩门洞, 加装水龙头]",
          "not_applicable: [高空作业费, 加长冷媒管, 支架费]      # 洗衣机不产生这些费用",
          "", "missing:"]
    if d["form"] == "front":
        L.append("  - door_width_min_mm（说明书未给搬运最小门宽，只有 900mm 使用空间）")
    else:
        L.append("  - 开盖预留空间具体数值（说明书为图示，需按现场确认）")
    L.append("  - 是否强制专用回路（说明书未写，按建议口径回答）")
    w(PROD / m / "install.yaml", L)


def gen_usage(m, d):
    L = ["# 操作与保养：使用咨询（s3）的唯一依据",
         "# 原则：一次只给一个动作，步骤照抄说明书不改序",
         f"product_id: littleswan-{m.lower()}",
         f"updated: {UPDATED}",
         ""]
    if d["form"] == "front":
        L += ["first_use:", "  items:",
              "    - 拆掉机身背面 4 颗运输螺栓（不拆会剧烈震动，是「新机就抖」的头号原因）",
              "    - 调平：对角线按压顶盖，调整底脚使机器平稳",
              "    - 先跑一次「筒自洁」程序清洁内筒，不加衣物、不加洗涤剂",
              "    - 打开进水龙头，检查进水管接口是否渗漏"]
        L += ["  source: \"ocr#温馨提示.jpg\"", "",
              "wash_steps:", "  items:",
              "    - 开门：按压机门右侧打开",
              "    - 投放衣物：不要把衣物夹在机门处，否则会损坏衣物甚至漏水",
              "    - 关门：听到「咔哒」声表示已关好",
              "    - 投放洗涤剂后按启动",
              "  source: \"ocr#快速入门-图文.jpg\"", "",
              "detergent:", "  items:",
              "    - 洗涤剂盒为自动投放盒，可存储一定量洗衣液和柔顺剂，不必每次都加",
              "    - 洗衣凝珠直接放进洗涤筒，不要放洗涤剂盒；用凝珠时要关闭自动投放",
              "    - 结块或粘稠的洗涤剂，倒入前先用少量水稀释",
              "    - 不要用高泡洗衣液：泡沫多会导致漂洗不净或泡沫溢出漏水",
              "    - 机器运行时不要打开洗涤剂盒；料盒没装到位时屏幕图标闪烁，装好即熄灭",
              "  source: \"ocr#快速入门-图文.jpg\"", ""]
    else:
        L += ["first_use:", "  items:",
              "    - 首次使用前，用柔软干净的布条清洁内桶",
              "    - 接通电源，放下排水管，放入衣物，打开水龙头，添加洗涤剂",
              "    - 因串色与染料有关，建议衣物分开洗涤",
              "  source: \"ocr#快速入门.jpg\"", "",
              "wash_steps:", "  items:",
              "    - 轻触电源键开机，等待功能键全亮进入待机状态",
              "    - 连续按「程序」键选择程序（数码管显示 01~07）",
              "    - 按「水位」键设定水量；需要更高洗净度可长按强度键调 P1/P2/P3",
              "    - 需要预约时，长按预约键 3 秒设定预约时间",
              "    - 按「启动/暂停」键开始洗涤",
              "  source: \"ocr#快速入门.jpg\"", "",
              "detergent:", "  items:",
              "    - 波轮为手动投放，按水位与衣物量自行添加，不要用高泡洗衣液",
              "    - 洗衣结束后请关闭水龙头、切断电源",
              "  source: \"ocr#快速入门.jpg\"", ""]
    L += ["maintenance:", "  filter_clean:", "    steps:",
          "      - 拔掉电源插头，打开过滤器门／排污口",
          "      - 先确认筒内水已冷却，再拔管堵头放水（避免烫伤）",
          "      - 取出过滤网清理杂物，装回并确认到位",
          "    source: \"ocr#清洁保养.jpg\""]
    if d["form"] == "front":
        L += ["  detergent_box_clean:", "    steps:",
              "      - 拉出洗涤剂盒并向上轻抬取出",
              "      - 分别取出洗衣液、柔顺剂自动投放盒，打开盒盖用软毛刷清洁后冲净",
              "      - 装回后清洁洗涤盒下方前封门处的水渍与残留",
              "    source: \"ocr#清洁保养.jpg\"",
              "  inlet_valve_filter:", "    steps:",
              "      - 先关闭进水龙头",
              "      - 拧下进水管，用毛刷清洗进水阀处过滤网",
              "      - 重新接好后打开水龙头，检查是否漏水",
              "    source: \"ocr#清洁保养.jpg\""]
    if d["form"] == "front":
        L += ["  emergency_door_open:", "    steps:",
              "      - 关闭电源并拔掉插头",
              "      - 打开过滤器门，先排掉筒内已冷却的水",
              "      - 用工具插入橙色紧急拉绳向下拉，打开门锁",
              "    warning: 正常洗涤过程中禁止使用紧急拉绳；必须使用时须等水冷却，避免高温水流出烫伤",
              "    source: \"ocr#清洁保养.jpg\""]
    L += ["", "missing:",
          "  - 过滤器清理的官方建议周期（说明书只写「定期」）"]
    if d["form"] == "front":
        L.append("  - 内筒深度清洁（除筒自洁外）的官方方法")
    w(PROD / m / "usage.yaml", L)


def gen_faults(m, d):
    L = ["# 型号差异：只放本型号特有的东西",
         "# 跨型号通用的现象排查在 04_scenario/s4_troubleshoot/symptoms.yaml",
         f"product_id: littleswan-{m.lower()}",
         f"updated: {UPDATED}",
         "",
         "codes:",
         "  has_code_display: true"]
    if d["form"] == "top":
        L.append("  display: 数码管（程序显示 01~07，报警显示 E 类代码）")
    else:
        L.append("  display: LED（面板显示信息代码）")
    L += ["  rule: [关机后重新开机启动, 同一代码再次出现则报修]"]
    if d["codes_known"]:
        L.append("  known:")
        for code, meaning, action, conf in d["codes_known"]:
            L.append(f"    - code: \"{code}\"")
            L.append(f"      meaning: {q(meaning)}")
            L.append(f"      action: {q(action)}")
            L.append(f"      confidence: {conf}")
            if conf != "confirmed":
                L.append("      note: 代码数字来自图片 OCR，需对照原图确认")
    else:
        L.append("  known: []      # 代码表 OCR 不可靠，未采信任何数字")
    L += ["  note: " + q(d["codes_note"])]
    L += ["", "# 说明书明确写出的「现象 → 原因 → 解决」，可作为自助排查第一步",
          "self_check:"]
    for sym, cause, act in SELF_CHECK:
        L += [f"  - symptom: {q(sym)}",
              f"    cause: {q(cause)}",
              f"    action: {q(act)}",
              "    source: \"ocr#常见问题.jpg\""]
    L += ["", "# 说明书明确「属正常」的现象，用于拦截假故障工单",
          "normal_phenomena:"]
    for sid, sym, exp in NORMAL:
        L += [f"  - id: {sid}",
              f"    symptom: {q(sym)}",
              f"    explain: {q(exp)}",
              "    source: \"ocr#常见问题.jpg\""]
    L += ["", "protection:"]
    if d["form"] == "front":
        L += ["  unbalance_detect_rpm: {value: 100, unit: r/min, source: \"ocr#使用说明书-特色功能介绍.jpg\",",
              "    text: 高速脱水前以约 100rpm 做偏心检测，多次仍不平衡则提示 Ub}",
              "  drain_hose_max_height_mm: {value: 1000, source: \"ocr#常见问题.jpg\"}"]
    else:
        L += ["  drain_hose_max_height_mm: {value: null, source: null,",
              "    note: 波轮说明书未给排水管高度限值，但排水管必须低于机器排水口才能自流排出}"]
    L += ["", "model_specific_notes:"]
    if d["drain"]:
        L.append("  - 本机为上排水：排水超时先查排水管高度（≤100cm）和管夹，不要先去查地漏")
    else:
        L.append("  - 本机排水方式说明书未标注：用户说「排不出去」时，先确认排水管是否已放下、是否高于机器排污口")
    if d["form"] == "front":
        L.append("  - 本机带自动投放：料盒未关会报代码，用户常误以为机器坏了")
    if d["features"].get("dry", (None,))[0] is False:
        L.append("  - 本机无烘干：用户说「不干」时先确认是不是单洗机型，不存在「烘干坏了」")
    elif d["features"].get("dry", (None,))[0] is None:
        L.append("  - 烘干能力说明书未明确：用户问「能不能烘干」必须让其看面板是否有烘干程序，不得凭型号回答")
    L += ["", "missing:",
          "  - 完整故障代码表（说明书代码数字 OCR 不可靠，需对照原图或向售后索取官方代码表）",
          "  - 各代码对应的具体故障部件（说明书未列）"]
    w(PROD / m / "faults.yaml", L)


def main():
    made = 0
    for m, d in DATA.items():
        src_dir = ROOT / "01_source/washer/littleswan" / m
        srcs = sorted(f.name for f in src_dir.iterdir() if f.is_file())
        gen_product(m, d, srcs)
        gen_specs(m, d)
        gen_features(m, d)
        gen_install(m, d)
        gen_usage(m, d)
        gen_faults(m, d)
        made += 1
        print(f"[card] littleswan/{m}  5 文件 + product.yaml")
    print(f"\n完成 {made} 个型号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
