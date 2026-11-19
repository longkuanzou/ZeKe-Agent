#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 09_eval/答案确认页.html —— 一个自包含的单文件确认工作台。

为什么要有这个：
  188 条标准答案，人工在 markdown 里逐条看、勾、批注，效率极低也留不下痕迹。
  这个页面把「结构评估结论 + 逐条确认 + 审计标记 + 导出」放在一起，
  确认完点导出，得到的 JSON 可以直接回灌到 gen_eval.py 的修正项。

页面能力：
  · 顶部：目录结构是否够建客服的结论 + 六场景覆盖度 + 缺口清单
  · 中部：188 条逐条确认（正确 / 有误 + 修正文本 / 跳过）
  · 审计标记：把 audit_eval.py 查出的可疑项顶到前面并高亮
  · 筛选：全部 / 待确认 / 有标记 / 按场景 / 按答案依据
  · 搜索 + 进度统计 + localStorage 断点续填 + 导出 JSON / 复制

用法:
  python tools/build_review_html.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "09_eval/questions.jsonl"
OUT = ROOT / "09_eval/答案确认页.html"

# ── 本轮已修正的条目（我审计时发现并已改 gen_eval.py，这里列出供你复核） ──
FIXED = [
    {"id": "e-s2_install-001", "issue": "答案里的「内机离地 2.1m 以上」和「220V/16A 插座」库内没有出处",
     "fix": "install.yaml#indoor_clearance 是 null（说明书为图示），本机最大输入电流是 15A 不是 16A。已按说明书重写为电气/空间/机位/管路四条"},
    {"id": "e-s2_install-009", "issue": "出处张冠李戴：空调高空费却指向洗衣机安装横比表",
     "fix": "出处改为 05_brand_ref/midea/fee_schedule.yaml#items_by_appliance.air_conditioner"},
    {"id": "e-s2_install-010", "issue": "同上（5 楼没电梯的安装总价题）",
     "fix": "出处同上"},
    {"id": "e-s2_install-011", "issue": "空调打孔收费题出处指向洗衣机安装横比表",
     "fix": "出处改为 05_brand_ref/haier/fee_schedule.yaml#items_by_appliance.air_conditioner"},
    {"id": "e-s2_install-013", "issue": "「24 小时内联系」的真实出处是 faq.yaml，不是 slots.yaml",
     "fix": "DEFAULT_SRC 的 install.booking / service.track 改指各自的 faq.yaml"},
    {"id": "e-s_adversarial-010", "issue": "「主板 300~600 元」是拼出来的区间，库里没有",
     "fix": "改为 室内机主板 200~600 元、室外机主板 300~1200 元，出处改指 parts_catalog"},
]

# ── 目录结构评估（结论 + 六场景覆盖度 + 缺口） ──
ASSESSMENT = {
    "verdict": "知识层够了，运行时不够",
    "verdict_detail":
        "现有 00~10 结构已经把「知识」这一层做扎实了：三品类 29 型号的事实层、六个场景的场景层、"
        "五品牌政策层、检索层、评测层、治理层都齐了，检索切片的 frontmatter 带 appliance/brand/model/"
        "intent/verified/source，1622 条分布均衡，可以直接支撑 RAG 检索。"
        "但一个能上线的智能客服还需要「运行时」和「契约」这两层，现在目录里没有："
        "会话/多轮上下文、外部接口契约、答案输出规范、评测执行器、拒答与置信度策略。"
        "补齐这五样，才算从知识库变成客服系统。",
    "layers": [
        {"name": "00_schema 规范层", "state": "ready",
         "note": "参数字典 3 份 + 命名/ID 规范 + confidence 四档定义，含「裁定≠核实」「冲突不覆盖」两条硬约定"},
        {"name": "01~02 原始与抽取层", "state": "ready",
         "note": "29 型号 PDF/JPG 全量抽取，页码锚点 + 乱码检测 + OCR 缓存，可重跑"},
        {"name": "03_product 事实层", "state": "ready",
         "note": "29 型号全 ready，参数带 source 与 confidence，冲突值留 cross_check_alt"},
        {"name": "04_scenario 场景层", "state": "ready",
         "note": "6 场景 × (playbook + slots + faq)，_shared 下有意图映射/安全红线/转人工规则/答案风格"},
        {"name": "05~06 政策层", "state": "partial",
         "note": "五品牌 ×5 文件 + 4 份平台政策。缺口：小天鹅分界年份、平台书面规则、备件价格非官方"},
        {"name": "07_dialogue 编排层", "state": "partial",
         "note": "有路由矩阵/澄清规则/槽位 schema，但没有会话状态与多轮上下文的落地文件"},
        {"name": "08_index 检索层", "state": "ready",
         "note": "1622 条切片 + manifest + 同义词 + 术语表，frontmatter 可按品类/品牌/型号过滤"},
        {"name": "09_eval 评测层", "state": "partial",
         "note": "188 条标准答案齐了，但没有自动判分的 runner（无法一键回归）"},
        {"name": "10_ops 治理层", "state": "ready",
         "note": "validate/audit 双检查 + 覆盖度报告 + backlog + confirm_queue + changelog"},
    ],
    "scenarios": [
        {"name": "售前咨询", "kb": "参数/功能/适配/对比",
         "status": "ok", "have": "compare.yaml 10 个对比组（空调 2/洗衣机 5/电饭煲 2/…）、四品类槽位、空调查表",
         "gap": "价格无数据（必须走外部接口，但接口契约不存在）；无「年耗电/电费估算」"},
        {"name": "安装咨询", "kb": "安装条件/预约流程/收费",
         "status": "ok", "have": "3 份 by_appliance 安装条件 + 各型号 install.yaml + 预约 FAQ + 4 品牌收费横比 + 备件价目",
         "gap": "「上门费」各品牌口径不一（海尔 50/次、西门子 50 含 15km、小天鹅美的首次免费），横着念会报错"},
        {"name": "使用咨询", "kb": "操作步骤/保养/注意事项",
         "status": "weak", "have": "FAQ 29 条（空调 10/洗衣机 8/电饭煲 11）+ 6 份 playbook + slots",
         "gap": "29 条偏少。高频未覆盖：空调除霜/自清洁失败/遥控器锁、洗衣机投放与残留、预约上限按型号差异"},
        {"name": "故障排查", "kb": "现象/排查步骤/转人工边界",
         "status": "ok", "have": "symptoms 44 条（含 severity/escalate_when）+ 3 份 by_appliance 排查链 + 安全红线",
         "gap": "排查链是跨型号通用的，型号差异只在 faults.yaml 里（代码/阈值）；无「自检流程」类交互"},
        {"name": "售后政策", "kb": "退换/保修/运费/二次销售",
         "status": "ok", "have": "5 品牌 warranty + return_policy（时限/举证/已拆未装 6 状态）+ shipping + opened_box",
         "gap": "小天鹅分界年份未给；平台规则只有业务方口述版（verified_platform_detail: partial）"},
        {"name": "报修建单", "kb": "故障信息/地址/预约/工单",
         "status": "weak", "have": "ticket_fields.yaml 字段定义 + s6 slots/faq/playbook + 预约/进度话术",
         "gap": "没有工单状态机（建单→派单→上门→备件→闭环的数据结构），也没有与 CRM 的接口契约"},
    ],
    "gaps": [
        {"p": "P0", "title": "外部接口契约层缺失", "detail":
         "评测答案里反复出现「价格必须走接口」「建单要调工单系统」「备件库存要查网点」，"
         "但目录里没有任何一个接口定义文件。建议新增 07_dialogue/interfaces.yaml，"
         "把 建单/查进度/查备件/查价格/查网点覆盖范围 五个接口的入参、出参、失败兜底写清楚。"},
        {"p": "P0", "title": "会话与多轮上下文缺失", "detail":
         "07_dialogue 有路由矩阵和槽位 schema，但没有 session 状态定义："
         "上一轮说了什么、槽位填到哪一步、什么时候该反问、什么时候该换意图。"
         "多轮场景（先说型号再问参数、先说故障再问收费）现在只能靠实现方临场发挥。"},
        {"p": "P0", "title": "没有评测执行器", "detail":
         "188 条题有 must_include/must_not_include，但只能人工比对。"
         "建议加 tools/run_eval.py：读评测集 → 调检索+生成 → 按断言自动判分 → 输出通过率。"
         "这才是能进 CI 的东西，也是改知识库后能立刻知道「有没有答坏」的保障。"},
        {"p": "P1", "title": "答案输出规范不完整", "detail":
         "_shared/answer_style.md 只有 47 行的口头风格。"
         "建议补：结构化输出 schema（结论/依据/下一步动作）、"
         "数字必须带口径（如「100~200 元（行业参考）」）、"
         "禁止句式清单、以及「库内没有」的统一话术。"},
        {"p": "P1", "title": "拒答与置信度策略缺失", "detail":
         "库里只有 2 条 out_of_scope（冰箱/微波炉），"
         "但真实客服会遇到的「不知道」远不止这两类："
         "型号不在库、参数缺失、代码未收录、库外机型、政策未覆盖。"
         "建议每类都给一条标准话术 + 转人工条件。"},
        {"p": "P1", "title": "使用类 FAQ 覆盖偏薄", "detail":
         "29 条覆盖不了真实咨询量。空调侧至少还缺：除霜制热、滤网清洗提示、"
         "遥控器锁/童锁、面板自诊断、关机不关机；"
         "洗衣机侧缺：洗涤剂投放、残留处理、脱水异响、噪音、童锁；"
         "电饭煲侧缺：内胆涂层养护、异味、预约与保温冲突。"},
        {"p": "P2", "title": "召回质量无度量", "detail":
         "synonyms.yaml 存在但没有召回率测试集。"
         "建议给每个意图写 5~10 条口语问法作为召回用例，"
         "确认用户说「空调不凉」「E1 报错」「洗衣机烘不干」都能命中正确的 intent。"},
        {"p": "P2", "title": "切片无版本号", "detail":
         "manifest 有 status/verified，但没有 version。"
         "线上回滚、灰度对比都需要能按版本切检索结果。"},
    ],
    "verdict_items": [
        "目录结构**足够支撑一个「基于知识库问答」的客服原型**：检索层切片字段齐全、场景层六套 playbook/slots/faq 齐备、"
        "三品类参数与政策都有出处可回溯。",
        "目录结构**不足以支撑一个能上线的客服系统**：缺接口契约、会话/多轮上下文、答案输出规范、"
        "拒答与置信度策略、评测执行器这五项运行时资产。",
        "知识本身的硬缺口只有 4 项（小天鹅分界年份、平台书面退换规则、西门子收费表错位、MB-40HB9T 内胆材质），"
        "其余都是「补件」而不是「补知识」。",
    ],
}


def main():
    rows = [json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if l.strip()]

    # 跑一遍审计拿标记
    audit = {"issues": []}
    try:
        subprocess.run([sys.executable, str(ROOT / "tools/audit_eval.py"), "--json"],
                       check=True, capture_output=True, cwd=ROOT)
        audit = json.loads((ROOT / "09_eval/_audit.json").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print("审计未成功，标记为空：", e)

    flags = {}
    for it in audit.get("issues", []):
        flags[it["id"]] = it["flags"]
    fixed_ids = {f["id"]: f for f in FIXED}

    data = {
        "rows": rows,
        "flags": flags,
        "fixed": fixed_ids,
        "assessment": ASSESSMENT,
    }
    payload = json.dumps(data, ensure_ascii=False)
    html = TEMPLATE.replace("/*__DATA__*/", payload)
    OUT.write_text(html, encoding="utf-8", newline="\n")
    print(f"→ {OUT}  ({len(rows)} 条，标记 {len(flags)} 条，本轮已修正 {len(fixed_ids)} 条）")
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>家电客服知识库 · 评测集答案确认</title>
<style>
:root{
  --bg:#f6f7f9; --card:#ffffff; --line:#e4e7ec; --ink:#1a1d21; --sub:#5b6472;
  --warn:#b54708; --warnbg:#fef3e2; --err:#b42318; --errbg:#fee4e2; --ok:#027a48; --okbg:#e6f4ec;
  --blue:#175cd3; --bluebg:#eff4ff; --grey:#667085;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.1);
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#14161a; --card:#1d2025; --line:#2c313a; --ink:#e7eaee; --sub:#9aa4b2;
    --warnbg:#3a2a12; --warn:#f5b544; --errbg:#3a1e1c; --err:#f97066; --okbg:#12301f; --ok:#6ce9a6;
    --bluebg:#16243f; --blue:#84adff; --grey:#8b95a3;
    --shadow:0 1px 2px rgba(0,0,0,.4); }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--blue)}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 80px}
header.top{background:var(--card);border-bottom:1px solid var(--line);padding:26px 0 20px;margin-bottom:22px}
header.top h1{margin:0 0 6px;font-size:21px;letter-spacing:.2px}
.sub{color:var(--sub);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:20px 22px;margin-bottom:18px}
h2{font-size:16px;margin:0 0 12px;display:flex;align-items:center;gap:9px}
h2::before{content:"";width:3px;height:15px;background:var(--blue);border-radius:2px}
h3{font-size:14px;margin:16px 0 8px}
.verdict{font-size:16px;font-weight:650;margin:0 0 8px}
ul.tight{margin:8px 0 0;padding-left:20px}
ul.tight li{margin:5px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--sub);font-weight:600;font-size:12px;background:var(--bg)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;
  background:var(--bg);padding:1px 5px;border-radius:4px;border:1px solid var(--line)}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;font-weight:600;white-space:nowrap}
.pill.ok{background:var(--okbg);color:var(--ok)} .pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.err{background:var(--errbg);color:var(--err)} .pill.info{background:var(--bluebg);color:var(--blue)}
.pill.grey{background:var(--bg);color:var(--grey);border:1px solid var(--line)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.bar{height:8px;background:var(--bg);border-radius:5px;overflow:hidden;border:1px solid var(--line)}
.bar>i{display:block;height:100%;background:var(--ok);width:0}
.stat{font-size:12.5px;color:var(--sub)}
.stat b{color:var(--ink);font-size:15px}
.sticky{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--line)}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:11px 0}
button,select,input{font:inherit;color:inherit}
button{border:1px solid var(--line);background:var(--card);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--blue);color:var(--blue)}
button.primary{background:var(--blue);color:#fff;border-color:var(--blue)}
button.primary:hover{opacity:.9;color:#fff}
input[type=search]{border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:6px 11px;min-width:230px;flex:1}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tabs button{border-radius:20px;font-size:12.5px;padding:4px 12px}
.tabs button.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.item{border:1px solid var(--line);border-radius:11px;background:var(--card);padding:14px 16px;margin-bottom:11px}
.item.done-ok{border-color:var(--ok);background:var(--okbg)}
.item.done-err{border-color:var(--err);background:var(--errbg)}
.item.hide{display:none}
.ihead{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:7px}
.id{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--sub)}
.q{font-weight:600;font-size:14.5px;margin:2px 0 9px}
.ans{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:10px 12px;margin:7px 0;white-space:pre-wrap}
.ans .k{color:var(--sub);font-size:12px;display:block;margin-bottom:3px}
.kv{display:flex;gap:7px;font-size:12.5px;color:var(--sub);flex-wrap:wrap;margin:5px 0}
.kv b{color:var(--ink);font-weight:600}
.mark{border-left:3px solid var(--warn);background:var(--warnbg);border-radius:0 7px 7px 0;padding:8px 11px;margin:8px 0;font-size:12.5px}
.mark.err{border-color:var(--err);background:var(--errbg)}
.acts{display:flex;gap:7px;margin-top:10px;align-items:center;flex-wrap:wrap}
.acts button.ok{border-color:var(--ok);color:var(--ok)}
.acts button.ok.on{background:var(--ok);color:#fff}
.acts button.err{border-color:var(--err);color:var(--err)}
.acts button.err.on{background:var(--err);color:#fff}
.fixbox{display:none;margin-top:9px}
.fixbox.show{display:block}
textarea{width:100%;min-height:76px;border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:var(--card);resize:vertical}
.toast{position:fixed;right:20px;bottom:20px;background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:9px;
  opacity:0;pointer-events:none;transition:.25s;font-size:13px;z-index:99}
.toast.on{opacity:1}
.srcline{font-size:12px;color:var(--sub)}
</style>
</head>
<body>
<header class="top"><div class="wrap">
  <h1>家电客服知识库 · 评测集答案确认</h1>
  <div class="sub" id="head-sub"></div>
</div></header>

<div class="wrap">
  <section class="card" id="gap"></section>

  <div class="sticky"><div class="wrap" style="padding-left:20px;padding-right:20px">
    <div class="toolbar">
      <div class="tabs" id="tabs"></div>
      <input type="search" id="q" placeholder="搜索问题 / 答案 / 型号 / id…">
      <span class="stat" id="prog"></span>
      <button id="btn-all-ok">全部标为正确</button>
      <button id="btn-export" class="primary">导出确认结果</button>
    </div>
    <div class="bar"><i id="barfill"></i></div>
  </div></div>

  <div id="list" style="padding-top:16px"></div>
</div>

<div class="toast" id="toast"></div>

<script>
const DATA = /*__DATA__*/;
const KEY = "kb_eval_review_v1";
let state = JSON.parse(localStorage.getItem(KEY) || "{}");
const save = () => localStorage.setItem(KEY, JSON.stringify(state));

const SC = {"s1_presale":"售前","s2_install":"安装","s3_usage":"使用",
  "s4_troubleshoot":"故障","s5_aftersale":"售后","s6_service":"报修","s_adversarial":"对抗"};
const AP = {"air_conditioner":"空调","washer":"洗衣机","rice_cooker":"电饭煲",
  "refrigerator":"冰箱","microwave":"微波炉",null:"—"};
const BASIS = {"说明书原文":"ok","业务方口径":"warn","推断":"warn","网络值":"warn",
  "政策文件":"info","库内缺失":"err"};

function el(t,a,c){const e=document.createElement(t);if(a)for(const k in a)e.setAttribute(k,a[k]);
  if(c)e.className=c;return e;}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

/* ── 顶部：结构评估 ───────────────────────────── */
(function(){
  const A=DATA.assessment, g=document.getElementById("gap");
  let h=`<h2>第一问 · 现有目录结构能否支撑一个家电智能客服</h2>
  <p class="verdict">结论：${esc(A.verdict)}</p>
  <p class="sub" style="margin:0 0 12px">${esc(A.verdict_detail)}</p>
  <div class="grid2">
    <div><h3>六场景覆盖度</h3><table>
      <tr><th>场景</th><th>状态</th><th>已具备</th><th>缺口</th></tr>`;
  A.scenarios.forEach(s=>{
    const cls=s.status==="ok"?"ok":(s.status==="weak"?"warn":"err");
    const lab=s.status==="ok"?"齐备":(s.status==="weak"?"偏薄":"缺");
    h+=`<tr><td><b>${esc(s.name)}</b><br><span class="sub">${esc(s.kb)}</span></td>
      <td><span class="pill ${cls}">${lab}</span></td>
      <td>${esc(s.have)}</td><td>${esc(s.gap)}</td></tr>`;});
  h+=`</table></div>
  <div><h3>目录分层就绪度</h3><table>
      <tr><th>层</th><th>状态</th><th>说明</th></tr>`;
  A.layers.forEach(l=>{
    const cls=l.state==="ready"?"ok":"warn";
    const lab=l.state==="ready"?"就绪":"部分";
    h+=`<tr><td><b>${esc(l.name)}</b></td><td><span class="pill ${cls}">${lab}</span></td>
      <td>${esc(l.note)}</td></tr>`;});
  h+=`</table></div></div>
  <h3>要补的运行时资产（按优先级）</h3><table>
    <tr><th>优先级</th><th>缺口</th><th>说明</th></tr>`;
  A.gaps.forEach(g2=>{
    const cls=g2.p==="P0"?"err":(g2.p==="P1"?"warn":"grey");
    h+=`<tr><td><span class="pill ${cls}">${esc(g2.p)}</span></td><td><b>${esc(g2.title)}</b></td>
      <td>${esc(g2.detail)}</td></tr>`;});
  h+=`</table><h3>一句话判断</h3><ul class="tight">`;
  A.verdict_items.forEach(v=>h+=`<li>${esc(v)}</li>`);
  h+=`</ul>`;
  g.innerHTML=h;

  const n=Object.keys(DATA.flags).length, fx=Object.keys(DATA.fixed).length;
  document.getElementById("head-sub").innerHTML=
    `共 ${DATA.rows.length} 条标准答案 · 机器审计标记 ${n} 条 · 本轮已修正 ${fx} 条待复核 · 逐条勾选后点右上角导出`;
})();

/* ── 确认工作台 ─────────────────────────────── */
let filter="todo";
function buildTabs(){
  const t=document.getElementById("tabs");
  const defs=[["todo","待确认"],["all","全部"],["flag","有标记"],["fixed","已修正待复核"]];
  Object.keys(SC).forEach(k=>defs.push([k,SC[k]]));
  Object.keys(BASIS).forEach(k=>defs.push(["b:"+k,"依据·"+k]));
  t.innerHTML="";
  defs.forEach(([k,lab])=>{
    const b=el("button",null,k===filter?"on":"");b.textContent=lab;
    b.onclick=()=>{filter=k;buildTabs();render();};
    t.appendChild(b);});
}
function passes(r){
  const st=state[r.id];
  if(filter==="todo")return !st||(!st.v&&!st.skip);
  if(filter==="all")return true;
  if(filter==="flag")return !!DATA.flags[r.id];
  if(filter==="fixed")return !!DATA.fixed[r.id];
  if(filter.startsWith("b:"))return r.answer_basis===filter.slice(2);
  return r.scenario===filter;
}
function match(r,term){
  if(!term)return true;
  const t=term.toLowerCase();
  return [r.id,r.question,r.answer,r.model,r.brand,r.scenario,r.answer_basis,
    (r.expect.source_contains||""),JSON.stringify(r.expect.must_include)].join(" ").toLowerCase().includes(t);
}
function render(){
  const term=document.getElementById("q").value.trim();
  const list=document.getElementById("list");
  list.innerHTML="";
  const rows=DATA.rows.filter(r=>passes(r)&&match(r,term));
  if(!rows.length){list.innerHTML=`<div class="card sub">当前筛选下没有条目。</div>`;}
  rows.forEach(r=>list.appendChild(card(r)));
  updateProg();
}
function card(r){
  const st=state[r.id]||{};
  const fl=DATA.flags[r.id]||[], fx=DATA.fixed[r.id];
  const d=el("div","id",null);d.id="i_"+r.id;
  d.className="item"+(st.v==="ok"?" done-ok":(st.v==="err"?" done-err":""));
  const worst=fl.some(f=>f.level==="error")?"err":(fl.length?"warn":"");
  const bs=BASIS[r.answer_basis]||"grey";
  let h=`<div class="ihead">
    <span class="id">${esc(r.id)}</span>
    <span class="pill grey">${esc(SC[r.scenario]||r.scenario)}</span>
    <span class="pill grey">${esc(AP[r.appliance]||r.appliance||"—")}</span>
    ${r.brand?`<span class="pill grey">${esc(r.brand)}</span>`:""}
    ${r.model?`<span class="pill grey">${esc(r.model)}</span>`:""}
    <span class="pill ${bs}">依据·${esc(r.answer_basis)}</span>
    ${r.expect.handoff?`<span class="pill warn">需转人工</span>`:""}
    ${worst?`<span class="pill ${worst}">${worst==="err"?"审计异常":"审计提示"}</span>`:""}
    ${fx?`<span class="pill info">本轮已修正</span>`:""}
  </div>
  <div class="q">${esc(r.question)}</div>
  <div class="ans"><span class="k">标准答案</span>${esc(r.answer)}</div>
  <div class="kv">
    <span>必须包含：<b>${esc((r.expect.must_include||[]).join(" / ")||"—")}</b></span>
    <span>禁止出现：<b>${esc((r.expect.must_not_include||[]).join(" / ")||"—")}</b></span>
  </div>
  <div class="srcline">出处：<code>${esc(r.expect.source_contains||"—")}</code></div>`;
  if(fl.length){
    h+=`<div class="mark ${worst}">`+fl.map(f=>
      `<b>${esc(f.code)}</b> ${esc(f.msg)}`).join("<br>")+`</div>`;
  }
  if(fx){
    h+=`<div class="mark"><b>本轮已修正（待你复核）</b><br>问题：${esc(fx.issue)}<br>处理：${esc(fx.fix)}</div>`;
  }
  if(r.expect.note){h+=`<div class="srcline" style="margin-top:6px">备注：${esc(r.expect.note)}</div>`;}
  h+=`<div class="acts">
    <button class="ok ${st.v==="ok"?"on":""}">✓ 正确</button>
    <button class="err ${st.v==="err"?"on":""}">✗ 有误</button>
    <button class="skip ${st.skip?"on":""}">跳过</button>
    <span class="stat" style="margin-left:auto">${st.v==="err"&&st.fix?`已填修正 <b>${st.fix.length}</b> 字`:""}</span>
  </div>
  <div class="fixbox ${st.v==="err"?"show":""}">
    <div class="sub" style="margin-bottom:5px">写出正确说法（会随导出结果一起给出）</div>
    <textarea placeholder="把这一条的正确标准答案写在这里…">${esc(st.fix||"")}</textarea>
  </div>`;
  d.innerHTML=h;
  const [bok,ber,bsk]=d.querySelectorAll(".acts button");
  bok.onclick=()=>{state[r.id]={v:"ok"};save();render();};
  ber.onclick=()=>{state[r.id]=Object.assign({v:"err"},state[r.id]);save();render();};
  bsk.onclick=()=>{state[r.id]=Object.assign({skip:!((state[r.id]||{}).skip)},state[r.id]);save();render();};
  d.querySelector("textarea").oninput=e=>{state[r.id]=Object.assign({v:"err"},state[r.id],{fix:e.target.value});save();};
  return d;
}
function updateProg(){
  const all=DATA.rows;
  const ok=all.filter(r=>(state[r.id]||{}).v==="ok").length;
  const err=all.filter(r=>(state[r.id]||{}).v==="err").length;
  const skip=all.filter(r=>(state[r.id]||{}).skip).length;
  const done=ok+err+skip;
  document.getElementById("prog").innerHTML=
    `已确认 <b>${done}</b> / ${all.length} · 正确 ${ok} · 有误 ${err} · 跳过 ${skip}`;
  document.getElementById("barfill").style.width=(all.length?done/all.length*100:0)+"%";
}
function toast(t){const x=document.getElementById("toast");x.textContent=t;x.classList.add("on");
  setTimeout(()=>x.classList.remove("on"),2200);}
document.getElementById("btn-all-ok").onclick=()=>{
  const term=document.getElementById("q").value.trim();
  const rows=DATA.rows.filter(r=>passes(r)&&match(r,term));
  rows.forEach(r=>{if(!state[r.id]||!state[r.id].v)state[r.id]={v:"ok"};});
  save();render();toast(`已把当前视图 ${rows.length} 条标为正确`);};
document.getElementById("btn-export").onclick=()=>{
  const out={confirmed_at:new Date().toISOString(),
    total:DATA.rows.length,
    ok:DATA.rows.filter(r=>(state[r.id]||{}).v==="ok").length,
    err:DATA.rows.filter(r=>(state[r.id]||{}).v==="err").length,
    corrections:[]};
  DATA.rows.forEach(r=>{const s=state[r.id]||{};
    if(s.v==="err")out.corrections.push({id:r.id,question:r.question,
      old_answer:r.answer,new_answer:s.fix||"",source:r.expect.source_contains});});
  const blob=new Blob([JSON.stringify(out,null,2)],{type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download="评测集确认结果_"+new Date().toISOString().slice(0,10)+".json";
  a.click();
  toast("已导出：确认 "+out.ok+" 条，有误 "+out.err+" 条");};
document.getElementById("q").oninput=render;
buildTabs();render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
