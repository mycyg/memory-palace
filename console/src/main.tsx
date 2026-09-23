import { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { commandId, currentToken, request, scopeDefault, setToken, stamp, type Scope } from "./api";
import "./style.css";

type View = "overview" | "sources" | "records" | "continuation" | "families" | "recall" | "reminders" | "jobs" | "settings";
type Page<T = any> = { items: T[]; cursor?: string | null };
const labels: Record<View, string> = {
  overview: "概览", sources: "来源", records: "记录", continuation: "任务续接",
  families: "事件族", recall: "召回", reminders: "提醒", jobs: "作业", settings: "设置",
};
const kinds: Record<string, string> = {
  episode: "事件", fact: "事实", state: "状态", preference: "偏好", procedure: "方法",
  relationship: "关系", commitment: "承诺", reminder: "提醒", summary: "摘要",
  knowledge: "知识", checkpoint: "检查点", observation: "观察",
};

function displayTitle(record: any): string {
  return record.kind === "checkpoint" && record.title === "Session checkpoint"
    ? "任务检查点" : record.title || record.content?.slice(0, 90) || record.id;
}

function displayBody(record: any): string {
  const content = record.content || record.text || record.excerpt || "";
  if (record.kind !== "checkpoint") return content;
  try {
    const data = JSON.parse(content);
    const fields: [string, string][] = [
      ["目标", "goals"], ["已确认进展", "confirmed_progress"], ["未验证结果", "unverified_results"],
      ["阻塞", "blockers"], ["承诺", "commitments"], ["下次入口", "next_entry"],
    ];
    const lines = fields.filter(([, key]) => data[key]).map(([label, key]) => label + "：" + data[key]);
    return lines.length ? lines.join("\n") : content;
  } catch { return content; }
}

function defaultReminderPolicy(scope: Scope): string {
  return scope.project === "personal" && scope.collection === "default" && scope.persona === "default" && scope.world === "real"
    ? "default" : [scope.project, scope.collection, scope.persona, scope.world].join("/");
}

function App() {
  const [token, setCredential] = useState(currentToken());
  const [scope, setScope] = useState<Scope>(() => {
    try { return { ...scopeDefault, ...JSON.parse(localStorage.getItem("memorypalace-scope") || "{}") }; }
    catch { return scopeDefault; }
  });
  const [view, setView] = useState<View>("overview");
  const [overview, setOverview] = useState<any>({});
  const [sources, setSources] = useState<Page>({ items: [] });
  const [records, setRecords] = useState<Page>({ items: [] });
  const [families, setFamilies] = useState<Page>({ items: [] });
  const [jobs, setJobs] = useState<Page>({ items: [] });
  const [reminders, setReminders] = useState<Page>({ items: [] });
  const [selected, setSelected] = useState<any>(null);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [recallQuery, setRecallQuery] = useState("");
  const [recall, setRecall] = useState<any>(null);
  const [models, setModels] = useState("{}");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const scopeQuery = { project: scope.project, persona: scope.persona, collection: scope.collection, world: scope.world };

  const execute = useCallback(async (work: () => Promise<unknown>, success: string | ((result: any) => string) = "已保存") => {
    setBusy(true); setError("");
    try { const result = await work(); setNotice(typeof success === "function" ? success(result) : success); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  }, []);

  const load = useCallback(async (target: View) => {
    if (!currentToken()) return;
    const queryScope = { project: scope.project, persona: scope.persona, collection: scope.collection, world: scope.world };
    const actions: Record<View, () => Promise<void>> = {
      overview: async () => setOverview(await request("/v1/overview")),
      sources: async () => setSources(await request("/v1/sources", { query: { ...queryScope, limit: 100 } })),
      records: async () => setRecords(await request("/v1/memories", { query: { ...queryScope, query, kind, limit: 100 } })),
      continuation: async () => setRecords(await request("/v1/memories", { query: { ...queryScope, group: "timeline", limit: 100 } })),
      families: async () => {
        setFamilies(await request("/v1/families", { query: { ...queryScope, limit: 100 } }));
        setRecords(await request("/v1/memories", { query: { ...queryScope, limit: 100 } }));
      },
      recall: async () => undefined,
      reminders: async () => {
        setReminders(await request("/v1/reminders/state/schedules", { query: { ...queryScope, limit: 100 } }));
        setRecords(await request("/v1/memories", { query: { ...queryScope, limit: 100 } }));
      },
      jobs: async () => setJobs(await request("/v1/jobs", { query: { limit: 100 } })),
      settings: async () => setModels(JSON.stringify(await request("/v1/settings/models"), null, 2)),
    };
    await execute(actions[target], "");
  }, [scope, query, kind, execute]);

  useEffect(() => { localStorage.setItem("memorypalace-scope", JSON.stringify(scope)); }, [scope]);
  useEffect(() => { void load(view); }, [view, load, token]);

  async function readRecord(id: string) {
    await execute(async () => {
      setSelected(await request("/v1/memories/" + encodeURIComponent(id), { query: { length: 32000, budget: 32000 } }));
    }, "");
  }
  async function readSource(id: string) {
    await execute(async () => {
      const source: any = await request("/v1/sources/" + encodeURIComponent(id));
      if (source.media_type?.startsWith("text/") && source.byte_length <= 200000) {
        const response = await fetch(source.attachment_url, {
          headers: { Authorization: "Bearer " + currentToken() }, cache: "no-store",
        });
        if (response.ok) source.content = await response.text();
      }
      setSelected(source);
    }, "");
  }
  async function addSource(form: HTMLFormElement) {
    const values = new FormData(form);
    const file = values.get("file");
    const metadata = {
      namespace: "console", key: commandId(), scope,
      title: String(values.get("title") || "").trim(),
      text: file instanceof File && file.size ? "" : String(values.get("content") || "").trim(),
      kind: String(values.get("kind") || "observation"),
      authority: "explicit",
      extract: false,
    };
    if (!metadata.text && !(file instanceof File && file.size)) throw new Error("请填写内容或选择文件");
    if (file instanceof File && file.size) {
      const upload = new FormData();
      upload.set("metadata", JSON.stringify({ ...metadata, media_type: file.type || "application/octet-stream" }));
      upload.set("file", file);
      await request("/v1/sources/upload", { method: "POST", body: upload });
    } else await request("/v1/sources", { method: "POST", body: metadata });
    form.reset();
    await load(view);
  }
  async function addCheckpoint(form: HTMLFormElement) {
    const values = new FormData(form);
    const session = String(values.get("session") || "").trim();
    if (!session) throw new Error("请填写任务或会话 ID");
    const checkpoint = {
      goals: String(values.get("goals") || "").trim(),
      confirmed_progress: String(values.get("progress") || "").trim(),
      unverified_results: String(values.get("unverified") || "").trim(),
      blockers: String(values.get("blockers") || "").trim(),
      commitments: String(values.get("commitments") || "").trim(),
      next_entry: String(values.get("next") || "").trim(),
    };
    await request("/v1/sessions/boundary", {
      method: "POST", body: { event: "checkpoint", session, scope, scenario: "tool", command_id: commandId(), checkpoint },
    });
    form.reset(); await load("continuation");
  }
  async function revise(action: string) {
    if (!selected?.id || !selected?.revision) return;
    const id = selected.id as string;
    await execute(async () => {
      await request("/v1/memories/" + encodeURIComponent(id) + "/revisions", {
        method: "POST", body: { expected_revision: selected.revision, command_id: commandId(), action, reason: "Console action" },
      });
      await readRecord(id); await load(view);
    });
  }

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">M</span><div><strong>MemoryPalace</strong><small>工作记忆 · 2.0</small></div></div>
      <nav aria-label="主导航">
        {(Object.keys(labels) as View[]).map(key =>
          <button key={key} className={view === key ? "active" : ""} onClick={() => { setView(key); setSelected(null); setNotice(""); }}>{labels[key]}</button>
        )}
      </nav>
      <p className="sidebar-foot">本地服务 · 来源可追溯</p>
    </aside>
    <main>
      <header className="topbar">
        <div><span className="eyebrow">MEMORYPALACE / {labels[view]}</span><h1>{labels[view]}</h1></div>
        <button className="quiet" onClick={() => void load(view)}>刷新</button>
      </header>
      <section className="scopebar" aria-label="记忆范围">
        <label>项目 <input aria-label="项目" value={scope.project} onChange={e => setScope({ ...scope, project: e.target.value })} /></label>
        <label>集合 <input aria-label="集合" value={scope.collection} onChange={e => setScope({ ...scope, collection: e.target.value })} /></label>
        <label>访问令牌 <input aria-label="访问令牌" type="password" value={token} onChange={e => { setCredential(e.target.value); setToken(e.target.value); }} placeholder="本地服务令牌" /></label>
      </section>
      {error && <div className="alert error" role="alert">{error}</div>}
      {notice && <div className="alert" role="status">{notice}</div>}
      {busy && <div className="loading">处理中…</div>}

      {view === "overview" && <div className="stack">
        <div className="intro"><h2>工作上下文</h2><p>来源、记录和续接线索汇集在同一范围，打开记录即可核对依据。</p></div>
        <div className="stats">
          {([["来源", overview.sources], ["记录", overview.records], ["事件族", overview.families], ["待整理", overview.dirty]] as const).map(([name, value]) =>
            <article className="stat" key={name}><span>{name}</span><strong>{value ?? "—"}</strong></article>)}
        </div>
        <div className="grid two">
          <article className="panel"><h3>记录类型</h3>{Object.entries(overview.kinds ?? {}).length
            ? Object.entries(overview.kinds ?? {}).map(([name, count]) => <div className="row" key={name}><span>{kinds[name] ?? name}</span><strong>{String(count)}</strong></div>)
            : <p className="empty">当前没有记录。</p>}</article>
          <article className="panel"><h3>作业状态</h3>{Object.entries(overview.jobs ?? {}).length
            ? Object.entries(overview.jobs ?? {}).map(([name, count]) => <div className="row" key={name}><span>{name}</span><strong>{String(count)}</strong></div>)
            : <p className="empty">当前没有作业。</p>}</article>
        </div>
      </div>}

      {view === "sources" && <div className="grid two wide-left">
        <section className="panel"><div className="panel-heading"><h2>来源列表</h2><span>{sources.items.length} 条</span></div>
          <div className="list">{sources.items.map(source => <button className="list-item" key={source.id} onClick={() => void readSource(source.id)}>
            <div><strong>{source.title || source.namespace}</strong><small>{source.namespace} · {source.version} · {stamp(source.received_at)}</small></div>
            <p>{source.excerpt || (source.media_type === "text/plain" ? "无文本" : source.media_type)}</p>
          </button>)}{!sources.items.length && <p className="empty">还没有来源。添加一段文本或上传文件。</p>}</div>
        </section>
        <section className="panel"><h2>添加来源</h2><form onSubmit={e => { e.preventDefault(); void execute(() => addSource(e.currentTarget)); }}>
          <label>标题<input name="title" required maxLength={1000} /></label>
          <label>类型<select name="kind">{["observation", "knowledge", "fact", "episode", "procedure", "commitment"].map(k => <option key={k} value={k}>{kinds[k]}</option>)}</select></label>
          <label>内容<textarea name="content" rows={7} placeholder="记录实际发生的事、决定或资料内容" /></label>
          <label>附件<input name="file" type="file" /></label>
          <button type="submit">保存来源</button>
        </form></section>
      </div>}

      {view === "records" && <div className="stack">
        <section className="toolbar"><label>搜索<input aria-label="搜索记录" value={query} onChange={e => setQuery(e.target.value)} /></label>
          <label>类型<select aria-label="记录类型" value={kind} onChange={e => setKind(e.target.value)}><option value="">全部类型</option>{Object.entries(kinds).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label>
          <button onClick={() => void load("records")}>查找</button></section>
        <RecordList records={records.items} read={readRecord} />
      </div>}

      {view === "continuation" && <div className="grid two wide-left">
        <section className="panel"><h2>检查点与承诺</h2><RecordList records={records.items.filter(r => ["checkpoint", "commitment", "reminder"].includes(r.kind))} read={readRecord} /></section>
        <section className="panel"><h2>保存检查点</h2><form onSubmit={e => { e.preventDefault(); void execute(() => addCheckpoint(e.currentTarget)); }}>
          <label>任务或会话 ID<input name="session" required placeholder="例如 project-migration" /></label>
          <label>目标<textarea name="goals" rows={2} /></label>
          <label>已确认进展<textarea name="progress" rows={3} /></label>
          <label>未验证结果<textarea name="unverified" rows={2} /></label>
          <label>阻塞<textarea name="blockers" rows={2} /></label>
          <label>承诺<textarea name="commitments" rows={2} /></label>
          <label>下次入口<textarea name="next" rows={2} /></label>
          <button type="submit">保存检查点</button>
        </form></section>
      </div>}

      {view === "families" && <div className="grid two wide-left">
        <section className="panel"><h2>事件族</h2><div className="list">{families.items.map(family => <article className="family" key={family.id}>
          <div className="row"><strong>{family.title}</strong><span className="pill">{family.state}</span></div>
          <p>{family.summary?.text || family.summary?.content || "摘要待生成"}</p>
          <small>{family.kind} · {family.member_count ?? family.members?.length ?? 0} 个成员 · r{family.revision}</small>
          <div className="actions"><button className="quiet" onClick={() => void execute(async () => setSelected(await request("/v1/families/" + encodeURIComponent(family.id))), "")}>查看成员</button>
          {family.state !== "published" && <button className="quiet" onClick={() => void execute(async () => {
            await request("/v1/families/" + encodeURIComponent(family.id), { method: "POST", body: { expected_revision: family.revision, action: "publish" } }); await load("families");
          })}>发布</button>}</div>
        </article>)}{!families.items.length && <p className="empty">当前范围没有事件族。</p>}</div></section>
        <section className="panel"><h2>新建事件族</h2><form onSubmit={e => { e.preventDefault(); const form = e.currentTarget; void execute(async () => {
          const values = new FormData(form);
          const members = records.items.filter(r => values.getAll("members").includes(r.id)).map(r => r.id);
          if (!members.length) throw new Error("请选择至少一条记录");
          await request("/v1/families", { method: "POST", body: { scope, title: String(values.get("title") || ""), members, kind: values.get("kind") } });
          form.reset(); await load("families");
        }); }}>
          <label>标题<input name="title" required /></label>
          <label>层级<select name="kind"><option value="event">事件</option><option value="family">事件族</option><option value="volume">卷册</option></select></label>
          <fieldset><legend>成员记录</legend><div className="choices">{records.items.map(record => <label key={record.id}><input type="checkbox" name="members" value={record.id} /><span>{record.title || record.content?.slice(0, 70) || record.id}</span></label>)}</div></fieldset>
          <button type="submit">建立事件族</button>
        </form></section>
      </div>}

      {view === "recall" && <div className="stack"><section className="panel">
        <h2>按问题召回</h2><form onSubmit={e => { e.preventDefault(); void execute(async () => setRecall(await request("/v1/recall", {
          method: "POST", body: { scope, query: recallQuery, budget: 4000, scenario: "tool", phase: "search", explain: true },
        })), ""); }}><label>问题<input aria-label="召回问题" value={recallQuery} onChange={e => setRecallQuery(e.target.value)} required placeholder="例如：上次迁移卡在什么地方？" /></label><button type="submit">召回</button></form>
      </section>{recall && <section className="panel"><h2>召回结果</h2><pre className="context-output">{recall.text}</pre><div className="list">{(recall.items ?? []).map((item: any) => <button className="list-item" key={item.id} onClick={() => void readRecord(item.id)}><strong>{item.title || item.id}</strong><small>{kinds[item.kind] || item.kind} · r{item.revision} · {item.confirmation}</small></button>)}</div></section>}</div>}

      {view === "reminders" && <div className="grid two wide-left"><section className="panel"><h2>明确安排的提醒</h2>
        <div className="list">{reminders.items.map(item => <article className="family" key={item.id}>
          <div className="row"><strong>{item.record_id}</strong><span className="pill">{item.state}</span></div>
          <small>{stamp(item.due_at)} · r{item.revision}</small>
          {["scheduled", "queued", "paused"].includes(item.state) && <div className="actions">
            {(["cancel", item.state === "paused" ? "resume" : "pause"] as string[]).map(action => <button className="quiet" key={action} onClick={() => void execute(async () => {
              const result: any = await request("/v1/reminders/" + encodeURIComponent(item.id), { method: "POST", body: { expected_revision: item.revision, action } });
              await load("reminders");
              return result;
            }, result => result.reconciliation_required ? "可能已发送；请按投递 ID 核对回执" : "已保存")}>{action === "cancel" ? "取消" : action === "resume" ? "恢复" : "暂停"}</button>)}
          </div>}
        </article>)}{!reminders.items.length && <p className="empty">当前没有已安排的提醒。</p>}</div>
      </section><section className="panel"><h2>安排提醒</h2><form onSubmit={e => { e.preventDefault(); const form = e.currentTarget; void execute(async () => {
        const values = new FormData(form);
        await request("/v1/reminders", { method: "POST", body: { command_id: commandId(), policy_id: String(values.get("policy_id")), record_id: String(values.get("record_id")), due_at: new Date(String(values.get("due_at"))).toISOString(), trigger: "reminder" } });
        form.reset(); await load("reminders");
      }); }}>
        <label>记录<select name="record_id" required>{records.items.filter(r => ["commitment", "reminder"].includes(r.kind)).map(r => <option key={r.id} value={r.id}>{r.title || r.content?.slice(0, 80) || r.id}</option>)}</select></label>
        <label>策略 ID<input name="policy_id" required key={defaultReminderPolicy(scope)} defaultValue={defaultReminderPolicy(scope)} /></label>
        <label>到期时间<input name="due_at" type="datetime-local" required /></label>
        <p className="hint">提醒使用已配置的本地回调策略。建议与发送状态会分别保留。</p>
        <button type="submit">安排提醒</button>
      </form></section></div>}

      {view === "jobs" && <section className="panel"><h2>后台作业</h2><div className="list">{jobs.items.map(job => <article className="family" key={job.id}>
        <div className="row"><strong>{job.kind}</strong><span className="pill">{job.state}</span></div>
        <small>{stamp(job.updated_at)} · {job.id}</small>{job.error && <p className="error-text">{job.error}</p>}
        {["failed", "waiting_config", "retry"].includes(job.state) && <button className="quiet" onClick={() => void execute(async () => {
          await request("/v1/jobs/" + encodeURIComponent(job.id) + "/retry", { method: "POST" }); await load("jobs");
        })}>重试</button>}
      </article>)}{!jobs.items.length && <p className="empty">当前没有后台作业。</p>}</div></section>}

      {view === "settings" && <section className="panel settings"><h2>模型设置</h2><p className="hint">每个角色指定 endpoint、model、协议和可选用量价格。密钥由环境变量提供。</p>
        <form onSubmit={e => { e.preventDefault(); void execute(async () => {
          const value = JSON.parse(models);
          await request("/v1/settings/models", { method: "PUT", body: value });
          await load("settings");
        }); }}><label>模型角色 JSON<textarea aria-label="模型角色 JSON" value={models} onChange={e => setModels(e.target.value)} rows={20} spellCheck={false} /></label><button type="submit">保存设置</button></form>
      </section>}
    </main>
    {selected && <div className="scrim" onMouseDown={e => { if (e.target === e.currentTarget) setSelected(null); }}>
      <aside className="drawer" role="dialog" aria-label="详情" aria-modal="true">
        <div className="row"><span className="eyebrow">DETAIL</span><button className="quiet" onClick={() => setSelected(null)}>关闭</button></div>
        <h2>{displayTitle(selected)}</h2><p className="meta">{selected.kind || selected.namespace || "事件族"} · {selected.status || selected.state || ""} · r{selected.revision || 1}</p>
        <pre className="body">{displayBody(selected) || selected.summary?.text || ""}</pre>
        {selected.id?.startsWith("mem_") && selected.source_ids?.length > 0 && <section><h3>来源</h3>{selected.source_ids.map((id: string) => <button className="link" key={id} onClick={() => void readSource(id)}>{id}</button>)}</section>}
        {selected.record_ids?.length > 0 && <section><h3>派生记录</h3>{selected.record_ids.map((id: string) => <button className="link" key={id} onClick={() => void readRecord(id)}>{id}</button>)}</section>}
        {selected.members?.length > 0 && <section><h3>成员</h3>{selected.members.map((id: string) => <button className="link" key={id} onClick={() => void readRecord(id)}>{id}</button>)}</section>}
        {selected.id?.startsWith("mem_") && selected.revision && <div className="actions">
          {selected.status === "archived" ? <button onClick={() => void revise("restore")}>恢复</button> : <button className="quiet" onClick={() => void revise("archive")}>归档</button>}
          {selected.status === "unverified" && <button onClick={() => void revise("confirm")}>确认</button>}
        </div>}
      </aside>
    </div>}
  </div>;
}

function RecordList({ records, read }: { records: any[]; read: (id: string) => Promise<void> }) {
  return <div className="list record-list">{records.map(record => <button className="list-item" key={record.id} onClick={() => void read(record.id)}>
    <div className="row"><strong>{displayTitle(record)}</strong><span className="pill">{kinds[record.kind] || record.kind}</span></div>
    <p>{displayBody(record).slice(0, 220)}</p><small>{stamp(record.updated_at || record.received_at)} · {record.confirmation} · r{record.revision} · {record.source_count ?? record.source_ids?.length ?? 0} 个来源</small>
  </button>)}{!records.length && <p className="empty">当前范围没有匹配的记录。</p>}</div>;
}

createRoot(document.getElementById("root")!).render(<App />);
