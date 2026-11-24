import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  const load = () =>
    api
      .总览()
      .then(setO)
      .catch((e) => setErr(e.message))
  useEffect(() => {
    load()
  }, [])
  if (!o)
    return (
      <div className="page">
        <p className="msg">{err || '加载中'}</p>
      </div>
    )
  // 总览只由事实表重算：无 surface 快照旁路，缺口未清时已中断齐为假。
  const items = [
    ['桩位空闲', o.桩位空闲],
    ['桩位占用', o.桩位占用],
    ['桩位故障', o.桩位故障],
    ['桩位维护', o.桩位维护],
    ['充电中', o.充电中],
    ['结算中', o.结算中],
    ['已关账', o.已关账],
    ['已超时', o.已超时],
    ['已中断', o.已中断],
    ['已中断待收口', o.已中断待收口],
    ['已中断残额电量', o.已中断残额电量],
    ['已中断残额金额', o.已中断残额金额],
    ['未清缺口数', o.未清缺口数],
    ['缺口会话数', o.缺口会话数],
    ['在充电量', o.在充电量],
    ['本日关账金额', o.本日关账金额],
  ]
  return (
    <div className="page">
      <h2>总览</h2>
      <p className="lead">
        营业日 {o.营业日} · 充日{o.充日已封印 ? '已封印' : '未封印'} ·{' '}
        {o.已中断齐 ? (
          <span className="ok">已中断证据齐</span>
        ) : (
          <span className="msg">已中断存在未清缺口，急停宣称冻结</span>
        )}
      </p>
      <div className="grid">
        {items.map(([k, v]) => (
          <div className="stat" key={k}>
            <div className="k">{k}</div>
            <div
              className="v"
              style={k === '未清缺口数' && v > 0 ? { color: 'var(--danger)' } : undefined}
            >
              {v}
            </div>
          </div>
        ))}
      </div>
      <button type="button" onClick={load}>
        刷新
      </button>
      <p className="msg">{err}</p>
    </div>
  )
}
