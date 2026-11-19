#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""西门子洗衣机/干衣机参数回填（方案 B：脚本生成 + 人工核对清单）

西门子资料与国产牌三点不同，脚本里必须区别对待：
  1. 尺寸按「高/宽/深 + 门关闭深度 + 门打开深度」给，比国产品牌多两个值，
     而且「门打开深度」才是搬运/过道真正要看的数；
  2. 水压下限是 0.1 MPa（1 bar），高于国产品牌的 0.03 MPa，不能互相套用；
  3. WQ 是热泵干衣机：**不需要进水**，冷凝水可存水箱或接软管直排。
     按洗衣机的「必须有进水龙头」回答会直接劝退用户。

数据纪律同 backfill_washer_sw.py：每项带 source，说明书没写的标 null 进 missing。
WM12P2602W / WN54A1X42W 两份 PDF 文本层损坏，数值全部来自 OCR，note 里单独标注。

用法:
  python tools/backfill_washer_siemens.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "03_product/washer/siemens"
UPDATED = "2026-10-23"


def q(s):
    s = str(s)
    if re.search(r"[:'\"#\[\]{}&*!|>%@`]", s) or s != s.strip():
        return "'" + s.replace("'", "''") + "'"
    return s


def S(page, ocr=False):
    """来源锚点：正常 PDF 指向 text.md 页码；乱码 PDF 指向 ocr.md 页码"""
    return f"ocr#p{page}" if ocr else f"p{page}"


DATA = {
    "WG52A108AW": {
        "ptype": "滚筒洗衣机（前开式，单洗）",
        "colloquial": "10公斤滚筒",
        "form": "front",
        "ocr": False,
        "page": 17,
        "cap": 10.0,
        "dry_cap": None,
        "power": 1900,
        "dry_power": None,
        "current": 10,
        "voltage": "220V，50Hz",
        "size": (598, 848, 590),
        "size_note": "原文「电器高度 84.8cm / 电器宽度 59.8cm / 电器深度 59.0cm」，已换算成 mm 并按 w/h/d 存放",
        "door_close_d": 636,
        "door_open_d": 1073,
        "weight": 70.1,
        "noise": (52, 72),
        "energy": 1,
        "wp_min": 0.1,
        "hose_in": 120,
        "hose_out": 150,
        "cord": 210,
        "spin": 1200,
        "dry": (False, "单洗机型，技术数据无干衣容量、无烘干功率"),
    },
    "WG54H2V00W": {
        "ptype": "滚筒洗衣机（前开式，单洗）",
        "colloquial": "10公斤滚筒",
        "form": "front",
        "ocr": False,
        "page": 18,
        "cap": 10.0,
        "dry_cap": None,
        "power": 1900,
        "dry_power": None,
        "current": 10,
        "voltage": "220V，50Hz",
        "size": (598, 848, 590),
        "size_note": "原文「电器高度 84.8cm / 电器宽度 59.8cm / 电器深度 59.0cm」，已换算成 mm 并按 w/h/d 存放",
        "door_close_d": 636,
        "door_open_d": 1073,
        "weight": 78.4,
        "noise": (46, 69),
        "energy": 1,
        "wp_min": 0.1,
        "hose_in": 120,
        "hose_out": 150,
        "cord": None,
        "spin": None,
        "dry": (False, "单洗机型，技术数据无干衣容量、无烘干功率"),
    },
    "WM12P2602W": {
        "ptype": "滚筒洗衣机（前开式，单洗）",
        "colloquial": "10公斤滚筒",
        "form": "front",
        "ocr": True,
        "page": 35,
        "cap": 10.0,
        "cap_inferred": True,
        "dry_cap": None,
        "power": 2100,
        "dry_power": None,
        "current": 10,
        "voltage": "220V，50Hz",
        "size": (598, 848, 590),
        "size_note": "原文「a=598mm b=590mm c=848mm」，按 a=宽 b=深 c=高 换算，已按 w/h/d 存放",
        "door_close_d": None,
        "door_open_d": None,
        "weight": 79,
        "noise": None,
        "energy": None,
        "wp_min": 0.1,
        "hose_in": None,
        "hose_out": None,
        "cord": None,
        "spin": None,
        "dry": (False, "单洗机型，技术规格无干衣容量、无烘干功率"),
    },
    "WN54A1X42W": {
        "ptype": "洗干一体机（滚筒，前开式）",
        "colloquial": "10公斤洗烘一体",
        "form": "washer_dryer",
        "ocr": True,
        "page": 44,
        "cap": 10.0,
        "dry_cap": 7.0,
        "power": 1900,
        "dry_power": 1650,
        "current": 10,
        "voltage": "220V，50Hz",
        "size": (598, 848, 590),
        "size_note": "原文「a=59.8cm b=59.0cm c=84.8cm」，按 a=宽 b=深 c=高 换算，已按 w/h/d 存放",
        "door_close_d": None,
        "door_open_d": None,
        "weight": 78,
        "noise": None,
        "energy": None,
        "wp_min": 0.1,
        "hose_in": None,
        "hose_out": None,
        "cord": None,
        "spin": None,
        "dry": (True, "洗干一体：最大洗涤 10.0kg、最大干衣 7.0kg（棉织物程序为 5.0kg 洗涤 / 7.0kg 干衣）"),
    },
    "WQ53E2D01W+WQ53E2D11W": {
        "ptype": "热泵干衣机（滚筒，两台套装）",
        "colloquial": "热泵干衣机",
        "form": "dryer",
        "ocr": False,
        "page": 19,
        "cap": None,
        "dry_cap": 10.0,
        "power": 600,
        "dry_power": None,
        "current": 10,
        "voltage": "220V，50Hz",
        "size": (598, 842, 613),
        "size_note": "原文「电器高度 84.2cm / 电器宽度 59.8cm / 电器深度 61.3cm」，已换算成 mm 并按 w/h/d 存放",
        "door_close_d": 648,
        "door_open_d": 1096,
        "weight": 50.1,
        "noise": (None, 61),
        "energy": None,
        "wp_min": None,
        "hose_in": None,
        "hose_out": None,
        "cord": 145,
        "spin": None,
        "dry": (True, "热泵干衣机，最大干衣容量 10.0kg；执行标准 GB/T 23118"),
    },
}

SELF_CHECK_COMMON = [
    ("机器不启动", "门没关好 / 未按启动键 / 儿童锁开启 / 停电",
     "关好门并听到锁合声；按启动/暂停；解除儿童锁；确认供电"),
    ("机器不进水", "水龙头未开 / 进水管弯折或过滤网堵 / 水压不足",
     "打开水龙头；检查进水管是否弯折；清洗进水阀过滤网；确认水压不低于 0.1MPa（1 bar）"),
    ("机器不排水", "排水软管弯折、挤压或安装过高 / 排水泵或滤网堵塞",
     "检查排水软管走向，末端不得高于 100cm 且不得浸入水中；清洁排水泵"),
    ("机器漏水", "进水管螺纹未拧紧 / 排水软管松脱或破损",
     "进水接头只用手拧紧（用工具会拧裂螺纹）；检查排水软管与接口"),
    ("脱水效果差、衣物仍湿", "设定了低脱水转速 / 衣物不平衡机器自动降速",
     "下次设定较高脱水转速；把大件与小件混装，避免单件偏心"),
    ("震动大、噪音大、机器移位", "运输固定件未拆除 / 未调平 / 地面不坚固",
     "拆除背面运输螺栓；用水平仪调平并锁紧防松螺母"),
    ("洗涤效果不佳", "衣物过脏 / 洗涤剂不足 / 装载过量",
     "选择合适程序与温度；按水质硬度调整用量；不要超过最大负载量"),
]

NORMAL_COMMON = [
    ("drum_light_noise", "排水泵有嘎嘎声",
     "排水泵排水时属正常；若持续异响先清排水泵滤网检查异物"),
    ("door_locked_running", "运行中机门打不开",
     "门锁保护，运行中不放行；程序结束约 2 分钟后自动解锁"),
    ("low_spin_auto", "脱水转速比设定的低",
     "机器检测到衣物不平衡会自动降低转速，属保护行为"),
    ("foam_extra_time", "程序时间变长、迟迟不结束",
     "检测到泡沫过多会追加漂洗，属正常"),
]


def w(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def gen_product(m, d, srcs):
    L = [f"product_id: siemens-{re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')}",
         "status: ready",
         "appliance: washer",
         "appliance_cn: 洗衣机",
         "brand: siemens",
         "brand_cn: 西门子",
         f"model: {m}",
         f"model_slug: {m}",
         "indoor_model: null",
         "outdoor_model: null",
         f"product_type: {q(d['ptype'])}",
         "product_type_source: 型号编码 + 技术数据页",
         f"colloquial: {q(d['colloquial'])}",
         f"energy_grade: {d['energy'] if d['energy'] is not None else 'null'}",
         "launch_year: null",
         f"updated: {UPDATED}",
         "",
         "source_docs:"]
    for s in srcs:
        L.append(f"  - file: {s}")
    if d["ocr"]:
        L.append("  - note: 该 PDF 文本层损坏（字体未嵌 ToUnicode，抽出即乱码），全部文本由 tools/ocr.py 整本识别")
    L += ["", "material_status:",
          "  text_extracted: true",
          f"  text_source: {'ocr' if d['ocr'] else 'pdf'}",
          f"  ocr_needed: {'true' if d['ocr'] else 'false'}",
          f"  ocr_done: {'true' if d['ocr'] else 'false'}",
          "", "notes:"]
    if d["form"] == "dryer":
        L.append("  - 热泵干衣机：不需要进水管。用户问「要不要接水龙头」答「不用」")
        L.append("  - 型号含 '+'，是两台机器的套装，共用一份说明书；两台参数不同时必须分别说明")
    if d["ocr"]:
        L.append("  - 数值全部来自 OCR（原 PDF 文本层不可用），比原生文本层更易错，回填时须对照原图")
    w(PROD / m / "product.yaml", L)


def gen_specs(m, d):
    p = S(d["page"], d["ocr"])
    L = [f"# 参数来源：西门子 {m}「技术数据」页（{'OCR，原 PDF 文本层损坏' if d['ocr'] else 'PDF 原生文本层'}）",
         "# 键名受 00_schema/spec_dictionary/washer.yaml 约束",
         f"product_id: siemens-{re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')}",
         f"updated: {UPDATED}",
         "",
         "specs:"]
    if d["cap"]:
        if d.get("cap_inferred"):
            L.append(f"  capacity_kg:          {{value: {d['cap']}, unit: kg, source: \"型号编码 XQG100\",")
            L.append("                         confidence: inferred,")
            L.append("                         note: 技术规格页未给最大负载量；型号编码 XQG100 中的 100 表示 10kg，故按 10kg 记录，需按铭牌复核}")
        else:
            L.append(f"  capacity_kg:          {{value: {d['cap']}, unit: kg, source: {q(p)}}}")
    else:
        L.append("  capacity_kg:          null            # 干衣机，无洗涤容量")
    if d["dry_cap"]:
        L.append(f"  dry_capacity_kg:      {{value: {d['dry_cap']}, unit: kg, source: {q(p)}}}")
    else:
        L.append("  dry_capacity_kg:      null            # 单洗机型，无干衣容量")
    L.append(f"  spin_speed_rpm:       {('{value: ' + str(d['spin']) + ', unit: r/min, source: ' + q(p) + '}') if d['spin'] else 'null            # 技术数据页未给脱水转速'}")
    L.append(f"  energy_grade:         {('{value: ' + str(d['energy']) + ', unit: 级, source: ' + q(p) + ', note: GB 12021.4}') if d['energy'] is not None else 'null            # 技术数据页未给能效等级'}")
    L.append("  water_consumption_l:  null")
    L.append(f"  power_w:              {{value: {d['power']}, unit: W, source: {q(p)}}}")
    if d["dry_power"]:
        L.append(f"  dry_power_w:          {{value: {d['dry_power']}, unit: W, source: {q(p)}, note: 额定烘干功率}}")
    L.append(f"  power_supply:         {{value: {q(d['voltage'])}, source: {q(p)}}}")
    L.append(f"  rated_current_a:      {{value: {d['current']}, unit: A, source: {q(p)}, note: 原文「最小接线保险」}}")
    w0, h0, dp0 = d["size"]
    L.append(f"  size_mm:              {{w: {w0}, h: {h0}, d: {dp0}, unit: mm, source: {q(p)},")
    L.append(f"                         note: {q(d['size_note'])}}}")
    if d["door_open_d"]:
        L.append(f"  door_open_depth_mm:   {{value: {d['door_open_d']}, unit: mm, source: {q(p)},")
        L.append("                         note: 门打开时的整机深度，过道/门口转弯要按这个算，不是机身深度}")
    else:
        L.append("  door_open_depth_mm:   null            # 技术规格页未给门打开深度")
    L.append(f"  weight_kg:            {{value: {d['weight']}, unit: kg, source: {q(p)}}}")
    L.append("  door_width_min_mm:    null")
    L.append(f"  loading_type:         {{value: {'滚筒（前开式）' if d['form'] != 'dryer' else '滚筒干衣机（前开式）'}, source: {q(p)}}}")
    if d["form"] == "dryer":
        L.append("  needs_water_inlet:    {value: false, source: null,")
        L.append("                         note: 热泵干衣机不需要进水管；冷凝水可存水箱或接软管直排}")
        L.append("  needs_drain:          {value: false, source: null,")
        L.append("                         note: 不强制接排水；接直排软管可免去倒水箱}")
        L.append("  drain_type:           null            # 干衣机无上下排水概念")
        L.append("  water_pressure_mpa:   null            # 干衣机不需要进水")
    else:
        L.append("  needs_water_inlet:    {value: true, source: %s}" % q(p))
        L.append("  needs_drain:          {value: true, source: %s}" % q(p))
        L.append("  drain_type:           {value: 上排水, source: %s, note: 排水泵排水，排水管末端 0~100cm 高度}" % q(p))
        L.append(f"  water_pressure_mpa:   {{min: {d['wp_min']}, max: 1.0, unit: MPa, source: {q(p)},")
        L.append("                         note: 原文 0.1~1 MPa（1~10 bar）；低于下限需增压，高于上限需装减压阀}")
    L.append(f"  needs_level_ground:   {{value: true, source: {q(p)}, note: 必须调平，否则震动与噪音明显增大}}")
    # 技术数据页可能整组缺噪声数据（乱码 PDF 尤其常见），按 (None, None) 兜底，
    # 不能直接解包，否则一个型号缺数据就把整批回填打断
    nw, ns = d["noise"] if d["noise"] else (None, None)
    if nw:
        L.append(f"  noise_wash_db:        {{value: {nw}, unit: dB(A), source: {q(p)}, note: GB/T 19606}}")
    else:
        L.append("  noise_wash_db:        null            # 技术数据页未给洗涤噪声")
    if ns:
        label = "烘干噪声" if d["form"] == "dryer" and not nw else "脱水噪声"
        L.append(f"  noise_spin_db:        {{value: {ns}, unit: dB(A), source: {q(p)}, note: {label}，GB/T 19606}}")
    else:
        L.append("  noise_spin_db:        null            # 技术数据页未给脱水噪声")
    if d["hose_in"]:
        L.append(f"  hose_inlet_cm:        {{value: {d['hose_in']}, unit: cm, source: {q(p)}}}")
    if d["hose_out"]:
        L.append(f"  hose_drain_cm:        {{value: {d['hose_out']}, unit: cm, source: {q(p)}}}")
    if d["cord"]:
        L.append(f"  power_cord_cm:        {{value: {d['cord']}, unit: cm, source: {q(p)}}}")
    L += ["", "missing:"]
    if not d["spin"]:
        L.append("  - spin_speed_rpm（技术数据页未给，面板可调）")
    if d["energy"] is None:
        L.append("  - energy_grade（技术数据页未给能效等级）")
    if not d["noise"] or d["noise"][0] is None:
        L.append("  - noise_wash_db（技术数据页未给洗涤噪声）")
    if not d["noise"] or d["noise"][1] is None:
        L.append("  - noise_spin_db（技术数据页未给脱水/烘干噪声）")
    L.append("  - door_width_min_mm（说明书未给搬运最小门宽，只给机身与开门深度）")
    L.append("  - water_consumption_l（说明书未给单次耗水量）")
    w(PROD / m / "specs.yaml", L)


def gen_features(m, d):
    L = ["# 功能清单：售前「有没有」「支不支持」类问题的唯一依据",
         "# supported: true 有 / false 明确没有 / null 说明书未提及（按不确定处理，不得推定）",
         f"product_id: siemens-{re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')}",
         f"updated: {UPDATED}",
         "",
         "features:"]
    sup, note = d["dry"]
    L.append(f"  dry: {{supported: {'true' if sup else 'false'}, note: {q(note)}}}")
    if d["form"] == "dryer":
        L.append("  wash: {supported: false, note: 干衣机，没有洗涤功能。用户问「能不能洗」答不能}")
    else:
        L.append("  wash: {supported: true}")
    L += ["  wifi: {supported: null, note: 说明书未单列联网功能，需按面板确认}",
          "  child_lock: {supported: true, note: 说明书有儿童锁按键说明}",
          "  delay_start: {supported: null, note: 按面板确认}",
          "  self_clean: {supported: null, note: 说明书未提筒自洁程序，仅有清洁保养章节}",
          "  auto_dosing: {supported: null, note: 说明书未见自动投放，按面板确认}",
          "  steam: {supported: null}",
          "", "# 明确不能说的（防止幻觉，也防投诉）", "cannot_claim:"]
    if d["form"] == "dryer":
        L.append("  - 洗涤功能（干衣机不能洗）")
        L.append("  - 蒸汽除菌（说明书未声明）")
    elif sup:
        L.append("  - 洗干同时进行（洗干一体是「洗完再烘」，不能一边洗一边烘）")
        L.append("  - 蒸汽除菌（说明书未声明）")
    else:
        L.append("  - 烘干 / 洗烘一体（本机为单洗）")
        L.append("  - 蒸汽除菌（说明书未声明）")
    L.append("  - 除菌率 / 除螨率的具体百分比（说明书未给检测数据）")
    w(PROD / m / "features.yaml", L)


def gen_install(m, d):
    p = S(d["page"], d["ocr"])
    L = ["# 安装条件：安装咨询（s2）与上门前确认的唯一依据",
         "# 洗衣机看 电/水/排水/门；干衣机只看 电/空间/排水（可无）",
         f"product_id: siemens-{re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')}",
         f"updated: {UPDATED}",
         "",
         "electrical:",
         f"  supply: {{value: {q(d['voltage'])}, source: {q(p)}}}",
         f"  rated_power_w: {{value: {d['power']}, source: {q(p)}}}"]
    if d["dry_power"]:
        L.append(f"  dry_power_w: {{value: {d['dry_power']}, source: {q(p)}}}")
    L.append(f"  min_fuse_a: {{value: {d['current']}, source: {q(p)},")
    L.append("    text: 原文「最小接线保险 10A」；插座与线路按此配置}")
    L.append("  grounding: {value: true, source: null,")
    L.append("    note: 说明书要求居家电气安装符合当地法规并包含漏电保护断路器}")
    if d["cord"]:
        L.append(f"  cord_length_cm: {{value: {d['cord']}, source: {q(p)}, note: 插座需在电源线可达范围内}}")
    L += ["", "water:"]
    if d["form"] == "dryer":
        L.append("  inlet_required: {value: false, source: null,")
        L.append("    text: 热泵干衣机不需要进水管，这是与洗衣机最大的差别}")
        L.append("  condensate: {value: true, source: \"p8\",")
        L.append("    text: 冷凝水可存于水箱定期倒掉，也可用软管直排；直排时必须正确固定软管，否则有漏水风险}")
    else:
        L.append("  inlet_required: {value: true, source: %s}" % q(p))
        L.append(f"  pressure_mpa: {{min: {d['wp_min']}, max: 1.0, source: {q(p)},")
        L.append("    text: 水压 0.1~1MPa（1~10 bar）；水龙头打开时流量至少 8 升/分钟；高于上限需装减压阀}")
        L.append("  connector: {value: '水龙头 G3/4 外螺纹', source: 'p8',")
        L.append("    text: 只用**手**拧紧螺纹接头，用钳子会拧裂螺纹}")
        if d["hose_in"]:
            L.append(f"  hose_inlet_cm: {{value: {d['hose_in']}, source: {q(p)}}}")
    L += ["", "drain:"]
    if d["form"] == "dryer":
        L.append("  required: {value: false, source: null,")
        L.append("    text: 不接排水也可以使用（倒水箱）；接直排更省事}")
    else:
        L.append("  type: {value: 上排水, source: %s}" % q(p))
        L.append("  sink_required: {value: false, source: null,")
        L.append("    text: 上排水不需要地漏，可排入水槽、虹吸管或下水道井口；不能按「必须留地漏」回答}")
        L.append("  max_hose_height_mm: {value: 1000, source: 'p8',")
        L.append("    text: 排水软管末端高度 0~100cm；任何部位不得高于 100cm，且末端不得浸入排出的水中（会回流）}")
        if d["hose_out"]:
            L.append(f"  hose_drain_cm: {{value: {d['hose_out']}, source: {q(p)}}}")
    L += ["", "space:"]
    w0, h0, dp0 = d["size"]
    L.append(f"  body_mm: {{w: {w0}, h: {h0}, d: {dp0}, source: {q(p)}}}")
    if d["door_open_d"]:
        L.append(f"  door_open_depth_mm: {{value: {d['door_open_d']}, source: {q(p)},")
        L.append("    text: 门打开时的深度；过道宽度与门洞转弯必须按这个算，只看机身深度会判断错误}")
    L.append("  stackable: {value: null, source: null,")
    L.append("    text: 是否可叠放需看是否配叠放支架与机型组合，说明书未给通用结论，不得口头承诺}")
    L += ["", "transport:",
          f"  net_weight_kg: {{value: {d['weight']}, source: {q(p)}}}",
          "  transport_bolts: {value: 4, source: 'p7',",
          "    text: 背面 4 颗运输固定螺栓，用 13 号扳手松开拆下，再装入孔塞；不拆就脱水会剧烈震动}"]
    L += ["", "pre_visit_checklist:"]
    if d["form"] == "dryer":
        chk = ["放置位置附近有插座吗（不需要水龙头）",
               "冷凝水打算倒水箱还是接软管直排",
               "机器正面开门空间是否足够（门打开深度近 110cm）",
               "是否需要与洗衣机叠放（需专用连接件）"]
    else:
        chk = ["放置位置有水龙头吗，接口是不是 G3/4 外螺纹",
               "水压是否稳定（低于 0.1MPa 会报进水故障）",
               "水龙头打开时流量是否达到 8 升/分钟",
               "插座位置与是否带地线，会不会被机身挡住",
               "是否需要嵌入柜体（决定预留间隙与散热）"]
    chk += ["从楼下到房间最窄门口宽度、楼梯转角",
            "楼层与是否有电梯（影响上楼费）"]
    for c in chk:
        L.append(f"  - {q(c)}")
    if d["form"] == "dryer":
        # 干衣机不进水、不排水，加长进水管/装水龙头这类收费项跟它无关，
        # 混进去会让客服在干衣机订单上开出不存在的水路费用
        L += ["", "chargeable_items: [上楼费, 远程上门, 拆门/扩门洞, 叠放支架]",
              "not_applicable: [高空作业费, 加长冷媒管, 加长进水管, 加装水龙头, 地漏改造]"]
    else:
        L += ["", "chargeable_items: [上楼费, 远程上门, 加长进水管, 拆门/扩门洞, 加装水龙头]",
              "not_applicable: [高空作业费, 加长冷媒管, 支架费]"]
    L += ["", "missing:"]
    L.append("  - door_width_min_mm（说明书未给搬运最小门宽）")
    L.append("  - 是否可叠放叠放支架型号（说明书未给通用结论）")
    w(PROD / m / "install.yaml", L)


def gen_usage(m, d):
    L = ["# 操作与保养：使用咨询（s3）的唯一依据",
         "# 原则：一次只给一个动作，步骤照抄说明书不改序",
         f"product_id: siemens-{re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')}",
         f"updated: {UPDATED}",
         ""]
    if d["form"] == "dryer":
        L += ["first_use:", "  items:",
              "    - 拆掉运输固定件（干衣机同样有运输螺栓）",
              "    - 调平：用水平仪确认，四个支脚都牢靠落地后锁紧防松螺母",
              "    - 确认冷凝水箱已装到位，或已正确连接直排软管",
              "  source: \"p7\"", "",
              "wash_steps:", "  items:",
              "    - 打开机门，抖散衣物后放入，不要塞紧",
              "    - 检查衣物护理标签，确认可滚筒烘干",
              "    - 关好机门，选择程序后启动",
              "    - 程序结束后及时取出衣物，避免回潮起皱",
              "  source: \"p19\"", "",
              "maintenance:", "  empty_water_tank:", "    steps:",
              "      - 每次烘干后倒空冷凝水箱",
              "      - 水箱没装到位时机器不会启动，属保护设计",
              "    source: \"p8\"",
              "  clean_filter:", "    steps:",
              "      - 每次烘干后清理门内绒毛滤网（热泵机尤其重要，堵了烘干时间变长）",
              "      - 清理机身下部换热器区域的绒毛",
              "    source: \"p28\""]
    else:
        L += ["first_use:", "  items:",
              "    - 拆掉背面 4 颗运输固定螺栓（13 号扳手），装入孔塞（不拆就脱水会剧烈震动）",
              "    - 调平：17 号扳手松开防松螺母，转动支脚，用水平仪确认后锁紧",
              "    - 打开水龙头，检查进水管接口是否渗漏（只用手拧紧）",
              "  source: \"p7\"", "",
              "wash_steps:", "  items:",
              "    - 打开机门，抖散衣物放入，注意不要夹在门口",
              "    - 按衣物护理标签分类，检查口袋内异物",
              "    - 选定程序与温度、脱水转速",
              "    - 按启动/暂停键",
              "  source: \"p19\"", "",
              "maintenance:", "  clean_pump_filter:", "    steps:",
              "      - 先让机器排水，等筒内水冷却（避免烫伤）",
              "      - 打开排水泵维护盖，拉出排水软管放水",
              "      - 旋开泵盖清理异物，装回并拧紧",
              "    source: \"p28\"",
              "  clean_detergent_drawer:", "    steps:",
              "      - 拉出洗涤剂盒，取出投放盒清洗",
              "      - 清洁盒内腔与下方前封门处的水渍残留",
              "    source: \"p28\""]
    L += ["", "model_notes:"]
    if d["form"] == "dryer":
        L += ["  - key: heat_pump", "    text: 热泵干衣机靠低温循环除湿，烘干时间比冷凝式长但更省电、对衣物更温和",
              "  - key: no_water", "    text: 不需要接水管；用户问「要不要装水龙头」答「不用」"]
    else:
        L += ["  - key: unbalanced_spin", "    text: 机器检测到衣物不平衡会自动降低脱水转速，属保护行为，不是故障",
              "  - key: door_delay", "    text: 程序结束后机门需约 2 分钟才解锁，属门锁保护"]
        if d["form"] == "washer_dryer":
            L += ["  - key: wash_then_dry", "    text: 洗干一体是「先洗完、再烘干」，不能边洗边烘；干衣容量（7kg）小于洗涤容量（10kg），装满洗的衣物需分两次烘"]
    w(PROD / m / "usage.yaml", L)


def gen_faults(m, d):
    L = ["# 型号差异：只放本型号特有的东西",
         "# 跨型号通用的现象排查在 04_scenario/s4_troubleshoot/symptoms.yaml",
         f"product_id: siemens-{re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')}",
         f"updated: {UPDATED}",
         "",
         "codes:",
         "  has_code_display: true",
         "  display: 数字显示屏 + 故障信息（E 开头数字码）",
         "  rule: [关机后重新开机启动, 同一故障信息再次出现则报修]",
         "  known: []      # 说明书按「故障信息」描述现象与处理，未给完整逐码表",
         "  note: 说明书用「故障信息」段落描述处理方法，不做逐码释义；用户报具体代码时先按现象对应，无法对应则建单"]
    L += ["", "self_check:"]
    for sym, cause, act in SELF_CHECK_COMMON:
        L += [f"  - symptom: {q(sym)}",
              f"    cause: {q(cause)}",
              f"    action: {q(act)}",
              "    source: \"p30\""]
    L += ["", "normal_phenomena:"]
    for sid, sym, exp in NORMAL_COMMON:
        L += [f"  - id: {sid}",
              f"    symptom: {q(sym)}",
              f"    explain: {q(exp)}",
              "    source: \"p30\""]
    L += ["", "protection:"]
    if d["form"] == "dryer":
        L += ["  drain_hose_max_height_mm: {value: null, source: null,",
              "    note: 干衣机不强制接排水；接直排时软管不得高于排水口}"]
    else:
        L += ["  drain_hose_max_height_mm: {value: 1000, source: 'p8',",
              "    text: 排水软管末端高度 0~100cm}",
              "  water_pressure_min_mpa: {value: 0.1, source: 'p8',",
              "    text: 低于 0.1MPa（1bar）会报进水故障，需先解决水压再报修}"]
    L += ["", "model_specific_notes:"]
    if d["form"] == "dryer":
        L.append("  - 干衣机「烘不干」先查绒毛滤网与换热器是否堵塞，再查衣物是否超量，最后才怀疑机器")
        L.append("  - 干衣机水箱未装到位不会启动，用户常误以为坏了")
    else:
        L.append("  - 本机为上排水：排水超时先查排水软管高度（≤100cm）与是否浸入水中，不要先查地漏")
        L.append("  - 进水故障先查水压（≥0.1MPa）与进水阀过滤网，再怀疑进水阀")
    if d["form"] == "washer_dryer":
        L.append("  - 洗干一体「烘不干」先确认衣物量是否超过干衣容量 7kg（洗涤 10kg ≠ 干衣 10kg）")
    L += ["", "missing:",
          "  - 完整故障代码表（说明书写的是故障现象与处理，未给逐码释义）",
          "  - 各代码对应的具体故障部件"]
    w(PROD / m / "faults.yaml", L)


def main():
    made = 0
    for m, d in DATA.items():
        src_dir = ROOT / "01_source/washer/siemens" / m
        srcs = sorted(f.name for f in src_dir.iterdir() if f.is_file())
        gen_product(m, d, srcs)
        gen_specs(m, d)
        gen_features(m, d)
        gen_install(m, d)
        gen_usage(m, d)
        gen_faults(m, d)
        made += 1
        print(f"[card] siemens/{m}")
    print(f"\n完成 {made} 个型号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
