from __future__ import annotations

import random

from . import enemies as enemies_mod

# 节点类型
ENCOUNTER = "encounter"
ELITE = "elite"
REST = "rest"
REWARD = "reward"
FORGE = "forge"
SHOP = "shop"
EVENT = "event"   # 奇遇节点（2.8.0：本地小奇遇或跨章奇遇链的一幕）
BOSS = "boss"

# 行数（从起点到首领的节点段数）
ROWS = 4
# 奇遇节点放置行：在这两行各放 1 个奇遇点（首幕在第 1 章、后续幕在第 2 章
# 可被确定性消费）。具体转换哪一列由 _pick_event_slots 按优先级确定性选择：
# 休息 > 锻造 > 奖励；兜底才转换一场普通遭遇。绝不覆盖商店（独特机制）与精英
# （高价值战斗）。因此除一个被转换节点外，其余类型、敌人与商店布局和历史
# 版本逐位一致。
EVENT_ROWS = (1, 2)
_EVENT_CONVERT_PRIORITY = (REST, FORGE, REWARD)
_EVENT_FALLBACK = ENCOUNTER
# 每条路线的列偏移；生成 ROWS 行，最后一行全指向 BOSS
NORMAL_POOL = ["goblin", "wolf", "brute", "maggot", "vampire", "echo_knight"]
ELITE_POOL = ["elite_warlord"]


def _pick_event_slots(raw):
    """在 EVENT_ROWS 各选一个确定性节点转换为奇遇槽。

    转换优先级固定（休息 > 锻造 > 奖励；不碰遭遇/精英/商店），同级按列序
    0、2、1（避开委托等既有路径最常依赖的中列 1）。整行三类都没有时退化为
    列 0（权重表下概率极低，仅为兜底）。选择只依赖已抽好的类型表，不消费
    主 RNG，因此是纯确定性的后处理。
    """
    slots = []
    for r in EVENT_ROWS:
        col = None
        for t in _EVENT_CONVERT_PRIORITY:
            for c in (0, 2, 1):
                if raw[(r, c)]["type"] == t:
                    col = c
                    break
            if col is not None:
                break
        if col is None:
            # 整行没有休息/锻造/奖励：兜底转换一场普通遭遇（仍不碰精英/商店）
            for c in (0, 2, 1):
                if raw[(r, c)]["type"] == _EVENT_FALLBACK:
                    col = c
                    break
        if col is None:
            # 兜底：优先转换普通遭遇，其次精英（二者都是战斗，路线强度影响最小）
            for t in (_EVENT_FALLBACK, ELITE):
                for c in (0, 2, 1):
                    if raw[(r, c)]["type"] == t:
                        col = c
                        break
                if col is not None:
                    break
        # 整行三个都是商店时 col=None：宁可不放奇遇点也不覆盖商店（该布局
        # 下商店本就可被绕过），返回 None 由生成方跳过。
        slots.append((r, col) if col is not None else None)
    return slots


def generate_map(seed, quest_events=True):
    """确定性生成地图。返回 map 结构与节点池。

    地图结构：{nodes: {nid: {...}}, routes: {nid: [next_ids]}, start, boss}
    三条路线在每一行给出一个三选一推进选择。

    quest_events=False 还原 2.8.0 之前的地图（无奇遇节点），仅供构造历史
    旧档/旧日志的测试夹具使用；在线开章与正常回放恒为 True（回放读的是
    持久化存档地图，本来就不会重生成布局）。
    """
    rng = random.Random((seed * 97 + 11) & 0xFFFFFFFF)
    nodes = {}
    routes = {}

    def nid(row, col):
        return f"{row}-{col}"

    start = "start"
    nodes[start] = {"id": start, "type": "start", "label": "营地", "row": -1}
    routes[start] = [nid(0, c) for c in range(3)]

    # 第一遍：按种子抽出每个节点的「原始类型」，完整消费 RNG（含敌人抽取），
    # 保证类型分布/敌人与历史版本完全一致。
    raw = {}
    for row in range(ROWS):
        for col in range(3):
            r = row
            # 行0 普通遭遇为主、不出商店（开局尚无金币）；越靠后越容易出精英/奖励/锻造/商店
            if r == 0:
                t = rng.choices([ENCOUNTER, REWARD, FORGE], [0.7, 0.2, 0.1])[0]
            elif r == 1:
                t = rng.choices([ENCOUNTER, REST, REWARD, FORGE, SHOP], [0.4, 0.2, 0.15, 0.1, 0.15])[0]
            elif r == 2:
                t = rng.choices([ENCOUNTER, ELITE, REWARD, FORGE, SHOP], [0.3, 0.25, 0.15, 0.15, 0.15])[0]
            else:
                t = rng.choices([ENCOUNTER, ELITE, REST, FORGE, SHOP], [0.3, 0.35, 0.15, 0.1, 0.1])[0]
            node = {"id": nid(row, col), "type": t, "row": r}
            if t in (ENCOUNTER, ELITE):
                pool = ELITE_POOL if t == ELITE else NORMAL_POOL
                node["enemy"] = rng.choice(pool)
            if t == ENCOUNTER and node["enemy"] == "echo_knight" and rng.random() < 0.5:
                node["enemy"] = "goblin"
            raw[(row, col)] = node

    # 第二遍：确定性挑选奇遇槽（常规只转换休息/锻造/奖励，极端布局兜底转换
    # 一场遭遇/精英；整行皆商店时该行不放奇遇点——绝不覆盖商店）。
    if quest_events:
        for slot in _pick_event_slots(raw):
            if slot is None:
                continue
            node = raw[slot]
            raw[slot] = {"id": node["id"], "type": EVENT, "row": node["row"]}

    for row in range(ROWS):
        for col in range(3):
            _id = nid(row, col)
            nodes[_id] = raw[(row, col)]
            if row < ROWS - 1:
                routes[_id] = [nid(row + 1, c) for c in range(3)]
            else:
                routes[_id] = ["boss"]

    boss = "boss"
    # 首领属性由 map 层携带；seed 亦用于重放
    nodes[boss] = {"id": boss, "type": BOSS, "row": ROWS, "enemy": "boss_ancient", "label": "远古守卫"}
    routes["boss"] = []

    return {
        "seed": seed, "start": start, "boss": boss, "rows": ROWS,
        "nodes": nodes, "routes": routes,
    }


def route_candidates(map_data, position):
    return list(map_data["routes"].get(position, []))


def next_from(map_data, position):
    """玩家在 start / 中间节点选定下一节点。合并三选一实际只有一条继续。"""
    cands = route_candidates(map_data, position)
    return cands[0] if cands else None