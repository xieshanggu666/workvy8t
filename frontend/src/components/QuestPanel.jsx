import React, { useState } from 'react'
import { api, handleActError } from '../api'
import { useStore } from '../store'

// 奇遇抉择面板（2.8.0 跨章节奇遇链）：
// - choice：三选一，点击即提交 quest_choose（代价先于效果权威结算，
//   request_id 幂等防双击，expected_rev 防过期提交）
// - ambush：面板仅展示伏击预告（进入节点时已自动开战，真正战斗走 BattleView）
// - 奖励幕进入即自动结算，不会出现在待办里
function CostTags({ option }) {
  const costs = option.costs || []
  if (!costs.length) return null
  return (
    <span className="quest-costs">
      {costs.map((c, i) =>
        c.hp != null ? <em key={i} className="cost-hp">−{c.hp} 生命</em>
        : c.gold != null ? <em key={i} className="cost-gold">−{c.gold} 金币</em>
        : null)}
    </span>
  )
}

export default function QuestPanel({ view }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const ev = view.quest_event

  async function choose(idx) {
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, { action: 'quest_choose', option: idx })
      applyRun(res.run)
    } catch (e) {
      setErr(await handleActError(e, runId, applyRun))
    } finally {
      setBusy(false)
    }
  }

  if (!ev) return null

  if (ev.kind === 'ambush') {
    return (
      <div className="panel quest-panel ambush">
        <h3>⚔️ {ev.icon || ''} {ev.name || '奇遇'} · 伏击！</h3>
        <p className="quest-title">{ev.title}</p>
        <p className="quest-text">{ev.text}</p>
        {ev.ambush?.hint && <p className="quest-hint">{ev.ambush.hint}</p>}
      </div>
    )
  }

  return (
    <div className="panel quest-panel">
      <h3>{ev.scope === 'chain' ? `${ev.icon || '✨'} ${ev.name}` : '✨ 奇遇'} · {ev.title}</h3>
      <p className="quest-text">{ev.text}</p>
      <div className="quest-options">
        {(ev.options || []).map((o) => (
          <button
            key={o.index}
            className="quest-option"
            disabled={busy}
            onClick={() => choose(o.index)}
            title={o.hint || ''}
          >
            <span className="quest-option-text">{o.text}</span>
            <CostTags option={o} />
            {o.hint && <span className="quest-option-hint">{o.hint}</span>}
          </button>
        ))}
      </div>
      {err && <div className="error">{err}</div>}
    </div>
  )
}
