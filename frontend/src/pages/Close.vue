<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const preview = ref(null)
const declared = ref(0)
const batches = ref([])
const msg = ref('')
const err = ref('')

async function load() {
  preview.value = await api.closePreview()
  declared.value = preview.value.cash_total
  batches.value = await api.batches()
}

async function seal() {
  msg.value = ''; err.value = ''
  try {
    const r = await api.seal({
      business_day: preview.value.business_day,
      declared_cash: declared.value,
    })
    msg.value = `已按「${r.scope_label}」封账：系统 ${r.system_total.toFixed(2)} / 实盘 ${r.declared_cash.toFixed(2)} / 长短款 ${r.variance.toFixed(2)}`
    await load()
  } catch (e) { err.value = e.message }
}

function includedList(b) {
  return (b.methods || []).filter((m) => m.included).map((m) => `${m.pay_method} ${m.total.toFixed(2)}`).join(' · ') || '—'
}

function excludedList(b) {
  return (b.methods || []).filter((m) => !m.included).map((m) => `${m.pay_method} ${m.total.toFixed(2)}`).join(' · ') || '—'
}

onMounted(load)

/* align-18 */
function alignHint(batch) {
  if (!batch) return ''
  const v = Number(batch.variance || 0)
  const s = Number(batch.system_total || 0)
  const d = Number(batch.declared_cash || 0)
  return [s, d, v].join('/')
}
function alignRows(rows) {
  return (rows || []).map(r => ({ ...r, align: alignHint(r) }))
}
</script>

<template>
  <h2>日结封账</h2>
  <div v-if="preview" class="panel">
    <p>
      营业日 {{ preview.business_day }} ·
      口径 <span class="tag">{{ preview.scope_label }}</span>
      <span class="tag" :class="preview.sealed ? 'ok' : 'warn'">{{ preview.sealed ? '已封' : '未封' }}</span>
    </p>
    <p class="msg">{{ preview.scope_description }}</p>
    <div class="stats scope-stats">
      <div class="stat"><div class="label">系统合计（{{ preview.scope_label }}）</div><div class="value">{{ preview.system_total.toFixed(2) }}</div></div>
      <div class="stat"><div class="label">计入笔数</div><div class="value">{{ preview.line_count }}</div></div>
      <div class="stat"><div class="label">现金合计 / 笔数</div><div class="value">{{ preview.cash_total.toFixed(2) }} / {{ preview.cash_line_count }}</div></div>
      <div class="stat"><div class="label">当日有效流水</div><div class="value">{{ preview.day_line_count }} 笔</div></div>
    </div>

    <table>
      <thead><tr><th>支付方式</th><th>合计</th><th>笔数</th><th>计入本口径</th></tr></thead>
      <tbody>
        <tr v-for="m in preview.methods" :key="m.pay_method">
          <td>{{ m.pay_method }}</td>
          <td>{{ m.total.toFixed(2) }}</td>
          <td>{{ m.line_count }}</td>
          <td><span class="tag" :class="m.included ? 'ok' : 'warn'">{{ m.included ? '计入' : '仅保留可查' }}</span></td>
        </tr>
      </tbody>
    </table>

    <div class="row">
      <label>实盘现金 <input type="number" step="0.01" v-model.number="declared" /></label>
      <button class="btn" :disabled="preview.sealed" @click="seal">封账</button>
      <button class="btn ghost" @click="load">刷新</button>
    </div>
  </div>
  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>

  <div class="panel">
    <h2>封账批次</h2>
    <table>
      <thead>
        <tr>
          <th>批次</th><th>营业日</th><th>口径</th><th>系统</th><th>现金笔数</th>
          <th>实盘</th><th>差额</th><th>状态</th><th>可核分项</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in batches" :key="b.id">
          <td>{{ b.batch_no }}</td>
          <td>{{ b.business_day }}</td>
          <td><span class="tag">{{ b.scope === 'cash_only' ? '仅现金' : '全支付方式' }}</span></td>
          <td>{{ b.system_total.toFixed(2) }}</td>
          <td>{{ b.cash_line_count }}</td>
          <td>{{ b.declared_cash.toFixed(2) }}</td>
          <td>{{ b.variance.toFixed(2) }}</td>
          <td>{{ b.status }}</td>
          <td class="mono">
            <div>计入：{{ includedList(b) }}</div>
            <div class="muted">未计：{{ excludedList(b) }}</div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
