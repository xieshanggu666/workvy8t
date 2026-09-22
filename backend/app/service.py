from __future__ import annotations

import copy
import hashlib
import json
import random
import uuid

from . import db
from . import enemies as enemies_mod
from . import mapgen
from . import rewards as rewards_mod
from . import forging as forging_mod
from . import shop as shop_mod
from . import commissions as commission_mod
from . import potions as potions_mod
from . import companions as companions_mod
from .cards import all_cards, get_card
from .engine import Battle, _statuses_public
from .forging import FORGE_COST, effective_card, node_name, growth_node_cost, validate_unlock
from .settlement import EffectEvent, SettlementQueue

# 规则版本：引擎/结算/存档结构发生语义变化时递增。
# 建局写入 run 状态、每个动作事件携带 ver；回放据此标记录制版本与旧日志兼容。
# 2.1.0：多章远征（章节 run 的 create 事件可携带交接快照 carry，回放据此重建初始状态）。
# 2.2.0：远征委托（commissions/chapter/chapters_total 进入 run 状态与交接快照）。
# 2.3.0：卡牌成长树（带前置条件与互斥分支的 DAG 取代三分支可重复锻造；
#        实例 forges:[分支] -> growth:[{node,cost}]；旧档与旧日志确定性迁移）。
# 2.4.0：修复跨章交接的章号归属 bug——交接快照 carry 里携带的 chapter 是「来源章」，
#        开新章时必须以显式入参（新章号）为准；旧实现误用 carry 的章号，导致第 2 章
#        及以后的 run 状态章号恒为 1，委托挂单 deadline、剩余期限、领奖记录全部错乱。
#        受影响的旧存档首次载入时按权威 runs.chapter 列修复（委托章号/期限/领奖位置/
#        被误判超期的进行中委托一并校正）；旧日志回放走兼容修复（修复前步骤按 legacy
#        跳过逐位哈希比对，最终状态与在线修复后的存档一致）。
# 2.5.0：跨章药水背包——potions/next_potion_seq 进入 run 状态与交接快照；
#        商店与战斗战利品产出药水（限容量 POTION_CAPACITY，背满购买/领取需指定
#        替换格），战斗中玩家回合可使用（消耗与战斗效果同一动作原子结算，
#        死亡打断/胜负/领奖/解锁与原战斗步骤同路径），非战斗可主动丢弃。
#        旧档无该字段，首次载入时 setdefault 空背包（结构迁移，本步按 legacy 处理）。
# 2.6.0：伙伴模块——companion 进入 run 状态与交接快照；商店限招一名，可随行/休整；
#        随行伙伴按回合协助战斗并援护玩家，生命归零后负伤暂停参战，休整伙伴在
#        休息节点治疗；招募扣款、战斗伤害与治疗全部复用普通动作和统一结算队列。
# 2.6.1：修复存档升级衔接——回放识别旧形状 create 校验点后，规则（药水 2.5.0 /
#        伙伴 2.6.0 / 2.4 章号修复点）必须在迁移点那个动作【之前】切换。旧实现
#        在动作推演之后才切换，导致升级后首个「进入新商店」动作仍按旧规则生成
#        库存（缺药水/伙伴货架），随后的购买无法重演（unknown shop item），
#        实玩与回放库存分叉；旧战斗战利品在 2.5.0 追加的药水选项也会让旧
#        claim_reward 下标错位。迁移点动作现按在线语义逐位重建（resume 静默
#        迁移时严格校验，act 迁移步按 legacy 豁免但规则已先行切换），历史交易
#        与升级前动作按 legacy 保留并可正常重演。
# 2.7.0：修复格挡/援护结算顺序——玩家格挡此前在 end_turn 敌人行动【之前】就被
#        清零，卡牌（防御等）与壁垒药水给的格挡完全无法抵挡敌方伤害；伙伴援护
#        又按全额伤害判穿（应只吃格挡吸收后的穿透溢出），导致格挡被直接绕过。
#        修复后顺序为：敌人攻击 -> 格挡吸收 -> 溢出由伙伴援护 -> 剩余扣血；
#        格挡持续整个敌方行动段、在回合末（下一回合 start_turn）清零。纯规则
#        修复、无存档结构变更：战斗中存档在玩家回合边界落库，在线升级载入即按
#        新规则；回放 2.7.0 之前日志时对越过修复点之前的战斗动作启用旧时序
#        （legacy_block：敌人行动前清格挡 + 按全额援护），历史动作逐位可重演，
#        跨越修复点后严格校验，续局与回放不分叉。
RULES_VERSION = "2.7.0"
GROWTH_RULES_VERSION = "2.3.0"  # 成长树规则起始版本：更早的 forge 日志走兼容重演
COMPANION_RULES_VERSION = "2.6.0"  # 伙伴字段进入 run 状态：更早日志的迁移步前按 legacy 比对
BLOCK_RULES_VERSION = "2.7.0"  # 格挡/援护结算顺序修复：更早日志的战斗动作走旧时序重演


def _ver_lt(ver, baseline):
    """简单语义版本比较；ver 为空/损坏时视为 True（按旧日志处理）。"""
    try:
        return tuple(int(x) for x in str(ver).split(".")[:3]) < \
               tuple(int(x) for x in baseline.split(".")[:3])
    except (ValueError, AttributeError):
        return True

# 初始牌组：卡牌 id 列表；建局时展开为独立实例（同名卡各持一份成长状态）
START_DECK = ["strike", "strike", "strike", "strike", "guard", "guard", "guard"]
INIT_LOCKED = ["heavy_blow", "cleave", "shield_bash", "pommel", "battle_trance",
               "flex", "iron_wave", "flurry", "adrenaline", "swift", "blood_echo",
               "reckless", "demon_form", "sword_dance"]

# ---------- 多章远征 ----------
DEFAULT_CHAPTERS = 3          # 默认章节数
MAX_CHAPTERS = 9              # 单次远征章节上限
CHAPTER_CLEAR_HEAL_RATIO = 0.25  # 章节交接休整：进入下一章时回复 max_health 的 25%


class DuplicateReward(Exception):
    pass


class InvalidAction(Exception):
    pass


class ShopSoldOut(Exception):
    """商店货架项已售出（重复购买/重复移除同一卡牌实例）。"""
    pass


def _make_instances(ids):
    """把卡牌 id 列表展开为 (uid 列表, 实例表, 下一序号)。"""
    uids, instances = [], {}
    for cid in ids:
        uid = f"c{len(uids) + 1}"
        uids.append(uid)
        instances[uid] = {"id": cid, "growth": []}
    return uids, instances


def _new_instance(cid):
    """新卡牌实例：独立成长树状态（已解锁节点记录为空）。"""
    return {"id": cid, "growth": []}


def _new_run_state(seed, carry=None, chapter=None, chapters_total=None, expedition_id=None):
    """构造初始 run 状态。

    carry 非 None（远征章节 run）时以交接快照为起点：牌组（含锻造成长）、遗物、
    金币、生命/能量上限、远征委托全部带入新章，并按 CHAPTER_CLEAR_HEAL_RATIO
    休整回血；进入新章时超期委托在调用方推进（advance）处理。
    chapter/chapters_total/expedition_id 为新章 run 的归属信息（普通局为 None）——
    这是新 run 自身的身份，必须以显式入参为准；carry 里同名字段是「交接快照来源章」
    的记录，只用于历史快照，绝不能覆盖新章号（否则第 2 章及以后的 run 章号恒为 1，
    委托挂单 deadline/剩余期限/领奖记录全部错乱）。
    同一组入参必得同一初始状态——在线开章与回放重建共用本函数，天然一致。
    """
    if carry is None:
        deck_uids, instances = _make_instances(START_DECK)
        carry = {
            "deck": deck_uids, "card_instances": instances,
            "next_card_seq": len(deck_uids) + 1,
            "relics": {}, "gold": 0,
            "max_health": 75, "health": 75, "base_energy": 3,
            "potions": [],
            "companion": None,
        }
        heal = 0
    else:
        heal = max(1, int(carry.get("max_health", 75) * CHAPTER_CLEAR_HEAL_RATIO))
    instances = copy.deepcopy(carry["card_instances"])
    # 兼容旧章交接快照（2.3.0 之前的 carry 里可能是 forges 结构）
    _normalize_instances(instances)
    max_hp = carry.get("max_health", 75)
    return {
        "seed": seed,
        "rules_version": RULES_VERSION,
        "status": "in_progress",
        "position": "start",
        "max_health": max_hp,
        "health": min(max_hp, max(1, carry.get("health", max_hp)) + heal),
        "base_energy": carry.get("base_energy", 3),
        "deck": list(carry["deck"]),      # 手牌引用（uid）
        "card_instances": instances,      # uid -> {id, growth:[{node,cost}]}
        "next_card_seq": carry.get("next_card_seq", len(instances) + 1),  # uid 单调发号器
        "gold": carry.get("gold", 0),
        "relics": dict(carry.get("relics", {})),
        # 药水背包：按下标排列的药水 id 列表（限容量，可跨章携带）；旧章快照缺省为空
        "potions": list(carry.get("potions", [])),
        # 伙伴：持久招募状态（id/mode/hp/wounded），随行/负伤随快照跨章继承
        "companion": copy.deepcopy(carry.get("companion")),
        # 远征委托：uid 单调发号器 + 委托实例（接取/进度/领奖/超期/战败失败）
        "next_commission_seq": carry.get("next_commission_seq", 1),
        "commissions": copy.deepcopy(carry.get("commissions", [])),
        # 远征归属（普通局均为 None）：新章 run 的身份只认真实入参；carry 的
        # chapter/chapters_total 是来源章快照，不参与新 run 的身份判定。
        "expedition_id": expedition_id,
        "chapter": chapter if chapter is not None else carry.get("chapter"),
        "chapters_total": (chapters_total if chapters_total is not None
                           else carry.get("chapters_total")),
        "in_battle": False,
        "battle_index": 0,
        "battle": None,
        "reward_options": [],
        "reward_claimed": True,
        "forge_claimed": True,        # 当前节点是否已完成锻造
        "shop": None,                 # 商店节点库存与交易记录（离开节点即清空）
        "events_log": [],
        "truncated": False,
    }


def _normalize_instances(instances):
    """把任意时期的卡牌实例表就地规范化为成长树结构（幂等）。

    - 旧档 forges:[分支id...] -> growth:[{node,cost}]（确定性默认链迁移）；
    - 缺失 growth 的实例补空列表；记录项缺 cost 按节点现价补（损坏档兜底）。
    返回是否发生结构变化。
    """
    changed = False
    for inst in instances.values():
        if "growth" not in inst:
            records, _did = forging_mod.migrate_forges_to_growth(inst.get("forges", []))
            inst["growth"] = records
            inst.pop("forges", None)
            changed = True
        else:
            for r in inst["growth"]:
                if isinstance(r, dict) and "cost" not in r:
                    r["cost"] = growth_node_cost(r.get("node")) or FORGE_COST
                    changed = True
    return changed


def _migrate_state(run):
    """旧档兼容：把裸 id 牌组升级为卡牌实例（同名卡获得独立 uid 与成长状态），
    并把旧版 forges 强化记录迁移为成长树 growth 节点序列。

    旧档可能在战斗中：牌堆/手牌/弃牌堆里仍是裸 id，此时按牌组顺序发号 uid，
    再把牌堆中的每次出现映射到该 id 的 uid 队列，洗牌布局与确定性保持不变。
    """
    changed = False
    if "card_instances" not in run:
        instances = {}
        deck_uids = []
        free_uids = {}  # card_id -> 尚未分配到牌堆的 uid 队列
        for cid in run.get("deck", []):
            uid = f"c{len(instances) + 1}"
            instances[uid] = _new_instance(cid)
            deck_uids.append(uid)
            free_uids.setdefault(cid, []).append(uid)

        b = run.get("battle")
        if b:
            for pile in ("draw_pile", "hand", "discard"):
                mapped = []
                for ref in b.get(pile, []):
                    if not isinstance(ref, str) or ref not in free_uids:
                        mapped.append(ref)
                        continue
                    queue = free_uids[ref]
                    mapped.append(queue.pop(0) if queue else ref)
                b[pile] = mapped
            b["card_instances"] = instances
        run["deck"] = deck_uids
        run["card_instances"] = instances
        run["next_card_seq"] = len(instances) + 1
        changed = True
    # 2.3.0：成长树迁移（旧 forges 记录 -> growth 节点序列）
    if _normalize_instances(run["card_instances"]):
        changed = True
    b = run.get("battle")
    if b and isinstance(b.get("card_instances"), dict):
        # 战斗内快照实例表与 run 级表一致迁移（旧档迁移时两者是同一 dict 的拷贝）
        _normalize_instances(b["card_instances"])
    # 旧档补记当前规则版本（仅标注；旧动作日志仍按 legacy 处理不做哈希校验）
    if "rules_version" not in run:
        run["rules_version"] = RULES_VERSION
        changed = True
    run.setdefault("forge_claimed", True)
    run.setdefault("shop", None)
    # 2.2.0：远征委托相关字段（旧档/老普通局无此字段）
    run.setdefault("commissions", [])
    run.setdefault("next_commission_seq", 1)
    run.setdefault("chapter", None)
    run.setdefault("chapters_total", None)
    run.setdefault("expedition_id", None)
    # 2.5.0：跨章药水背包（旧档无此字段，空背包开局；结构变化随本步原子落库）
    if "potions" not in run:
        run["potions"] = []
        changed = True
    companion, companion_changed = companions_mod.normalize_state(run.get("companion"))
    if "companion" not in run:
        run["companion"] = None
        changed = True
    elif companion_changed:
        run["companion"] = companion
        changed = True
    return changed


def get_profile_unlocked():
    prof = db.get_profile()
    if prof is None:
        return {"unlocked": list(START_DECK), "locked": list(INIT_LOCKED)}
    return {"unlocked": list(prof.get("unlocked", START_DECK)),
            "locked": list(prof.get("locked", INIT_LOCKED))}


def create_run(seed=None):
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    run_id = uuid.uuid4().hex[:12]
    state = _new_run_state(seed)
    map_data = mapgen.generate_map(seed)
    # 建局：存档与首条动作日志在同一事务，任何写入失败都不会留下“无日志的局”
    with db.transaction() as conn:
        db.insert_run(conn, run_id, state["seed"], state["status"], state["position"], map_data, state)
        db.append_event_conn(conn, run_id, 1, "create", {
            "seed": state["seed"], "ver": RULES_VERSION, "ckpt": state_checkpoint(state),
        })
    return _public_view(state, map_data, run_id, rev=1)


def load_run(run_id):
    return db.load_run(run_id)


# ---------- 多章远征 ----------
def _chapter_seed(exp_seed, chapter):
    """远征种子 -> 第 N 章种子（确定性派生；各章地图/洗牌独立但可复现）。"""
    return (exp_seed * 131 + (chapter - 1) * 7919) & 0x7FFFFFFF


def _carry_from_run(run):
    """章节通关后的「奖励交接」快照：牌组（含锻造成长）、遗物、金币、生命与能量上限，
    以及远征委托（含进度/可领奖/已终结状态）与委托 uid 发号器。"""
    instances = run.get("card_instances", {})
    return {
        "deck": list(run["deck"]),
        "card_instances": copy.deepcopy(instances),
        "next_card_seq": run.get("next_card_seq", len(instances) + 1),
        "relics": dict(run["relics"]),
        "gold": run["gold"],
        "max_health": run["max_health"],
        "health": run["health"],
        "base_energy": run.get("base_energy", 3),
        "potions": list(run.get("potions", [])),
        "companion": copy.deepcopy(run.get("companion")),
        "commissions": copy.deepcopy(run.get("commissions", [])),
        "next_commission_seq": run.get("next_commission_seq", 1),
        "chapter": run.get("chapter"),
        "chapters_total": run.get("chapters_total"),
    }


def _exp_badge(exp):
    """随 run 视口下发的远征摘要（章节进度/状态），供前端展示横幅。"""
    return {
        "id": exp["id"], "status": exp["status"],
        "chapter": exp["chapter"], "chapters_total": exp["chapters_total"],
    }


def _commission_summary(commissions):
    """交接/结算快照里的委托摘要：各状态计数 + 简要明细（整程回放可读）。"""
    counts = {"active": 0, "ready": 0, "claimed": 0, "failed": 0}
    for c in commissions:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return counts


def _carry_public(carry):
    """交接快照的只读视口：牌组按实例呈现（携带各自成长树节点与累计成本）。"""
    instances = carry.get("card_instances", {})
    return {
        "deck": [{
            "uid": uid, "id": instances[uid]["id"],
            "growth": [dict(r) for r in instances[uid].get("growth", [])],
            "growth_nodes": [r["node"] for r in instances[uid].get("growth", [])],
            "growth_spent": forging_mod.growth_spent(instances[uid]),
        } for uid in carry.get("deck", []) if uid in instances],
        "gold": carry.get("gold", 0),
        "relics": dict(carry.get("relics", {})),
        "health": carry.get("health"),
        "max_health": carry.get("max_health"),
        "potions": [potions_mod.public_potion(pid) for pid in carry.get("potions", [])],
        "companion": companions_mod.public_companion(carry.get("companion")),
        "commissions": [
            commission_mod.commission_public(c, carry.get("chapter") or 0)
            for c in carry.get("commissions", [])
        ],
    }


def _expedition_view(exp):
    """远征全量视口：状态/章节进度/交接快照/各章存档索引。"""
    return {
        "id": exp["id"],
        "seed": exp["seed"],
        "status": exp["status"],
        "chapter": exp["chapter"],
        "chapters_total": exp["chapters_total"],
        "current_run_id": exp["current_run_id"],
        "carry": _carry_public(exp["carry"]) if exp.get("carry") else None,
        "chapters": db.list_expedition_runs(exp["id"]),
        "rev": exp["rev"],
    }


def create_expedition(seed=None, chapters=None):
    """创建远征：远征记录与第 1 章 run 在同一事务落库，绝不留下「无章节」的远征。"""
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    total = chapters if chapters is not None else DEFAULT_CHAPTERS
    if not isinstance(total, int) or not (1 <= total <= MAX_CHAPTERS):
        raise InvalidAction(f"chapters must be 1..{MAX_CHAPTERS}")
    exp_id = uuid.uuid4().hex[:12]
    run_id = uuid.uuid4().hex[:12]
    state = _new_run_state(_chapter_seed(seed, 1), chapter=1,
                           chapters_total=total, expedition_id=exp_id)
    map_data = mapgen.generate_map(state["seed"])
    with db.transaction() as conn:
        db.insert_expedition(conn, exp_id, seed, total, run_id)
        db.insert_run(conn, run_id, state["seed"], state["status"], state["position"],
                      map_data, state, expedition_id=exp_id, chapter=1)
        db.append_event_conn(conn, run_id, 1, "create", {
            "seed": state["seed"], "ver": RULES_VERSION, "ckpt": state_checkpoint(state),
            "expedition": exp_id, "chapter": 1, "chapters_total": total,
        })
        db.append_expedition_event_conn(conn, exp_id, 1, "create", {
            "seed": seed, "chapters": total, "chapter": 1, "run_id": run_id,
        })
    exp = db.load_expedition(exp_id)
    return {
        "expedition": _expedition_view(exp),
        "run": _public_view(state, map_data, run_id, rev=1, expedition=_exp_badge(exp)),
    }


def get_expedition(exp_id):
    """远征视口 + 当前章节 run 视口（续远征入口）。"""
    exp = db.load_expedition(exp_id)
    if exp is None:
        raise InvalidAction("expedition not found")
    return {"expedition": _expedition_view(exp), "run": resume(exp["current_run_id"])}


def advance_expedition(exp_id, request_id=None):
    """进入下一章：以当前章的交接快照开新章 run。

    防重复开章：
    - 仅当远征进行中且当前章 run 已通关（won）才允许推进；推进后 chapter 与
      current_run_id 原子更新，重复调用看到的当前章不再是「已通关」状态 -> 400；
    - request_id 幂等：同一令牌重复/并发提交返回首次响应（duplicate:true），
      不会重复创建章节 run；
    - 远征记录、新章 run、双方日志在同一事务提交，任何写入失败整体回滚。
    """
    with db.run_lock(f"exp:{exp_id}"):
        with db.transaction() as conn:
            prior = db.get_idempotent(conn, f"exp:{exp_id}", request_id)
            if prior is not None:
                cached = dict(prior["response"])
                cached["duplicate"] = True
                return cached

            row = conn.execute("SELECT * FROM expeditions WHERE id=?", (exp_id,)).fetchone()
            if row is None:
                raise InvalidAction("expedition not found")
            if row["status"] != "in_progress":
                # 已结算（won/lost）：不重复结算、不再开章
                raise DuplicateReward(f"expedition already settled ({row['status']})")
            cur = conn.execute("SELECT * FROM runs WHERE id=?",
                               (row["current_run_id"],)).fetchone()
            if cur is None:
                raise InvalidAction("current chapter run not found")
            if cur["status"] != "won":
                raise InvalidAction("current chapter not cleared yet")
            chapter = row["chapter"]
            if chapter >= row["chapters_total"]:
                raise InvalidAction("expedition already at final chapter")

            nxt = chapter + 1
            carry = _carry_from_run(json.loads(cur["state_json"]))
            # 超期失败：进入第 nxt 章时限章早于 nxt 的进行中委托（可领奖的保留）
            expired = commission_mod.expire_active(carry["commissions"], nxt)
            run_id = uuid.uuid4().hex[:12]
            state = _new_run_state(_chapter_seed(row["seed"], nxt), carry=carry,
                                   chapter=nxt, chapters_total=row["chapters_total"],
                                   expedition_id=exp_id)
            map_data = mapgen.generate_map(state["seed"])
            try:
                db.insert_run(conn, run_id, state["seed"], state["status"], state["position"],
                              map_data, state, expedition_id=exp_id, chapter=nxt)
                db.append_event_conn(conn, run_id, 1, "create", {
                    "seed": state["seed"], "ver": RULES_VERSION, "ckpt": state_checkpoint(state),
                    "expedition": exp_id, "chapter": nxt,
                    "chapters_total": row["chapters_total"], "carry": carry,
                })
                seq = db.next_expedition_seq_conn(conn, exp_id)
                db.append_expedition_event_conn(conn, exp_id, seq, "advance", {
                    "chapter": nxt, "run_id": run_id, "carry": carry,
                    "rest_heal": state["health"] - carry["health"],
                    "expired": expired,
                })
                db.save_expedition_conn(conn, exp_id, "in_progress", nxt, run_id, carry,
                                        expected_rev=row["rev"])
            except db.ConcurrentModification as e:
                raise StaleState(str(e))

            exp = db.load_expedition(exp_id)
            response = {
                "expedition": _expedition_view(exp),
                "run": _public_view(state, map_data, run_id, rev=1, expedition=_exp_badge(exp)),
                "duplicate": False,
            }
            db.put_idempotent(conn, f"exp:{exp_id}", request_id, seq, response)
            return response


def _ensure_expedition_fields_conn(conn, run, rec):
    """旧档/2.2.0 之前的远征章节 run：从 runs 归属列与远征表回填委托所需章号字段。

    结构迁移只在内存的 run 状态上补默认值（随本次行动原子落库）；普通局保持 None。
    """
    exp_id = rec.get("expedition_id")
    if run.get("expedition_id") is None and exp_id:
        run["expedition_id"] = exp_id
        run["chapter"] = rec.get("chapter")
        row = conn.execute(
            "SELECT chapters_total FROM expeditions WHERE id=?", (exp_id,)
        ).fetchone()
        if row is not None:
            run["chapters_total"] = row["chapters_total"]


# ---------- 旧版跨章委托修复（2.4.0 之前的受影响存档/日志） ----------
def _commission_duration(c, at_chapter, chapters_total):
    """从一条委托的（已损坏）deadline 反推原始挂单时限跨度 duration（确定性）。

    旧实现下所有章节 run 的状态章号恒为 1，因此挂单 deadline=min(total,1+duration)：
    deadline<total 时 duration=deadline-1；deadline==total 时 duration 可能是
    total-1 或 MAX_DURATION 中任一被截断的值——二者修复后算出的 deadline 仍同为
    total（min 截断），所以取 total-1 即可逐位还原。
    """
    dl = c.get("deadline_chapter", at_chapter)
    duration = max(1, dl - 1)
    if chapters_total is not None and dl >= chapters_total:
        duration = max(duration, chapters_total - 1)
    return duration


def _repair_commissions_pure(commissions, cur_chapter, chapters_total,
                             accepted_chapter=None, claimed_chapter=None,
                             carry_ids=None, carry_boundary=None,
                             revive_chapter=None):
    """把 2.4.0 之前跨章 run 里被错误章号污染的委托就地修复为一致状态。

    旧实现下第 2 章及以后的 run 状态章号恒为 1，因此「在第 2 章及以后接取」的挂单
    deadline 被错误地按「章号 1」计算（旧值=min(total,1+duration)）；第 1 章接取的
    委托状态章号本就正确，deadline 是真值，绝不改动（含真实超期失败）。
    对真实接取章>=2 的委托，duration 可由存储 deadline 反推（dl<total ->
    duration=dl-1；dl==total 被截断时取 total-1，修复后 min 截断结果相同），按真实
    接取章重算：deadline' = min(total, accepted_chapter + duration)。
    - accepted_chapter：交接快照边界映射（真实接取章）；缺失时用 carry_boundary
      （本章 create 事件入章快照 cid->commission）；再缺失 -> 当前章（本章新接取）；
    - claimed_chapter：cid -> 真正领奖章，修正 claimed_at 章号前缀；
    - revive_chapter 非 None 时，仅对「真实接取章>=2（确受 bug 影响）、被旧超期
      判定 failed(expired)、但修复后 deadline 仍 >= revive_chapter」的带入委托
      撤销误判（恢复进行中）；第 1 章接取的失败终态保持不变。
    返回发生变化的委托 id 列表。
    """
    carry_ids = set(carry_ids or ())
    accepted_chapter = accepted_chapter or {}
    claimed_chapter = claimed_chapter or {}
    carry_boundary = carry_boundary or {}
    changed = []

    def true_accepted_chapter(cid):
        if accepted_chapter.get(cid) is not None:
            return accepted_chapter[cid]
        bc = carry_boundary.get(cid)
        if bc is not None and bc.get("accepted_chapter") is not None:
            return bc["accepted_chapter"]
        return cur_chapter          # 本章新接取

    for c in commissions:
        cid = c["id"]
        before = copy.deepcopy(c)
        at_ch = true_accepted_chapter(cid)
        c["accepted_chapter"] = at_ch
        if chapters_total is not None and at_ch >= 2:
            # 仅第 2 章及以后接取的挂单被旧错误章号锚定，按真实接取章重算期限
            duration = _commission_duration(c, at_ch, chapters_total)
            c["deadline_chapter"] = min(chapters_total, at_ch + duration)
        # 撤销被旧逻辑误判的超期失败：只救真实接取章>=2、修复后仍未到期的带入委托；
        # 第 1 章接取的 deadline 从未被污染，其超期失败是真实终态，不复活。
        if (revive_chapter is not None and cid in carry_ids and at_ch >= 2
                and c.get("status") == commission_mod.FAILED
                and c.get("fail_reason") == "expired"
                and c["deadline_chapter"] >= revive_chapter):
            c["status"] = commission_mod.ACTIVE
            c["fail_reason"] = None
        # 领奖记录的章节前缀：claimed_at 形如 "chapter:<章号>:<节点>"
        claimed_at = c.get("claimed_at")
        if claimed_at and cid in claimed_chapter:
            parts = claimed_at.split(":", 2)
            if len(parts) == 3 and parts[0] == "chapter":
                c["claimed_at"] = f"chapter:{claimed_chapter[cid]}:{parts[2]}"
        if c != before:
            changed.append(cid)
    return changed


def reanchor_shop_commission_offers(shop, chapter, chapters_total):
    """把商店货架上尚未接取的委托挂单按真实当前章重新锚定期限。

    旧实现下章号恒为 1，挂单 deadline=min(total,1+duration)；duration 可由存储
    deadline 反推，与委托同款逐位还原。货架卡牌/遗物/交易记录不受影响。
    """
    if not isinstance(shop, dict):
        return
    for o in shop.get("commission_offers", []) or []:
        o["offered_chapter"] = chapter
        if chapters_total is not None:
            dl = o.get("deadline_chapter", chapter)
            duration = max(1, dl - 1)
            if dl >= chapters_total:
                duration = max(duration, chapters_total - 1)
            o["deadline_chapter"] = min(chapters_total, chapter + duration)


def _commission_chapter_maps_conn(conn, exp_id):
    """扫描远征事件与各章动作日志，确定性建立委托的接取章/领奖章映射。

    返回 (accepted_map, claimed_map)：
    - accepted_map[cid]：真实接取章 = 委托最早出现的「章节结束快照」来源章。
      chapter_clear 事件的 carry 是该章刚通关时（advance 超期判定之前）的快照，
      章号即来源章；advance 事件的 carry 来自上一章（事件章号-1）。优先采用
      chapter_clear，advance 仅在没有对应 clear 时兜底（取最早来源章）。
    - claimed_map[cid]：真正领奖章（各章 commission_claim 日志，按章序首次出现）。
    只在本章接取、从未进过任何 carry 的委托不在映射中（调用方按「当前章接取」处理）。
    """
    accepted, claimed = {}, {}
    # 先收集 chapter_clear（接取章末、advance 前的权威快照），再用 advance 兜底
    for prefer_clear in (True, False):
        for e in db.load_expedition_events(exp_id):
            p = e.get("payload") or {}
            if not isinstance(p, dict) or not isinstance(p.get("carry"), dict):
                continue
            if (e["kind"] == "chapter_clear") != prefer_clear:
                continue
            ev_ch = p.get("chapter")
            origin = ev_ch - 1 if (e["kind"] == "advance" and ev_ch) else ev_ch
            if not origin:
                continue
            for c in p["carry"].get("commissions", []):
                cid = c.get("id")
                if cid and cid not in accepted:
                    accepted[cid] = origin
    rows = conn.execute(
        "SELECT be.payload_json AS pj, r.chapter AS ch FROM battle_events be "
        "JOIN runs r ON r.id = be.run_id "
        "WHERE r.expedition_id=? AND be.action='commission_claim' ORDER BY r.chapter, be.seq",
        (exp_id,),
    ).fetchall()
    for r in rows:
        try:
            p = json.loads(r["pj"])
        except (ValueError, TypeError):
            continue
        cid = p.get("commission") if isinstance(p, dict) else None
        if cid and r["ch"] is not None:
            claimed.setdefault(cid, r["ch"])
    return accepted, claimed


def _repair_expedition_carry_conn(conn, exp_row, origin_chapter, accepted_chapter,
                                  claimed_chapter, revive=True):
    """修复远征表交接快照：把 carry.chapter 校正为来源章，并对其中的委托做同款修复。

    carry 是「origin_chapter 章结束」的快照，其中每个委托都是在 origin 或更早章
    接取的（没有「origin+1 章新接取」），因此一律按跨章带入处理、用边界映射确定
    真实接取章并按反推 duration 重算期限。revive=True 时撤销进入下一章
    （origin+1）被旧逻辑误判超期的进行中委托；终章结算快照 revive=False 不复活。
    返回修复后的 carry（无快照时原样返回）。
    """
    carry = exp_row["carry"]
    if not isinstance(carry, dict):
        return carry
    carry_ids = {c["id"] for c in carry.get("commissions", [])}
    if carry.get("chapter") != origin_chapter:
        carry["chapter"] = origin_chapter
    _repair_commissions_pure(
        carry.get("commissions", []), origin_chapter, exp_row["chapters_total"],
        accepted_chapter=accepted_chapter, claimed_chapter=claimed_chapter,
        carry_ids=carry_ids,
        revive_chapter=(origin_chapter + 1 if revive else None))
    conn.execute("UPDATE expeditions SET carry_json=?, updated_at=datetime('now') WHERE id=?",
                 (json.dumps(carry, ensure_ascii=False), exp_row["id"]))
    return carry


def _repair_expedition_chapter_run_conn(conn, run, rec):
    """受影响旧档（2.4.0 前跨章 run）首次载入时的一致性修复。

    旧实现把 carry 的来源章号当成新 run 的章号：第 2 章及以后的 run 状态里
    chapter 恒为旧值，进而委托挂单 deadline、剩余期限、领奖位置章号、跨章超期
    判定全部错乱。runs.chapter 列（开章/推进时由远征状态权威写入）始终正确，
    以它为准修复 run 状态与远征表交接快照。幂等：已修复（章号一致）直接返回。
    返回是否发生修复（调用方据此把本步事件标记 migrated、rev+1 原子落库）。
    """
    exp_id = rec.get("expedition_id") or run.get("expedition_id")
    true_chapter = rec.get("chapter")
    if not exp_id or true_chapter is None:
        return False
    if run.get("chapter") == true_chapter:
        return False  # 已是修复后的结构
    # 第 1 章不会受影响；只修复章号确实错位的章节 run
    exp_row = conn.execute("SELECT * FROM expeditions WHERE id=?", (exp_id,)).fetchone()
    if exp_row is None:
        # 远征记录缺失（异常档）：至少把 run 章号对齐，保证委托视口自洽
        run["chapter"] = true_chapter
        return True
    chapters_total = exp_row["chapters_total"]
    run["chapter"] = true_chapter
    run["chapters_total"] = chapters_total

    accepted_chapter, claimed_chapter = _commission_chapter_maps_conn(conn, exp_id)
    # 进入本章时已随快照带入的委托 id（create 事件 carry）；真实接取章以交接快照
    # 边界为准（本章新接取的不在其中，按当前章重算 deadline）。
    create_carry_ids = set()
    create_carry_commissions = {}
    rows = conn.execute(
        "SELECT payload_json FROM battle_events WHERE run_id=? AND action='create'",
        (rec.get("id"),),
    ).fetchall()
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            continue
        if isinstance(p, dict) and isinstance(p.get("carry"), dict):
            cs = p["carry"].get("commissions", [])
            create_carry_ids = {c["id"] for c in cs}
            create_carry_commissions = {c["id"]: c for c in cs}
            break
    _repair_commissions_pure(
        run.get("commissions", []), true_chapter, chapters_total,
        accepted_chapter=accepted_chapter, claimed_chapter=claimed_chapter,
        carry_ids=create_carry_ids,
        carry_boundary=create_carry_commissions, revive_chapter=true_chapter)
    # 正停在章>=2 商店时，货架未接取挂单也按真实章号重新锚定（与回放重建一致）
    reanchor_shop_commission_offers(run.get("shop"), true_chapter, chapters_total)

    # 远征表交接快照：当前章 run 持有的 carry 是其来源章的快照。
    # - 进行中远征：carry 来自上一章通关（origin=本章-1），之后还会再开一章，
    #   撤销「进入下一章」被误判超期的进行中委托；
    # - 已结算远征（won/lost）：carry 是本章（终章/战败章）结束时写入的终态快照，
    #   之后不再开章，只校正章号/期限/领奖记录，不改变失败终态。
    if exp_row["current_run_id"] == rec.get("id"):
        # 原始 SQL 行只有 carry_json，解析为内存快照后再修复（同 db._row_to_expedition）
        exp_dict = {k: exp_row[k] for k in exp_row.keys()}
        try:
            exp_dict["carry"] = json.loads(exp_row["carry_json"]) \
                if exp_row["carry_json"] else None
        except (ValueError, TypeError):
            exp_dict["carry"] = None
        if exp_row["status"] == "in_progress":
            _repair_expedition_carry_conn(
                conn, exp_dict, true_chapter - 1,
                accepted_chapter, claimed_chapter, revive=True)
        else:
            _repair_expedition_carry_conn(
                conn, exp_dict, true_chapter,
                accepted_chapter, claimed_chapter, revive=False)
    return True


def _claim_allowed_after_chapter(conn, run):
    """章节已通关（run=won）后是否仍可领奖：仅限远征进行中（尚未进入下一章）。

    终章通关（远征 won）/战败（远征 lost）后委托随远征一并终结，不再开放领奖。
    """
    exp_id = run.get("expedition_id")
    if run.get("status") != "won" or not exp_id:
        return False
    row = conn.execute("SELECT status FROM expeditions WHERE id=?", (exp_id,)).fetchone()
    return row is not None and row["status"] == "in_progress"


def _sync_expedition_conn(conn, exp_id, run_rec, run):
    """在线行动提交时同步远征状态（与存档/日志同一事务）。

    章节 run 结束时：
    - 战败 -> 远征结算为 lost（战败解锁由行动事务内的 profile 写入一并提交）；
    - 击败终章首领 -> 远征结算为 won；
    - 击败非终章首领 -> 记录 chapter_clear 事件与交接快照，等待 advance。
    已结算或不是当前章节的重复触发直接跳过（不重复结算）。
    返回随视口下发的远征摘要。
    """
    row = conn.execute("SELECT * FROM expeditions WHERE id=?", (exp_id,)).fetchone()
    if row is None:
        return None
    if (row["status"] == "in_progress" and row["current_run_id"] == run_rec["id"]
            and run["status"] in ("won", "lost")):
        carry = _carry_from_run(run)
        chapter = run_rec.get("chapter") or row["chapter"]
        if run["status"] == "lost":
            new_status, kind = "lost", "settle"
            payload = {"result": "lost", "chapter": chapter, "run_id": run_rec["id"],
                       "battles": run.get("battle_index", 0), "carry": carry,
                       "commissions": _commission_summary(carry["commissions"])}
        elif chapter >= row["chapters_total"]:
            new_status, kind = "won", "settle"
            payload = {"result": "won", "chapter": chapter, "run_id": run_rec["id"],
                       "chapters": row["chapters_total"], "carry": carry,
                       "commissions": _commission_summary(carry["commissions"])}
        else:
            new_status, kind = "in_progress", "chapter_clear"
            payload = {"chapter": chapter, "run_id": run_rec["id"], "carry": carry}
        seq = db.next_expedition_seq_conn(conn, exp_id)
        db.append_expedition_event_conn(conn, exp_id, seq, kind, payload)
        db.save_expedition_conn(conn, exp_id, new_status, row["chapter"],
                                row["current_run_id"], carry, expected_rev=row["rev"])
        return {"id": exp_id, "status": new_status,
                "chapter": chapter, "chapters_total": row["chapters_total"]}
    return {"id": exp_id, "status": row["status"],
            "chapter": row["chapter"], "chapters_total": row["chapters_total"]}


def expedition_replay(exp_id):
    """整程回放：远征事件时间线 + 逐章完整回放（每章复用单局可交互回放）。

    全程只读：不写 runs/battle_events/profile/expeditions，战败章节不发解锁。
    """
    exp = db.load_expedition(exp_id)
    if exp is None:
        raise InvalidAction("expedition not found")
    chapters = []
    for r in db.list_expedition_runs(exp_id):
        rep = replay(r["run_id"])
        chapters.append({
            "chapter": r["chapter"],
            "run_id": r["run_id"],
            "status": r["status"],
            "replay": rep,
        })
    return {
        "expedition": _expedition_view(exp),
        "events": db.load_expedition_events(exp_id),
        "chapters": chapters,
        "isolated": True,  # 声明：本次整程回放无任何存档写入与解锁副作用
    }


def _require_run(run_id):
    run = db.load_run(run_id)
    if run is None:
        raise InvalidAction("run not found")
    return run


# ---------- 敌人解析 ----------
def _enemy_by_node(node_data):
    eid = node_data["enemy"]
    return enemies_mod.get_enemy(eid)


# ---------- 战斗绑定 ----------
def _build_battle(run_state, node_data):
    enemy_def = _enemy_by_node(node_data)
    relic = run_state["relics"]
    boss_hp_bonus = 0
    if node_data["type"] == mapgen.BOSS and "boss_hp_bonus" in relic:
        boss_hp_bonus = relic["boss_hp_bonus"]
    battle = Battle(
        {"max_health": run_state["max_health"], "health": run_state["health"],
         "deck": run_state["deck"], "relics": relic, "base_energy": run_state["base_energy"]},
        enemy_def, seed=run_state["seed"], battle_index=run_state["battle_index"],
        boss_hp_bonus=boss_hp_bonus,
        card_instances=run_state.get("card_instances", {}),
        companion_state=run_state.get("companion"),
    )
    # 回放 2.7.0 之前日志时，建场首回合（start_turn 抽牌/协助）也在旧规则区间：
    # 战斗全程（建场->各动作->读档续演）必须使用同一格挡时序，否则旧战斗会被
    # 按新格挡重演、承伤与历史校验点分叉。在线 run 不含该瞬态键，恒为新规则。
    battle.legacy_block = bool(run_state.get(_LEGACY_BLOCK_KEY))
    _snapshot, initial_log = battle.start_turn()
    return battle, initial_log


def _sync_companion_from_battle(run, battle):
    """把战斗内伙伴实体生命/负伤同步回持久伙伴状态（战斗外的休整状态原样保留）。"""
    companion = run.get("companion")
    if not companion:
        return
    ent = battle.entities.get("companion")
    if ent is None:
        # 休整模式或战前已负伤：持久状态不变，不参与本场战斗
        return
    companion["hp"] = ent["hp"]
    wounded = not ent["alive"] or ent["hp"] <= 0
    companion["wounded"] = wounded
    if wounded:
        # 负伤后自动转入休整：暂停后续参战，直到休息节点治疗并重新安排随行
        companion["mode"] = companions_mod.REST


def _load_battle(run_state):
    bstate = run_state["battle"]
    enemy_def = enemies_mod.get_enemy(bstate["enemy"])
    # 战斗内实例表优先（旧档迁移时写入），否则用 run 级实例表
    instances = bstate.get("card_instances", run_state.get("card_instances", {}))
    battle = Battle.from_state(bstate, enemy_def, run_state["seed"],
                               health=run_state["health"], max_health=run_state["max_health"])
    # 回放 2.7.0 之前的历史战斗动作时沿用旧格挡时序（在线 run 不含该瞬态键）
    battle.legacy_block = bool(run_state.get(_LEGACY_BLOCK_KEY))
    battle.card_instances = dict(instances)
    battle.companion_state = copy.deepcopy(bstate.get("companion_state"))
    battle.companion_def = (
        companions_mod.COMPANIONS.get(battle.companion_state.get("id"), companions_mod.SQUIRE)
        if battle.companion_state else None
    )
    if battle.companion_state is not None and "companion" not in battle.entities:
        ent = companions_mod.snapshot_for_battle(battle.companion_state)
        if ent is not None:
            battle.entities["companion"] = ent
    return battle


# ---------- 行动 ----------
class StaleState(Exception):
    """客户端携带的状态版本已过期（基于旧视口提交），要求刷新后重试 -> 409。"""
    pass


def act(run_id, action):
    """在线行动：加锁 -> 校验/幂等 -> 纯状态推演（无 DB）-> 单事务原子提交。

    纯推演部分（_apply_action）与回放共享同一条代码路径，保证“玩的时候”
    和“回放重建”永远使用同一套规则。提交时 存档 + 动作日志 + 战败解锁 在同一
    个 SQLite 事务里：任何写入失败整体回滚，不会出现存档推进了但日志缺行
    （或日志有行但存档没动）的存档/回放分叉。

    并发与重复：
    - per-run 锁串行化同一局的读改写，双击/并发的两个请求只会有一个生效，
      另一个看到的是推进后的状态（由业务幂等键 reward_claimed/forge_claimed/
      售罄等判为 409，或直接基于新状态合法执行）；
    - 客户端可带 request_id 做请求级幂等：重复请求原样返回首次响应，不重复
      扣款/不重复发奖；
    - 可携带 expected_rev（视口版本）基于旧状态提交时返回 409 状态冲突。
    """
    request_id = action.get("request_id")
    expected_rev = action.get("expected_rev")
    a = action.get("action")

    with db.run_lock(run_id):
        with db.transaction() as conn:
            # 幂等命中：重复请求直接回放首次响应（同一 run 锁内，结果确定）
            prior = db.get_idempotent(conn, run_id, request_id)
            if prior is not None:
                # 重复请求：返回首次响应的副本并标注 duplicate，不改存的幂等记录
                cached = dict(prior["response"])
                cached["duplicate"] = True
                return cached

            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise InvalidAction("run not found")
            rec = {
                "id": row["id"], "seed": row["seed"], "status": row["status"],
                "position": row["position"], "map": json.loads(row["map_json"]),
                "state": json.loads(row["state_json"]), "rev": row["rev"],
                "expedition_id": row["expedition_id"], "chapter": row["chapter"],
            }
            if expected_rev is not None and expected_rev != rec["rev"]:
                raise StaleState(f"state version conflict: expected {expected_rev}, actual {rec['rev']}")

            run = rec["state"]
            map_data = rec["map"]
            # 旧档兼容：首次载入即迁移到卡牌实例结构（随本次行动结果一起原子落库）。
            # migrated=True 时本步状态结构与回放起点（已是新结构）不同，校验点天然
            # 不可比，事件打 migrated 标记 -> 回放按 legacy 处理本步，后续步骤仍严格校验。
            migrated = _migrate_state(run)
            _ensure_expedition_fields_conn(conn, run, rec)
            # 2.4.0：修复跨章交接章号错位的受影响旧档（章号/委托期限/领奖记录一致性）
            if _repair_expedition_chapter_run_conn(conn, run, rec):
                migrated = True
            ended_before = run["status"] != "in_progress"
            # 章节已通关（won）但远征尚未推进时，仍允许领取已完成委托（领奖后再开新章）；
            # 其余情况下结束的 run 不再接受行动。
            if run["status"] != "in_progress" and not (
                    a == "commission_claim" and _claim_allowed_after_chapter(conn, run)):
                raise InvalidAction(f"run already ended ({run['status']})")

            # 纯推演：不触碰数据库；失败抛异常 -> 事务回滚，零副作用
            log = _apply_action(run, a, action, map_data, grant_unlocks=True)
            pending_unlock = run.pop(_PENDING_UNLOCK_KEY, None)

            payload = {
                "node": action.get("node"), "card": action.get("card"),
                "option": action.get("option"),
                "growth_node": action.get("growth_node"),
                "branch": action.get("branch"),
                "kind": action.get("kind"), "sku": action.get("sku"),
                "commission": action.get("commission"),
                "slot": action.get("slot"),
                "replace": action.get("replace"),
                "mode": action.get("mode"),
                "ver": RULES_VERSION, "ckpt": state_checkpoint(run),
            }
            if migrated:
                payload["migrated"] = True
            # 存档推进 + 日志追加 + 解锁发放：同生共死
            try:
                db.save_run_run(conn, run_id, run["status"], run["position"], run,
                                expected_rev=rec["rev"])
            except db.ConcurrentModification as e:
                # 多进程部署下存档在本事务期间被别处推进：按状态冲突处理（回滚 -> 409）
                raise StaleState(str(e))
            seq = db.next_seq_conn(conn, run_id)
            db.append_event_conn(conn, run_id, seq, a, payload)
            if pending_unlock is not None:
                db.upsert_profile_conn(conn, pending_unlock)
            # 远征章节 run：章节通关/战败在同一事务内同步远征状态（不重复结算）。
            # 通关后的委托领奖不再触发同步（结算/章节事件已在通关那一步落库）。
            exp_badge = None
            if rec.get("expedition_id") and not ended_before:
                try:
                    exp_badge = _sync_expedition_conn(conn, rec["expedition_id"], rec, run)
                except db.ConcurrentModification as e:
                    raise StaleState(str(e))
            elif rec.get("expedition_id"):
                exp = db.load_expedition(rec["expedition_id"])
                if exp is not None:
                    exp_badge = _exp_badge(exp)

            response = {"seq": seq, "log": log,
                        "run": _public_view(run, map_data, run_id, expedition=exp_badge),
                        "rev": rec["rev"] + 1, "duplicate": False}
            db.put_idempotent(conn, run_id, request_id, seq, response)
            return response


def _apply_action(run, a, action, map_data, grant_unlocks=False):
    """对内存中的 run 状态执行一个动作（纯函数语义）。

    map_data 由调用方持有（在线=存档地图；回放=按种子重新生成的同一地图）。
    grant_unlocks=False（回放/模拟）时，战败也绝不触发 profile 解锁写入。
    不读写数据库、不迁移存档——调用方负责准备好已迁移的状态。
    """
    if a == "choose_node":
        return _choose_node(run, map_data, action["node"],
                            grant_unlocks=grant_unlocks)
    if a == "play":
        return _play(run, action["card"], grant_unlocks=grant_unlocks)
    if a == "end_turn":
        return _end_turn(run, grant_unlocks=grant_unlocks)
    if a == "claim_reward":
        return _claim_reward(run, action["option"], replace=action.get("replace"))
    if a == "forge":
        return _forge(run, action.get("card"), action.get("growth_node") or action.get("branch"),
                      legacy=action.get("_legacy_forge", False))
    if a == "shop_buy":
        return _shop_buy(run, action.get("kind"), action.get("sku"),
                         replace=action.get("replace"))
    if a == "shop_remove":
        return _shop_remove(run, action.get("card"))
    if a == "use_potion":
        return _use_potion(run, action.get("slot"), grant_unlocks=grant_unlocks)
    if a == "discard_potion":
        return _discard_potion(run, action.get("slot"))
    if a == "companion_set_mode":
        return _companion_set_mode(run, action.get("mode"))
    if a == "commission_accept":
        return _commission_accept(run, action.get("sku"),
                                  legacy_offer=action.get("_legacy_offer"),
                                  legacy_ignore_cap=action.get("_legacy_ignore_cap", False))
    if a == "commission_claim":
        return _commission_claim(run, action.get("commission"))
    raise InvalidAction(f"unknown action {a}")


def _choose_node(run, map_data, node, grant_unlocks=True):
    from_current = map_data["routes"].get(run["position"], [])
    if run["position"] != mapgen.BOSS and node not in from_current:
        raise InvalidAction(f"node {node} unreachable from {run['position']}")
    node_data = map_data["nodes"][node]
    run["position"] = node
    initial_log = []
    # 每进入一个新节点都清掉上个节点的商店库存（商店仅在其节点内有效）
    run["shop"] = None

    t = node_data["type"]
    if t in (mapgen.ENCOUNTER, mapgen.ELITE, mapgen.BOSS):
        run["in_battle"] = True
        run["battle_index"] += 1
        battle, companion_log = _build_battle(run, node_data)
        initial_log = list(companion_log)
        if battle.battle_result() != "ongoing":
            # 极端情况下伙伴开场攻击直接结束战斗：奖励/章节状态仍走统一战后结算
            return _after_battle_step(run, battle, initial_log, grant_unlocks=grant_unlocks)
        run["battle"] = battle.dump()
        run["health"] = battle.entities["player"]["hp"]
        _sync_companion_from_battle(run, battle)
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        initial_log.append({"snapshot": battle.to_snapshot()})
        run["events_log"].append({"at": f"battle:{node}:{run['battle_index']}", "battle": True})
    elif t == mapgen.REST:
        heal = max(1, int(run["max_health"] * 0.2))
        run["health"] = min(run["max_health"], run["health"] + heal)
        companion_healed, companion_heal = companions_mod.heal_resting(run.get("companion"))
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        rest_log = {"at": f"rest:{node}", "heal": heal}
        if companion_healed:
            rest_log["companion_healed"] = companion_heal
        run["events_log"].append(rest_log)
    elif t == mapgen.REWARD:
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = rewards_mod.relic_choice_options(run["seed"] + run["battle_index"] * 7)
        run["reward_claimed"] = False
        run["forge_claimed"] = True
    elif t == mapgen.FORGE:
        # 锻造节点：进入即待锻造，玩家可花金币为一张牌选择强化分支（仅一次）
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = False
        run["events_log"].append({"at": f"forge:{node}"})
    elif t == mapgen.SHOP:
        # 旅途商店：按 (种子, 节点) 确定性生成库存；交易记录随库存保存，续局/回放一致
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        stock_seed = (run["seed"] * 10007 + node_data["row"] * 131 + ord(node[0])) & 0xFFFFFFFF
        owned_cards = {inst["id"] for inst in run.get("card_instances", {}).values()}
        if run.get("expedition_id"):
            # 远征章节商店额外挂出限章委托（内容随种子/章号/持有委托确定性生成）
            expedition_ctx = {
                "chapter": run.get("chapter") or 1,
                "chapters_total": run.get("chapters_total") or DEFAULT_CHAPTERS,
                "commissions": run.get("commissions", []),
            }
        else:
            expedition_ctx = None
        run["shop"] = shop_mod.generate_stock(
            stock_seed, owned_cards, set(run["relics"]),
            expedition_ctx=expedition_ctx,
            has_companion=bool(run.get("companion")) and not run.get(_LEGACY_NO_COMPANION_KEY),
            has_potions=not run.get(_LEGACY_NO_POTIONS_KEY))
        run["events_log"].append({"at": f"shop:{node}",
                                  "cards": len(run["shop"]["cards"]),
                                  "relics": len(run["shop"]["relics"]),
                                  "commissions": len(run["shop"].get("commission_offers", []))})
    elif t == "start":
        run["reward_claimed"] = True
        run["forge_claimed"] = True
    return initial_log


def _battle_or_raise(run):
    if not run["in_battle"] or not run["battle"]:
        raise InvalidAction("not in battle")


def _play(run, card_ref, grant_unlocks=True):
    _battle_or_raise(run)
    battle = _load_battle(run)
    if not battle.in_turn:
        raise InvalidAction("not player turn")
    # card 为手牌引用（uid；旧版 v1 动作日志记录的是裸卡牌 id）
    card_ref = _resolve_hand_ref(run, battle, card_ref)
    if card_ref not in battle.hand:
        raise InvalidAction("hand does not contain that card")
    card = battle._card_def(card_ref)
    if card["cost"] > battle.energy:
        raise InvalidAction("not enough energy")
    battle.energy -= card["cost"]
    try:
        log = battle.play_card(card_ref)
    except ValueError as e:
        raise InvalidAction(str(e))
    return _after_battle_step(run, battle, log, grant_unlocks)

def _resolve_hand_ref(run, battle, ref):
    """动作里的卡牌引用 -> 手牌引用。

    新档/现版日志：uid，原样返回。
    旧版日志（v1，卡牌实例化之前）：记录的是裸卡牌 id；按 run 级实例表把它
    解析为手牌中同 id 的某个 uid（同 id 实例在未锻造时完全等价），实现旧日志回放。
    """
    if ref in battle.hand:
        return ref
    instances = run.get("card_instances", {})
    if ref in instances:  # 卡在实例表但不在手牌（非法动作，保持原校验语义）
        return ref
    for uid in battle.hand:
        inst = instances.get(uid)
        if inst is not None and inst.get("id") == ref:
            return uid
    return ref  # 解析不到：交回上层的“手牌不存在”校验


def _end_turn(run, grant_unlocks=True):
    _battle_or_raise(run)
    battle = _load_battle(run)
    run["reward_claimed"] = True
    enemy_log, intent = battle.end_turn()
    log = []
    if intent is not None:
        # 敌方回合标记（含技能名），前端据此播放“敌方行动”横幅
        log.append({"action": "enemy_turn", "target": "player", "value": 0,
                    "source": "enemy", "tags": ["system"],
                    "extra": {"name": intent.get("name", "")}})
    # 敌方结算事件按结算顺序入日志，前端依序播放连锁动画
    log.extend(enemy_log)
    return _after_battle_step(run, battle, log, grant_unlocks)


def _potion_slot_index(slot):
    """动作里的格位参数 -> 合法 int 下标；非法一律 400。"""
    if isinstance(slot, bool) or not isinstance(slot, int):
        raise InvalidAction("potion slot index required")
    return slot


def _use_potion(run, slot, grant_unlocks=True):
    """战斗中在自己的回合使用一瓶药水。

    原子性：消耗（背包移除）与战斗效果（伤害/格挡/治疗/状态/能量，走同一个
    结算队列）以及随后的死亡打断/胜负/领奖/战败解锁，全部发生在这一个动作的
    纯推演里——成功才随行动事务落库；推演中任何异常由上层回滚整个 run 快照
    （见 _commit_shop_tx 同款策略不适用于战斗，但 act 的事务在异常时整体
    rollback，存档与日志都不会推进），因此「扣了药水却没生效」不可能发生。
    request_id 幂等 + per-run 锁保证双击/超时重试只生效一次（重复请求返回
    首次响应，不会再扣一瓶）。
    """
    _battle_or_raise(run)
    idx = _potion_slot_index(slot)
    potions = run.setdefault("potions", [])
    if not (0 <= idx < len(potions)):
        raise InvalidAction("unknown potion slot")
    battle = _load_battle(run)
    if not battle.in_turn:
        raise InvalidAction("potions can only be used on your turn")
    pid = potions[idx]
    pdef = potions_mod.get_potion(pid)

    # 先消耗再结算：两步同属一个纯推演动作，异常即整体回滚（含背包）
    potions.pop(idx)
    log = [{"potion": {"slot": idx, "id": pid, "name": pdef["name"], "icon": pdef["icon"]}}]
    q = SettlementQueue(battle)
    for eff in pdef["effects"]:
        t = eff["type"]
        if t in ("apply_status", "set_status", "gain_block", "heal", "gain_energy"):
            target = eff.get("target", "player")
        else:
            target = eff.get("target", "enemy")
        q.push(EffectEvent(
            t, target=target, value=eff.get("value", 0),
            source="player", tags=eff.get("tags", []),
            extra={"status": eff.get("status"), "ticks": eff.get("ticks"),
                   "stack": eff.get("stack")}))
    log.extend(q.run())
    battle.truncated = battle.truncated or q.truncated
    return _after_battle_step(run, battle, log, grant_unlocks)


def _discard_potion(run, slot):
    """主动丢弃一瓶药水（仅非战斗、run 进行中；战斗中请直接使用或战后整理）。

    丢弃不返钱、无其它副作用；重复/并发请求由 request_id 幂等与格位校验拦截
    （第二次格位已变化/为空 -> 400 或命中首次响应，绝不会多丢）。
    """
    if run.get("in_battle"):
        raise InvalidAction("cannot discard potions during battle")
    if run["status"] != "in_progress":
        raise InvalidAction("run already ended")
    idx = _potion_slot_index(slot)
    potions = run.setdefault("potions", [])
    if not (0 <= idx < len(potions)):
        raise InvalidAction("unknown potion slot")
    pid = potions.pop(idx)
    pdef = potions_mod.get_potion(pid)
    return [{"potion_discarded": {"slot": idx, "id": pid, "name": pdef["name"]}}]


def _companion_set_mode(run, mode):
    """在非战斗时安排伙伴随行或休整。

    休整不是治疗本身：进入休息节点时仅休整中的伙伴会被治疗。这样玩家可在危险
    战斗前主动让低血量伙伴休整，也能明确选择继续随行承担援护风险。
    """
    if mode not in (companions_mod.ACCOMPANY, companions_mod.REST):
        raise InvalidAction("companion mode must be accompany or rest")
    companion = run.get("companion")
    if not companion:
        raise InvalidAction("no companion recruited")
    if run.get("in_battle"):
        raise InvalidAction("cannot change companion mode during battle")
    if run["status"] != "in_progress":
        raise InvalidAction("run already ended")
    if companion.get("mode") == mode:
        raise DuplicateReward(f"companion already {mode}")
    companion["mode"] = mode
    run["events_log"].append({
        "at": f"companion:{run['position']}", "mode": mode,
        "wounded": bool(companion.get("wounded")),
    })
    return [{"companion_mode": companions_mod.public_companion(companion)}]


def _after_battle_step(run, battle, log, grant_unlocks=True):
    snap = battle.to_snapshot()
    result = battle.battle_result()
    run["health"] = battle.entities["player"]["hp"]
    _sync_companion_from_battle(run, battle)
    if result == "ongoing":
        run["battle"] = battle.dump()
        log.append({"snapshot": snap})
        return log

    # 战斗结束
    run["in_battle"] = False
    if result == "won":
        run["battle"] = None
        # 用当前节点敌人掉落生成奖励
        enemy = battle.enemy_def
        # 2.5.0 之前的旧战斗不出药水战利品：回放旧日志时按旧选项集重建，
        # 否则追加的药水项会让旧 claim_reward 下标错位（在线新档恒为 True）。
        include_potions = not run.get(_LEGACY_NO_POTIONS_KEY)
        run["reward_options"] = rewards_mod.battle_reward_options(
            run["seed"] + run["battle_index"], enemy, run,
            include_potions=include_potions)
        run["reward_claimed"] = False
        log.append({"result": "won", "snapshot": snap})
        # 远征委托：胜利自动推进讨伐目标（可能转为可领奖）
        _progress_commissions(run, commission_mod.apply_battle_result(
            run.get("commissions", []), True), log, "battle_win")
        if enemy.get("boss"):
            run["status"] = "won"
            run["reward_options"] = []
            run["reward_claimed"] = True
            log.append({"result": "run_won"})
    else:
        run["battle"] = None
        run["status"] = "lost"
        log.append({"result": "lost", "snapshot": snap})
        # 远征委托：战败则所有进行中委托立即失败
        _progress_commissions(run, commission_mod.apply_battle_result(
            run.get("commissions", []), False), log, "battle_lost")
        # 仅在线路径计算失败解锁；回放/模拟（grant_unlocks=False）不产生 profile 变更
        if grant_unlocks:
            new_profile, changed = _grant_unlock_on_loss(run)
            if changed:
                run[_PENDING_UNLOCK_KEY] = new_profile
    return log


def _progress_commissions(run, changed_ids, log, reason):
    """把委托自动推进结果写入战斗/交易动作日志（前端提示 + 回放可读）。"""
    if not changed_ids:
        return
    by_id = {c["id"]: c for c in run.get("commissions", [])}
    for cid in changed_ids:
        c = by_id[cid]
        log.append({"commission_progress": {
            "id": cid, "status": c["status"], "progress": c["progress"],
            "target": c["target"], "reason": reason,
            "fail_reason": c.get("fail_reason"),
        }})


def _claim_reward(run, option_idx, replace=None):
    if run["reward_claimed"]:
        raise DuplicateReward("reward already claimed")
    if run["status"] != "in_progress":
        raise InvalidAction("not in progress")
    opts = run["reward_options"]
    if option_idx < 0 or option_idx >= len(opts):
        raise InvalidAction("bad reward option")
    opt = opts[option_idx]
    # 战利品药水：背满时必须先指定替换格（claim_reward 携带 replace 下标），
    # 校验先于领奖状态翻转，失败整体无副作用（可改选别的选项或带格重领）。
    potion_eff = next((e for e in opt.get("effects", []) if e.get("type") == "add_potion"), None)
    if potion_eff is not None:
        _check_potion_capacity(run, replace)
        potion_eff["replace"] = replace
    _apply_option(run, opt)
    run["reward_claimed"] = True
    run["reward_options"] = []
    return [{"reward_claimed": opt["name"]}]


def _check_potion_capacity(run, replace):
    """背满且未给合法替换格 -> 400（不翻转领奖状态，零副作用）。"""
    if potions_mod.is_full(run.get("potions", [])):
        n = len(run["potions"])
        if not isinstance(replace, int) or not (0 <= replace < n):
            raise InvalidAction("potion belt full; choose a slot to replace")


def _apply_option(run, opt):
    for eff in opt.get("effects", []):
        _apply_option_effect(run, eff)


def _apply_option_effect(run, eff):
    t = eff["type"]
    if t == "add_card":
        _add_card_instance(run, eff["card"])
    elif t == "add_potion":
        _add_potion(run, eff["potion"], eff.get("replace"))
    elif t == "gold":
        run["gold"] += eff["value"]
    elif t == "heal_run":
        run["health"] = min(run["max_health"], run["health"] + eff["value"])
    elif t == "relic_set":
        run["relics"][eff["relic"]] = eff.get("value", 1)


def _add_potion(run, pid, replace=None):
    """药水入包（商店购买/战利品共用）：背满按 replace 替换丢弃，空位直接入包。"""
    try:
        slot, discarded = potions_mod.add(run.setdefault("potions", []), pid, replace)
    except KeyError:
        raise InvalidAction("unknown potion")
    except ValueError as e:
        raise InvalidAction(str(e))
    return slot, discarded


def _add_card_instance(run, cid):
    """奖励入牌：发放带独立成长树状态的新卡牌实例（同名卡互不共享成长）。"""
    seq = run.get("next_card_seq", len(run.get("card_instances", {})) + 1)
    uid = f"c{seq}"
    run["next_card_seq"] = seq + 1
    run.setdefault("card_instances", {})[uid] = _new_instance(cid)
    run["deck"].append(uid)
    return uid


# ---------- 卡牌成长树 ----------
def _forge(run, card_uid, growth_node, legacy=False):
    """在锻造节点为指定卡牌实例解锁一个成长节点。

    幂等：forge_claimed 承担“每锻造节点仅一次操作”的幂等键，重复请求 409 不扣款。
    成长规则（按实例保存，随存档/交接快照持久化）：
    - 节点必须存在且未解锁；前置节点（requires）必须已解锁；
    - 互斥分支（mutex_with，同一 lane 的同级分支及其后裔）已选其一则其余锁定；
    - 按节点 tier 收费（T1 25 / T2 40 / T3 60），金币不足 400；
    - 校验全部通过才扣款（无副作用），选择与成本一起写入实例 growth 记录。
    legacy=True（回放 2.3.0 之前的 forge 日志）：branch 为旧分支 id，
    按旧固定价 25 与确定性默认链展开，复刻“同分支可重复选择”的旧行为。
    """
    if run.get("forge_claimed", True):
        raise DuplicateReward("forge already used at this node")
    if not card_uid or card_uid not in run.get("card_instances", {}):
        raise InvalidAction("unknown card instance")
    inst = run["card_instances"][card_uid]
    if not growth_node:
        raise InvalidAction("growth node required")

    if legacy:
        return _forge_legacy(run, inst, card_uid, growth_node)

    if growth_node not in forging_mod.NODE_IDS:
        raise InvalidAction("unknown growth node")
    reason = validate_unlock(inst, growth_node)
    if reason == forging_mod.ERR_ACQUIRED:
        # 已解锁的节点重复请求：按节点幂等冲突处理（409），不扣款
        raise DuplicateReward(reason)
    if reason:
        # 前置不满足 / 互斥分支已选：400（校验先于扣款，无副作用）
        raise InvalidAction(reason)
    cost = growth_node_cost(growth_node)
    if run["gold"] < cost:
        raise InvalidAction("not enough gold")

    run["gold"] -= cost
    inst.setdefault("growth", []).append({"node": growth_node, "cost": cost})
    run["forge_claimed"] = True
    run["events_log"].append({
        "at": f"forge:{run['position']}", "card": inst["id"], "uid": card_uid,
        "growth_node": growth_node, "cost": cost,
    })
    eff = effective_card(get_card(inst["id"]), inst["growth"])
    return [{"forged": {"uid": card_uid, "card": inst["id"], "node": growth_node,
                        "name": node_name(growth_node), "cost": cost,
                        "gold_left": run["gold"], "card_cost": eff["cost"],
                        "growth_spent": forging_mod.growth_spent(inst)}}]


def _forge_legacy(run, inst, card_uid, branch):
    """旧版 forge 日志（2.3.0 之前）的确定性重演。

    旧规则：固定 25 金、三分支、同分支可重复叠加。映射到成长树：
    每次付款沿该分支默认链解锁“下一个未拥有”的节点；链已满的额外付款
    不再产生节点但依旧扣款（忠实复刻旧存档的金币轨迹）。
    """
    if branch not in forging_mod.LEGACY_BRANCH_TO_NODE:
        raise InvalidAction("unknown forge branch")
    if run["gold"] < FORGE_COST:
        raise InvalidAction("not enough gold")
    run["gold"] -= FORGE_COST
    owned = {r["node"] for r in inst.setdefault("growth", [])}
    chain = forging_mod.LEGACY_CHAINS[branch]
    picked = next((c for c in chain if c not in owned), None)
    if picked is not None:
        inst["growth"].append({"node": picked, "cost": FORGE_COST})
    run["forge_claimed"] = True
    run["events_log"].append({
        "at": f"forge:{run['position']}", "card": inst["id"], "uid": card_uid,
        "branch": branch, "legacy": True, "node": picked,
    })
    return [{"forged": {"uid": card_uid, "card": inst["id"], "node": picked,
                        "branch": branch, "legacy": True, "cost": FORGE_COST,
                        "gold_left": run["gold"]}}]


# ---------- 旅途商店 ----------
def _active_shop(run):
    if run.get("in_battle") or not run.get("shop"):
        raise InvalidAction("not at a shop")
    return run["shop"]


def _find_offer(shop, kind, sku):
    if kind == "card":
        bucket = shop["cards"]
    elif kind == "relic":
        bucket = shop["relics"]
    elif kind == "potion":
        bucket = shop.get("potions", [])
    elif kind == "companion":
        bucket = shop.get("companions", [])
    else:
        raise InvalidAction("unknown shop shelf (kind must be card/relic/potion/companion)")
    item = next((it for it in bucket if it["sku"] == sku), None)
    if item is None:
        raise InvalidAction("unknown shop item")
    if item["sold"]:
        # 售罄（含重复购买）：不扣款
        raise ShopSoldOut("item already sold")
    return item


def _commit_shop_tx(run, mutate, record):
    """统一交易：先对 run 做深拷贝快照，执行扣款与变更；任意异常都整体回退到快照，
    保证失败请求零副作用（不扣款、不改牌组/遗物/库存）。成功才写入交易记录。"""
    snapshot = copy.deepcopy(run)
    try:
        result = mutate(run)
    except Exception:
        run.clear()
        run.update(snapshot)
        raise
    record_obj = record(result)
    run["shop"]["tx"].append(record_obj)
    log = [{"shop_tx": record_obj}]
    # 远征委托：成功完成一笔交易（购买/移除）自动推进贸易目标
    changed = commission_mod.apply_trade(run.get("commissions", []))
    _progress_commissions(run, changed, log, "trade")
    return log


def _shop_buy(run, kind, sku, replace=None):
    shop = _active_shop(run)
    item = _find_offer(shop, kind, sku)  # 非法货架/售罄在扣款前拦截
    price = item["price"]
    if run["gold"] < price:
        raise InvalidAction("not enough gold")  # 校验先于扣款，无副作用

    if kind == "card":
        cid = item["card"]
        # 货架生成时按未持有过滤；若已通过其它途径获得同名卡，仍允许购买（独立新实例）
        def mutate(r):
            r["gold"] -= price
            uid = _add_card_instance(r, cid)
            next(it for it in r["shop"]["cards"] if it["sku"] == sku)["sold"] = True
            return {"uid": uid}
    elif kind == "potion":
        pid = item["potion"]
        # 背满时必须指定替换格（在扣款前校验，失败零副作用）
        _check_potion_capacity(run, replace)

        def mutate(r):
            r["gold"] -= price
            slot, discarded = _add_potion(r, pid, replace)
            next(it for it in r["shop"]["potions"] if it["sku"] == sku)["sold"] = True
            return {"potion": pid, "slot": slot, "discarded": discarded}
    elif kind == "companion":
        cid = item["companion"]
        if run.get("companion"):
            raise ShopSoldOut("companion already recruited")

        def mutate(r):
            r["gold"] -= price
            companion = companions_mod.make_companion(cid)
            r["companion"] = companion
            next(it for it in r["shop"]["companions"] if it["sku"] == sku)["sold"] = True
            return {"companion": cid, "name": companion["name"]}
    else:
        rid = item["relic"]
        if rid in run["relics"]:
            raise ShopSoldOut("relic already owned")
        relic = shop_mod.relic_def(rid)

        def mutate(r):
            r["gold"] -= price
            for eff in relic.get("effects", []):
                _apply_option_effect(r, eff)
            next(it for it in r["shop"]["relics"] if it["sku"] == sku)["sold"] = True
            return {"relic": rid}

    return _commit_shop_tx(
        run, mutate,
        lambda res: {"type": "buy", "kind": kind, "sku": sku, "price": price,
                     "gold_left": run["gold"], **res})


def _shop_remove(run, card_uid):
    shop = _active_shop(run)
    if not card_uid or card_uid not in run.get("card_instances", {}):
        raise InvalidAction("unknown card instance")
    if len(run["deck"]) <= shop_mod.REMOVE_MIN_DECK:
        raise InvalidAction("deck too small to remove")
    cost = shop["remove"]["cost"]
    if run["gold"] < cost:
        raise InvalidAction("not enough gold")  # 校验先于扣款，无副作用
    cid = run["card_instances"][card_uid]["id"]

    def mutate(r):
        r["gold"] -= cost
        r["deck"].remove(card_uid)
        del r["card_instances"][card_uid]
        r["shop"]["remove"]["used"] += 1
        r["shop"]["remove"]["cost"] = shop_mod.next_remove_cost(r["shop"]["remove"]["used"])
        return {"uid": card_uid, "card": cid, "deck_size": len(r["deck"])}

    return _commit_shop_tx(
        run, mutate,
        lambda res: {"type": "remove", **res, "price": cost, "gold_left": run["gold"]})


# ---------- 远征委托 ----------
def _find_commission_offer(run, sku):
    """在当前商店挂单中按 sku 查找委托挂单项；已接取（挂单移除）视为售罄。"""
    shop = _active_shop(run)
    if not run.get("expedition_id"):
        raise InvalidAction("commissions are only available in expeditions")
    offers = shop.get("commission_offers", [])
    offer = next((o for o in offers if f"commission:{o['signature']}" == sku), None)
    if offer is None:
        # 已接取/已离开商店的重复请求：按售罄（409）处理，不重复发委托
        if sku and any(f"commission:{c['signature']}" == sku
                       for c in run.get("commissions", [])
                       if c["status"] in (commission_mod.ACTIVE, commission_mod.READY)):
            raise ShopSoldOut("commission already accepted")
        raise InvalidAction("unknown commission offer")
    return offer


def _commission_accept(run, sku, legacy_offer=None, legacy_ignore_cap=False):
    """在商店接取限章委托：免费，接取后挂单移除、委托进入进行中。

    委托限张（commission_mod.MAX_ACTIVE，含已完成待领取）；委托随章节交接，
    战斗胜利/商店交易自动推进，领奖走 commission_claim（防重复）。

    legacy_offer（仅回放受影响旧日志时由回放层注入）：旧实现章号错位，修复后
    重放的挂单与旧挂单可能不再逐项相同；回放层按旧委托内容合成挂单项，使历史
    接取动作在修复路径上仍可重放，并同步忽略限张（旧超期判定少失败委托时，
    修复后的持有数可能超过旧的挂单上限）。
    """
    if legacy_offer is not None:
        offer = legacy_offer
        commissions = run.setdefault("commissions", [])
    else:
        offer = _find_commission_offer(run, sku)
        commissions = run.setdefault("commissions", [])
        active = [c for c in commissions
                  if c["status"] in (commission_mod.ACTIVE, commission_mod.READY)]
        if len(active) >= commission_mod.MAX_ACTIVE:
            raise InvalidAction("too many active commissions")
    seq = run.get("next_commission_seq", 1)
    run["next_commission_seq"] = seq + 1
    commission = commission_mod.make_commission(seq, offer)
    commissions.append(commission)
    offers = run["shop"].setdefault("commission_offers", [])
    run["shop"]["commission_offers"] = [
        o for o in offers if o["signature"] != offer["signature"]
    ]
    run["events_log"].append({
        "at": f"commission:{run['position']}", "accepted": commission["id"],
        "kind": commission["kind"], "target": commission["target"],
        "deadline": commission["deadline_chapter"],
    })
    return [{"commission_accepted": commission_mod.commission_public(
        commission, run.get("chapter") or 1)}]


def _commission_claim(run, commission_id):
    """领取已完成委托的奖励（金币或卡牌实例）。

    幂等键：委托状态机 ready -> claimed 只流转一次；重复领取返回 409 且不重复
    发奖。非法 id / 未完成 / 已失败返回 400。战斗中不可领奖（战斗结束结算前
    状态不稳定）；章节通关后、推进下一章前仍允许领奖。
    """
    if not commission_id:
        raise InvalidAction("commission id required")
    commissions = run.get("commissions", [])
    commission = next((c for c in commissions if c["id"] == commission_id), None)
    if commission is None:
        raise InvalidAction("unknown commission")
    if commission["status"] == commission_mod.CLAIMED:
        raise DuplicateReward("commission reward already claimed")
    if commission["status"] == commission_mod.FAILED:
        raise InvalidAction("commission already failed")
    if commission["status"] != commission_mod.READY:
        raise InvalidAction("commission objective not completed yet")
    if run.get("in_battle"):
        raise InvalidAction("cannot claim commission reward during battle")

    reward = commission["reward"]
    granted = None
    if reward["type"] == "gold":
        run["gold"] += reward["amount"]
        granted = {"type": "gold", "amount": reward["amount"], "gold": run["gold"]}
    elif reward["type"] == "card":
        uid = _add_card_instance(run, reward["card"])
        granted = {"type": "card", "card": reward["card"], "uid": uid}
    else:
        raise InvalidAction("unknown commission reward")

    commission["status"] = commission_mod.CLAIMED
    commission["claimed_at"] = f"chapter:{run.get('chapter')}:{run['position']}"
    run["events_log"].append({
        "at": f"commission:{run['position']}", "claimed": commission["id"],
        "reward": reward,
    })
    return [{"commission_claimed": {
        "id": commission["id"], "granted": granted,
        "commission": commission_mod.commission_public(commission, run.get("chapter") or 1),
    }}]


def _grant_unlock_on_loss(run):
    """在线战败：纯计算本次解锁结果（不写库）。

    返回 (新 profile, 是否有变化)；由 act 的原子事务与存档/日志一起提交。
    回放路径（grant_unlocks=False）不会调用本函数。
    """
    prof = db.get_profile()
    unlocked = list(prof["unlocked"]) if prof else list(START_DECK)
    locked = list(prof["locked"]) if prof else list(INIT_LOCKED)
    pool = [c for c in locked if all_cards_locked().get(c)]
    if not pool:
        return None, False
    rng = random.Random(run["seed"] + run["battle_index"])
    cid = pool[rng.randrange(len(pool))]
    unlocked.append(cid)
    locked.remove(cid)
    return {"unlocked": unlocked, "locked": locked}, True


def all_cards_locked():
    return {c["id"]: c for c in all_cards()}


# ---------- 视口 ----------
def resume(run_id):
    # 旧档兼容迁移与读改写同一把锁/事务：并发续局或“续局与首行动撞车”时
    # 不会发生两次迁移互相覆盖（迁移结果与日志一起原子落库）。
    with db.run_lock(run_id):
        with db.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise InvalidAction("run not found")
            state = json.loads(row["state_json"])
            map_data = json.loads(row["map_json"])
            rec = {
                "id": run_id,
                "expedition_id": row["expedition_id"] if "expedition_id" in row.keys() else None,
                "chapter": row["chapter"] if "chapter" in row.keys() else None,
            }
            structural = _migrate_state(state)
            _ensure_expedition_fields_conn(conn, state, rec)
            # 2.4.0：受影响的跨章旧档首次续局同样执行章号/委托一致性修复
            repaired = _repair_expedition_chapter_run_conn(conn, state, rec)
            if structural or repaired:
                # 迁移是幂等的结构升级：无条件落库即可（per-run 锁已串行化，
                # 多进程下即便并发迁移，写入的也是等价结构），不做 rev 冲突判定。
                db.save_run_run(conn, run_id, row["status"], row["position"], state)
                rev = row["rev"] + 1
            else:
                rev = row["rev"]
            # 远征章节 run：视口携带远征摘要（章节进度/结算状态）
            exp_badge = None
            if row["expedition_id"]:
                exp = db.load_expedition(row["expedition_id"])
                if exp is not None:
                    exp_badge = _exp_badge(exp)
            return _public_view(state, map_data, run_id, rev=rev, expedition=exp_badge)


# ---------- 规则版本与校验点 ----------
# 不参与状态校验的瞬时/派生字段：
# - events_log 只用于事件叙述，不影响规则推演
# - _pending_unlock 是在线行动在内存中暂存的战败解锁，随事务提交到 profile，
#   不属于 run 状态本身（绝不写入 state_json）
_PENDING_UNLOCK_KEY = "_pending_unlock"
_LEGACY_NO_COMPANION_KEY = "_legacy_no_companion"
_LEGACY_NO_POTIONS_KEY = "_legacy_no_potions"
# 仅存在于回放内存 run：当前是否仍处于 2.7.0 之前的旧格挡时序（不写入存档、
# 不参与校验点哈希）。_load_battle 据此给 Battle 置 legacy_block。
_LEGACY_BLOCK_KEY = "_legacy_block"
_CKPT_SKIP_KEYS = {
    "events_log", _PENDING_UNLOCK_KEY, _LEGACY_NO_COMPANION_KEY,
    _LEGACY_NO_POTIONS_KEY, _LEGACY_BLOCK_KEY, "_ckpt_skip_keys",
}


def state_checkpoint(run, include_companion=True, include_potions=True):
    """权威状态校验点：对完整 run 状态取稳定哈希（SHA-256 截断 16 位）。

    include_*=False 仅用于旧规则升级时的历史初态/迁移前哈希兼容。
    """
    skip = set(_CKPT_SKIP_KEYS)
    if not include_companion:
        skip.add("companion")
    if not include_potions:
        skip.add("potions")
    material = {k: v for k, v in run.items() if k not in skip}
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _create_ckpt_candidates(seed, create_payload):
    """回放开章：构造「修复后正确初态」与「旧版错误初态」两种候选及其校验点。

    2.4.0 之前的跨章 run 用 carry 里的来源章号覆盖了新章号；create 事件里记录的
    ckpt 是错误初态的哈希。比对记录值即可识别受影响旧日志：记录命中错误候选、
    且不等于正确候选 -> 本 run 是受影响章，回放走兼容修复路径。
    返回 (正确初态, 正确ckpt, 错误ckpt 或 None)。
    """
    carry = create_payload.get("carry") if not create_payload.get("_corrupt") else None
    chapter = create_payload.get("chapter")
    total = create_payload.get("chapters_total")
    sim = _new_run_state(
        seed, carry=carry, chapter=chapter, chapters_total=total,
        expedition_id=create_payload.get("expedition"))
    # 状态内的 rules_version 标签在旧版本建局时就是旧串（在线修复不改写它）；
    # 用 create 事件记录的 ver 还原标签，旧存档哈希才能逐位比对（新档 ver 即当前版）。
    recorded_ver = create_payload.get("ver")
    if recorded_ver:
        sim["rules_version"] = recorded_ver
    full_ckpt = state_checkpoint(sim)
    no_companion_ckpt = state_checkpoint(sim, include_companion=False)
    no_potions_ckpt = state_checkpoint(sim, include_potions=False)
    legacy_shape_ckpt = state_checkpoint(sim, include_companion=False, include_potions=False)
    buggy = None
    if carry is not None and chapter is not None and chapter > 1:
        buggy = _new_run_state(
            seed, carry=carry,
            chapter=carry.get("chapter"),   # 旧实现：来源章号覆盖新章号
            chapters_total=carry.get("chapters_total", total),
            expedition_id=create_payload.get("expedition"))
        if recorded_ver:
            buggy["rules_version"] = recorded_ver
    candidates = {
        "full": (state_checkpoint(buggy) if buggy is not None else None, full_ckpt),
        "no_companion": (state_checkpoint(buggy, include_companion=False)
                         if buggy is not None else None, no_companion_ckpt),
        "no_potions": (state_checkpoint(buggy, include_potions=False)
                       if buggy is not None else None, no_potions_ckpt),
        "legacy_shape": (state_checkpoint(buggy, include_companion=False, include_potions=False)
                         if buggy is not None else None, legacy_shape_ckpt),
    }
    recorded = create_payload.get("ckpt") if not create_payload.get("_corrupt") else None
    matched_shape = "full"
    matched_buggy_ckpt = candidates["full"][0]
    fixed_ckpt = full_ckpt
    if recorded is not None:
        for shape, (buggy_hash, fixed_hash) in candidates.items():
            if recorded in (buggy_hash, fixed_hash):
                matched_shape = shape
                matched_buggy_ckpt = buggy_hash
                fixed_ckpt = recorded
                break
    pre_companion = matched_shape in ("no_companion", "legacy_shape")
    pre_potions = matched_shape in ("no_potions", "legacy_shape")
    return sim, fixed_ckpt, matched_buggy_ckpt, pre_companion, pre_potions


def _replay_commission_maps(conn, exp_id, run_id):
    """回放层建立委托章号映射（只读）：有远征用交接快照边界（在线修复同款）；
    单局回放只补领奖章（本章 commission_claim 动作 -> runs.chapter 权威章号）。"""
    accepted, claimed = {}, {}
    if exp_id:
        accepted, claimed = _commission_chapter_maps_conn(conn, exp_id)
    row = conn.execute("SELECT chapter FROM runs WHERE id=?", (run_id,)).fetchone()
    ch = row["chapter"] if row is not None else None
    if ch is not None:
        for e in db.load_events(run_id):
            if e["action"] == "commission_claim":
                cid = (e.get("payload") or {}).get("commission")
                if cid:
                    claimed.setdefault(cid, ch)
    return accepted, claimed


def _legacy_offer_for_accept(run_rec, sku, fixed_chapter, chapters_total, exp_id=None):
    """回放受影响旧日志时，为旧的 commission_accept 合成修复路径上的挂单项。

    修复点之前的接取都发生在当前章：deadline 按真实章号 fixed_chapter 重算，
    duration 从本档状态里同 signature 的旧委托反推（旧挂单按章号 1 锚定）；
    找不到时从 signature 文本解析 kind/target/reward 并按最大时限兜底。
    """
    sig = sku.split("commission:", 1)[-1] if sku else ""
    commissions = (run_rec.get("state") or {}).get("commissions", [])
    match = next((c for c in commissions if c.get("signature") == sig), None)
    if match is not None:
        kind, target, reward = match["kind"], match["target"], dict(match["reward"])
        duration = _commission_duration(match, 1, chapters_total)
    else:
        # 兜底：signature 形如 "battle:2:gold:30" / "trade:3:card:cleave"
        parts = sig.split(":")
        try:
            kind, target = parts[0], int(parts[1])
        except (ValueError, IndexError):
            return None
        if parts[2] == "gold":
            reward = {"type": "gold", "amount": int(parts[3])}
        else:
            reward = {"type": "card", "card": ":".join(parts[3:])}
        duration = commission_mod.MAX_DURATION
    deadline = min(chapters_total or fixed_chapter, fixed_chapter + duration)
    return {
        "kind": kind, "target": target, "deadline_chapter": deadline,
        "reward": reward, "signature": sig, "offered_chapter": fixed_chapter,
    }


def replay(run_id):
    """整局可交互回放。

    从“建局初始状态”开始，按动作日志逐步调用与在线完全相同的纯推演函数
    （_apply_action，grant_unlocks=False），为每个动作产出一帧：
      - view：该动作完成后的完整只读视口（地图/战斗/锻造/商店，结构与 /resume 一致）
      - events：该动作产生的结算事件（战斗动画逐条播放；锻造/交易结果同构）
      - kind/title/summary：时间轴分组与人类可读描述
      - result：战斗/整局在本步结束（won/lost/run_won）
    校验：每步与日志记录的 ckpt 哈希比对；旧日志（无 ckpt/无 ver）标记 legacy
    并跳过校验。整个回放只读内存与已持久化的日志，不写 runs/battle_events/profile，
    战败不触发解锁奖励。
    """
    rec = load_run(run_id)
    if rec is None:
        raise InvalidAction("run not found")
    seed = rec["state"]["seed"]
    map_data = rec["map"]
    # 只读已持久化日志：经共享连接读取，保证读到的都是已提交事务
    events = db.load_events(run_id)

    # 回放起点：重新构造建局时的初始状态（不读、不写、不迁移真实存档）。
    # 远征章节 run 的起点由 create 事件携带的交接快照（carry）重建，与在线开章一致。
    create_payload = next(
        (e.get("payload") for e in events
         if e.get("action") == "create" and isinstance(e.get("payload"), dict)),
        {},
    )
    # 修复后正确初态 + 旧版错误初态两个候选：用记录的 create ckpt 识别受影响旧日志
    (sim, initial_ckpt, buggy_ckpt,
     pre_companion_create, pre_potions_create) = _create_ckpt_candidates(seed, create_payload)
    recorded_initial = (create_payload.get("ckpt")
                        if not create_payload.get("_corrupt") else None)
    pre_companion_replay = pre_companion_create
    pre_potions_replay = pre_potions_create
    companion_migration_seen = not pre_companion_replay
    potions_migration_seen = not pre_potions_replay
    # 2.7.0 格挡/援护顺序修复：无存档结构变更，create 形状无法区分新旧——
    # 统一按「首个 2.7.0+ 动作之前为旧时序」处理。在线路径上旧战斗中存档
    # 在玩家回合边界落库，升级后的首个动作（可能直接就是 end_turn）即按新
    # 规则推演；回放必须在同一个动作之前切换，续局与逐位校验点才能对齐。
    block_rule_seen = False
    sim[_LEGACY_BLOCK_KEY] = True
    # 受影响章（记录的是错误初态哈希）：整段走兼容修复——沿修复后的正确路径重放，
    # 修复点之前的历史步骤记录的是错误状态哈希，按 legacy 跳过逐位比对；修复点
    # （在线迁移发生的首个动作）及之后严格比对。最终帧与在线修复后的存档一致。
    fixed_current_ckpt = state_checkpoint(sim)
    legacy_chapter = bool(
        recorded_initial and buggy_ckpt is not None
        and recorded_initial == buggy_ckpt and recorded_initial != fixed_current_ckpt)

    fixed_chapter = create_payload.get("chapter")
    chapters_total = create_payload.get("chapters_total")
    exp_id = rec.get("expedition_id") or create_payload.get("expedition")

    # 受影响章的委托接取/领奖章号映射与入章携带委托集合（纯修复与在线同款）
    accepted_chapter, claimed_chapter = {}, {}
    carry_ids = set()
    if legacy_chapter:
        with db.get_conn() as conn:
            accepted_chapter, claimed_chapter = \
                _replay_commission_maps(conn, exp_id, run_id)
        carry = create_payload.get("carry") or {}
        carry_commissions = carry.get("commissions", [])
        carry_ids = {c["id"] for c in carry_commissions}
        _repair_commissions_pure(
            sim.get("commissions", []), fixed_chapter, chapters_total,
            accepted_chapter=accepted_chapter,
            claimed_chapter=claimed_chapter, carry_ids=carry_ids,
            carry_boundary={c["id"]: c for c in carry_commissions},
            revive_chapter=fixed_chapter)
        # 货架未接取挂单同样重新锚定到真实当前章（在线修复同款，保证逐位一致）
        reanchor_shop_commission_offers(sim.get("shop"), fixed_chapter, chapters_total)
    if pre_companion_replay:
        sim[_LEGACY_NO_COMPANION_KEY] = True
        if sim.get("shop"):
            sim["shop"].pop("companions", None)
    if pre_potions_replay:
        sim[_LEGACY_NO_POTIONS_KEY] = True
        sim["potions"] = []
        if sim.get("shop"):
            sim["shop"].pop("potions", None)
    # 远征章节 run：帧视口携带远征摘要（只读，不阻断回放）
    exp_badge = None
    if rec.get("expedition_id"):
        exp = db.load_expedition(rec["expedition_id"])
        if exp is not None:
            exp_badge = _exp_badge(exp)

    steps = []
    checks = []          # 每步校验结果
    legacy_steps = 0
    repaired_steps = 0   # 受影响旧日志的兼容修复步骤（修复点之前，按 legacy 呈现）
    skipped_errors = 0
    versions = set()
    expected_seq = 1     # 序号连续性检查（旧档/异常日志可能有缺口）
    gap_steps = 0
    post_fix = not legacy_chapter  # 已越过在线迁移点（2.4.0 新事件）-> 恢复严格校验

    for ev in events:
        payload = ev.get("payload") or {}
        corrupt_row = bool(payload.get("_corrupt"))
        # 旧档迁移步：状态从旧结构迁到新结构，与回放起点（已为新结构）的校验点
        # 天然不可逐位比较。正常推演但本步跳过哈希校验，按 legacy 呈现。
        migrated_step = bool(payload.get("migrated")) and not corrupt_row
        ver = payload.get("ver") if not corrupt_row else None
        if ver:
            versions.add(ver)
        a = ev["action"]
        # 结构迁移点必须在推演本步动作【之前】切换规则：在线路径上 /resume 迁移
        # （无日志，首个 2.5.0/2.6.0 事件之前）与 /act 迁移（本步动作推演之前）
        # 都已让新规则生效。若在动作之后才切换，首个新版动作若是「进入新商店」，
        # 回放仍按旧规则生成库存（缺药水/伙伴货架），后续升级后的购买动作将无法
        # 重放，在线与回放的库存/背包随之分叉。create 事件不产生状态变化，不切换。
        crossing_companion = (a != "create" and pre_companion_replay
                              and not companion_migration_seen
                              and ver and not _ver_lt(ver, COMPANION_RULES_VERSION))
        crossing_potions = (a != "create" and pre_potions_replay
                            and not potions_migration_seen
                            and ver and not _ver_lt(ver, "2.5.0"))
        # 格挡规则修复点：首个携带 2.7.0+ 版本的动作（含战斗动作）之前一切
        # 按旧格挡时序推演；本动作本身在线上已是修复后语义（规则先行切换）。
        crossing_block = (a != "create" and not block_rule_seen
                          and ver and not _ver_lt(ver, BLOCK_RULES_VERSION))
        # 受影响旧日志的修复点（2.4.0 跨章章号错位）：首个当前版本事件之前一切
        # 按 legacy 修复路径重放；越过该点后恢复严格校验。
        at_fix_point = (legacy_chapter and not post_fix and a != "create"
                        and (migrated_step or (ver and not _ver_lt(ver, RULES_VERSION))
                             or crossing_companion or crossing_potions or crossing_block))
        if at_fix_point or crossing_companion or crossing_potions or crossing_block:
            if crossing_companion:
                companion_migration_seen = True
                sim.pop(_LEGACY_NO_COMPANION_KEY, None)
                sim.setdefault("companion", None)
            if crossing_potions:
                potions_migration_seen = True
                sim.pop(_LEGACY_NO_POTIONS_KEY, None)
                sim.setdefault("potions", [])
            if crossing_block:
                block_rule_seen = True
                sim.pop(_LEGACY_BLOCK_KEY, None)
        if at_fix_point:
            post_fix = True
            # 2.4.0 修复点会同时越过伙伴/药水迁移（首个当前版本事件不可能早于它们）
            if not companion_migration_seen:
                companion_migration_seen = True
                sim.pop(_LEGACY_NO_COMPANION_KEY, None)
                sim.setdefault("companion", None)
            if not potions_migration_seen:
                potions_migration_seen = True
                sim.pop(_LEGACY_NO_POTIONS_KEY, None)
                sim.setdefault("potions", [])
            if not block_rule_seen:
                block_rule_seen = True
                sim.pop(_LEGACY_BLOCK_KEY, None)
        # 修复点之前（不含 create 与修复点本身）记录的是错误状态，按 legacy 处理
        pre_repair = (legacy_chapter and not post_fix
                      and a != "create" and not at_fix_point)
        create_of_legacy = (legacy_chapter and a == "create"
                            and not post_fix)
        # 仍处于旧结构区间的步骤：伙伴/药水字段在记录哈希中缺席（旧形状）。
        # 跨越结构迁移的本步不再属于旧区间——在线迁移先于本步动作，逐位可比。
        pre_companion_step = (pre_companion_replay and not companion_migration_seen
                              and a != "create")
        pre_potions_step = (pre_potions_replay and not potions_migration_seen
                            and a != "create")
        # 仍处于旧格挡时序区间的步骤（2.7.0 修复点之前）：同一动作在旧时序下
        # 的落库状态与新推演不同（格挡/承伤/伙伴生命），按 legacy 呈现并跳过
        # 哈希比对；动作仍经 legacy_block 旧时序逐位重演。纯规则修复、无结构
        # 变化，因此 create 帧不豁免（create 不含战斗，旧/新初态逐位相同）。
        pre_block_step = (not block_rule_seen and a != "create")
        # migrated 步（本步事务内发生旧档结构升级：裸 id 牌组 -> 实例、补伙伴/
        # 药水字段等）：记录的 ckpt 是「迁移后」状态，回放起点却已是新结构，
        # 结构性差异使本步哈希不可逐位比较——一律按 legacy 呈现、跳过比对；
        # 但动作仍按迁移后的新规则推演（上面的 crossing 已切换规则），保证
        # 迁移点上的新商店/战利品与在线一致、后续升级后的动作严格校验。
        # 损坏行没有可信版本号，按旧日志处理但仍会因推演失败标注 error
        is_legacy = not ver
        skip_ckpt = (corrupt_row or migrated_step or pre_repair
                     or create_of_legacy or pre_companion_step or pre_potions_step
                     or pre_block_step)
        if (is_legacy or migrated_step or pre_repair or create_of_legacy
                or pre_companion_step or pre_potions_step or pre_block_step):
            legacy_steps += 1
        if pre_repair:
            repaired_steps += 1
        recorded = None if skip_ckpt else payload.get("ckpt")
        # 序号缺口不阻断后续推演（可能是旧档缺行），但记录警告便于排障
        gap_warning = None
        if ev["seq"] != expected_seq:
            gap_warning = f"seq gap: expected {expected_seq}, found {ev['seq']}"
            gap_steps += 1
        expected_seq = ev["seq"] + 1

        log, error = [], None
        if corrupt_row:
            error = "CorruptLog: 动作日志载荷损坏，无法重演该步"
            skipped_errors += 1
        elif a == "create":
            # 建局事件只携带种子；初始状态已在循环外构造，不产生状态变化
            pass
        else:
            try:
                # 2.3.0 之前（含无版本号）的 forge 日志：旧三分支可重复锻造，
                # 按成长树默认链兼容重演，避免旧规则下“重复同分支”的合法动作
                # 在新规则里被拒而导致后续整段状态分叉
                replay_action = dict(payload)
                if a == "forge" and (is_legacy or _ver_lt(ver, GROWTH_RULES_VERSION)):
                    replay_action["_legacy_forge"] = True
                # 受影响旧日志在修复点之前的委托接取：修复后挂单与旧挂单不再逐项
                # 相同，按旧委托内容合成修复路径上的挂单项（放宽旧限张/超期连锁差异）
                if pre_repair and a == "commission_accept":
                    offer = _legacy_offer_for_accept(
                        rec, payload.get("sku"), fixed_chapter, chapters_total,
                        exp_id=exp_id)
                    if offer is not None:
                        replay_action["_legacy_offer"] = offer
                        replay_action["_legacy_ignore_cap"] = True
                log = _apply_action(sim, a, replay_action, map_data, grant_unlocks=False)
                # 仍在旧结构区间时，动作产生的商店库存也必须保持旧形状
                # （伙伴/药水货架是随字段升级才加入的规则产物；战后药水战利品
                # 已在 _after_battle_step 按 include_potions 旧规则不生成）。
                if sim.get("shop") and not companion_migration_seen:
                    sim["shop"].pop("companions", None)
                if sim.get("shop") and not potions_migration_seen:
                    sim["shop"].pop("potions", None)
            except Exception as e:  # 损坏/越权动作不抹掉整段回放：断在此步并标注
                error = f"{type(e).__name__}: {e}"
                skipped_errors += 1

        # create 帧在循环外已完成修复/兼容形状调整：直接使用候选校验点，
        # 不再对修复后的 sim 重新取哈希（修复前记录的就是待修复初态）。
        # migrated 步跳过比对（recorded=None），哈希取完整形状即可。
        # _legacy_block 是回放瞬态键（_CKPT_SKIP_KEYS），不影响哈希取值。
        actual = initial_ckpt if a == "create" else state_checkpoint(
            sim,
            include_companion=not pre_companion_step,
            include_potions=not pre_potions_step,
        )
        if error:
            status = "error"
        elif not recorded:
            status = "legacy"          # 旧版/修复前日志：可播放但不保证逐位一致
        elif recorded == actual:
            status = "ok"
        else:
            status = "mismatch"
        checks.append({"seq": ev["seq"], "action": a, "status": status,
                       "recorded": recorded, "actual": actual})

        # 事件与在线 /act 返回的 log 同构（含 snapshot 校正点），前端播放器可复用
        # 同一套结算事件驱动；帧 view 本身已携带权威状态，跳转时直接落帧无需放动画。
        anim_events = [dict(x) for x in log]
        steps.append({
            "seq": ev["seq"],
            "action": a,
            "payload": payload,
            "kind": _step_kind(sim, a, payload, log),
            "title": _step_title(sim, map_data, a, payload, log),
            "summary": _step_summary(a, payload, log),
            "events": anim_events,
            "result": _step_result(log),
            "view": _public_view(sim, map_data, run_id, include_unlocks=False,
                                 expedition=exp_badge),
            "check": status,
            "legacy": (is_legacy or migrated_step or pre_repair
                       or create_of_legacy or pre_companion_step or pre_potions_step
                       or pre_block_step),
            "migrated": migrated_step,
            "repaired": pre_repair,
            "error": error,
            "warning": gap_warning,
        })

    recorded_versions = sorted(versions)
    current = RULES_VERSION
    final_match = all(c["status"] in ("ok", "legacy") for c in checks)
    return {
        # 兼容旧客户端：仍返回扁平动作序列与种子
        "run_id": run_id,
        "seed": seed,
        "actions": events,
        # 交互式回放
        "rules_version": current,
        "recorded_versions": recorded_versions,
        "legacy": legacy_steps > 0 or not recorded_versions,
        "initial": {"checkpoint": initial_ckpt},
        "steps": steps,
        "final_view": _public_view(sim, map_data, run_id, include_unlocks=False,
                                   expedition=exp_badge),
        "verification": {
            "ok": sum(c["status"] == "ok" for c in checks),
            "legacy": sum(c["status"] == "legacy" for c in checks),
            "repaired": repaired_steps,
            "mismatch": sum(c["status"] == "mismatch" for c in checks),
            "error": sum(c["status"] == "error" for c in checks),
            "seq_gaps": gap_steps,
            "final_match": final_match,
            "checks": checks,
            "skipped_errors": skipped_errors,
        },
        "isolated": True,  # 声明：本次回放无任何存档写入与解锁副作用
    }


def _step_result(log):
    for x in reversed(log):
        if isinstance(x, dict) and x.get("result"):
            return x["result"]
    return None


def _step_kind(sim, action, payload, log):
    if action == "choose_node":
        return "battle_entry" if sim.get("in_battle") else "route"
    if action in ("play", "end_turn", "use_potion"):
        return "battle"
    if action == "claim_reward":
        return "reward"
    if action == "forge":
        return "forge"
    if action in ("shop_buy", "shop_remove"):
        return "trade"
    if action in ("commission_accept", "commission_claim"):
        return "commission"
    if action == "discard_potion":
        return "potion"
    if action == "companion_set_mode":
        return "companion"
    if action == "create":
        return "create"
    return "other"


def _node_label(map_data, node):
    if not node:
        return ""
    nd = map_data["nodes"].get(node)
    labels = {"encounter": "遭遇", "elite": "精英", "rest": "休息", "reward": "奖励",
              "forge": "锻造", "shop": "商店", "boss": "首领", "start": "营地"}
    return labels.get((nd or {}).get("type"), node)


def _card_name(cid):
    from .cards import CARDS
    c = CARDS.get(cid)
    return c["name"] if c else cid


def _step_title(sim, map_data, action, payload, log):
    if action == "create":
        return "建局"
    if action == "choose_node":
        node = payload.get("node")
        return f"前往{_node_label(map_data, node)}节点"
    if action == "play":
        inst = sim.get("card_instances", {}).get(payload.get("card"))
        cid = inst.get("id") if inst else payload.get("card")
        return f"打出「{_card_name(cid)}」"
    if action == "end_turn":
        for x in log:
            if isinstance(x, dict) and x.get("action") == "enemy_turn":
                who = x.get("extra", {}).get("name")
                return f"结束回合 · 敌方行动：{who}" if who else "结束回合 · 敌方行动"
        return "结束回合"
    if action == "claim_reward":
        for x in log:
            if isinstance(x, dict) and x.get("reward_claimed"):
                return f"领取奖励「{x['reward_claimed']}」"
        return "领取奖励"
    if action == "forge":
        inst = sim.get("card_instances", {}).get(payload.get("card"))
        cname = _card_name(inst["id"]) if inst else (payload.get("card") or "")
        f = next((x.get("forged") for x in log if isinstance(x, dict) and x.get("forged")), None)
        picked_id = (f or {}).get("node") or payload.get("growth_node") or payload.get("branch") or ""
        if picked_id:
            picked = (f or {}).get("name") or node_name(picked_id)
        else:
            picked = "已满（无新节点）"  # 旧版重复分支把默认链填满后的付款
        return f"锻造 {cname} · {picked}"
    if action == "shop_buy":
        return f"商店购买（{payload.get('sku')}）"
    if action == "shop_remove":
        return "商店移除卡牌"
    if action == "commission_accept":
        return "接取远征委托"
    if action == "commission_claim":
        return "领取委托奖励"
    if action == "use_potion":
        used = next((x.get("potion") for x in log
                     if isinstance(x, dict) and x.get("potion")), None)
        return f"使用药水「{used['name']}」" if used else "使用药水"
    if action == "discard_potion":
        d = next((x.get("potion_discarded") for x in log
                  if isinstance(x, dict) and x.get("potion_discarded")), None)
        return f"丢弃药水「{d['name']}」" if d else "丢弃药水"
    if action == "companion_set_mode":
        mode = payload.get("mode")
        return "伙伴随行" if mode == companions_mod.ACCOMPANY else "伙伴休整"
    return action


def _step_summary(action, payload, log):
    """时间轴上的简短状态变化描述（金币/牌组/战斗结果/交易）。"""
    if action == "shop_buy" or action == "shop_remove":
        tx = next((x.get("shop_tx") for x in log if isinstance(x, dict) and x.get("shop_tx")), None)
        if tx:
            if action == "shop_buy" and tx.get("kind") == "potion":
                pname = potions_mod.POTIONS.get(tx.get("potion"), {}).get("name", tx.get("sku"))
                base = f"购入药水「{pname}」，花费 {tx.get('price')}，余额 {tx.get('gold_left')}"
                if tx.get("discarded"):
                    dname = potions_mod.POTIONS.get(tx["discarded"], {}).get("name", tx["discarded"])
                    base += f"（替换丢弃「{dname}」）"
            elif action == "shop_buy" and tx.get("kind") == "companion":
                base = f"招募伙伴「{tx.get('name')}」，花费 {tx.get('price')}，余额 {tx.get('gold_left')}"
            else:
                base = f"花费 {tx.get('price')}，余额 {tx.get('gold_left')}"
            ready = [x for x in log if isinstance(x, dict)
                     and x.get("commission_progress", {}).get("status") == "ready"]
            if ready:
                base += "；委托已完成可领奖"
            return base
    if action == "forge":
        f = next((x.get("forged") for x in log if isinstance(x, dict) and x.get("forged")), None)
        if f:
            return f"花费 {f.get('cost')}，余额 {f.get('gold_left')}"
    if action in ("play", "end_turn", "use_potion"):
        r = _step_result(log)
        prog = [x.get("commission_progress") for x in log
                if isinstance(x, dict) and x.get("commission_progress")]
        if r == "won":
            ready = [p for p in prog if p["status"] == "ready"]
            return ("战斗胜利；委托已完成可领奖" if ready else "战斗胜利")
        if r == "lost":
            failed = [p for p in prog if p["status"] == "failed"]
            return ("战斗失败；进行中委托全部失败" if failed else "战斗失败")
        if r == "run_won":
            return "通关！"
        dmg = sum(x.get("value", 0) for x in log
                  if isinstance(x, dict) and x.get("action") in ("damage", "echo_damage"))
        if dmg:
            return f"结算 {len([x for x in log if isinstance(x, dict) and x.get('action')])} 个事件"
    if action == "commission_accept":
        acc = next((x.get("commission_accepted") for x in log
                    if isinstance(x, dict) and x.get("commission_accepted")), None)
        if acc:
            return f"接取：{acc['objective']}（限第 {acc['deadline_chapter']} 章前）"
    if action == "commission_claim":
        cl = next((x.get("commission_claimed") for x in log
                   if isinstance(x, dict) and x.get("commission_claimed")), None)
        if cl:
            g = cl["granted"]
            if g["type"] == "gold":
                return f"获得金币 {g['amount']}（余额 {g['gold']}）"
            return f"获得卡牌「{_card_name(g['card'])}」"
    if action == "claim_reward":
        for x in log:
            if isinstance(x, dict) and x.get("reward_claimed"):
                return f"获得「{x['reward_claimed']}」"
    if action == "discard_potion":
        d = next((x.get("potion_discarded") for x in log
                  if isinstance(x, dict) and x.get("potion_discarded")), None)
        return f"丢弃药水「{d['name']}」" if d else "丢弃药水"
    if action == "companion_set_mode":
        return "随行参战" if payload.get("mode") == companions_mod.ACCOMPANY else "休整待命；休息节点可治疗"
    if action == "choose_node" and payload.get("node"):
        return f"位置 → {payload['node']}"
    return ""


def _growth_public(inst):
    """实例成长状态的公开形态：节点记录 + 节点 id 速查 + 累计投入 + 当前可解锁节点。"""
    records = [dict(r) for r in inst.get("growth", [])]
    return {
        "growth": records,
        "nodes": [r["node"] for r in records],
        "growth_spent": forging_mod.growth_spent(inst),
        "available": forging_mod.available_nodes(inst),
    }


def _hand_public(run, bstate):
    """战斗手牌视口：新档给出含生效费用/成长标记的实例项，旧档回退为裸 id。"""
    instances = bstate.get("card_instances", run.get("card_instances", {}))
    if not instances:
        return list(bstate["hand"])
    out = []
    for ref in bstate["hand"]:
        inst = instances.get(ref)
        if inst is None:
            out.append(ref)
            continue
        eff = effective_card(get_card(inst["id"]), inst.get("growth", []))
        gp = _growth_public(inst)
        out.append({
            "uid": ref, "id": inst["id"], "cost": eff["cost"],
            "growth": gp["growth"], "growth_nodes": gp["nodes"],
        })
    return out


def _public_view(run, map_data, run_id, include_unlocks=True, rev=None, expedition=None):
    """只读视口。include_unlocks=False（回放）时不读取 profile 库，省略解锁信息。

    rev 非 None 时附带存档乐观版本号，客户端下次行动可作为 expected_rev 回传。
    expedition 非 None（远征章节 run）时附带远征摘要（章节进度/结算状态）。
    """
    reachable = map_data["routes"].get(run["position"], [])
    snap = None
    if run["in_battle"] and run["battle"]:
        def pub_ent(k):
            e = run["battle"]["entities"][k]
            return {
                "name": e["name"], "hp": e["hp"], "max_hp": e["max_hp"],
                "block": e["block"], "alive": e["alive"],
                "statuses": _statuses_public(e["statuses"]),
            }
        snap = {
            "player": pub_ent("player"),
            "enemy": pub_ent("enemy"),
            "companion": pub_ent("companion") if "companion" in run["battle"]["entities"] else None,
            "energy": run["battle"]["energy"],
            "max_energy": run["battle"]["max_energy"],
            "turn": run["battle"]["turn"],
            "in_turn": run["battle"]["in_turn"],
            "truncated": run["battle"]["truncated"],
            "hand": _hand_public(run, run["battle"]),
            "enemy_id": run["battle"]["enemy"],
        }
    # 牌组视口：同名卡按实例独立呈现（携带各自成长树节点与累计成本）
    instances = run.get("card_instances", {})
    node_data = map_data["nodes"].get(run["position"], {})
    at_forge_node = node_data.get("type") == mapgen.FORGE
    deck_view = []
    for uid in run["deck"]:
        inst = instances.get(uid)
        if inst is None:
            deck_view.append(uid)
            continue
        gp = _growth_public(inst)
        deck_view.append({
            "uid": uid, "id": inst["id"],
            "growth": gp["growth"], "growth_nodes": gp["nodes"],
            "growth_spent": gp["growth_spent"],
            # 仅在锻造节点附带“该实例当前可解锁节点”，减少非锻造场景的冗余
            "growth_available": gp["available"] if at_forge_node else [],
        })
    deck_view = deck_view or list(run["deck"])
    view = {
        "run_id": run_id,
        "seed": run["seed"],
        "status": run["status"],
        "position": run["position"],
        "health": run["health"],
        "max_health": run["max_health"],
        "gold": run["gold"],
        "energy": run["battle"]["energy"] if run["in_battle"] else run["base_energy"],
        "deck": deck_view,
        "relics": dict(run["relics"]),
        "potions": potions_mod.belt_public(run.get("potions", [])),
        "potion_capacity": potions_mod.POTION_CAPACITY,
        "companion": companions_mod.public_companion(run.get("companion")),
        "potion_catalog": [potions_mod.public_potion(pid) for pid in sorted(potions_mod.POTIONS)],
        "reward_options": list(run["reward_options"]),
        "reward_claimed": run["reward_claimed"],
        "forge_available": node_data.get("type") == mapgen.FORGE and not run.get("forge_claimed", True),
        "forge_claimed": bool(run.get("forge_claimed", True)),
        "forge_cost": FORGE_COST,
        "growth_tree": forging_mod.public_tree(),
        "growth_tier_cost": dict(forging_mod.TIER_COST),
        "shop_available": node_data.get("type") == mapgen.SHOP and bool(run.get("shop")),
        "shop": shop_mod.public_view(run.get("shop")),
        "commissions": [
            commission_mod.commission_public(c, run.get("chapter") or 1)
            for c in run.get("commissions", [])
        ],
        "commission_kinds": commission_mod.public_commission_kinds(),
        "in_battle": run["in_battle"],
        "battle": snap,
        "reachable": [map_data["nodes"][n] for n in reachable],
        "map": _map_public(map_data, run["position"]),
        "unlocked_cards": get_profile_unlocked() if include_unlocks else None,
        "expedition": expedition,
        "truncated": bool(run["battle"]["truncated"]) if run["in_battle"] and run["battle"] else bool(run.get("truncated", False)),
    }
    if rev is not None:
        view["rev"] = rev
    return view


def _map_public(map_data, position):
    return {
        "nodes": map_data["nodes"],
        "routes": map_data["routes"],
        "start": map_data["start"],
        "boss": map_data["boss"],
        "position": position,
    }