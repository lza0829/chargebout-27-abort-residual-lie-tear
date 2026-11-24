# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

## 急停中断残额口径（全局唯一，方案 B）

中断保留**可结算残额**，全项目只贯彻这一种，不得混用：

- 中断回执：冻结残额（电量 × 锁定单价 = 金额），写 `aborted_at / abort_reason`，落“异常中断”事件；
- 占用：中断即释放桩位与枪线（只把“占用”翻回空闲/可用，不动人工置的故障/维护）；
- 脉冲：中断后拒绝再记脉冲，残额不再变动；脉冲电量和与冻结电量在回执里对账；
- 收口：已中断会话允许在结算入口走**一次**收口（`已中断 → 已关账`），收口金额恒等于冻结残额，另落“中断结算收口”事件与 `settled_at`；
- 展示：详情、中断页、回执、充日“未结”、总览“已中断”桶同源，不在展示层另算残额；
- 对账：`GET /api/中断对账` 暴露“已中断仍占用 / 残额静默清零 / 脉冲与电量漂移”；
- 并发：写入口统一 `BEGIN IMMEDIATE`，中断与脉冲互斥，回执与落账不会长期不一致。

超时强制关不在本口径内（另立需求）。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。
