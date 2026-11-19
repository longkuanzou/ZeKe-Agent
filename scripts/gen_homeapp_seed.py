"""生成 ``homeapp`` 行业包的业务种子数据（30 单 / 12 商品 / 全链路场景）。

## 为什么用脚本而不是手写 YAML

1. **场景依赖相对时间**：「7 天内可退」「超 48h 未发货」「物流停滞 5 天」这些判定，
   写死绝对日期后，过两周全部失效。脚本按**运行时刻**反推。
2. **金额必须能对账**：订单金额 = 商品价，退款 ≤ 订单金额，促销价用于价保。
   同一份价格表生成，避免手抄出错。
3. **数据结构有约束**：退款只能挂在已签收订单、工单必须属于订单的用户、
   物流只能挂在已发货订单。生成时自检，错了直接抛异常。

## 商品价格是模拟值

``appliance-kb`` 只有参数、**没有价格**。本脚本的 ``price_cents`` / 库存 / 促销
全部为演示用模拟值；容量、能效取自 KB 的真实参数。尺寸 KB 多数缺失，同样为模拟值。

## 用法

```bash
python -m scripts.gen_homeapp_seed          # 写入 apps/pack_templates/homeapp/seed_data/
python -m apps.build_kb_pack --kb appliance-kb --pack homeapp   # 打包时复制进行业包
zkagent --industry homeapp db seed          # 灌入 data/business-homeapp.db
```

生成物会随打包被复制；**重跑打包不会覆盖掉它们**（模板是事实来源）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import yaml

__all__ = ["main", "generate", "TEMPLATE_SEED_DIR"]

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_SEED_DIR = REPO_ROOT / "apps" / "pack_templates" / "homeapp" / "seed_data"

_FMT = "%Y-%m-%dT%H:%M:%S"
_NOW = datetime.now().replace(microsecond=0)


def _macro(sign: str, **kwargs: float) -> str:
    """输出时间宏（M16）：灌库时才解析，隔几周重跑也不会让 7 天窗口漂移。"""
    if "days" in kwargs:
        return f"@now{sign}{int(kwargs['days'])}d"
    if "hours" in kwargs:
        return f"@now{sign}{int(kwargs['hours'])}h"
    raise ValueError(f"时间宏只支持 days / hours：{kwargs}")


def _ago(**kwargs: float) -> str:
    return _macro("-", **kwargs)


def _later(**kwargs: float) -> str:
    return _macro("+", **kwargs)


# --------------------------------------------------------------------- 主体

#: ``phone_masked`` 的后四位就是 ``verify_user`` 的校验码（见 mock 后端实现）。
USERS = [
    {"user_id": "H1", "name": "王芳", "phone_masked": "138****2046", "level": "gold",
     "created_at": _ago(days=210)},
    {"user_id": "H2", "name": "陈磊", "phone_masked": "139****7712", "level": "silver",
     "created_at": _ago(days=120)},
    {"user_id": "H3", "name": "周敏", "phone_masked": "137****3388", "level": "standard",
     "created_at": _ago(days=3)},
    {"user_id": "H4", "name": "李强", "phone_masked": "136****9051", "level": "gold",
     "created_at": _ago(days=400)},
]

#: 房屋条件：安装适配的真实判据（门宽 / 电梯 / 水电位 / 外机位）。
HOMES = [
    {"home_id": "HM1", "user_id": "H1", "city": "杭州", "district": "西湖区", "floor": 6,
     "has_elevator": 0, "door_width_cm": 68, "water_inlet": 1, "drain": 1,
     "outdoor_slot": "有", "power_spec": "16A 独立回路", "is_default": 1,
     "updated_at": _ago(days=30)},
    {"home_id": "HM2", "user_id": "H2", "city": "上海", "district": "浦东新区", "floor": 18,
     "has_elevator": 1, "door_width_cm": 75, "water_inlet": 1, "drain": 1,
     "outdoor_slot": "无（需物业协调）", "power_spec": "10A", "is_default": 1,
     "updated_at": _ago(days=20)},
    # H3 是新客，房屋条件两项不满足：用于「能不能装」的售前判断
    {"home_id": "HM3", "user_id": "H3", "city": "南京", "district": "鼓楼区", "floor": 3,
     "has_elevator": 0, "door_width_cm": 60, "water_inlet": 0, "drain": 0,
     "outdoor_slot": "无", "power_spec": "10A", "is_default": 1,
     "updated_at": _ago(days=3)},
    # H4 有 15 单却没有房屋记录，与「下单必有收货地址」矛盾（地址见 _ADDRESS）；
    # 补一条与其收货地址一致的房屋：成都 3 楼、无电梯（安装费场景要用）。
    {"home_id": "HM4", "user_id": "H4", "city": "成都", "district": "武侯区", "floor": 3,
     "has_elevator": 0, "door_width_cm": 72, "water_inlet": 1, "drain": 1,
     "outdoor_slot": "有", "power_spec": "10A", "is_default": 1,
     "updated_at": _ago(days=45)},
]

#: 12 个 SKU，全部取自 appliance-kb 的 28 个真实型号；
#: 容量 / 能效为 KB 真实值，价格 / 尺寸为模拟值。
PRODUCTS = [
    {"sku": "HA-KFR35-E1", "category": "aircon", "model": "KFR-35GW/E1-1",
     "name": "海尔 1.5 匹变频挂机 KFR-35GW/E1-1", "price_cents": 289900,
     "capacity": "1.5匹 / 3500W", "energy_grade": "1级", "dimensions_mm": "900×200×300",
     "features": ["变频", "自清洁", "新一级能效", "静音", "电辅热", "APP控制"],
     "install_notes": "需 16A 独立回路；外机位留 30cm 散热间距", "status": "on_sale"},
    {"sku": "MD-KFR35-Q2", "category": "aircon", "model": "KFR-35GW/Q2",
     "name": "美的 1.5 匹变频挂机 KFR-35GW/Q2", "price_cents": 329900,
     "capacity": "1.5匹 / 3510W", "energy_grade": "1级", "dimensions_mm": "880×195×295",
     "features": ["变频", "自清洁", "新一级能效", "无风感", "电辅热"],
     "install_notes": "需 16A 独立回路；外机位留 30cm 散热间距", "status": "on_sale"},
    {"sku": "HA-KFR72-E1", "category": "aircon", "model": "KFR-72LW/E1-1",
     "name": "海尔 3 匹变频柜机 KFR-72LW/E1-1", "price_cents": 699900,
     "capacity": "3匹 / 7330W", "energy_grade": "1级", "dimensions_mm": "400×1850×380",
     "features": ["变频", "自清洁", "新一级能效", "电辅热", "强力制冷"],
     "install_notes": "需 16A 独立回路；柜机需预留 40cm 检修空间", "status": "on_sale"},
    {"sku": "MD-KFR72-N8", "category": "aircon", "model": "KFR-72LW/N8KS1-1P",
     "name": "美的 3 匹变频柜机 KFR-72LW/N8KS1-1P", "price_cents": 759900,
     "capacity": "3匹 / 7320W", "energy_grade": "1级", "dimensions_mm": "420×1860×390",
     "features": ["变频", "自清洁", "新一级能效", "无风感", "电辅热"],
     "install_notes": "需 16A 独立回路；柜机需预留 40cm 检修空间", "status": "on_sale"},
    {"sku": "LS-TB100V26", "category": "washer", "model": "TB100V26DT",
     "name": "小天鹅 10 公斤波轮洗衣机 TB100V26DT", "price_cents": 189900,
     "capacity": "10kg", "energy_grade": "1级", "dimensions_mm": "580×590×940",
     "features": ["波轮", "除菌洗", "桶自洁", "10kg大容量"],
     "install_notes": "需进水与地漏；门宽 ≥62cm", "status": "on_sale"},
    {"sku": "LS-TG100V618", "category": "washer", "model": "TG100V618PLUS",
     "name": "小天鹅 10 公斤滚筒洗衣机 TG100V618PLUS", "price_cents": 279900,
     "capacity": "10kg", "energy_grade": "1级", "dimensions_mm": "595×600×850",
     "features": ["滚筒", "除菌洗", "筒自洁", "变频", "智能投放"],
     "install_notes": "需进水与地漏；门宽 ≥62cm；预留 5cm 散热", "status": "on_sale"},
    {"sku": "SI-WM12P2602", "category": "washer", "model": "WM12P2602W",
     "name": "西门子 10 公斤滚筒洗衣机 WM12P2602W", "price_cents": 399900,
     "capacity": "10kg", "energy_grade": "1级", "dimensions_mm": "598×590×848",
     "features": ["滚筒", "除菌洗", "变频", "15分钟快洗"],
     "install_notes": "已下架机型；备件供应至 2029 年", "status": "off_shelf"},
    {"sku": "SI-WN54A1X42", "category": "washer", "model": "WN54A1X42W",
     "name": "西门子 10 公斤洗烘一体机 WN54A1X42W", "price_cents": 529900,
     "capacity": "10kg（烘干 6kg）", "energy_grade": "1级", "dimensions_mm": "598×590×848",
     "features": ["滚筒", "洗烘一体", "烘干", "除菌洗", "智能投放"],
     "install_notes": "需进水与地漏；门宽 ≥62cm；烘干需独立排水", "status": "on_sale"},
    {"sku": "MD-MB40HB9T", "category": "rice_cooker", "model": "MB-40HB9T",
     "name": "美的 4L IH 电饭煲 MB-40HB9T", "price_cents": 59900,
     "capacity": "4L", "energy_grade": "1级", "dimensions_mm": "350×280×220",
     "features": ["IH加热", "24小时预约", "可拆内盖", "一级能效"],
     "install_notes": "免安装，开箱即用；需 10A 插座", "status": "on_sale"},
    {"sku": "MD-MBRE576S", "category": "rice_cooker", "model": "MB-RE576S",
     "name": "美的 5L IH 电饭煲 MB-RE576S", "price_cents": 89900,
     "capacity": "5L", "energy_grade": "1级", "dimensions_mm": "380×300×240",
     "features": ["IH加热", "5L大容量", "24小时预约", "可拆内盖"],
     "install_notes": "免安装，开箱即用；需 10A 插座", "status": "on_sale"},
    {"sku": "SP-F40H8093S", "category": "rice_cooker", "model": "F40H8093S",
     "name": "苏泊尔 4L 电饭煲 F40H8093S", "price_cents": 45900,
     "capacity": "4L", "energy_grade": "2级", "dimensions_mm": "340×270×215",
     "features": ["底盘加热", "24小时预约", "可拆内盖"],
     "install_notes": "免安装，开箱即用；需 10A 插座", "status": "on_sale"},
    {"sku": "SP-F50X96", "category": "rice_cooker", "model": "F50X96",
     "name": "苏泊尔 5L IH 电饭煲 F50X96", "price_cents": 79900,
     "capacity": "5L", "energy_grade": "2级", "dimensions_mm": "370×295×235",
     "features": ["IH加热", "5L大容量", "24小时预约"],
     "install_notes": "免安装，开箱即用；需 10A 插座", "status": "on_sale"},
]

_PRICE = {item["sku"]: item["price_cents"] for item in PRODUCTS}
_NAME = {item["sku"]: item["name"] for item in PRODUCTS}
_CATEGORY = {item["sku"]: item["category"] for item in PRODUCTS}

#: 12 个 SKU 的库存：2 个缺货（用于「有货吗/几天到」）。
INVENTORY = [
    {"sku": item["sku"], "warehouse": "华东仓", "qty": 12, "restock_eta": None,
     "updated_at": _ago(hours=6)}
    for item in PRODUCTS
]
_ZERO_STOCK = {
    "SI-WN54A1X42": _later(days=7),   # 洗烘一体缺货，7 天后到
    "MD-KFR72-N8": _later(days=3),    # 3 匹柜机缺货，3 天后到
}
INVENTORY = [
    {**row, "qty": 0, "restock_eta": _ZERO_STOCK[row["sku"]]}
    if row["sku"] in _ZERO_STOCK else row
    for row in INVENTORY
]

#: 5 条促销：直降 / 满减 / 赠品 / 以旧换新 / 分期。
PROMOTIONS = [
    {"promo_id": "PM2601", "sku": "MD-KFR35-Q2", "category": "aircon", "kind": "discount",
     "amount_cents": 20000, "threshold_cents": 0, "gift": "",
     "start_at": _ago(days=3), "end_at": _later(days=4), "stackable": 0,
     "note": "限时直降 200 元（价保场景的依据）"},
    {"promo_id": "PM2602", "sku": None, "category": "washer", "kind": "threshold",
     "amount_cents": 15000, "threshold_cents": 250000, "gift": "",
     "start_at": _ago(days=5), "end_at": _later(days=2), "stackable": 0,
     "note": "洗衣机满 2500 减 150"},
    {"promo_id": "PM2603", "sku": "LS-TG100V618", "category": "washer", "kind": "gift",
     "amount_cents": 0, "threshold_cents": 0, "gift": "洗衣液套装 + 延保 1 年",
     "start_at": _ago(days=10), "end_at": _later(days=10), "stackable": 1,
     "note": "赠品与延保同时给"},
    {"promo_id": "PM2604", "sku": None, "category": "rice_cooker", "kind": "trade_in",
     "amount_cents": 5000, "threshold_cents": 0, "gift": "",
     "start_at": _ago(days=15), "end_at": _later(days=15), "stackable": 0,
     "note": "电饭煲以旧换新补贴 50 元"},
    {"promo_id": "PM2605", "sku": "SI-WN54A1X42", "category": "washer", "kind": "installment",
     "amount_cents": 0, "threshold_cents": 0, "gift": "",
     "start_at": _ago(days=20), "end_at": _later(days=20), "stackable": 0,
     "note": "12 期免息"},
]

# 订单：(order_id, user, sku, status, 下单于多少小时前, 签收于多少天前)
_ORDER_ROWS = [
    ("H2601", "H1", "HA-KFR35-E1", "paid", 2, None),
    ("H2602", "H2", "LS-TG100V618", "paid", 120, None),
    ("H2603", "H4", "MD-MB40HB9T", "paid", 26, None),
    ("H2604", "H4", "HA-KFR72-E1", "paid", 72, None),
    ("H2605", "H2", "SI-WN54A1X42", "paid", 6, None),
    ("H2606", "H4", "LS-TB100V26", "paid", 48, None),
    ("H2607", "H4", "MD-MBRE576S", "paid", 8, None),
    ("H2608", "H4", "MD-KFR72-N8", "paid", 12, None),
    ("H2609", "H1", "HA-KFR35-E1", "shipped", 60, None),
    ("H2610", "H2", "HA-KFR72-E1", "shipped", 96, None),
    ("H2611", "H2", "MD-KFR35-Q2", "shipped", 144, None),
    ("H2612", "H4", "MD-MB40HB9T", "shipped", 20, None),
    ("H2613", "H4", "LS-TG100V618", "shipped", 72, None),
    ("H2614", "H4", "MD-MBRE576S", "shipped", 48, None),
    ("H2615", "H4", "HA-KFR35-E1", "shipped", 120, None),
    ("H2616", "H1", "LS-TG100V618", "delivered", 72, 2),
    ("H2617", "H1", "HA-KFR35-E1", "delivered", 168, 6),
    ("H2618", "H1", "HA-KFR72-E1", "delivered", 528, 20),
    ("H2619", "H2", "LS-TB100V26", "delivered", 1008, 40),
    ("H2620", "H1", "SI-WM12P2602", "delivered", 648, 25),
    ("H2621", "H4", "MD-MB40HB9T", "delivered", 48, 1),
    ("H2622", "H4", "SI-WN54A1X42", "delivered", 168, 5),
    ("H2623", "H1", "MD-KFR35-Q2", "delivered", 144, 4),
    ("H2624", "H2", "LS-TG100V618", "delivered", 408, 15),
    ("H2625", "H4", "MD-MBRE576S", "delivered", 768, 30),
    # 3 年多前购机：用于「是否过保 / 保外维修要不要收费」的判断
    ("H2626", "H1", "HA-KFR72-E1", "delivered", 29400, 1200),
    ("H2627", "H4", "SI-WN54A1X42", "delivered", 216, 7),
    ("H2628", "H2", "SP-F40H8093S", "cancelled", 240, None),
    ("H2629", "H4", "SP-F50X96", "cancelled", 144, None),
    ("H2630", "H4", "MD-MB40HB9T", "cancelled", 72, None),
]

_ADDRESS = {
    "H1": "浙江省杭州市西湖区****小区 3-601",
    "H2": "上海市浦东新区****路 88 弄 2-1802",
    "H3": "江苏省南京市鼓楼区****",
    "H4": "四川省成都市武侯区****苑 5-302",
}

_CATEGORY_CN = {"aircon": "空调", "washer": "洗衣机", "rice_cooker": "电饭煲"}

ORDERS = [
    {
        "order_id": order_id,
        "user_id": user_id,
        "product": _CATEGORY_CN[_CATEGORY[sku]],
        "sku": sku,
        "amount_cents": _PRICE[sku],
        "status": status,
        "created_at": _ago(hours=created_h),
        "delivered_at": _ago(days=delivered_d) if delivered_d is not None else None,
        "address_masked": _ADDRESS[user_id],
    }
    for order_id, user_id, sku, status, created_h, delivered_d in _ORDER_ROWS
]

#: 物流：7 个运输中订单 + 1 个已签收订单（其余已签收订单**故意没有**物流记录，
#: 用于验证「暂无物流记录」时不编造轨迹）。
_CARRIERS = {
    "aircon": ("德邦快递", "DB"),
    "washer": ("顺丰速运", "SF"),
    "rice_cooker": ("京东物流", "JD"),
}
LOGISTICS = []
LOGISTICS_EVENTS = []
for index, order in enumerate(row for row in ORDERS if row["status"] in {"shipped", "delivered"}):
    if order["status"] == "delivered" and order["order_id"] != "H2616":
        continue
    carrier, prefix = _CARRIERS[_CATEGORY[order["sku"]]]
    events = [
        (order["created_at"], "揽收", f"{carrier}已揽收，发往杭州转运中心"),
        (_ago(hours=max(6, 30 - index)), "运输中", "到达杭州转运中心，准备发往网点"),
    ]
    if order["order_id"] == "H2611":
        # 停滞单：最后一条节点是 5 天前，用于「为什么不动了」
        events.append((_ago(days=5), "运输中", "快件在杭州转运中心滞留（超 72 小时无新节点）"))
    elif order["status"] == "delivered":
        events.extend(
            [
                (_ago(days=3), "派送中", "快件已交给派送员，正在派送"),
                (order["delivered_at"], "已签收", "本人签收"),
            ]
        )
    else:
        events.append((_ago(hours=max(2, 12 - index)), "派送中", "快件到达网点，等待派送"))

    LOGISTICS.append(
        {
            "order_id": order["order_id"],
            "carrier": carrier,
            "tracking_no": f"{prefix}{2601000 + index}",
            "status": "已签收" if order["status"] == "delivered" else "运输中",
            # 与最后一条轨迹保持一致：两张表打架时，用户看到的就是「自相矛盾」
            "updated_at": events[-1][0],
        }
    )
    LOGISTICS_EVENTS.extend(
        {
            "order_id": order["order_id"],
            "happened_at": happened_at,
            "node": node,
            "description": description,
        }
        for happened_at, node, description in events
    )

# --------------------------------------------------------------------- 售后

#: 7 张工单：安装 3 / 维修 3 / 保养 1（H2625 同一订单两张维修单 = 二次故障）。
TICKETS = [
    {"ticket_id": "T2601", "order_id": "H2617", "user_id": "H1", "type": "install",
     "description": "空调挂机安装（已完成）", "status": "completed",
     "scheduled_time": _ago(days=6), "created_at": _ago(days=6)},
    {"ticket_id": "T2602", "order_id": "H2601", "user_id": "H1", "type": "install",
     "description": "空调安装，等待预约", "status": "created",
     "scheduled_time": None, "created_at": _ago(hours=1)},
    {"ticket_id": "T2603", "order_id": "H2610", "user_id": "H2", "type": "install",
     "description": "柜机安装，已约周六上午", "status": "scheduled",
     "scheduled_time": _later(days=2), "created_at": _ago(days=1)},
    {"ticket_id": "T2604", "order_id": "H2619", "user_id": "H2", "type": "repair",
     "description": "洗衣机脱水噪音大（保内，待上门检测）", "status": "created",
     "scheduled_time": None, "created_at": _ago(days=3)},
    {"ticket_id": "T2605", "order_id": "H2625", "user_id": "H4", "type": "repair",
     "description": "电饭煲不加热，二次报修，等待加热盘备件", "status": "scheduled",
     "scheduled_time": _later(days=1), "created_at": _ago(days=6)},
    {"ticket_id": "T2606", "order_id": "H2625", "user_id": "H4", "type": "repair",
     "description": "电饭煲不加热（首次维修，已更换温控器）", "status": "completed",
     "scheduled_time": _ago(days=19), "created_at": _ago(days=20)},
    {"ticket_id": "T2607", "order_id": "H2626", "user_id": "H1", "type": "repair",
     "description": "柜机制冷差，第三次报修", "status": "created",
     "scheduled_time": None, "created_at": _ago(days=1)},
]

TICKET_EVENTS = [
    {"ticket_id": "T2601", "happened_at": _ago(days=6), "kind": "created", "note": "用户预约安装"},
    {"ticket_id": "T2601", "happened_at": _ago(days=6), "kind": "scheduled", "note": "约定次日上门"},
    {"ticket_id": "T2601", "happened_at": _ago(days=6), "kind": "repaired", "note": "安装完成，试机正常"},
    {"ticket_id": "T2601", "happened_at": _ago(days=6), "kind": "closed", "note": "工单关闭"},
    {"ticket_id": "T2602", "happened_at": _ago(hours=1), "kind": "created", "note": "待用户确认上门时间"},
    {"ticket_id": "T2603", "happened_at": _ago(days=1), "kind": "created", "note": "下单后预约安装"},
    {"ticket_id": "T2603", "happened_at": _ago(hours=20), "kind": "scheduled", "note": "已约 2 天后上午"},
    {"ticket_id": "T2604", "happened_at": _ago(days=3), "kind": "created", "note": "保外，需先报价"},
    {"ticket_id": "T2604", "happened_at": _ago(days=2), "kind": "assigned", "note": "已派单给杭州网点"},
    {"ticket_id": "T2605", "happened_at": _ago(days=6), "kind": "created", "note": "二次报修：仍不加热"},
    {"ticket_id": "T2605", "happened_at": _ago(days=5), "kind": "assigned", "note": "已派单"},
    {"ticket_id": "T2605", "happened_at": _ago(days=4), "kind": "part_wait", "note": "加热盘缺货，预计 5 天后到货"},
    {"ticket_id": "T2606", "happened_at": _ago(days=20), "kind": "created", "note": "首次报修"},
    {"ticket_id": "T2606", "happened_at": _ago(days=19), "kind": "repaired", "note": "更换温控器，恢复正常"},
    {"ticket_id": "T2606", "happened_at": _ago(days=19), "kind": "closed", "note": "工单关闭"},
    {"ticket_id": "T2607", "happened_at": _ago(days=1), "kind": "created", "note": "同一故障第三次报修"},
]

REFUNDS = [
    {"refund_id": "R2601", "order_id": "H2616", "user_id": "H1",
     "items_json": json.dumps([_NAME["LS-TG100V618"]], ensure_ascii=False),
     "amount_cents": 279900, "reason": "七天无理由退货（未拆封）",
     "status": "pending_confirmation", "idempotency_key": None,
     "created_at": _ago(days=1), "updated_at": _ago(hours=20)},
    {"refund_id": "R2602", "order_id": "H2625", "user_id": "H4",
     "items_json": json.dumps([_NAME["MD-MBRE576S"]], ensure_ascii=False),
     "amount_cents": 89900, "reason": "超过 7 天，申请无理由退货",
     "status": "rejected", "idempotency_key": None,
     "created_at": _ago(days=25), "updated_at": _ago(days=24)},
    {"refund_id": "R2603", "order_id": "H2618", "user_id": "H1",
     "items_json": json.dumps([_NAME["HA-KFR72-E1"]], ensure_ascii=False),
     "amount_cents": 699900, "reason": "质量问题：压缩机异响，已出具检测单",
     "status": "approved", "idempotency_key": None,
     "created_at": _ago(days=3), "updated_at": _ago(days=2)},
    {"refund_id": "R2604", "order_id": "H2624", "user_id": "H2",
     "items_json": json.dumps([_NAME["LS-TG100V618"]], ensure_ascii=False),
     "amount_cents": 279900, "reason": "质量问题：进水阀漏水",
     "status": "completed", "idempotency_key": None,
     "created_at": _ago(days=12), "updated_at": _ago(days=10)},
]

INVOICES = [
    {"invoice_id": "IV2601", "order_id": "H2616", "user_id": "H1", "kind": "normal",
     "title": "个人", "amount_cents": 279900, "status": "issued",
     "issued_at": _ago(days=3), "updated_at": _ago(days=3)},
    {"invoice_id": "IV2602", "order_id": "H2610", "user_id": "H2", "kind": "special",
     "title": "上海某某科技有限公司", "amount_cents": 699900, "status": "pending",
     "issued_at": None, "updated_at": _ago(days=2)},
    {"invoice_id": "IV2603", "order_id": "H2618", "user_id": "H1", "kind": "normal",
     "title": "个人", "amount_cents": 699900, "status": "not_requested",
     "issued_at": None, "updated_at": _ago(days=20)},
    {"invoice_id": "IV2604", "order_id": "H2622", "user_id": "H4", "kind": "normal",
     "title": "个人", "amount_cents": 529900, "status": "red_flushed",
     "issued_at": _ago(days=20), "updated_at": _ago(days=2)},
]


# --------------------------------------------------------------------- 自检与落盘


def _validate() -> None:
    """生成前自检：任何一条不满足，直接失败（宁可不出数据，也不出假数据）。"""
    users = {row["user_id"] for row in USERS}
    orders = {row["order_id"]: row for row in ORDERS}
    products = {row["sku"] for row in PRODUCTS}
    tickets = {row["ticket_id"] for row in TICKETS}

    assert len(orders) == 30, f"订单应为 30 单，实际 {len(orders)}"
    for row in ORDERS:
        assert row["status"] in {"paid", "shipped", "delivered", "cancelled"}, row
        assert row["sku"] in products, f"订单引用了不存在的 SKU：{row['sku']}"
        assert row["user_id"] in users, row
        if row["status"] == "delivered":
            assert row["delivered_at"], f"已签收订单缺签收时间：{row['order_id']}"

    for row in LOGISTICS:
        assert orders[row["order_id"]]["status"] in {"shipped", "delivered"}, row
    for row in LOGISTICS_EVENTS:
        assert row["order_id"] in orders, row
    for row in TICKETS:
        order = orders[row["order_id"]]
        assert order["user_id"] == row["user_id"], f"工单与订单用户不一致：{row['ticket_id']}"
    for row in TICKET_EVENTS:
        assert row["ticket_id"] in tickets, row
    for row in REFUNDS:
        order = orders[row["order_id"]]
        assert order["status"] == "delivered", f"退款单挂在未签收订单：{row['refund_id']}"
        assert row["amount_cents"] <= order["amount_cents"], f"退款超过订单金额：{row['refund_id']}"
        assert order["user_id"] == row["user_id"], row
    for row in INVOICES:
        assert row["order_id"] in orders, row
        assert orders[row["order_id"]]["user_id"] == row["user_id"], row


def _write(seed_dir: Path) -> dict[str, int]:
    files = {
        "users.yaml": ("users", USERS),
        "user_homes.yaml": ("user_homes", HOMES),
        "products.yaml": (
            "products",
            [
                {
                    "sku": item["sku"], "category": item["category"], "model": item["model"],
                    "name": item["name"], "price_cents": item["price_cents"],
                    "capacity": item["capacity"], "energy_grade": item["energy_grade"],
                    "dimensions_mm": item["dimensions_mm"],
                    "features_json": json.dumps(item["features"], ensure_ascii=False),
                    "install_notes": item["install_notes"], "status": item["status"],
                    "updated_at": _ago(days=1),
                }
                for item in PRODUCTS
            ],
        ),
        "inventory.yaml": ("inventory", INVENTORY),
        "promotions.yaml": ("promotions", PROMOTIONS),
        "orders.yaml": ("orders", ORDERS),
        "logistics.yaml": ("logistics", LOGISTICS),
        "logistics_events.yaml": ("logistics_events", LOGISTICS_EVENTS),
        "service_tickets.yaml": ("service_tickets", TICKETS),
        "ticket_events.yaml": ("ticket_events", TICKET_EVENTS),
        "refund_requests.yaml": ("refund_requests", REFUNDS),
        "invoices.yaml": ("invoices", INVOICES),
    }
    seed_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for filename, (key, rows) in files.items():
        header = (
            "# 由 scripts/gen_homeapp_seed.py 生成，请勿手改。\n"
            "# 数据基准时间：灌库时按 @now 时间宏解析（不再写死生成日期）。\n"
            "# 价格 / 库存 / 促销为**模拟值**；容量与能效取自 appliance-kb 的真实参数。\n"
            "# 场景依赖相对时间，重跑脚本即可刷新（见 KNOWN_ISSUES M16）。\n"
        )
        body = yaml.safe_dump({key: rows}, allow_unicode=True, sort_keys=False, width=120)
        (seed_dir / filename).write_text(header + body, encoding="utf-8")
        counts[key] = len(rows)
    return counts


def generate(seed_dir: Path | str | None = None) -> dict[str, int]:
    """生成全部种子文件；返回「表名 → 行数」。"""
    _validate()
    return _write(Path(seed_dir or TEMPLATE_SEED_DIR))


def main() -> int:
    counts = generate()
    print(json.dumps({"ok": True, "generated_at": _NOW.strftime(_FMT), "rows": counts},
                     ensure_ascii=False, indent=2))
    print(f"\n已写入：{TEMPLATE_SEED_DIR}")
    print("下一步：python -m apps.build_kb_pack --kb appliance-kb --pack homeapp"
          " && zkagent --industry homeapp db seed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
