import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

function GapList({ gaps }) {
  if (!gaps?.length) return null
  return (
    <div className="panel" style={{ borderColor: 'var(--danger)' }}>
      <h3 style={{ color: 'var(--danger)' }}>未清可核缺口（急停宣称已冻结，禁止收口）</h3>
      <table>
        <thead>
          <tr>
            <th>类型</th>
            <th>明细</th>
            <th>登记时间</th>
          </tr>
        </thead>
        <tbody>
          {gaps.map((g, i) => (
            <tr key={i}>
              <td>{g.类型}</td>
              <td>{g.明细}</td>
              <td>{g.登记时间}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Receipt({ r }) {
  if (!r) return null
  const ok = r.急停宣称 && r.对账?.脉冲电量和相符 && r.对账?.残额可复算
  return (
    <div className="panel">
      <h3>
        中断回执 · {r.会话号} · <span className={`tag ${r.状态}`}>{r.状态}</span>{' '}
        {r.急停宣称 ? (
          <span className="tag 已关账">急停宣称成立</span>
        ) : (
          <span className="tag 已中断">急停宣称冻结</span>
        )}
      </h3>
      <div className="grid">
        <div className="stat">
          <div className="k">冻结残额金额</div>
          <div className="v">{r.残额?.金额}</div>
        </div>
        <div className="stat">
          <div className="k">残额电量</div>
          <div className="v">{r.残额?.电量}</div>
        </div>
        <div className="stat">
          <div className="k">锁定单价</div>
          <div className="v">{r.残额?.单价}</div>
        </div>
        <div className="stat">
          <div className="k">脉冲条数</div>
          <div className="v">{r.脉冲?.条数}</div>
        </div>
        <div className="stat">
          <div className="k">桩位 / 枪线</div>
          <div className="v" style={{ fontSize: '1rem' }}>
            {r.占用?.桩位状态} / {r.占用?.枪线状态}
          </div>
        </div>
        <div className="stat">
          <div className="k">待收口</div>
          <div className="v" style={{ fontSize: '1.2rem' }}>
            {r.结算?.待收口 ? (r.结算?.收口冻结 ? '冻结' : '是') : '否'}
          </div>
        </div>
      </div>
      <GapList gaps={r.缺口} />
      <p className={`msg ${ok ? 'ok' : ''}`}>
        对账：脉冲电量和 {r.脉冲?.电量和} 对会话电量 {r.残额?.电量}
        {r.对账?.脉冲电量和相符 ? ' 相符' : ' 不符'}；复算金额 {r.对账?.复算金额} / 冻结金额{' '}
        {r.对账?.冻结金额}
        {r.对账?.残额可复算 ? ' 可复算' : ' 不可复算'}；占用
        {r.占用?.占用已释放
          ? '已释放'
          : r.占用?.本会话仍占用
            ? '本会话仍占用（异常）'
            : `桩位已由新会话 ${r.占用?.当前占用会话} 接占`}
      </p>
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>动作</th>
            <th>备注</th>
          </tr>
        </thead>
        <tbody>
          {(r.事件 || []).map((e) => (
            <tr key={e.编号}>
              <td>{e.时间}</td>
              <td>{e.动作}</td>
              <td>{e.备注}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Abort() {
  const [open, setOpen] = useState([])
  const [aborted, setAborted] = useState([])
  const [audit, setAudit] = useState(null)
  const [receipt, setReceipt] = useState(null)
  const [msg, setMsg] = useState('')

  const load = useCallback(() => {
    Promise.all([api.会话('充电中'), api.会话('结算中'), api.会话('已超时')]).then(([a, b, c]) =>
      setOpen([...a, ...b, ...c]),
    )
    api.中断会话().then(setAborted)
    api.中断对账().then(setAudit)
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const abort = async (id) => {
    setMsg('')
    try {
      const r = await api.中断(id, '急停')
      setReceipt(r)
      setMsg(
        r.急停宣称
          ? `会话 ${r.会话编号} 已中断，残额 ${r.残额?.金额} 待收口`
          : `会话 ${r.会话编号} 已落中断态字，但存在未清缺口，急停宣称冻结`,
      )
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }

  const close = async (id) => {
    setMsg('')
    try {
      const r = await api.结算(id)
      setMsg(`会话 ${r.会话编号} 残额已收口入账：${r.金额}`)
      setReceipt(await api.中断回执(id))
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }

  const reconcile = async (id) => {
    setMsg('')
    try {
      const r = await api.缺口对账(id)
      setMsg(
        `缺口对账：核销 ${r.已核销.length} 条，新登记 ${r.新登记.length} 条，未清 ${r.未清缺口数} 条`,
      )
      setReceipt(await api.中断回执(id))
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }

  const replay = async (id) => {
    setMsg('')
    try {
      setReceipt(await api.中断回执(id))
      setMsg('回执已按冻结数据重放')
    } catch (e) {
      setMsg(e.message)
    }
  }

  return (
    <div className="page">
      <h2>急停中断</h2>
      <p className="lead">
        方案 B（残额钉枪缺口口径）：中断即冻结残额、释放桩位枪线并落事件；证据不齐登记可核缺口并冻结急停宣称，
        缺口未清禁止收口。残额保留可对账，随后可在结算页走一次收口。
      </p>

      <div className="panel">
        <h3>可中断会话</h3>
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位 · 枪线</th>
              <th>状态</th>
              <th>电量</th>
              <th>金额</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {open.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>
                  <button type="button" className="danger" onClick={() => abort(s.id)}>
                    急停中断
                  </button>
                </td>
              </tr>
            ))}
            {!open.length && (
              <tr>
                <td colSpan={6}>无可中断会话</td>
              </tr>
            )}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>

      <Receipt r={receipt} />

      <div className="panel">
        <h3>已中断会话</h3>
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>状态</th>
              <th>残额电量</th>
              <th>残额金额</th>
              <th>急停宣称</th>
              <th>未清缺口</th>
              <th>待收口</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {aborted.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>{s.急停宣称 ? '成立' : '冻结'}</td>
                <td style={{ color: s.缺口数 ? 'var(--danger)' : undefined }}>{s.缺口数}</td>
                <td>{s.待收口 ? (s.缺口数 ? '冻结' : '是') : '否'}</td>
                <td className="row" style={{ border: 0 }}>
                  {s.待收口 && !s.缺口数 && (
                    <button type="button" className="primary" onClick={() => close(s.id)}>
                      收口残额
                    </button>
                  )}
                  <button type="button" onClick={() => replay(s.id)}>
                    重放回执
                  </button>
                  <button type="button" onClick={() => reconcile(s.id)}>
                    缺口对账
                  </button>
                </td>
              </tr>
            ))}
            {!aborted.length && (
              <tr>
                <td colSpan={8}>暂无已中断会话</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>中断对账</h3>
        {!audit ? (
          <p className="msg">加载中</p>
        ) : (
          <>
            <p className={`msg ${audit.合计问题 === 0 && audit.未清缺口数 === 0 ? 'ok' : ''}`}>
              合计问题 {audit.合计问题}：已中断仍占用 {audit.已中断仍占用.length}，残额静默清零{' '}
              {audit.残额静默清零.length}，脉冲与电量漂移 {audit.脉冲与电量漂移.length}
              ，中断态半截 {audit.中断态半截?.length ?? 0}；持久化未清缺口 {audit.未清缺口数}
            </p>
            {!!audit.未清缺口?.length && (
              <table>
                <thead>
                  <tr>
                    <th>可核缺口</th>
                    <th>会话号</th>
                    <th>类型</th>
                    <th>明细</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.未清缺口.map((x, i) => (
                    <tr key={i}>
                      <td>{x.会话编号}</td>
                      <td>{x.会话号}</td>
                      <td>{x.类型}</td>
                      <td>{x.明细}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {!!audit.已中断仍占用.length && (
              <table>
                <thead>
                  <tr>
                    <th>仍占用</th>
                    <th>会话号</th>
                    <th>桩位状态</th>
                    <th>枪线状态</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.已中断仍占用.map((x) => (
                    <tr key={x.会话编号}>
                      <td>{x.会话编号}</td>
                      <td>{x.会话号}</td>
                      <td>{x.桩位状态}</td>
                      <td>{x.枪线状态}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {!!audit.残额静默清零.length && (
              <table>
                <thead>
                  <tr>
                    <th>静默清零</th>
                    <th>会话号</th>
                    <th>电量</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.残额静默清零.map((x) => (
                    <tr key={x.会话编号}>
                      <td>{x.会话编号}</td>
                      <td>{x.会话号}</td>
                      <td>{x.电量}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {!!audit.脉冲与电量漂移.length && (
              <table>
                <thead>
                  <tr>
                    <th>漂移</th>
                    <th>会话号</th>
                    <th>脉冲电量和</th>
                    <th>会话电量</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.脉冲与电量漂移.map((x) => (
                    <tr key={x.会话编号}>
                      <td>{x.会话编号}</td>
                      <td>{x.会话号}</td>
                      <td>{x.脉冲电量和}</td>
                      <td>{x.会话电量}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </div>
  )
}
