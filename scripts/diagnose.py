#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卡点回溯诊断引擎
================
沿 Marble 前置依赖图回溯，把 assessmentPrompt 变成本地模型生成的中文提问，
由家长转述孩子真实回答，本地模型判定掌握度，定位「最浅层的失败点」= 卡点。

探测顺序：最深的先问（地基优先）。一旦某层失败，上面全部必然连带失败，
不必再问 —— 最少的问题数定位根因。若整条前置链全过、只有目标点本身失败，
则判定为「练习缺口」而非「前置缺口」，这是两种完全不同的处方。

用法:
  python scripts/diagnose.py plan  --topic mt_xmmgAzxe5j [--depth 4]
  python scripts/diagnose.py ask   --topic mt_xmmgAzxe5j          # 只出这道题
  python scripts/diagnose.py judge --topic mt_xmmgAzxe5j --answer "孩子原话/现场表现"
  python scripts/diagnose.py report --session <session.json>
"""
import argparse, json, os, sys, time, urllib.request, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API = os.environ.get("DIAG_API", "http://127.0.0.1:8088/v1/chat/completions")
CACHE = os.path.join(HERE, "_qcache.json")
CHILD_NAME = os.environ.get("DIAG_CHILD", "予淳")

sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------- graph
def load():
    topics = {x["id"]: x for x in json.load(open(os.path.join(ROOT, "data/topics.json"), encoding="utf-8"))["topics"]}
    deps = json.load(open(os.path.join(ROOT, "data/dependencies.json"), encoding="utf-8"))["dependencies"]
    parents, children = {}, {}
    for e in deps:
        parents.setdefault(e["topicId"], []).append(e)
        children.setdefault(e["prerequisiteId"], []).append(e)
    return topics, parents, children

TOPICS, PARENTS, CHILDREN = load()

def trace_up(start, depth):
    """返回 [(depth, topic_id, reason, strength)]，depth 越大越接近地基。"""
    out, seen, frontier = [], {start}, [(start, 0, "")]
    while frontier:
        cur, d, why = frontier.pop(0)
        if d >= depth:
            continue
        for e in sorted(PARENTS.get(cur, []), key=lambda x: (x["strength"] != "hard", -TOPICS.get(x["prerequisiteId"], {}).get("centrality", 0))):
            p = e["prerequisiteId"]
            if p in seen or p not in TOPICS:
                continue
            seen.add(p)
            out.append((d + 1, p, e.get("reason", ""), e.get("strength", "hard")))
            frontier.append((p, d + 1, e.get("reason", "")))
    return out

def unlocks(topic_id):
    return [e["topicId"] for e in CHILDREN.get(topic_id, []) if e["topicId"] in TOPICS]

# ---------------------------------------------------------------- LLM
def llm(system, user, temperature=0.35, max_tokens=500):
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=120).read())
            return r["choices"][0]["message"]["content"]
        except Exception as ex:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))

def _extract_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.startswith("json") else t
    i, j = t.find("{"), t.rfind("}")
    return json.loads(t[i:j + 1])

Q_SYS = f"""你是学习诊断助手，面对一个中国 7 岁孩子。
给你一个「知识点」和它的「掌握标准」，请做三件事：
一、译成中国家长/老师熟悉的中文说法（name 短、meaning 一句话，用中国小学数学课标的常用词，不要直译腔）。
二、判断它对中国小学生是否适用（fit）：
   - "适用"：中国教材也教这个
   - "需调整"：中国教材有对应内容但切法/场景不同（说明怎么调）
   - "不适用"：欧美课程特有，中国孩子不需要（如英文 handwriting、英文数词拼写）
三、用一句中文说明它为什么是后续知识的地基（why）。
四、设计一道**现场可操作**的诊断题。要求：
1. 口语化，7 岁孩子一听就懂，不超过 45 字，不出现任何术语（如"位值""基数""分解"）。
2. 必须能用实物/心算/口头当场完成，不需要读写、不需要纸笔也行。
3. 场景贴近中国孩子生活（积木、糖、弹珠、人民币、小棒、楼梯、筷子）。
4. 只考这一个知识点，不要顺带考别的。
5. 另给一句「追问」：如果孩子答对，用来确认他是真懂还是背出来的。
严格输出 JSON，不要多余文字：
{{"name":"中文知识点名","meaning":"一句话中文含义","fit":"适用|需调整|不适用","fit_note":"需调整/不适用时说明原因，适用则留空","why":"它为什么是后面那些知识的地基","question":"...","probe":"追问...","testing":"这题在考什么(给家长看的一句话)"}}"""

def _warm(topic_ids):
    """并发预热缓存：把一批知识点的中文提问一次性生成完。"""
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    todo = [tid for tid in topic_ids
            if hashlib.md5((tid + CHILD_NAME).encode()).hexdigest() not in cache]
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    def one(tid):
        t = TOPICS[tid]
        ap = (t.get("assessmentPrompt") or "").replace("{{name}}", CHILD_NAME)
        user = (f"知识点：{t['name']}\n含义：{t.get('description','')}\n"
                f"掌握标准：{json.dumps(t.get('evidence', []), ensure_ascii=False)}\n"
                f"原始提问建议：{ap}")
        return tid, _extract_json(llm(Q_SYS, user, 0.5, 500))
    with ThreadPoolExecutor(max_workers=3) as ex:
        for tid, q in ex.map(one, todo):
            cache[hashlib.md5((tid + CHILD_NAME).encode()).hexdigest()] = q
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def gen_question(topic_id):
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    key = hashlib.md5((topic_id + CHILD_NAME).encode()).hexdigest()
    if key in cache:
        return cache[key]
    _warm([topic_id])
    return json.load(open(CACHE, encoding="utf-8"))[key]

J_SYS = """你是学习诊断裁判。给你一个知识点、它的掌握标准、诊断题、以及家长转述的孩子的真实表现。
判断孩子对这个知识点的掌握度，三档：
- mastered：明确掌握，做法正确且能说出理由
- partial：答案蒙对/只会在熟悉场景做/换个说法就不会/需要提示
- not_mastered：不会、做错、或完全无法回应
判据只看「掌握标准」里的能力是否出现，不要因为孩子态度好就放水，也不要因为表达笨拙就误判为不会。
严格输出 JSON：
{"verdict":"mastered|partial|not_mastered","reason":"一句话依据(引用孩子的具体表现)","next":"下一步该怎么补(给家长的一句话)"}"""

def judge(topic_id, answer):
    t = TOPICS[topic_id]
    q = gen_question(topic_id)
    user = (f"知识点：{t['name']}\n含义：{t.get('description','')}\n"
            f"掌握标准：{json.dumps(t.get('evidence', []), ensure_ascii=False)}\n"
            f"诊断题：{q['question']}\n追问：{q['probe']}\n\n"
            f"家长转述孩子的真实表现：{answer}")
    return _extract_json(llm(J_SYS, user, 0.1, 350))

# ---------------------------------------------------------------- commands
def zh(topic_id):
    """中文知识点名（缓存复用，不额外调用模型）。"""
    try:
        return gen_question(topic_id).get("name") or TOPICS[topic_id]["name"]
    except Exception:
        return TOPICS[topic_id]["name"]

def cmd_plan(a):
    chain = trace_up(a.topic, a.depth)
    order = sorted(chain, key=lambda x: (-x[0], x[3] != "hard"))
    _warm([tid for _, tid, _, _ in chain] + [a.topic] + unlocks(a.topic))
    tq = gen_question(a.topic)
    print(f"目标知识点：{tq.get('name') or TOPICS[a.topic]['name']}  ({a.topic})")
    print(f"含义：{tq.get('meaning') or TOPICS[a.topic].get('description','')}")
    print(f"前置链 {len(order)} 层，探测顺序（地基优先，问到第一个失败就停）：\n")
    skipped = 0
    for i, (d, tid, why, st) in enumerate(order, 1):
        t = TOPICS[tid]
        q = gen_question(tid)
        fit = q.get("fit", "适用")
        if fit == "不适用":
            skipped += 1
            print(f"[—] 跳过 · {q.get('name') or t['name']}（{fit}：{q.get('fit_note','')}）\n")
            continue
        tag = "硬" if st == "hard" else "软"
        flag = "" if fit == "适用" else f"  ⚠需调整：{q.get('fit_note','')}"
        print(f"[{i}] 深度{d} {tag}前置 · {q.get('name') or t['name']} ({tid}){flag}")
        print(f"    含义：{q.get('meaning','')}")
        print(f"    为什么必须先会：{q.get('why') or why}")
        print(f"    诊断题：{q['question']}")
        print(f"    追问：{q['probe']}")
        print(f"    考什么：{q['testing']}")
        print(f"    会了之后解锁：{', '.join(zh(u) for u in unlocks(tid)[:6])}\n")
    if skipped:
        print(f"（跳过 {skipped} 个欧美课程特有、对中国孩子不适用的前置）")

def cmd_ask(a):
    q = gen_question(a.topic)
    t = TOPICS[a.topic]
    print(f"{q.get('name') or t['name']}  [{t.get('ageRangeStart')}-{t.get('ageRangeEnd')}岁]  中心度 {t.get('centrality')}")
    print(f"含义：{q.get('meaning','')}")
    print(f"问孩子：{q['question']}")
    print(f"追问：  {q['probe']}")
    print(f"（家长看）考的是：{q['testing']}")

def cmd_judge(a):
    print(json.dumps(judge(a.topic, a.answer), ensure_ascii=False, indent=1))

def cmd_session(a):
    """一问一答的现场诊断：地基优先，问到第一个失败就停，实时出报告。"""
    chain = trace_up(a.topic, a.depth)
    order = [x for x in sorted(chain, key=lambda x: (-x[0], x[3] != "hard"))]
    _warm([tid for _, tid, _, _ in order] + [a.topic])
    tq = gen_question(a.topic)
    batch = a.answers.split("||") if a.answers else None
    results, stopped = [], False

    print(f"诊断对象：{CHILD_NAME}   目标：{tq.get('name')}（{TOPICS[a.topic].get('ageRangeStart')}-{TOPICS[a.topic].get('ageRangeEnd')}岁）")
    print(f"含义：{tq.get('meaning')}")
    print(f"共 {len(order)} 层前置，从最地基开始问。输入孩子的真实表现；s=跳过这题，q=结束并出报告。\n")

    for n, (d, tid, why, st) in enumerate(order, 1):
        q = gen_question(tid)
        fit = q.get("fit", "适用")
        if fit == "不适用":
            print(f"— 跳过 {q.get('name')}（欧美课程特有：{q.get('fit_note','')}）")
            continue
        print(f"── 第 {n} 问 · 深度{d} · {q.get('name')}")
        print(f"   问孩子：{q['question']}")
        print(f"   考什么：{q['testing']}")
        if batch is not None:
            ans = batch.pop(0) if batch else "无法回应"
            print(f"   家长转述：{ans}")
        else:
            try:
                ans = input("   孩子表现> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(); break
            if ans.lower() == "q":
                break
            if ans.lower() == "s":
                continue
        if not ans:
            continue
        j = judge(tid, ans)
        v = j.get("verdict", "partial")
        mark = {"mastered": "✓ 会", "partial": "△ 半会", "not_mastered": "✗ 不会"}[v]
        print(f"   → {mark}｜{j.get('reason','')}\n")
        results.append({"depth": d, "topic": tid, "verdict": v, "reason": j.get("reason", ""), "next": j.get("next", "")})
        if v == "not_mastered":
            stopped = True
            print("   地基在这里断了，上面那些不用问了 —— 停止探测。\n")
            break
        if batch is not None and not batch:
            break

    sess = {"child": CHILD_NAME, "target": a.topic, "results": results,
            "stopped_early": stopped, "ts": time.strftime("%Y-%m-%d %H:%M")}
    os.makedirs(os.path.join(ROOT, "sessions"), exist_ok=True)
    path = os.path.join(ROOT, "sessions", f"{time.strftime('%Y%m%d_%H%M')}_{a.topic}.json")
    json.dump(sess, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("=" * 58)
    _report(sess)
    print(f"\n（会话已存：{path}）")

def _report(s):
    res = s["results"]
    fail = [r for r in res if r["verdict"] != "mastered"]
    print(f"孩子：{s.get('child','?')}   目标：{zh(s['target'])}")
    print(f"共探测 {len(res)} 个知识点\n")
    if not res:
        print("（没有有效探测记录）")
        return
    for r in res:
        mark = {"mastered": "✓", "partial": "△", "not_mastered": "✗"}[r["verdict"]]
        print(f"{mark} 深度{r['depth']} {zh(r['topic'])}  —— {r['reason']}")
    if not fail:
        print(f"\n【结论】前置链全通，卡点不在前置，在「{zh(s['target'])}」本身 —— 练习缺口，不是能力缺口。")
        print(f"【处方】直接练这个点，不用回头补地基。")
        return
    root = max(fail, key=lambda r: (r["depth"], {"not_mastered": 2, "partial": 1}.get(r["verdict"], 0)))
    t = TOPICS[root["topic"]]
    above = [r for r in res if r["depth"] < root["depth"]]
    print(f"\n【卡点】{zh(root['topic'])}（{t.get('ageRangeStart')}-{t.get('ageRangeEnd')}岁该会，中心度 {round(t.get('centrality',0),2)}）")
    print(f"【为什么】{root['reason']}")
    print(f"【为什么表现为】{zh(s['target'])} 出错：", end="")
    print("  ".join(f"{zh(r['topic'])}({r['verdict']})" for r in above) or "  直接前置")
    print(f"【处方】{root['next']}")
    print(f"【补完解锁】{', '.join(zh(u) for u in unlocks(root['topic'])[:8])}")

def cmd_report(a):
    _report(json.load(open(a.session, encoding="utf-8")))

def cmd_screen(a):
    """摸底单：一次出若干高中心度地基题，用来找「该问哪条链」。"""
    cands = [x for x in TOPICS.values()
             if x["subject"] == a.subject
             and x.get("ageRangeStart", 99) <= a.age <= x.get("ageRangeEnd", 0)]
    cands.sort(key=lambda x: -x.get("centrality", 0))
    picked, skipped = [], 0
    for x in cands:
        if len(picked) >= a.n:
            break
        q = gen_question(x["id"])
        if q.get("fit") == "不适用":
            skipped += 1
            continue
        picked.append((x, q))
    _warm([x["id"] for x, _ in picked])
    print(f"摸底单 · {CHILD_NAME} · {a.subject} · {a.age}岁 · {len(picked)} 题（按地基重要度排序）")
    print("用法：挑一个安静时段，一次问完，把原话记下来。别提示、别纠正。\n")
    for i, (x, q) in enumerate(picked, 1):
        print(f"{i}. {q.get('name')}  [{x.get('ageRangeStart')}-{x.get('ageRangeEnd')}岁该会]")
        print(f"   问：{q['question']}")
        print(f"   追问：{q['probe']}")
        print(f"   看什么：{q['testing']}")
        print(f"   若这题不会，往回追：{', '.join(zh(p) for p in [e['prerequisiteId'] for e in PARENTS.get(x['id'],[])][:4])}")
        print()
    path = os.path.join(ROOT, "sessions", f"screen_{a.subject}_{a.age}.md")
    os.makedirs(os.path.join(ROOT, "sessions"), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 摸底单 · {CHILD_NAME} · {a.subject} · {a.age}岁\n\n")
        for i, (x, q) in enumerate(picked, 1):
            f.write(f"## {i}. {q.get('name')}（{x['id']}）\n")
            f.write(f"- 问：{q['question']}\n- 追问：{q['probe']}\n- 看什么：{q['testing']}\n- 孩子表现：\n\n")
    print(f"（可打印版已存：{path}）")

p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
for name in ("plan", "ask"):
    s = sub.add_parser(name); s.add_argument("--topic", required=True); s.add_argument("--depth", type=int, default=4)
s = sub.add_parser("judge"); s.add_argument("--topic", required=True); s.add_argument("--answer", required=True)
s = sub.add_parser("session"); s.add_argument("--topic", required=True); s.add_argument("--depth", type=int, default=4)
s.add_argument("--answers", help="非交互模式：用 || 分隔的多条家长转述（仅用于自动化测试）")
s = sub.add_parser("screen"); s.add_argument("--subject", default="Mathematics"); s.add_argument("--age", type=int, default=7); s.add_argument("--n", type=int, default=12)
s = sub.add_parser("report"); s.add_argument("--session", required=True)
a = p.parse_args()
{"plan": cmd_plan, "ask": cmd_ask, "judge": cmd_judge, "session": cmd_session, "screen": cmd_screen, "report": cmd_report}[a.cmd](a)
