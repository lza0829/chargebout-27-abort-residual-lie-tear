import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

export default function Timeout() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () => api.会话('充电中').then(setList)
  useEffect(() => {
    load()
  }, [])
  const mark = async (id) => {
    try {
      const r = await api.超时(id)
      setMsg(`会话 ${r.会话编号} 已标 ${r.状态}`)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className="page">
      <h2>超时标记</h2>
      <p className="lead">
        对充电中会话标超时；急停中断请走{' '}
        <Link to="/中断" style={{ color: 'var(--lime)' }}>
          急停中断
        </Link>
        ，两处结局与残额口径不同。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>电量</th>
              <th>开始时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name}
                </td>
                <td>{s.kwh}</td>
                <td>{s.started_at}</td>
                <td>
                  <button type="button" onClick={() => mark(s.id)}>
                    标超时
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>
    </div>
  )
}
