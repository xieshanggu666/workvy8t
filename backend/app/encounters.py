"""跨章节奇遇链（规则 2.8.0）。

玩家在路线上的「奇遇」节点面对抉择并**立即承担代价**（生命/金币）；选择写入
跨章继承的奇遇状态，后续章节按选择触发不同的遭遇（伏击战斗）或奖励（牌组/
遗物/金币/生命）。

确定性：某节点触发哪条链的哪一幕，只由「链定义 + 已开放链 + 本章已访问奇遇
节点数」决定（见 resolve_event）；本地小奇遇由节点 id 确定性选取。因此
进入/抉择/伏击/领奖全部是动作序列的纯函数——续局与版本化回放逐位一致。

状态（run 状态字段，随交接快照跨章继承）：
- quests.chains：已开放的跨章链，每项
  {key, chain, chapter, step, choice, status: open|resolved|closed,
   offered_step, ambush: None|{enemy, win:[effects]}, log: [轨迹]}
- quest_event：当前节点待抉择/待打的奇遇幕（离开/结算后清空）

status：
- open     链尚未走到终幕（等待后续章节触发）
- resolved 终幕已结算（奖励/伏击胜利）
- closed   玩家选了直接结束链的分支（如「忽视」）
"""
from __future__ import annotations

import random

EVENT = "event"            # 地图节点类型：奇遇
QUEST_RULES_VERSION = "2.8.0"

OPEN = "open"
RESOLVED = "resolved"
CLOSED = "closed"

# 幕类型
CHOICE = "choice"          # 抉择幕：进入即待选择，quest_choose 结算
AMBUSH = "ambush"          # 伏击幕：进入即开战，胜利后结算 win 效果
REWARD = "reward"          # 奖励幕：进入即自动结算（无可选项）

# 选项可承担的代价类型（立即承担：HP 保底 1 点生命）
COST_HP = "hp"
COST_GOLD = "gold"


class QuestError(ValueError):
    """奇遇动作非法（非抉择节点/重复抉择/未知选项/付不起代价）。"""


# ---------- 链定义 ----------
# 每条链：
#   key/name/icon/desc
#   start_chapter：首幕最早出现章（默认 1）
#   steps：有序的幕；第 0 幕在 start_chapter 开放，玩家选择后按
#          option.next 跳到后续幕（next=None 表示该选项直接终结链）。
#   后续幕声明 chapter（相对开放章的章数要求：at_chapter = open_chapter + delta）
#          与 need（必须命中的选择标记，None 表示任意选择都可触发）。
CHAINS: dict[str, dict] = {}


def _option(text, effects=(), costs=(), next=None, mark=None, hint=None):
    return {
        "text": text,
        "effects": list(effects),
        "costs": list(costs),
        "next": next,      # 选中后跳转的幕 id；None = 链立即结束（closed）
        "mark": mark,      # 记录到 chain.choice 上的分支标记（后续幕 need 判定）
        "hint": hint,      # 代价/后果的人类可读提示（前端渲染）
    }


def _step(sid, kind, chapter, title, text, options=(), need=None,
          enemy=None, win=(), lose_mark=None, ambush_hint=None):
    return {
        "id": sid, "kind": kind, "chapter": chapter, "need": need,
        "title": title, "text": text, "options": list(options),
        "ambush": {"enemy": enemy, "win": list(win), "hint": ambush_hint,
                   "lose_mark": lose_mark} if enemy else None,
    }


def _chain(key, name, icon, desc, steps, start_chapter=1):
    CHAINS[key] = {
        "key": key, "name": name, "icon": icon, "desc": desc,
        "start_chapter": start_chapter, "steps": steps,
    }


# 链 1：游方僧人——第 1 章三选一，帮助/行劫在第 2 章各有报应
_chain(
    "wandering_monk", "游方僧人", "🧑‍🦲",
    "路遇负伤的游方僧人，你的抉择将在下一章引来不同的遭遇。",
    [
        _step(
            "monk_meet", CHOICE, 0, "游方僧人",
            "路边的古树下，一名负伤的游方僧人向你乞食，他说自己正赶往邻城超度亡魂。",
            options=[
                _option("分出干粮并为他包扎（-8 生命）",
                        costs=[{COST_HP: 8}], next="monk_help", mark="help",
                        hint="付出 8 点生命；僧人承诺在第 2 章相报"),
                _option("夺下他的钱袋（+25 金币）",
                        effects=[{"type": "gold", "value": 25}],
                        next="monk_rob", mark="rob",
                        hint="立即获得 25 金币，但僧人的同伴不会善罢甘休"),
                _option("装作没看见，径直离开",
                        next=None, mark="ignore",
                        hint="奇遇到此结束"),
            ]),
        _step(
            "monk_help", REWARD, 1, "僧人的报答",
            "行至下一章，一队僧人在道旁设粥棚等候——正是你救过的那位老僧的同门。"
            "他们执意将一部护体心经与盘缠相赠。",
            need="help",
            options=[
                _option("接受谢礼",
                        effects=[{"type": "add_card", "card": "iron_wave"},
                                 {"type": "gold", "value": 35}],
                        next=None),
            ]),
        _step(
            "monk_rob", AMBUSH, 1, "护寺武僧",
            "山隘处三名护寺武僧拦住去路：「劫我师弟财物者，留下买路钱！」",
            need="rob",
            enemy="ambush_guardian",
            win=[{"type": "add_card", "card": "heavy_blow"},
                 {"type": "gold", "value": 30}],
            ambush_hint="击败护寺武僧：获得卡牌「重击破」与 30 金币；战败则远征终结"),
    ],
)

# 链 2：古老祭坛——第 1 章献血/献祭卡牌，第 2 章祭坛苏醒给奖或索债
_chain(
    "ancient_altar", "古老祭坛", "🗿",
    "荒原上的滴血祭坛，献血可换现世福报，渎神之举则要小心来日。",
    [
        _step(
            "altar_meet", CHOICE, 0, "古老祭坛",
            "荒原中央立着一座裂纹遍布的黑石祭坛，凹槽中残留着褐色血迹。",
            options=[
                _option("献上 10 点鲜血（-10 生命）",
                        costs=[{COST_HP: 10}], next="altar_blood", mark="blood",
                        hint="付出 10 点生命；祭坛许诺下一章的馈赠"),
                _option("往祭坛上撒尿取乐",
                        next="altar_defile", mark="defile",
                        hint="什么也没发生……至少现在没有"),
                _option("绕开祭坛",
                        next=None, mark="avoid",
                        hint="奇遇到此结束"),
            ]),
        _step(
            "altar_blood", REWARD, 1, "祭坛的馈赠",
            "再遇祭坛时，石面的裂纹正透出暗红色的光，一枚温热的石符与一卷武技"
            "残页浮在凹槽之上。",
            need="blood",
            options=[
                _option("取走馈赠",
                        effects=[{"type": "relic_set", "relic": "power_up", "value": 1},
                                 {"type": "add_card", "card": "battle_trance"}],
                        next=None),
            ]),
        _step(
            "altar_defile", AMBUSH, 1, "祭坛守卫",
            "祭坛方向传来石块摩擦的轰鸣——被亵渎的石像拔地而起，要你血债血偿。",
            need="defile",
            enemy="ambush_idol",
            win=[{"type": "relic_set", "relic": "power_up", "value": 1}],
            ambush_hint="击败苏醒的石像：获得遗物「碎像核心」（本局增伤）；战败则远征终结"),
    ],
)

# 链 3：迷路商队——第 1 章买消息或打劫，第 2 章兑现宝藏或遭伏击
_chain(
    "lost_caravan", "迷路商队", "🛻",
    "商队护卫说前方岔路通往宝藏，也可能通往埋伏。",
    [
        _step(
            "caravan_meet", CHOICE, 0, "迷路商队",
            "一支偏离商路的马车陷在泥里。护卫队长压低声音：给点辛苦费，就把附近"
            "一处藏宝点的位置告诉你。",
            options=[
                _option("买下藏宝图（-30 金币）",
                        costs=[{COST_GOLD: 30}], next="caravan_map", mark="map",
                        hint="付出 30 金币；下一章按图寻宝"),
                _option("趁火打劫（+20 金币）",
                        effects=[{"type": "gold", "value": 20}],
                        next="caravan_rob", mark="rob",
                        hint="立即获得 20 金币，但商队的报复就在下一章"),
                _option("拒绝并离开",
                        next=None, mark="leave",
                        hint="奇遇到此结束"),
            ]),
        _step(
            "caravan_map", REWARD, 1, "藏宝点",
            "按图索骥，你在一处坍塌的路标下挖出了商队藏匿的铁箱。",
            need="map",
            options=[
                _option("打开铁箱",
                        effects=[{"type": "gold", "value": 60},
                                 {"type": "add_card", "card": "swift"}],
                        next=None),
            ]),
        _step(
            "caravan_rob", AMBUSH, 1, "商队伏兵",
            "山道两侧杀出一队商队雇佣的弩手——他们等你整整一章了。",
            need="rob",
            enemy="ambush_bandit",
            win=[{"type": "gold", "value": 45}],
            ambush_hint="击退伏兵：夺下 45 金币；战败则远征终结"),
    ],
)

CHAIN_ORDER = ["wandering_monk", "ancient_altar", "lost_caravan"]

# ---------- 本地小奇遇（不跨章，选完即了；确定性按节点选取） ----------
LOCAL_EVENTS = [
    {
        "key": "local_spring",
        "title": "治疗泉水",
        "text": "一汪冒着热气的清泉从岩缝中涌出，水面泛着微光。",
        "options": [
            _option("饮水休整（回复 12 点生命）",
                    effects=[{"type": "heal_run", "value": 12}], hint="回复 12 点生命"),
            _option("灌满水袋后离开（+10 金币）",
                    effects=[{"type": "gold", "value": 10}], hint="获得 10 金币"),
            _option("不做停留", hint="什么也不发生"),
        ],
    },
    {
        "key": "local_cache",
        "title": "遗弃的行囊",
        "text": "路边有一只被野兽撕开的行囊，里面似乎还剩些值钱的东西。",
        "options": [
            _option("翻找值钱物件（+20 金币）",
                    effects=[{"type": "gold", "value": 20}], hint="获得 20 金币"),
            _option("检查夹层（-5 生命，获得卡牌）",
                    costs=[{COST_HP: 5}],
                    effects=[{"type": "add_card", "card": "pommel"}],
                    hint="被机关划伤，付出 5 点生命，获得卡牌「柄击」"),
            _option("不去碰它", hint="什么也不发生"),
        ],
    },
    {
        "key": "local_shrine",
        "title": "路边神龛",
        "text": "一座不起眼的小石龛，香炉里的香还燃着。",
        "options": [
            _option("捐 15 金币祈福",
                    costs=[{COST_GOLD: 15}],
                    effects=[{"type": "heal_run", "value": 10}],
                    hint="付出 15 金币，回复 10 点生命"),
            _option("闭目祈祷后离开",
                    effects=[{"type": "heal_run", "value": 4}], hint="回复 4 点生命"),
            _option("扬长而去", hint="什么也不发生"),
        ],
    },
]


# ---------- 初始化 / 迁移 ----------
def new_quest_state():
    # event_visits 为历史计数（旧版按访问序消费候选时使用）；现行调度只依赖
    # 链账本（offered_step 占位），该键仅为旧档结构兼容保留，不再读写语义。
    return {"chains": [], "event_visits": {}}


def normalize_state(quests):
    """旧档兜底：补齐奇遇字段（幂等）。返回 (规范化状态, 是否发生结构变化)。"""
    changed = False
    if not isinstance(quests, dict):
        return new_quest_state(), True
    if "chains" not in quests:
        quests["chains"] = []
        changed = True
    if "event_visits" not in quests or not isinstance(quests.get("event_visits"), dict):
        quests["event_visits"] = {}
        changed = True
    for ch in quests["chains"]:
        if "log" not in ch:
            ch["log"] = []
            changed = True
        ch.setdefault("ambush", None)
        ch.setdefault("choice", None)
        ch.setdefault("offered_step", None)
    return quests, changed


# ---------- 节点 -> 奇遇幕 ----------
def chain_def(key):
    return CHAINS.get(key)


def step_def(chain_key, step_id):
    c = CHAINS.get(chain_key)
    if not c:
        return None
    return next((s for s in c["steps"] if s["id"] == step_id), None)


def _due_follow(rec, chapter):
    """已开放且已抉择的链在本章当前槽位是否到期。

    step.chapter 是「相对开放章的章数」：首幕定义为开放章（delta=0，抉择前
    choice=None 已在前面挡掉），终幕 delta=1（开放章的下一章）。offered_step
    保证同幕只在一个节点呈现（每个槽位独立匹配，呈现过即占位，不重复触发）。
    """
    if rec["status"] != OPEN or rec["choice"] is None:
        return None
    if rec.get("offered_step") == rec["step"]:
        return None
    step = step_def(rec["chain"], rec["step"])
    if step is None:
        return None
    if chapter != rec["chapter"] + step["chapter"]:
        return None
    if step["need"] is not None and rec["choice"] != step["need"]:
        return None
    return step


def _first_step_due(key, chapter, open_keys):
    """链首幕是否在本章开放（尚未开放且到了 start_chapter）。"""
    if key in open_keys:
        return None
    c = CHAINS[key]
    if chapter < c["start_chapter"]:
        return None
    return c["steps"][0]


def local_event_for(node_id):
    """节点 id -> 本地小奇遇定义（确定性，不依赖全局 RNG）。"""
    rng = random.Random((sum(node_id.encode("utf-8")) * 7919 + 13) & 0xFFFFFFFF)
    return LOCAL_EVENTS[rng.randrange(len(LOCAL_EVENTS))]


def resolve_event(quests, chapter, node_id):
    """进入一个奇遇节点时决定本节点的奇遇幕（纯函数）。

    每个 (章, 槽位序) 的解析互相独立、只依赖已积累的链账本，因此玩家先访问
    哪个奇遇节点是确定性输入，同种子重放必得同一分配：
    1. 按链定义序，匹配已开放且在本章到期、分支标记满足 need 的后续幕
       （已选「离开/closed」或分支不满足的链自然跳过）；
    2. 否则按定义序开放一条到了 start_chapter 的新链首幕；
    3. 都没有则生成本地小奇遇。
    返回 (event_dict, chain_record_or_None)。
    """
    chains = quests.setdefault("chains", [])
    by_key = {c["chain"]: c for c in chains}
    open_keys = set(by_key)

    # 1. 已开放链的到期后续幕（定义序）
    for key in CHAIN_ORDER:
        rec = by_key.get(key)
        if rec is None:
            continue
        step = _due_follow(rec, chapter)
        if step is not None:
            rec["offered_step"] = step["id"]
            return _event_public(rec, step), rec

    # 2. 开放新链首幕（定义序）
    for key in CHAIN_ORDER:
        step = _first_step_due(key, chapter, open_keys)
        if step is not None:
            cdef = CHAINS[key]
            rec = {
                "key": key, "chain": key, "chapter": chapter,
                "step": step["id"], "choice": None, "status": OPEN,
                "ambush": None, "offered_step": step["id"],
                "log": [{"chapter": chapter, "step": step["id"]}],
            }
            chains.append(rec)
            return _event_public(rec, step), rec

    # 3. 本地小奇遇
    ev = local_event_for(node_id)
    return {
        "scope": "local", "key": ev["key"], "kind": CHOICE,
        "title": ev["title"], "text": ev["text"],
        "options": [_option_public(o, i) for i, o in enumerate(ev["options"])],
    }, None


def _option_public(o, idx):
    return {
        "index": idx, "text": o["text"], "hint": o.get("hint"),
        "costs": list(o.get("costs", [])),
        "effects": list(o.get("effects", [])),
    }


def _event_public(rec, step):
    """待处理奇遇幕的公开形态（供前端渲染抉择/伏击/奖励）。"""
    ev = {
        "scope": "chain", "chain": rec["chain"], "key": rec["chain"],
        "step": step["id"], "kind": step["kind"],
        "title": step["title"], "text": step["text"],
        "name": CHAINS[rec["chain"]]["name"],
        "icon": CHAINS[rec["chain"]]["icon"],
        "options": [_option_public(o, i) for i, o in enumerate(step.get("options", []))],
    }
    if step.get("ambush"):
        ev["ambush"] = {
            "enemy": step["ambush"]["enemy"],
            "hint": step["ambush"].get("hint"),
        }
    return ev


# ---------- 抉择结算 ----------
def can_pay(run, option):
    """校验玩家付不付得起代价（先于一切状态翻转，失败零副作用）。"""
    for cost in option.get("costs", []):
        if COST_GOLD in cost and run.get("gold", 0) < cost[COST_GOLD]:
            return False
        if COST_HP in cost and run.get("health", 1) <= cost[COST_HP]:
            # 代价不致命：至少保留 1 点生命
            return False
    return True


def apply_costs(run, option):
    """扣除抉择代价（HP 代价保底 1 点生命）。返回代价描述供日志。"""
    paid = []
    for cost in option.get("costs", []):
        if COST_GOLD in cost:
            run["gold"] -= cost[COST_GOLD]
            paid.append({"gold": cost[COST_GOLD]})
        if COST_HP in cost:
            run["health"] = max(1, run["health"] - cost[COST_HP])
            paid.append({"hp": cost[COST_HP]})
    return paid


def choose(quests, run, option_idx):
    """对当前待抉择奇遇幕执行选择（纯推演辅助：不改 run 上的费用/效果以外字段，
    效果由 service 统一经 _apply_option_effect 回写牌组/遗物/生命）。

    返回 (event, chain_rec, option_def, paid)。调用方负责应用 option.effects
    与推进链状态（见 service._quest_choose）。
    """
    ev = run.get("quest_event")
    if not ev or ev.get("kind") != CHOICE:
        raise QuestError("no quest choice pending")
    options = _pending_options(quests, ev)
    if not isinstance(option_idx, int) or isinstance(option_idx, bool) \
            or not (0 <= option_idx < len(options)):
        raise QuestError("unknown quest option")
    o = options[option_idx]
    if not can_pay(run, o):
        raise QuestError("cannot pay the choice cost")
    paid = apply_costs(run, o)
    rec = next((c for c in quests.get("chains", [])
                if c["chain"] == ev.get("chain") and c["step"] == ev.get("step")), None)
    return ev, rec, o, paid


def _pending_options(quests, ev):
    """当前抉择幕的权威选项定义（链幕取定义；本地幕取本地表）。"""
    if ev.get("scope") == "local":
        le = next((e for e in LOCAL_EVENTS if e["key"] == ev["key"]), None)
        return le["options"] if le else []
    step = step_def(ev["chain"], ev["step"])
    return step.get("options", []) if step else []


def advance_chain(rec, option):
    """抉择后推进链状态：跳转下一幕（open）或终结（closed）。返回轨迹记录。"""
    nxt = option.get("next")
    rec["choice"] = option.get("mark")
    if nxt is None:
        rec["status"] = CLOSED
        rec["offered_step"] = None
    else:
        rec["step"] = nxt
        rec["status"] = OPEN
        # 新幕尚未在任何节点呈现过；由后续章节的 resolve_event 重新占位
        rec["offered_step"] = None
    return {"step": rec["step"], "choice": rec["choice"], "status": rec["status"]}


def mark_visited(quests, chapter):
    """记录本章奇遇节点访问序（决定同章多候选的确定性消费顺序）。"""
    key = str(chapter)
    quests.setdefault("event_visits", {})[key] = quests["event_visits"].get(key, 0) + 1


# ---------- 伏击 ----------
def ambush_for(ev):
    """伏击幕 -> 敌人 id 与胜利效果；非伏击幕返回 None。"""
    if ev.get("kind") != AMBUSH:
        return None
    step = step_def(ev["chain"], ev["step"])
    return step["ambush"] if step else None


def resolve_ambush(rec):
    """伏击胜利：链标记 resolved（终幕）。返回胜利效果列表。"""
    step = step_def(rec["chain"], rec["step"])
    rec["status"] = RESOLVED
    rec["choice"] = "ambush_won"
    rec["ambush"] = None
    return list(step["ambush"]["win"]) if step and step.get("ambush") else []


def resolve_reward(rec):
    """奖励幕自动领取：链标记 resolved（终幕，无分支选择）。"""
    rec["status"] = RESOLVED
    rec["ambush"] = None


# ---------- 只读视口 ----------
def chains_public(quests, chapter):
    """侧栏奇遇链追踪：已开放链的当前状态与后续预告。"""
    out = []
    for rec in quests.get("chains", []):
        cdef = CHAINS.get(rec["chain"])
        if not cdef:
            continue
        step = step_def(rec["chain"], rec["step"])
        status = rec["status"]
        if status == OPEN and step is not None:
            if rec.get("choice") is None:
                waiting = "待抉择"
            else:
                at_chapter = rec["chapter"] + step["chapter"]
                waiting = f"第 {at_chapter} 章继续寻找踪迹"
        elif status == RESOLVED:
            waiting = "已了结（获得回报）"
        else:
            waiting = "已了结"
        out.append({
            "key": rec["chain"], "name": cdef["name"], "icon": cdef["icon"],
            "desc": cdef["desc"], "status": status, "choice": rec["choice"],
            "opened_chapter": rec["chapter"], "waiting": waiting,
            "log": list(rec.get("log", [])),
        })
    return out


def quest_catalog():
    """元数据（供前端/调试）：链定义摘要。"""
    return [{"key": c["key"], "name": c["name"], "icon": c["icon"], "desc": c["desc"]}
            for c in (CHAINS[k] for k in CHAIN_ORDER)]
