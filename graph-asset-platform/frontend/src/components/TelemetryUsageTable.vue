<template>
  <section class="tut">
    <div class="tut-head">
      <h2 class="tut-title">调用底表</h2>
      <span class="tut-hint">每次 REST/MCP 图谱工具调用一行；可切对象级细粒度</span>
    </div>

    <div class="tut-bar">
      <el-radio-group v-model="scope" size="small" @change="reload">
        <el-radio-button value="call">调用级</el-radio-button>
        <el-radio-button value="object">对象级</el-radio-button>
      </el-radio-group>
      <el-select v-model="endpoints" multiple collapse-tags clearable filterable
        size="small" placeholder="端点（全部）" class="tut-ep" @change="reload">
        <el-option v-for="e in ENDPOINTS" :key="e.value" :label="e.label" :value="e.value" />
      </el-select>
      <el-select v-model="days" size="small" class="tut-days" @change="reload">
        <el-option :value="7" label="近 7 天" />
        <el-option :value="30" label="近 30 天" />
        <el-option :value="90" label="近 90 天" />
        <el-option :value="0" label="全部时间" />
      </el-select>
      <el-input v-model="q" size="small" placeholder="账号 / 工号" clearable
        class="tut-q" @keyup.enter="reload" @clear="reload" />
      <el-button size="small" @click="reload">查询</el-button>
      <el-pagination small layout="total, prev, pager, next" :total="total"
        :page-size="size" :current-page="page" @current-change="onPage" />
    </div>

    <el-table v-loading="loading" :data="rows" size="small">
      <el-table-column label="时间" width="150">
        <template #default="{ row }">
          <span class="mono">{{ fmtTs(row.ts) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="通道" width="80">
        <template #default="{ row }">
          <span class="tut-caller" :class="`c-${row.caller}`">{{ callerLabel(row.caller) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="endpoint" label="端点" width="150" />
      <el-table-column label="对象 / 参数" min-width="240">
        <template #default="{ row }">
          <span v-if="row.level === 'object'" class="mono" :title="row.obj_type">{{ row.obj_id }}</span>
          <JsonPopover v-else :value="row.params" />
        </template>
      </el-table-column>
      <el-table-column prop="user" label="账号" width="100" />
      <el-table-column label="工号" width="80">
        <template #default="{ row }">
          <span class="mono">{{ row.operator || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="会话" width="100">
        <template #default="{ row }">
          <span class="mono" :title="row.session_id">{{ row.session_id ? row.session_id.slice(0, 10) : '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="结果" min-width="160">
        <template #default="{ row }">
          <JsonPopover :value="row.result" />
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  ElButton, ElInput, ElOption, ElPagination, ElRadioButton, ElRadioGroup,
  ElSelect, ElTable, ElTableColumn,
} from 'element-plus'
import {
  fetchTelemetryUsage, type TelemetryUsageRow,
} from '../api'
import JsonPopover from './JsonPopover.vue'

const ENDPOINTS = [
  { value: '/md', label: 'POST /md' },
  { value: '/domains', label: 'POST /domains' },
  { value: '/search', label: 'POST /search' },
  { value: '/files', label: 'POST /files' },
  { value: 'mcp:get_md', label: 'mcp:get_md' },
  { value: 'mcp:get_domains', label: 'mcp:get_domains' },
  { value: 'mcp:search_graph', label: 'mcp:search_graph' },
  { value: 'mcp:search_files', label: 'mcp:search_files' },
  { value: 'mcp:search_objects', label: 'mcp:search_objects' },
  { value: 'mcp:search_md', label: 'mcp:search_md' },
  { value: 'mcp:get_object', label: 'mcp:get_object' },
]

const scope = ref<'call' | 'object'>('call')
const endpoints = ref<string[]>([])
const days = ref(30)
const q = ref('')
const rows = ref<TelemetryUsageRow[]>([])
const total = ref(0)
const page = ref(1)
const size = 50
const loading = ref(false)

function dateStr(offsetDays: number): string {
  if (!offsetDays) return ''
  const d = new Date(Date.now() - offsetDays * 86400000)
  return d.toISOString().slice(0, 10)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const r = await fetchTelemetryUsage({
      scope: scope.value,
      start: dateStr(days.value),
      endpoint: endpoints.value.join(','),
      q: q.value.trim(),
      page: page.value,
      size,
    })
    rows.value = r.rows
    total.value = r.total
  } finally {
    loading.value = false
  }
}

function reload(): void {
  page.value = 1
  void load()
}

function onPage(p: number): void {
  page.value = p
  void load()
}

function fmtTs(ts: string): string {
  // UTC → 本地 yyyy-MM-dd HH:mm:ss
  const d = new Date(ts)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function callerLabel(c: string): string {
  return c === 'mcp' ? 'MCP' : 'REST'
}

onMounted(load)
</script>

<style scoped>
.tut { display: flex; flex-direction: column; gap: var(--space-3); }
.tut-head { display: flex; align-items: baseline; gap: var(--space-3); }
.tut-title { font-family: var(--display); font-size: 15px; font-weight: 600; color: var(--text); margin: 0; }
.tut-hint { font-size: 11px; color: var(--text-faint); }
.tut-bar { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.tut-ep { width: 210px; }
.tut-days { width: 110px; }
.tut-q { width: 160px; }
.tut-caller { font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 999px; white-space: nowrap; }
.c-mcp { background: rgba(139, 92, 246, 0.12); color: #8b5cf6; }
.c-skill { background: rgba(14, 165, 233, 0.12); color: #0ea5e9; }
</style>
