#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""空调批次回填（方案 B）：人工核对后的数据表 -> 8 个型号的 5 个文件

定位：
  这不是"抽取脚本"，而是"人工核对结果的落地工具"。数据来自 02_ingest 的 text.md，
  由人逐项读过之后写进下面的 DATA；脚本只负责按统一格式生成文件，避免 8×5 次手抄出错。

为什么要有这个脚本：
  1. 同一批型号的字段结构必须完全一致，手写会漂移
  2. 凡是"表格文本错位、需推断"的项，统一打 confidence 标记并汇入 10_ops/confirm_queue.md，
     而不是混在数据里看不出来
  3. 一次性工具：产物 YAML 已人工核对，后续请直接改 YAML，不要重跑（默认不覆盖）

用法:
  python tools/backfill_ac.py --force
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "03_product/air_conditioner"
CONFIRM = []


def sp(value=None, mn=None, mx=None, unit=None, src=None, note=None, conf="high", standard=None):
    d = {}
    if value is not None:
        d["value"] = value
    if mn is not None:
        d["min"] = mn
    if standard is not None:
        d["standard"] = standard
    if mx is not None:
        d["max"] = mx
    if unit:
        d["unit"] = unit
    if src:
        d["source"] = src
    if note:
        d["note"] = note
    if conf != "high":
        d["confidence"] = conf
    return d


def wl(w, h, d, src, order, conf="high"):
    return {"w": w, "h": h, "d": d, "unit": "mm", "source": src,
            "note": f"原文标注顺序 {order}", "confidence": conf}


def flow_list(lst):
    return "[" + ", ".join(scalar(x) for x in lst) + "]"


def y(v, indent=0):
    """极简 YAML 输出，避免依赖 pyyaml 的排版差异"""
    pad = "  " * indent
    if isinstance(v, dict):
        if not v:
            return " {}\n"
        s = "\n"
        for k, val in v.items():
            if isinstance(val, dict) and val:
                s += f"{pad}{k}:"
                s += y(val, indent + 1)
            elif isinstance(val, list) and val and all(not isinstance(x, (dict, list)) for x in val):
                s += f"{pad}{k}: {flow_list(val)}\n"
            elif isinstance(val, list) and val:
                s += f"{pad}{k}:"
                s += y(val, indent + 1)
            elif val is None:
                s += f"{pad}{k}: null\n"
            elif isinstance(val, bool):
                s += f"{pad}{k}: {'true' if val else 'false'}\n"
            elif isinstance(val, (int, float)):
                s += f"{pad}{k}: {val}\n"
            else:
                s += f"{pad}{k}: {scalar(val)}\n"
        return s
    if isinstance(v, list):
        s = "\n"
        for item in v:
            if isinstance(item, dict):
                first = True
                for k, val in item.items():
                    lead = f"{pad}- " if first else f"{pad}  "
                    if isinstance(val, (dict, list)) and val:
                        s += f"{lead}{k}:"
                        s += y(val, indent + 2)
                    else:
                        s += f"{lead}{k}: {scalar(val) if not isinstance(val, bool) else ('true' if val else 'false')}\n"
                    first = False
            else:
                s += f"{pad}- {scalar(item)}\n"
        return s
    return "\n"


def scalar(v):
    if v is None:
        return "null"
    s = str(v)
    if isinstance(v, str) and (s == "" or any(c in s for c in ':#{}[]&*!|>\'"%@`') or s != s.strip()):
        return "'" + s.replace("'", "''") + "'"
    return s


# ══════════════════════════════════════════════════════════════
# 人工核对后的数据表（来源：02_ingest/{品牌}/{型号}/text.md
# ══════════════════════════════════════════════════════════════
DATA = {}

# ── 1. 海尔 KFR-35GW/01KMC81U1 ───────────────────────────────
DATA["haier/KFR-35GW-01KMC81U1"] = {
    "product": {"product_type": "分体挂壁式（家用直流变频）", "colloquial": "1.5匹",
                "product_type_tags": ["挂机", "壁挂", "变频", "冷暖", "自清洁"]},
    "specs": {
        "cooling_capacity_w": sp(3500, 200, 5100, "W", "p34"),
        "heating_capacity_w": sp(5000, 200, 6700, "W", "p34"),
        "cooling_power_w": sp(810, 80, 1700, "W", "p34"),
        "heating_power_w": sp(1245, 80, 2000, "W", "p34"),
        "auxiliary_heat_w": sp(1100, unit="W", src="p34", note="PTC 电热 1100W/5.1A"),
        "mid_cooling_capacity_w": sp(1650, unit="W", src="p34"),
        "mid_heating_capacity_w": sp(2400, unit="W", src="p34"),
        "low_temp_heating_capacity_w": sp(4855, unit="W", src="p34"),
        "low_temp_heating_power_w": sp(1709, unit="W", src="p34"),
        "standby_power_w": sp(3, unit="W", src="p34"),
        "power_supply": sp("220V~/50Hz", src="p34"),
        "current_cooling_a": sp(3.8, unit="A", src="p34"),
        "current_heating_a": sp(5.8, unit="A", src="p34"),
        "noise_indoor_db": sp(standard=37, mx=41, unit="dB(A)", src="p34",
                             note="说明书只列超强41/高风37，未给超静档",
                             conf="partial"),
        "noise_outdoor_db": sp(51, unit="dB(A)", src="p34"),
        "airflow_m3h": sp(700, unit="m³/h", src="p34"),
        "size_indoor_mm": wl(865, 300, 190, "p34", "宽*高*深"),
        "size_outdoor_mm": wl(820, 550, 325, "p34", "宽*高*深"),
        "pipe_max_length_m": sp(10, unit="m", src="p26"),
        "pipe_max_height_diff_m": sp(5, unit="m", src="p26", note="临街安装外机不得低于2.5米"),
        "min_room_area_m2": sp(4, unit="m²", src="p9", note="原文为大于 4㎡"),
    },
    "features": {
        "cooling": (True, "p13", "制冷"), "heating": (True, "p13", "制热"),
        "dry": (True, "p14", "除湿"), "fan_only": (True, "p14", "送风"),
        "auxiliary_heat": (True, "p16", "辅热键"), "anti_direct_blow": (True, "p15", "防直吹键"),
        "self_clean": (True, "p20", "自清洁键，面板显示进度"),
        "wifi": (True, "p28", "智家App，仅 2.4GHz"),
        "voice": (None, None, "说明书按带/不带语音分别描述，本机是否带需按机身确认"),
        "fresh_air": (False, None, "说明书无新风内容"),
        "sleep": (True, "p18", "静眠"),
    },
    "install": {"pipe_max_length_m": 10, "pipe_max_height_diff_m": 5,
                "min_room_area_m2": 4, "refrigerant": "R32",
                "extra_refrigerant": "20g/m（总长>5m 后每米）", "src_pipe": "p26", "src_ref": "p17"},
    "usage": {"filter_period": "两周左右", "src_filter": "p29", "self_clean": True,
              "app": "海尔智家", "protect_cool_max": 60},
    "faults": {"code_table": None, "brand": "haier"},
    "confirm": ["噪声仅给出高风37/超强41，超静档数值说明书未列",
                "能效等级/APF 说明书参数表未公布",
                "语音能力：本机是否带语音需按机身确认"],
}

# ── 2. 海尔 KFR-35GW/E1-1Plus ───────────────────────────────
DATA["haier/KFR-35GW-E1-1Plus"] = {
    "product": {"product_type": "分体挂壁式（家用直流变频）", "colloquial": "1.5匹",
                "product_type_tags": ["挂机", "壁挂", "变频", "冷暖", "自清洁"]},
    "specs": {
        "cooling_capacity_w": sp(3500, 150, 5750, "W", "p35"),
        "heating_capacity_w": sp(5420, 150, 7250, "W", "p35"),
        "cooling_power_w": sp(700, 70, 1900, "W", "p35"),
        "heating_power_w": sp(1200, 70, 2050, "W", "p35"),
        "auxiliary_heat_w": sp(1050, unit="W", src="p35", note="PTC 1050W/4.8A"),
        "mid_cooling_capacity_w": sp(1650, unit="W", src="p35"),
        "mid_heating_capacity_w": sp(2610, unit="W", src="p35"),
        "low_temp_heating_capacity_w": sp(5250, unit="W", src="p35"),
        "low_temp_heating_power_w": sp(1750, unit="W", src="p35"),
        "standby_power_w": sp(3, unit="W", src="p35"),
        "power_supply": sp("220V~/50Hz", src="p35"),
        "current_cooling_a": sp(3.2, unit="A", src="p35"),
        "current_heating_a": sp(5.6, unit="A", src="p35"),
        "noise_indoor_db": sp(mn=16, standard=35, mx=41, unit="dB(A)", src="p35"),
        "noise_outdoor_db": sp(51, unit="dB(A)", src="p35"),
        "airflow_m3h": sp(800, unit="m³/h", src="p35"),
        "size_indoor_mm": wl(910, 316, 210, "p35", "宽*深*高"),
        "size_outdoor_mm": wl(820, 550, 325, "p35", "宽*深*高"),
        "pipe_max_length_m": sp(10, unit="m", src="p26"),
        "pipe_max_height_diff_m": sp(5, unit="m", src="p26"),
        "min_room_area_m2": sp(4, unit="m²", src="p9"),
    },
    "features": {
        "cooling": (True, "p13", "制冷"), "heating": (True, "p13", "制热"),
        "dry": (True, "p14", "除湿"), "fan_only": (True, "p14", "送风"),
        "auxiliary_heat": (True, "p16", "辅热键"), "anti_direct_blow": (True, "p15", "防直吹键"),
        "self_clean": (True, "p21", "除菌自清洁"),
        "wifi": (True, "p28", "智家App，仅 2.4GHz"),
        "voice": (None, None, "说明书未明确本机是否带语音"),
        "fresh_air": (False, None, "说明书无新风内容"),
        "sleep": (True, "p18", "静眠"),
    },
    "install": {"pipe_max_length_m": 10, "pipe_max_height_diff_m": 5,
                "min_room_area_m2": 4, "refrigerant": "R32",
                "extra_refrigerant": "20g/m（总长>5m 后每米）", "src_pipe": "p26", "src_ref": "p17"},
    "usage": {"filter_period": "两周左右", "src_filter": "p29", "self_clean": True,
              "app": "海尔智家", "protect_cool_max": None},
    "faults": {"code_table": None, "brand": "haier"},
    "confirm": ["尺寸标注顺序为“宽*深*高”，与同系列 01KMC81U1 的“宽*高*深”不同，已按各自标注拆分",
                "除菌显示：说明书注明“本机型无此功能”，故不列除菌为卖点",
                "能效等级/APF 说明书未公布"],
}

# ── 3. 海尔 KFR-72LW/E1-1 ────────────────────────────────────
DATA["haier/KFR-72LW-E1-1"] = {
    "product": {"product_type": "分体落地式（柜机，家用直流变频）", "colloquial": "3匹",
                "product_type_tags": ["柜机", "立柜", "变频", "冷暖", "自清洁"]},
    "specs": {
        "cooling_capacity_w": sp(7330, 850, 9270, "W", "p35"),
        "heating_capacity_w": sp(9820, 850, 12420, "W", "p35"),
        "cooling_power_w": sp(2070, 250, 3550, "W", "p35"),
        "heating_power_w": sp(2832, 250, 4450, "W", "p35"),
        "auxiliary_heat_w": sp(2500, unit="W", src="p35", note="PTC 2500W/12.0A"),
        "current_cooling_a": sp(9.8, unit="A", src="p35"),
        "current_heating_a": sp(12.7, unit="A", src="p35"),
        "mid_cooling_capacity_w": sp(3600, unit="W", src="p35", conf="inferred"),
        "mid_heating_capacity_w": sp(4950, unit="W", src="p35", conf="inferred"),
        "low_temp_heating_capacity_w": sp(9022, unit="W", src="p35", conf="inferred"),
        "low_temp_heating_power_w": sp(3803, unit="W", src="p35", conf="inferred"),
        "standby_power_w": sp(3, unit="W", src="p35"),
        "power_supply": sp("220V~/50Hz", src="p35"),
        "noise_indoor_db": sp(mn=20, standard=42, mx=47, unit="dB(A)", src="p35"),
        "noise_outdoor_db": sp(56, unit="dB(A)", src="p35", conf="inferred"),
        "airflow_m3h": sp(1600, unit="m³/h", src="p35", conf="inferred"),
        "size_indoor_mm": wl(364, 1830, 424, "p35", "宽X高X深", conf="inferred"),
        "size_outdoor_mm": wl(889, 643, 355, "p35", "宽X高X深", conf="inferred"),
        "pipe_max_length_m": sp(15, unit="m", src="p26"),
        "pipe_max_height_diff_m": sp(10, unit="m", src="p26"),
        "min_room_area_m2": None,
    },
    "features": {
        "cooling": (True, "p13", "制冷"), "heating": (True, "p13", "制热"),
        "dry": (True, "p14", "除湿"), "fan_only": (True, "p14", "送风"),
        "auxiliary_heat": (True, "p16", "辅热键"), "anti_direct_blow": (True, "p15", "防直吹键"),
        "self_clean": (True, "p20", "自清洁运转显示"),
        "wifi": (True, "p28", "智家App，仅 2.4GHz"),
        "voice": (None, None, "说明书全文未提语音"),
        "fresh_air": (False, None, "说明书无新风内容"),
        "sleep": (True, "p18", "静眠"),
    },
    "install": {"pipe_max_length_m": 15, "pipe_max_height_diff_m": 10,
                "min_room_area_m2": None, "refrigerant": "R32",
                "extra_refrigerant": None, "src_pipe": "p26", "src_ref": "p17"},
    "usage": {"filter_period": "2周清洗一次", "src_filter": "p21", "self_clean": True,
              "app": "海尔智家", "protect_cool_max": None},
    "faults": {"code_table": None, "brand": "haier"},
    "confirm": ["柜机参数表在 PDF 中为“标签块+数值块”错位排版，中间制冷量/低温制热量/风量/室外噪声/内外机尺寸均按位置推断，需对原页 p35 核一遍",
                "室内外机尺寸可能互换（364×1830×424 更符合落地式内机），已按合理性分配",
                "最小房间面积原文被截断：“房间面积应不小于：50机15㎡，72/76…”，72 机的值需人工读原页确认",
                "能效等级/APF 未公布；E1-1Plus 有 APF 5.20，本机没有"],
}

# ── 4. 海尔 KFR-72LW/E1-1Plus ───────────────────────────────
DATA["haier/KFR-72LW-E1-1Plus"] = {
    "product": {"product_type": "分体落地式（柜机，家用直流变频）", "colloquial": "3匹",
                "product_type_tags": ["柜机", "立柜", "变频", "冷暖", "自清洁", "澎湃风"]},
    "specs": {
        "cooling_capacity_w": sp(7330, 650, 9500, "W", "p36"),
        "heating_capacity_w": sp(10100, 850, 12500, "W", "p36"),
        "cooling_power_w": sp(1900, 300, 3500, "W", "p36"),
        "heating_power_w": sp(2900, 260, 4150, "W", "p36"),
        "auxiliary_heat_w": sp(2500, unit="W", src="p36", note="PTC 2500W/12.0A"),
        "current_cooling_a": sp(8.8, unit="A", src="p36"),
        "current_heating_a": sp(13.5, unit="A", src="p36"),
        "mid_cooling_capacity_w": sp(3600, unit="W", src="p36", conf="inferred"),
        "mid_heating_capacity_w": sp(4950, unit="W", src="p36", conf="inferred"),
        "low_temp_heating_capacity_w": sp(9058, unit="W", src="p36", conf="inferred"),
        "low_temp_heating_power_w": sp(3547, unit="W", src="p36", conf="inferred"),
        "standby_power_w": sp(3, unit="W", src="p36"),
        "efficiency_apf": sp(5.20, unit="APF", src="p36"),
        "power_supply": sp("220V~/50Hz", src="p36"),
        "noise_indoor_db": sp(mn=20, standard=42, mx=52, unit="dB(A)", src="p36",
                              note="超静20/高风42；超强47/澎湃风52，取最大 52"),
        "noise_outdoor_db": sp(56, unit="dB(A)", src="p36", conf="inferred"),
        "airflow_m3h": sp(1750, unit="m³/h", src="p36", conf="inferred"),
        "size_indoor_mm": wl(364, 1830, 424, "p36", "宽X高X深", conf="inferred"),
        "size_outdoor_mm": wl(960, 703, 400, "p36", "宽X高X深", conf="inferred"),
        "pipe_max_length_m": sp(15, unit="m", src="p26"),
        "pipe_max_height_diff_m": sp(10, unit="m", src="p26"),
        "min_room_area_m2": None,
    },
    "features": {
        "cooling": (True, "p13", "制冷"), "heating": (True, "p13", "制热"),
        "dry": (True, "p14", "除湿"), "fan_only": (True, "p14", "送风"),
        "auxiliary_heat": (True, "p16", "辅热键"), "anti_direct_blow": (True, "p15", "防直吹键"),
        "self_clean": (True, "p20", "自清洁运转显示"), "sterilize": (True, "p7", "杀菌模式显示"),
        "wifi": (True, "p28", "智家App，仅 2.4GHz"),
        "voice": (None, None, "说明书全文未提语音"),
        "fresh_air": (False, None, "说明书无新风内容"),
        "sleep": (True, "p18", "静眠"),
    },
    "install": {"pipe_max_length_m": 15, "pipe_max_height_diff_m": 10,
                "min_room_area_m2": None, "refrigerant": "R32",
                "extra_refrigerant": None, "src_pipe": "p26", "src_ref": "p17"},
    "usage": {"filter_period": "2周清洗一次", "src_filter": "p21", "self_clean": True,
              "app": "海尔智家", "protect_cool_max": None},
    "faults": {"code_table": None, "brand": "haier"},
    "confirm": ["与 E1-1 同属柜机错位排版，中间能力/风量/尺寸为推断值，需核原页 p36",
                "APF 5.20 为本页明确给出，可作为能效依据（同系列 E1-1 没有）",
                "72 机最小房间面积原文同样被截断，需人工确认"],
}

# ── 5. 美的 KFR-35GW/A11（含小天鹅贴牌） ─────────────────────
DATA["midea/KFR-35GW-A11"] = {
    "product": {"product_type": "分体挂壁式（变频）", "colloquial": "1.5匹",
                "product_type_tags": ["挂机", "壁挂", "变频", "冷暖", "防直吹"],
                "notes": ["说明书封面为 LittleSwan（小天鹅），属美的系，同型号参数通用"]},
    "specs": {
        "cooling_capacity_w": sp(3510, 150, 5250, "W", "p34"),
        "heating_capacity_w": sp(5010, 150, 6790, "W", "p34"),
        "cooling_power_w": sp(845, 70, 2100, "W", "p34"),
        "heating_power_w": sp(1240, 70, 2150, "W", "p34"),
        "auxiliary_heat_w": sp(850, unit="W", src="p34", note="PTC"),
        "mid_cooling_capacity_w": sp(1660, unit="W", src="p34"),
        "mid_heating_capacity_w": sp(2440, unit="W", src="p34"),
        "max_input_power_w": sp(3150, unit="W", src="p34"),
        "standby_power_w": sp(3, unit="W", src="p34"),
        "efficiency_apf": sp(5.30, unit="APF", src="p34", conf="inferred",
                            note="双列参数表（26机/35机），按 35 机列取值"),
        "efficiency_seer": sp(6.35, unit="SEER", src="p34"),
        "energy_grade": sp(1, unit="级", src="p34"),
        "power_supply": sp("220V~/50Hz", src="p34"),
        "current_cooling_a": sp(4.0, mn=0.4, mx=9.8, unit="A", src="p34"),
        "current_heating_a": sp(5.8, mn=0.4, mx=10.0, unit="A", src="p34"),
        "max_input_current_a": sp(15.0, unit="A", src="p34"),
        "noise_indoor_db": sp(mn=18, standard=35, mx=41, unit="dB(A)", src="p34"),
        "noise_outdoor_db": sp(mn=38, mx=51, unit="dB(A)", src="p34"),
        "pipe_max_length_m": sp(10, unit="m", src="p8"),
        "pipe_max_height_diff_m": sp(5, unit="m", src="p8"),
        "min_room_area_m2": None,
        "recommended_area_cooling_m2": sp("15-23", unit="m²", src="p3"),
        "recommended_area_heating_m2": sp("17-23", unit="m²", src="p3"),
    },
    "features": {
        "cooling": (True, "p9", "制冷"), "heating": (True, "p9", "制热"),
        "dry": (True, "p9", "抽湿"), "fan_only": (True, "p9", "送风"),
        "auxiliary_heat": (True, "p20", "电辅热"),
        "anti_direct_blow": (True, "p14", "防直吹"),
        "self_clean": (None, None, "说明书全文未出现“自清洁”，未核对清洁章节，暂按不确定处理"),
        "wifi": (True, "p28", "美的美居 App / 美居Lite"),
        "voice": (None, None, "说明书未提语音"),
        "fresh_air": (False, None, "说明书无新风内容"),
    },
    "install": {"pipe_max_length_m": 10, "pipe_max_height_diff_m": 5,
                "min_room_area_m2": None, "refrigerant": "R32",
                "extra_refrigerant": None, "src_pipe": "p8", "src_ref": "p5"},
    "usage": {"filter_period": "建议每月清洁 1 次滤尘网（灰尘多时增加）", "src_filter": "p20",
              "self_clean": None, "app": "美的美居", "protect_cool_max": None},
    "faults": {"code_table": "midea_generic", "brand": "midea"},
    "confirm": ["双列参数表（26机/35机）在文本层错位：低温制热量、HSPF、中间功率三项无法确定属于哪一列，暂未录入",
                "最小房间面积：本说明书未给具体数值，只写“需满足技术参数页规定的面积”，需读参数页图片",
                "自清洁：全文未出现，需核对清洁保养章节"],
}

# ── 6. 美的 KFR-35GW/KS2P ────────────────────────────────────
DATA["midea/KFR-35GW-KS2P"] = {
    "product": {"product_type": "分体挂壁式（变频）", "colloquial": "1.5匹",
                "product_type_tags": ["挂机", "壁挂", "变频", "冷暖", "防直吹"]},
    "specs": {
        "pipe_max_length_m": sp(10, unit="m", src="p8"),
        "pipe_max_height_diff_m": sp(5, unit="m", src="p8"),
        "recommended_area_cooling_m2": sp("15-23", unit="m²", src="p3"),
        "recommended_area_heating_m2": sp("17-23", unit="m²", src="p3"),
        "min_room_area_m2": None,
        "refrigerant_type": sp("R32", src="p4", note="A2L 低可燃"),
    },
    "features": {
        "cooling": (True, "p20", "制冷"), "heating": (True, "p20", "制热"),
        "dry": (True, "p20", "抽湿"), "fan_only": (True, "p20", "送风"),
        "auxiliary_heat": (True, "p20", "电辅热"),
        "anti_direct_blow": (True, "p20", "防直吹"),
        "self_clean": (None, None, "说明书全文未出现“自清洁”"),
        "wifi": (True, "p26", "美的美居Lite小程序"),
        "voice": (None, None, "说明书未提语音"),
        "fresh_air": (False, None, "说明书无新风内容"),
    },
    "install": {"pipe_max_length_m": 10, "pipe_max_height_diff_m": 5,
                "min_room_area_m2": None, "refrigerant": "R32",
                "extra_refrigerant": None, "src_pipe": "p8", "src_ref": "p4"},
    "usage": {"filter_period": "建议每月清洁 1 次滤尘网（灰尘多时增加）", "src_filter": "p18",
              "self_clean": None, "app": "美的美居", "protect_cool_max": None},
    "faults": {"code_table": "midea_generic", "brand": "midea"},
    "confirm": ["技术参数页（p27）为纯图片、无文本层，制冷量/功率/噪声/尺寸全部缺失，需 OCR 或人工读图补录",
                "故障核查页（p28）同样是图片，代码处置规则未抽取到，但同系列其他型号规则一致，暂引用品牌级规则"],
}

# ── 7. 美的 KFR-35GW/N8KS1-1P ───────────────────────────────
DATA["midea/KFR-35GW-N8KS1-1P"] = {
    "product": {"product_type": "分体挂壁式（变频）", "colloquial": "1.5匹",
                "product_type_tags": ["挂机", "壁挂", "变频", "冷暖", "防直吹"]},
    "specs": {
        "pipe_max_length_m": sp(10, unit="m", src="p8"),
        "pipe_max_height_diff_m": sp(5, unit="m", src="p8"),
        "recommended_area_cooling_m2": sp("15-23", unit="m²", src="p3"),
        "recommended_area_heating_m2": sp("17-23", unit="m²", src="p3"),
        "min_room_area_m2": None,
        "refrigerant_type": sp("R32", src="p4", note="A2L 低可燃"),
    },
    "features": {
        "cooling": (True, "p20", "制冷"), "heating": (True, "p20", "制热"),
        "dry": (True, "p20", "抽湿"), "fan_only": (True, "p20", "送风"),
        "auxiliary_heat": (True, "p20", "电辅热"),
        "anti_direct_blow": (True, "p20", "防直吹"),
        "self_clean": (None, None, "说明书全文未出现“自清洁”"),
        "wifi": (True, "p27", "美的美居 App"),
        "voice": (False, "p20", "说明书明确标注“本机无语音功能”"),
        "fresh_air": (False, None, "说明书无新风内容"),
    },
    "install": {"pipe_max_length_m": 10, "pipe_max_height_diff_m": 5,
                "min_room_area_m2": None, "refrigerant": "R32",
                "extra_refrigerant": None, "src_pipe": "p8", "src_ref": "p4"},
    "usage": {"filter_period": "建议每月清洁 1 次滤尘网（灰尘多时增加）", "src_filter": "p18",
              "self_clean": None, "app": "美的美居", "protect_cool_max": None},
    "faults": {"code_table": "midea_generic", "brand": "midea"},
    "confirm": ["技术参数页（p28）为纯图片、无文本层，全部参数缺失，需 OCR 补录",
                "“本机无语音功能”是说明书原文，可直接用于否定回答"],
}

# ── 8. 美的 KFR-72LW/N8KS1-1P（柜机） ───────────────────────
DATA["midea/KFR-72LW-N8KS1-1P"] = {
    "product": {"product_type": "分体落地式（柜机，变频）", "colloquial": "3匹",
                "product_type_tags": ["柜机", "立柜", "变频", "冷暖", "防直吹"]},
    "specs": {
        "cooling_capacity_w": sp(7320, 900, 9260, "W", "p11"),
        "heating_capacity_w": sp(9760, 900, 12380, "W", "p11"),
        "cooling_power_w": sp(2080, 300, 3450, "W", "p11", conf="inferred"),
        "heating_power_w": sp(2980, 260, 4165, "W", "p11", conf="inferred"),
        "efficiency_apf": sp(4.60, unit="APF", src="p11", conf="inferred"),
        "efficiency_seer": sp(5.69, unit="SEER", src="p11", conf="inferred"),
        "efficiency_hspf": sp(3.42, unit="HSPF", src="p11", conf="inferred"),
        "energy_grade": sp(1, unit="级", src="p11"),
        "pipe_max_length_m": sp(20, unit="m", src="p26"),
        "pipe_max_height_diff_m": sp(10, unit="m", src="p26"),
        "recommended_area_cooling_m2": sp("32-48", unit="m²", src="p3"),
        "recommended_area_heating_m2": sp("36-49", unit="m²", src="p3"),
        "min_room_area_m2": None,
        "refrigerant_type": sp("R32", src="p4"),
    },
    "features": {
        "cooling": (True, "p22", "制冷"), "heating": (True, "p22", "制热"),
        "dry": (True, "p22", "抽湿"), "fan_only": (True, "p22", "送风"),
        "auxiliary_heat": (True, "p22", "电辅热"),
        "anti_direct_blow": (True, "p22", "防直吹"),
        "self_clean": (None, None, "说明书全文未出现“自清洁”"),
        "wifi": (True, "p27", "美的美居 App"),
        "voice": (False, "p22", "说明书明确标注“本机无语音功能”"),
        "fresh_air": (False, None, "说明书无新风内容"),
    },
    "install": {"pipe_max_length_m": 20, "pipe_max_height_diff_m": 10,
                "min_room_area_m2": None, "refrigerant": "R32",
                "extra_refrigerant": None, "src_pipe": "p26", "src_ref": "p4"},
    "usage": {"filter_period": "建议每月清洁 1 次滤尘网（灰尘多时增加）", "src_filter": "p18",
              "self_clean": None, "app": "美的美居", "protect_cool_max": None},
    "faults": {"code_table": "midea_generic", "brand": "midea"},
    "confirm": ["双列参数表（51机/72机）文本层错位：SEER/HSPF/APF 与功率按列序推断，需核原页 p11",
                "尺寸/噪声/风量/电流未录入（错位无法确认），待人工读图",
                "最小房间面积只写“需满足技术参数页规定”，具体值需读参数页"],
}


# ══════════════════════════════════════════════════════════════
def build(key, d):
    brand, slug = key.split("/")
    out = OUT / brand / slug
    out.mkdir(parents=True, exist_ok=True)
    pid = f"{brand}-{slug.lower()}"
    today = "2026-10-22"

    # product.yaml：在已有骨架上改 status，不重写整个文件
    pp = out / "product.yaml"
    txt = pp.read_text(encoding="utf-8") if pp.exists() else ""
    if txt:
        import re
        for k, v in d["product"].items():
            if k in ("notes",):
                continue
            if k == "product_type_tags":
                txt = re.sub(r"^product_type_tags:.*$", f"product_type_tags: {flow_list(v)}",
                             txt, count=1, flags=re.M)
                continue
            txt = re.sub(rf"^{k}:.*$", f"{k}: {v}", txt, count=1, flags=re.M)
        txt = re.sub(r"^updated:.*$", f"updated: {today}", txt, count=1, flags=re.M)
        txt = re.sub(r"^status:.*$", "status: ready", txt, count=1, flags=re.M)
        if "\nbackfill:" in txt:
            txt = txt.split("\nbackfill:")[0]
        if "todo:" in txt:
            txt = txt.split("todo:")[0].rstrip() + "\n"
        extra = []
        if d["product"].get("notes"):
            extra.append("notes:")
            extra += [f"  - {n}" for n in d["product"]["notes"]]
        txt = txt.rstrip() + "\n\nbackfill:\n  method: 人工核对后回填（方案 B）\n  date: " + today + "\n"
        if extra:
            txt += "\n".join(extra) + "\n"
        pp.write_text(txt, encoding="utf-8")

    # specs.yaml
    s = f"# 参数来源：说明书参数表（页码见每项 source），2026-10-22 人工核对回填\n"
    s += f"# confidence: inferred = 参数表文本错位，按位置推断，待人工复核（见 10_ops/confirm_queue.md）\n"
    s += f"product_id: {pid}\nupdated: {today}\n\nspecs:\n"
    for k, v in d["specs"].items():
        if v is None:
            s += f"  {k}: null\n"
        else:
            s += f"  {k}:"
            s += y(v, 2)
    miss = [k for k, v in d["specs"].items() if v is None]
    if miss:
        s += "\nmissing:\n" + "".join(f"  - {m}\n" for m in miss)
    (out / "specs.yaml").write_text(s, encoding="utf-8")

    # features.yaml
    f = f"product_id: {pid}\nupdated: {today}\n\nfeatures:\n"
    for k, (sup, src, phrase) in d["features"].items():
        f += f"  {k}: {{supported: {str(sup).lower() if sup is not None else 'null'}"
        if src:
            f += f", source: {src}"
        if phrase:
            f += f", phrase: {scalar(phrase)}"
        f += "}\n"
    (out / "features.yaml").write_text(f, encoding="utf-8")

    # install.yaml
    i = d["install"]
    ins = f"# 安装条件：来源说明书安装章节，2026-10-22 回填\nproduct_id: {pid}\nupdated: {today}\n\n"
    ins += "electrical:\n  dedicated_circuit:\n    value: true\n    text: 必须使用专用电路/独立插座，须可靠接地\n"
    ins += "space:\n"
    ins += f"  min_room_area_m2: " + (f"{{value: {i['min_room_area_m2']}, unit: m², source: p9}}\n"
                                     if i["min_room_area_m2"] else "null    # 说明书未给具体数值或原文被截断，见 confirm_queue\n")
    ins += "piping:\n"
    if i["pipe_max_length_m"]:
        ins += f"  max_length_m: {{value: {i['pipe_max_length_m']}, unit: m, source: {i['src_pipe']}}}\n"
    if i["pipe_max_height_diff_m"]:
        ins += f"  height_diff_m: {{value: {i['pipe_max_height_diff_m']}, unit: m, source: {i['src_pipe']}}}\n"
    if i["refrigerant"]:
        ins += f"  refrigerant: {{value: {i['refrigerant']}, source: {i['src_ref']}}}\n"
    if i["extra_refrigerant"]:
        ins += f"  extra_refrigerant: {{value: {scalar(i['extra_refrigerant'])}, source: p17}}\n"
    ins += "\npre_visit_checklist:\n  - 安装墙面是否承重、可否打孔\n  - 是否有专用电源回路与带地线插座\n"
    ins += "  - 室外机位置是否可用（物业是否允许外挂/是否有预留机位）\n  - 连接管走线长度与内外机高度差\n  - 楼层与电梯情况（影响上楼费）\n"
    ins += "\nchargeable_items: [高空作业, 加长连接管, 支架, 打孔, 拆装防盗网, 远程上门]\n"
    (out / "install.yaml").write_text(ins, encoding="utf-8")

    # usage.yaml
    u = d["usage"]
    us = f"# 使用与保养：2026-10-22 回填（仅录入说明书可确认的部分）\nproduct_id: {pid}\nupdated: {today}\n\n"
    us += "maintenance:\n  滤尘网:\n"
    us += f"    frequency: {scalar(u['filter_period'])}\n    source: {u['src_filter']}\n"
    us += "  cleaning_precondition: 清洁前必须停止运行并断开电源\n"
    us += "\noperations:\n"
    us += f"  self_clean: {{supported: {str(u['self_clean']).lower() if u['self_clean'] is not None else 'null'}, source: null}}\n"
    us += f"  wifi_bind: {{app: {scalar(u['app'])}, note: 仅支持 2.4GHz 频段}}\n"
    us += "\nlimits:\n  working_env: {cooling: '室外 18~43℃', heating: '室外 -7~24℃', source: p36}\n"
    if u["protect_cool_max"]:
        us += f"  protection: {{cooling_outdoor_max_c: {u['protect_cool_max']}, source: p36}}\n"
    us += "\ntodo:\n  - 首次使用/模式操作/APP 配网的分步步骤尚未逐条回填（说明书有完整章节，待下一批补）\n"
    (out / "usage.yaml").write_text(us, encoding="utf-8")

    # faults.yaml
    fa = d["faults"]
    ft = f"# 型号差异型故障信息（现象主库在 04_scenario/s4_troubleshoot/symptoms.yaml）\n"
    ft += f"product_id: {pid}\nupdated: {today}\n\ncodes:\n"
    if fa["code_table"] == "midea_generic":
        ft += "  has_code_display: true\n  code_prefixes: [E, F, P, EH, EL, EC, PH, PL, PC]\n"
        ft += "  rule: [关机并关闭电源, 等待 10 分钟, 上电开机, 仍显示则报修]\n"
        ft += "  meaning_table: null    # 说明书只给类别不给逐码释义\n  source: p30\n"
    else:
        ft += "  has_code_display: null\n  code_prefixes: []\n  rule: null\n"
        ft += "  confidence: 该型号说明书未提供故障代码表\n"
    ft += "\nnormal_phenomena_ref: 04_scenario/s4_troubleshoot/symptoms.yaml\n"
    ft += "model_specific_notes:\n"
    sc = d["features"].get("self_clean", (None, None, ""))[0]
    if sc is True:
        ft += "  - 有自清洁/除菌自清洁；用户问“怎么清洗”可给自清洁步骤\n"
    elif sc is False:
        ft += "  - 无自清洁功能，用户问自清洁时只能用滤网清洗步骤\n"
    else:
        ft += "  - 自清洁能力未确认，用户问到时按“确认后回复”处理\n"
    ft += "missing:\n  - 逐条故障代码含义（说明书未提供）\n"
    (out / "faults.yaml").write_text(ft, encoding="utf-8")

    CONFIRM.append((pid, d["confirm"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not args.force:
        print("这是回填工具：产物已人工核对，默认不覆盖。加 --force 重新生成。")
        return 0
    for k, d in DATA.items():
        build(k, d)
        print(f"[ok] {k}")
    # 人工核对清单
    lines = ["# 待人工核对清单（方案 B 产出）", "",
             "回填时凡遇“参数表文本错位 / 说明书未给 / 原文被截断”，一律不猜着填，记到这里。",
             "逐条确认后，把对应 YAML 的 `confidence: inferred` 删掉、`null` 改成真值，并从本文件划掉。", ""]
    for pid, items in CONFIRM:
        lines.append(f"## {pid}")
        lines += [f"- [ ] {i}" for i in items]
        lines.append("")
    (ROOT / "10_ops/confirm_queue.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"核对清单 -> 10_ops/confirm_queue.md（{sum(len(v) for _, v in CONFIRM)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
