# Dayseal（日封台）

收银流水按营业日归集，日结封账后锁定；长短款可查。

- 前端 http://127.0.0.1:5700
- API http://127.0.0.1:10700

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10700 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5700
```

## 封账合计口径

全局互斥，只贯彻一种；口径配置、流水过滤、封账预览、批次快照、长短款同源。

- **仅现金 `cash_only`**：系统合计只累加支付方式为现金的有效流水。非现金流水仍保留可查，
  但不进入封账对照与现金长短款；批次快照记录 `scope=cash_only` 与现金笔数 `cash_line_count`。
- **全支付方式 `all_methods`**：系统合计累加该营业日全部有效流水。实盘现金与系统合计之差
  仍记长短款；批次同时按支付方式写出分项合计（`close_batch_methods`）。

在「设置 · 封账合计口径」或 `PUT /api/settings/close-scope`（`{"scope": "cash_only|all_methods"}`）切换。
切换只改配置，**不会改写已 `sealed` 批次的数字**。封账成功后，该营业日的有效流水禁止增、改、删
（服务层 409 + SQLite 触发器双保险）。

## 测试

```powershell
cd backend
.\.venv\Scripts\pip install pytest httpx
.\.venv\Scripts\pytest tests
```
