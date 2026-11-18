<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useChat } from "../stores/chat";
import { uploadFiles } from "../api/endpoints";
import MessageItem from "./MessageItem.vue";
import InterruptCard from "./InterruptCard.vue";

const chat = useChat();
const draft = ref("");
const scroller = ref<HTMLElement | null>(null);

const placeholder = computed(() =>
  chat.threadId ? "问点什么…（Ctrl+Enter 发送）" : "输入问题，会自动新建会话",
);

async function scrollDown() {
  await nextTick();
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}

watch(() => [chat.messages.length, chat.streaming, chat.interrupts.length], scrollDown);

const uploading = ref(false);
const uploads = ref<{ name: string; ok: boolean; text: string }[]>([]);

/** 上传到当前会话；必须先有 thread_id（发过一句话）。逐文件结果直接贴出来。 */
async function pick(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = ""; // 清掉才能再选同一个文件
  if (!files.length) return;
  uploading.value = true;
  try {
    // 没有会话就先建一个：用户通常先传文件再提问
    const { files: results } = await uploadFiles(await chat.ensureThread(), files);
    uploads.value = results.map((r) =>
      r.status === "ok"
        ? { name: r.name, ok: true, text: r.sheets.length ? `已上传（${r.sheets.length} 个工作表）` : "已上传" }
        : { name: r.name, ok: false, text: r.error },
    );
  } catch (err) {
    uploads.value = [{ name: files.map((f) => f.name).join("、"), ok: false, text: err instanceof Error ? err.message : String(err) }];
  } finally {
    uploading.value = false;
    await scrollDown();
  }
}

async function submit() {
  const text = draft.value.trim();
  if (!text || chat.running) return;
  draft.value = "";
  await chat.send(text);
  await scrollDown();
}
</script>

<template>
  <div class="messages" ref="scroller">
    <p v-if="!chat.messages.length && !chat.running" class="muted">
      试试：「混合键合 DOE 里良率最高的是哪个？」「CBAM 铝的默认因子是多少？」「SBOL3 中 gene 和 CDS 的区别」
    </p>

    <MessageItem v-for="m in chat.messages" :key="m.key" :message="m" />

    <div v-if="chat.streaming" class="msg ai">
      <div class="who">assistant</div>
      <div class="body">{{ chat.streaming }}</div>
    </div>

    <InterruptCard v-if="chat.interrupts.length" :interrupt="chat.interrupts[0]!" />

    <p v-if="chat.running && !chat.streaming" class="muted">思考中…</p>
  </div>

  <ul v-if="uploads.length" class="uploads">
    <li v-for="u in uploads" :key="u.name" :class="{ bad: !u.ok }">
      {{ u.name }}：{{ u.text }}
    </li>
  </ul>

  <div class="composer">
    <textarea
      v-model="draft"
      :placeholder="placeholder"
      @keydown.ctrl.enter.prevent="submit"
      @keydown.meta.enter.prevent="submit"
    />
    <div style="display: flex; flex-direction: column; gap: 6px">
      <label class="ghost upload" :class="{ off: uploading }" title="一次可传多个文件">
        {{ uploading ? "上传中…" : "上传文件" }}
        <input type="file" multiple hidden :disabled="uploading" @change="pick" />
      </label>
      <button class="primary" :disabled="chat.running || !draft.trim()" @click="submit">发送</button>
      <button v-if="chat.running" class="ghost" @click="chat.cancel()">停止</button>
    </div>
  </div>
</template>
