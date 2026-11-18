<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import * as api from "../api/endpoints";
import { useChat } from "../stores/chat";

const chat = useChat();
const identity = ref("root");
const displayName = ref("");
const password = ref("");
const first = ref(false);
const checked = ref(false);
const busy = ref(false);
const err = ref("");
// 被限流/锁定时的倒计时（秒）：>0 时按钮禁用，每秒减一
const lockedSeconds = ref(0);
let countdown: ReturnType<typeof setInterval> | undefined;

function startCountdown(seconds: number) {
  lockedSeconds.value = seconds;
  clearInterval(countdown);
  countdown = setInterval(() => {
    lockedSeconds.value -= 1;
    if (lockedSeconds.value <= 0) clearInterval(countdown);
  }, 1000);
}

onUnmounted(() => clearInterval(countdown));

// 用户表为空 = 第一次访问：这时才允许建超级用户，之后这里只能登录
onMounted(async () => {
  try {
    first.value = (await api.firstRun()).first_run;
  } catch {
    // 后端没起时按登录页走，提交时再报错
  } finally {
    checked.value = true;
  }
});

async function submit() {
  busy.value = true;
  err.value = "";
  try {
    if (first.value) {
      await chat.bootstrapSuperUser(identity.value.trim(), displayName.value.trim(), password.value);
    } else {
      await chat.login(identity.value.trim(), password.value);
    }
  } catch (e) {
    err.value = (e as Error).message;
    const wait = (e as { retryAfter?: number }).retryAfter ?? 0;
    if (wait > 0) startCountdown(wait);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="login">
    <form @submit.prevent="submit">
      <h2 style="margin: 0 0 4px">{{ first ? "初始化超级用户" : "Aegra 登录" }}</h2>
      <p class="muted" style="margin: 0 0 8px">
        {{ first ? "系统还没有账号：第一个用户就是超级用户，之后由它给其他人开号。" : "用账号登录后开始对话" }}
      </p>
      <input v-model="identity" placeholder="账号（字母/数字/下划线，3-20 位）" autocomplete="username" />
      <input v-if="first" v-model="displayName" placeholder="显示名（10 字以内）" />
      <input
        v-model="password"
        type="password"
        :placeholder="first ? '密码（至少 8 位）' : '密码'"
        autocomplete="current-password"
      />
      <button class="primary" :disabled="busy || !checked || lockedSeconds > 0">
        {{
          lockedSeconds > 0
            ? `请等待 ${lockedSeconds} 秒`
            : busy
              ? first
                ? "初始化中…"
                : "登录中…"
              : first
                ? "创建超级用户"
                : "登录"
        }}
      </button>
      <p v-if="err" class="error" style="padding: 0">{{ err }}</p>
    </form>
  </div>
</template>
