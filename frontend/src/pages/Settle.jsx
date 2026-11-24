import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Settle() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () =>
    Promise.all([
      api.会话('充电中'),
      api.会话('已超时'),
      api.会话('结算中'),
      api.会话('已中断'),
    ]).then(([a, b, c, d]) => setList([...a, ...b, ...c, ...d]))
  useEffect(() => {
    load()
  }, [])
  const go = async (s) => {
    setMsg('')
    try {
      const r = await api.结算(s.id)
      setMsg(
        `已关账（${r.来源}）：电量 ${r.电量}，金额 ${r.金额}` +
          (s.status === '已中断' ? '，中断残额已收口' : ''),
      )
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className="page">
      <h2>结算关账</h2>
      <p className="lead">
        按锁定电价与累计电量关账，释放桩位与枪线。已中断会话保留冻结残额，由此处走一次收口；
        存在未清缺口的中断会话，收口冻结，须先在急停页做缺口对账。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>金额</th>
              <th>未清缺口</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => {
              const frozen = s.status === '已中断' && (s.缺口数 ?? 0) > 0
              return (
                <tr key={s.id}>
                  <td>{s.slip_no}</td>
                  <td>
                    {s.site_name} · {s.pile_name}
                  </td>
                  <td>
                    <span className={`tag ${s.status}`}>{s.status}</span>
                  </td>
                  <td>{s.kwh}</td>
                  <td>{s.amount}</td>
                  <td style={{ color: frozen ? 'var(--danger)' : undefined }}>
                    {s.status === '已中断' ? (s.缺口数 ?? 0) : '—'}
                  </td>
                  <td>
                    <button
                      type="button"
                      className={frozen ? 'danger' : 'primary'}
                      disabled={frozen}
                      title={frozen ? '缺口未清，禁止收口' : ''}
                      onClick={() => go(s)}
                    >
                      {frozen ? '收口冻结' : s.status === '已中断' ? '收口残额' : '结算关账'}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className={`msg ${msg.startsWith('已关账') ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
