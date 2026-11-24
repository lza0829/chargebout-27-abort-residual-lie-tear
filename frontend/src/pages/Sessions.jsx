import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Sessions() {
  const [list, setList] = useState([])
  const [detail, setDetail] = useState(null)
  const [msg, setMsg] = useState('')
  const load = useCallback(() => api.会话().then(setList), [])
  useEffect(() => {
    load()
  }, [load])

  const openDetail = async (id) => {
    setMsg('')
    try {
      const d = await api.会话详情(id)
      setDetail(d)
      if (d.中断) {
        // 详情与中断页/总览同源：直接复算残额对账
        const r = await api.中断回执(id)
        setDetail({ ...d, 回执: r })
      }
    } catch (e) {
      setMsg(e.message)
    }
  }

  const settle = async (id) => {
    setMsg('')
    try {
      const r = await api.结算(id)
      setMsg(`已关账：电量 ${r.电量}，金额 ${r.金额}（${r.来源}）`)
      await load()
      await openDetail(id)
    } catch (e) {
      setMsg(e.message)
    }
  }

  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>会话册</h2>
      <p className="lead">最近会话列表；点开可看脉冲与事件流水，中断会话可重放残额对账。</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>站点桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>金额</th>
              <th>营业日</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>{s.business_day}</td>
                <td>
                  <button type="button" onClick={() => openDetail(s.id)}>
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>
      {detail && (
        <div className="panel">
          <h3>
            {detail.slip_no} · <span className={`tag ${detail.status}`}>{detail.status}</span>
          </h3>
          <p>
            锁定电价 {detail.price_locked} · 电量 {detail.kwh} · 金额 {detail.amount} · 脉冲电量和{' '}
            {detail.脉冲电量和}
          </p>
          {detail.中断 && (
            <div className="panel" style={{ background: 'var(--panel2)' }}>
              <h3>中断残额</h3>
              <p>
                时间 {detail.中断.时间} · 原因 {detail.中断.原因} · 冻结 电量{' '}
                {detail.中断.残额.电量} × 单价 {detail.中断.残额.单价} = 金额{' '}
                {detail.中断.残额.金额}
              </p>
              <p className={detail.回执?.对账?.残额可复算 ? 'ok' : 'msg'}>
                {detail.中断.待收口 ? '待收口（可在结算页收口一次）' : `已收口于 ${detail.中断.收口时间}`}
                ；回执复算 {detail.回执?.对账?.复算金额}，脉冲电量和相符{' '}
                {detail.回执?.对账?.脉冲电量和相符 ? '是' : '否'}
              </p>
            </div>
          )}
          {detail.可结算 && (
            <div className="row">
              <button type="button" className="primary" onClick={() => settle(detail.id)}>
                {detail.status === '已中断' ? '收口残额' : '结算关账'}
              </button>
            </div>
          )}
          <h3>脉冲</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>电量增量</th>
                <th>功率</th>
              </tr>
            </thead>
            <tbody>
              {(detail.脉冲 || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.created_at}</td>
                  <td>{p.delta_kwh}</td>
                  <td>{p.power_kw}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>事件</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.事件 || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td>{e.action}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
