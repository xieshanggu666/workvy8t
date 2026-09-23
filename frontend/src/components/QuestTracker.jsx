import React from 'react'

// 跨章节奇遇链追踪（侧栏）：显示已开放的链、当前分支与后续章节预告/终态。
const STATUS_LABEL = {
  open: '进行中',
  resolved: '已了结',
  closed: '已结束',
}

export default function QuestTracker({ view }) {
  const quests = view.quests || []
  if (!quests.length) return null
  return (
    <div className="panellist quest-tracker">
      <h3>🔮 奇遇链</h3>
      {quests.map((q) => (
        <div key={q.key} className={`quest-chain ${q.status}`} title={q.desc}>
          <div className="quest-chain-head">
            <span className="quest-chain-name">{q.icon} {q.name}</span>
            <span className={`quest-status q-${q.status}`}>{STATUS_LABEL[q.status] || q.status}</span>
          </div>
          <div className="quest-chain-wait">{q.waiting}</div>
          {q.status === 'open' && q.choice && (
            <div className="quest-chain-choice">抉择：{q.choice}</div>
          )}
        </div>
      ))}
    </div>
  )
}
