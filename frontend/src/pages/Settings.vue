<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const day = ref('')
const scope = ref(null)
const chosen = ref('')
const msg = ref('')
const err = ref('')

onMounted(async () => {
  const [d, s] = await Promise.all([api.getDay(), api.getCloseScope()])
  day.value = d.business_day
  scope.value = s
  chosen.value = s.scope
})

async function save() {
  msg.value = ''; err.value = ''
  try {
    await api.setDay(day.value)
    msg.value = '已更新营业日'
  } catch (e) { err.value = e.message }
}

async function saveScope() {
  msg.value = ''; err.value = ''
  try {
    const r = await api.setCloseScope(chosen.value)
    scope.value = { ...scope.value, ...r }
    msg.value = `封账口径已切换为「${r.label}」；已封 ${r.sealed_batch_count} 个批次保持原快照不变`
  } catch (e) { err.value = e.message }
}
</script>

<template>
  <h2>设置</h2>
  <div class="row">
    <label>营业日 <input v-model="day" placeholder="YYYY-MM-DD" /></label>
    <button class="btn" @click="save">保存</button>
  </div>

  <div class="panel" v-if="scope">
    <h2>封账合计口径</h2>
    <p class="msg">全局互斥，只贯彻一种口径；已封批次保留封账当时的快照，切换不会改写其数字。</p>
    <div class="scope-choices">
      <label v-for="o in scope.options" :key="o.value" class="scope-choice" :class="{ on: chosen === o.value }">
        <span class="scope-head">
          <input type="radio" name="close-scope" :value="o.value" v-model="chosen" />
          <strong>{{ o.label }}</strong>
        </span>
        <small>{{ o.description }}</small>
      </label>
    </div>
    <div class="row">
      <button class="btn" @click="saveScope" :disabled="chosen === scope.scope">保存口径</button>
      <span class="msg">当前生效：<span class="tag">{{ scope.label }}</span></span>
    </div>
  </div>

  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>
</template>
