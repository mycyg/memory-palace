/** Durable host transport. Recall and lifecycle policy live in the service. */
import { createHash, randomUUID } from "node:crypto";
import {
  mkdirSync,
  openSync,
  closeSync,
  fsyncSync,
  readFileSync,
  renameSync,
  writeFileSync,
  unlinkSync,
} from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Config } from "./config.js";

export type InjectFn = (text: string) => string;
export interface ToolObservation {
  sessionId: string;
  cwd: string;
  toolName: string;
  callId: string;
  args: unknown;
  isError: boolean;
  value?: unknown;
  errorMessage?: string;
  contentText: string;
}

export class ServiceRuntime {
  private pending = new Map<string, Promise<void>>();
  private sessions = new Map<string, string>();
  private closed = new Set<string>();
  private activeSteps = new Map<string, number>();
  private context = new Map<string, {
    sessionId: string;
    cwd: string;
    body: string;
    delivery: { id: string; body_hash: string };
    claimedTurn?: number;
  }>();
  constructor(readonly config: Config) {}
  private root() {
    return process.env.EVENTMEM_HOME ?? join(homedir(), ".memorypalace");
  }
  private spool(value: unknown): { raw: string; path: string } {
    const raw = JSON.stringify(value);
    const directory = join(this.root(), "host-spool");
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const path = join(directory, createHash("sha256").update(raw).digest("hex") + ".json");
    const temporary = path + "." + randomUUID() + ".tmp";
    const descriptor = openSync(temporary, "wx", 0o600);
    try {
      writeFileSync(descriptor, raw);
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
    renameSync(temporary, path);
    return { raw, path };
  }
  private async settle(sessionId: string, cwd: string, delivery: { id: string; body_hash: string }, body: string, state: "accepted" | "discarded"): Promise<void> {
    if (createHash("sha256").update(body).digest("hex") !== delivery.body_hash) return;
    const receipt = {
      session: sessionId,
      scope: { project: cwd, persona: "default", collection: "default", world: "real" },
      delivery_id: delivery.id,
      body_hash: delivery.body_hash,
      state,
    };
    const pending = this.spool({ event: "context_receipt", payload: receipt });
    try {
      const token = readFileSync(join(this.root(), "local-token"), "utf8").trim();
      const response = await fetch(
        (process.env.EVENTMEM_URL ?? "http://127.0.0.1:8319") + "/v1/context/receipts",
        { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
          body: JSON.stringify(receipt), signal: AbortSignal.timeout(7000) },
      );
      if (!response.ok) throw new Error("Context receipt was not accepted");
      unlinkSync(pending.path);
    } catch {
      // The service worker replays the exact accepted receipt after recovery.
    }
  }
  private send(
    sessionId: string,
    cwd: string,
    event: string,
    payload: Record<string, unknown>,
    inject?: InjectFn,
  ): void {
    this.sessions.set(sessionId, cwd);
    const normalized = {
      event,
      payload: {
        ...payload,
        session_id: sessionId,
        cwd,
        host: "deepseek-harness",
      },
    };
    const pending = this.spool(normalized);
    const operation = (this.pending.get(sessionId) ?? Promise.resolve()).then(
      async () => {
        try {
          const token = readFileSync(
            join(this.root(), "local-token"),
            "utf8",
          ).trim();
          const response = await fetch(
            (process.env.EVENTMEM_URL ?? "http://127.0.0.1:8319") +
              "/v1/host/events",
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${token}`,
              },
              body: pending.raw,
              signal: AbortSignal.timeout(7000),
            },
          );
          if (!response.ok)
            throw new Error(`MemoryPalace HTTP ${response.status}`);
          const result = (await response.json()) as {
            text?: string;
            delivery?: { id: string; body_hash: string };
          };
          try {
            unlinkSync(pending.path);
          } catch {
            /* worker may already have replayed receipt */
          }
          if (result.text && inject && !this.closed.has(sessionId)) {
            const messageId = inject(result.text);
            if (result.delivery && messageId)
              this.context.set(messageId, { sessionId, cwd, body: result.text, delivery: result.delivery });
          }
        } catch {
          // The private spool remains for service replay. A failed call never
          // acknowledges receipt or computes an alternate local recall ranking.
        }
      },
    );
    this.pending.set(sessionId, operation);
  }
  sessionStart(
    sessionId: string,
    cwd: string,
    inject: InjectFn,
    source?: string,
  ): void {
    this.closed.delete(sessionId);
    const completedCompaction = source === "compact";
    this.send(
      sessionId,
      cwd,
      completedCompaction ? "compact" : "start",
      completedCompaction ? { command_id: randomUUID() } : {},
      this.config.injectWorkingSet ? inject : undefined,
    );
  }
  async preAction(
    sessionId: string,
    cwd: string,
    name: string,
    args: unknown,
    inject: InjectFn,
  ): Promise<void> {
    this.send(
      sessionId,
      cwd,
      "pre_action",
      { tool_name: name, tool_input: args },
      inject,
    );
    await this.flush(sessionId);
  }
  message(
    sessionId: string,
    cwd: string,
    role: string,
    text: string,
    seq: number,
  ): void {
    if (this.config.writeFeed && text)
      this.send(sessionId, cwd, "message", {
        role,
        text,
        command_id: `message:${seq}`,
        extract: role === "user",
      });
  }
  toolResult(observation: ToolObservation, inject: InjectFn): void {
    if (!this.config.writeFeed) return;
    this.send(
      observation.sessionId,
      observation.cwd,
      "tool",
      { ...observation },
      inject,
    );
  }
  todoWrite(
    sessionId: string,
    cwd: string,
    todos: readonly unknown[],
    inject: InjectFn,
  ): void {
    if (!this.config.writeFeed) return;
    this.send(
      sessionId,
      cwd,
      "tool",
      {
        tool_name: "TodoWrite",
        tool_input: { todos },
        command_id: createHash("sha256")
          .update(JSON.stringify(todos))
          .digest("hex"),
      },
      inject,
    );
  }
  boundary(
    sessionId: string,
    cwd: string,
    kind: string,
    data: Record<string, unknown>,
  ): void {
    if (this.config.writeFeed)
      this.send(sessionId, cwd, "boundary", {
        data: { kind, ...data },
        command_id: `${kind}:${String(data.seq ?? randomUUID())}`,
      });
  }
  turnStarted(sessionId: string, _turn: number): void {
    this.activeSteps.delete(sessionId);
  }
  contextClaimed(sessionId: string, messageId: string, body: string, turn: number): void {
    const pending = this.context.get(messageId);
    if (pending?.sessionId === sessionId && pending.body === body)
      pending.claimedTurn = turn;
  }
  contextDiscarded(sessionId: string, messageId: string, body: string): void {
    const pending = this.context.get(messageId);
    if (pending?.sessionId !== sessionId || pending.body !== body) return;
    this.context.delete(messageId);
    this.queueReceipt(sessionId, pending, "discarded");
  }
  private queueReceipt(sessionId: string, pending: { cwd: string; body: string; delivery: { id: string; body_hash: string } }, state: "accepted" | "discarded"): void {
    const earlier = this.pending.get(sessionId) ?? Promise.resolve();
    this.pending.set(sessionId, earlier.then(() => this.settle(
      sessionId, pending.cwd, pending.delivery, pending.body, state,
    )));
  }
  contextEntered(sessionId: string, messageId: string, body: string): void {
    const pending = this.context.get(messageId);
    const activeTurn = this.activeSteps.get(sessionId);
    // DSH opens a step before appending its model-visible user/message.
    if (pending?.sessionId !== sessionId || pending.body !== body ||
        activeTurn === undefined || pending.claimedTurn !== activeTurn) return;
    this.context.delete(messageId);
    this.queueReceipt(sessionId, pending, "accepted");
  }
  stepStarted(sessionId: string, turn: number): void {
    this.activeSteps.set(sessionId, turn);
  }
  stepEnded(sessionId: string, turn: number): void {
    if (this.activeSteps.get(sessionId) === turn) this.activeSteps.delete(sessionId);
  }
  turnEnded(sessionId: string, turn: number): void {
    this.stepEnded(sessionId, turn);
    for (const [messageId, pending] of this.context) {
      if (pending.sessionId !== sessionId || pending.claimedTurn !== turn) continue;
      this.context.delete(messageId);
      this.queueReceipt(sessionId, pending, "discarded");
    }
  }
  async flush(sessionId: string): Promise<void> {
    await this.pending.get(sessionId);
  }
  async flushAll(): Promise<void> {
    for (const sessionId of [...this.sessions.keys()]) this.drop(sessionId);
    await Promise.all(this.pending.values());
  }
  drop(sessionId: string): void {
    const cwd = this.sessions.get(sessionId);
    if (cwd)
      this.send(sessionId, cwd, "end", { command_id: `end:${sessionId}` });
    this.closed.add(sessionId);
    this.sessions.delete(sessionId);
    this.activeSteps.delete(sessionId);
    for (const [messageId, pending] of this.context)
      if (pending.sessionId === sessionId) this.context.delete(messageId);
  }
}
