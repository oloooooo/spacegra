<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import * as api from "../api/endpoints";
import { useChat } from "../stores/chat";

const chat = useChat();
const tab = ref<"tasks" | "spaces" | "users" | "cron">("tasks");

// ---------- 知识空间 ----------
const spaceList = ref<api.Space[]>([]);
const isSuper = ref(false);
const opened = ref<string | null>(null);
const files = ref<api.SpaceFile[]>([]);
const members = ref<string[]>([]);
const newSpace = ref({ name: "", path: "" });
const assignTo = ref({ identity: "", space: "" });
const notice = ref("");

async function loadSpaces() {
  const res = await api.spaces();
  spaceList.value = res.spaces;
  isSuper.value = res.is_super;
}

async function openSpace(name: string) {
  if (opened.value === name) {
    opened.value = null;
    return;
  }
  opened.value = name;
  files.value = [];
  members.value = [];
  try {
    files.value = (await api.spaceFiles(name)).files;
    if (isSuper.value) members.value = (await api.spaceUsers(name)).users;
  } catch (e) {
    notice.value = (e as Error).message;
  }
}

async function submitSpace() {
  notice.value = "";
  try {
    await api.createSpace(newSpace.value.name.trim(), newSpace.value.path.trim());
    newSpace.value = { name: "", path: "" };
    await loadSpaces();
    notice.value = "已创建";
  } catch (e) {
    notice.value = (e as Error).message;
  }
}

async function submitAssign() {
  notice.value = "";
  try {
    const res = await api.assignSpace(assignTo.value.identity.trim(), assignTo.value.space.trim());
    notice.value = res.message ?? "已分配";
    await loadSpaces();
    if (opened.value) members.value = (await api.spaceUsers(opened.value)).users;
  } catch (e) {
    notice.value = (e as Error).message;
  }
}

async function dropMember(identity: string) {
  if (!opened.value) return;
  try {
    await api.removeSpaceUser(opened.value, identity);
    members.value = (await api.spaceUsers(opened.value)).users;
  } catch (e) {
    notice.value = (e as Error).message;
  }
}

// ---------- 账号（只有超管能看到这一栏）----------
const adminUsers = ref<api.AdminUser[]>([]);
const newUser = ref({ identity: "", display_name: "", password: "" });

async function loadUsers() {
  adminUsers.value = (await api.users()).users;
}

async function addUser() {
  notice.value = "";
  try {
    await api.createUser({ ...newUser.value, is_super: false });
    newUser.value = { identity: "", display_name: "", password: "" };
    await loadUsers();
    notice.value = "已创建";
  } catch (e) {
    notice.value = (e as Error).message;
  }
}

async function resetUserPassword(identity: string) {
  const password = prompt(`给 ${identity} 设置新密码（至少 8 位）`);
  if (!password) return;
  notice.value = "";
  try {
    await api.resetPassword(identity, password);
    notice.value = `已重置 ${identity} 的密码`;
  } catch (e) {
    notice.value = (e as Error).message;
  }
}

async function removeUser(identity: string) {
  if (!confirm(`删除账号 ${identity}？`)) return;
  notice.value = "";
  try {
    await api.deleteUser(identity);
    await loadUsers();
  } catch (e) {
    notice.value = (e as Error).message;
  }
}

// ---------- 定时任务 ----------
const crons = ref<api.Cron[]>([]);
const newCron = ref({ prompt: "", schedule: "0 9 * * *" });

async function loadCrons() {
  crons.value = await api.searchCrons();
}

async function addCron() {
  notice.value = "";
  try {
    const t = await api.createThread(); // 线程级 cron：结果落在这个会话里
    await api.createCron(t.thread_id, {
      assistant_id: "supervisor",
      schedule: newCron.value.schedule.trim(),
      timezone: "Asia/Shanghai",
      input: { messages: [{ role: "user", content: newCron.value.prompt.trim() }] },
    });
    newCron.value.prompt = "";
    await Promise.all([loadCrons(), chat.loadThreads()]);
  } catch (e) {
    notice.value = (e as Error).message;
  }
}

async function toggleCron(c: api.Cron) {
  await api.patchCron(c.cron_id, { enabled: !c.enabled });
  await loadCrons();
}

async function removeCron(c: api.Cron) {
  if (!confirm("删除这个定时任务？")) return;
  await api.deleteCron(c.cron_id);
  await loadCrons();
}

function cronTitle(c: api.Cron) {
  return c.payload?.input?.messages?.[0]?.content ?? c.cron_id.slice(0, 8);
}

onMounted(async () => {
  await loadSpaces().catch((e) => (notice.value = e.message));
  await loadCrons().catch(() => {});
});

watch(tab, (t) => {
  if (t === "cron") void loadCrons();
  if (t === "spaces") void loadSpaces();
  if (t === "users") void loadUsers();
});

const taskList = computed(() => Object.values(chat.tasks));

// 与服务端 _TERMINAL 对齐：终态任务不再显示"取消"
const DONE = new Set(["cancelled", "success", "error", "timeout", "interrupted"]);
const done = (status: string) => DONE.has(status);

async function stop(taskId: string) {
  try {
    await chat.cancelTask(taskId);
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e);
  }
}
</script>

<template>
  <aside class="panel" style="border-left: 1px solid var(--line); border-right: none">
    <div class="tabs">
      <button :class="{ active: tab === 'tasks' }" @click="tab = 'tasks'">任务 {{ taskList.length }}</button>
      <button :class="{ active: tab === 'spaces' }" @click="tab = 'spaces'">知识库</button>
      <button v-if="chat.isSuper" :class="{ active: tab === 'users' }" @click="tab = 'users'">用户</button>
      <button :class="{ active: tab === 'cron' }" @click="tab = 'cron'">定时</button>
    </div>

    <div class="panel-body">
      <p v-if="notice" class="muted" style="padding: 4px 6px">{{ notice }}</p>

      <!-- 子 agent 任务 -->
      <template v-if="tab === 'tasks'">
        <p v-if="!taskList.length" class="muted">本案暂无后台子任务。派活后这里会实时显示进度。</p>
        <div v-for="t in taskList" :key="t.task_id" class="card">
          <div class="row" style="justify-content: space-between">
            <h4>{{ t.agent_name }}</h4>
            <span :class="['badge', t.status]">{{ t.status }}</span>
          </div>
          <div class="row" style="justify-content: flex-end; margin-top: 2px">
            <button v-if="!done(t.status)" class="tiny ghost" @click="stop(t.task_id)">取消</button>
          </div>
          <div class="meta">{{ t.task_id.slice(0, 8) }} · {{ t.message_count ?? "-" }} 条消息</div>
          <div v-if="t.events.length" class="meta" style="margin-top: 6px">
            <div v-for="(e, i) in t.events.slice(-4)" :key="i">· {{ e }}</div>
          </div>
          <div v-if="t.text" class="meta" style="margin-top: 6px; white-space: pre-wrap">{{ t.text.slice(-400) }}</div>
        </div>
      </template>

      <!-- 知识空间 -->
      <template v-else-if="tab === 'spaces'">
        <div v-for="s in spaceList" :key="s.name" class="card">
          <div class="row" style="justify-content: space-between; cursor: pointer" @click="openSpace(s.name)">
            <h4>{{ s.name }}</h4>
            <span class="badge">{{ s.file_count }} 文件</span>
          </div>
          <div class="meta">{{ s.path }}<span v-if="!s.accessible"> · 无权限</span></div>

          <template v-if="opened === s.name">
            <div class="file-tree" style="margin-top: 8px">
              <div v-for="f in files" :key="f.path">
                {{ f.path }} <span class="ext">· {{ (f.size / 1024).toFixed(1) }}KB</span>
              </div>
              <p v-if="!files.length" class="muted">空目录</p>
            </div>
            <div v-if="isSuper && members.length" style="margin-top: 8px">
              <div class="muted">成员</div>
              <div class="row" v-for="u in members" :key="u" style="justify-content: space-between">
                <span class="meta">{{ u }}</span>
                <button class="tiny ghost" @click="dropMember(u)">移除</button>
              </div>
            </div>
          </template>
        </div>

        <template v-if="isSuper">
          <div class="card">
            <h4>新建空间</h4>
            <input v-model="newSpace.name" placeholder="空间名（如 bio-lab）" />
            <input v-model="newSpace.path" placeholder="目录（如 graphs/okf/bio-lab）" style="margin-top: 6px" />
            <button class="primary tiny" style="margin-top: 6px" @click="submitSpace">创建</button>
          </div>
          <div class="card">
            <h4>分配空间</h4>
            <input v-model="assignTo.identity" placeholder="账号（如 alice）" />
            <input v-model="assignTo.space" placeholder="空间名" style="margin-top: 6px" />
            <button class="primary tiny" style="margin-top: 6px" @click="submitAssign">分配</button>
          </div>
        </template>
      </template>

      <!-- 账号（超管） -->
      <template v-else-if="tab === 'users'">
        <div v-for="u in adminUsers" :key="u.identity" class="card">
          <div class="row" style="justify-content: space-between">
            <h4>{{ u.identity }}</h4>
            <span class="badge">{{ u.is_super ? "超级用户" : "普通用户" }}</span>
          </div>
          <div class="meta">{{ u.display_name }}</div>
          <div class="row" style="margin-top: 6px">
            <button class="tiny ghost" @click="resetUserPassword(u.identity)">重置密码</button>
            <button v-if="u.identity !== chat.identity" class="tiny ghost" @click="removeUser(u.identity)">删除</button>
          </div>
        </div>

        <div class="card">
          <h4>新建账号</h4>
          <input v-model="newUser.identity" placeholder="账号（字母/数字/下划线）" />
          <input v-model="newUser.display_name" placeholder="显示名（10 字以内）" style="margin-top: 6px" />
          <input v-model="newUser.password" type="password" placeholder="初始密码（至少 8 位）" style="margin-top: 6px" />
          <button class="primary tiny" style="margin-top: 6px" @click="addUser">创建</button>
          <p class="muted" style="margin: 6px 0 0">建完再到「知识库」把空间分配给他。</p>
        </div>
      </template>

      <!-- 定时任务 -->
      <template v-else>
        <div v-for="c in crons" :key="c.cron_id" class="card">
          <div class="row" style="justify-content: space-between">
            <h4 style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 150px">{{ cronTitle(c) }}</h4>
            <span :class="['badge', c.enabled ? 'success' : '']">{{ c.enabled ? "启用" : "已暂停" }}</span>
          </div>
          <div class="meta">{{ c.schedule }} · 下次 {{ c.next_run_date?.slice(5, 16) ?? "-" }}</div>
          <div class="row" style="margin-top: 6px">
            <button class="tiny ghost" @click="toggleCron(c)">{{ c.enabled ? "暂停" : "启用" }}</button>
            <button class="tiny ghost" @click="removeCron(c)">删除</button>
          </div>
        </div>

        <div class="card">
          <h4>新建定时任务</h4>
          <textarea v-model="newCron.prompt" placeholder="每次触发要 agent 做的事" style="height: 64px" />
          <input v-model="newCron.schedule" placeholder="cron 表达式，如 0 9 * * *" style="margin-top: 6px" />
          <button class="primary tiny" style="margin-top: 6px" @click="addCron">创建</button>
          <p class="muted" style="margin: 6px 0 0">创建后立即先跑一次，之后按表达式重复。</p>
        </div>
      </template>
    </div>
  </aside>
</template>
