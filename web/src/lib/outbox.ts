// Messages the session has not taken yet, and the one thing allowed to hand them over.

/** What became of one attempt to hand a message over. */
export type Delivery =
  /** The session took it. It is gone from the queue. */
  | "accepted"
  /** The session is parked on a decision and took nothing. It stays queued, and stays visible. */
  | "refused"
  /** The attempt did not reach the session at all. It stays queued. */
  | "failed";

export interface OutboxMessage {
  id: string;
  text: string;
  dataParts: Record<string, unknown>[];
}

/** Why the queue is not moving, which is a different question from whether it is empty. */
export type OutboxHold =
  /** The session is parked on a decision. Answering it releases these. */
  | "decision"
  /** The last attempt never reached the session. It will go on the next attempt. */
  | "unreachable"
  /** Nothing is wrong: the queue is empty, or delivery is under way. */
  | null;

export interface OutboxState {
  /** In the order they were typed. Every one of these is undelivered — that is what being here means. */
  messages: OutboxMessage[];
  hold: OutboxHold;
  /** The message being handed over right now: in the list, but going rather than waiting. */
  delivering: string | null;
}

export interface OutboxPorts {
  /** Hand one message to the session. Must not throw; a thrown error is treated as `failed`. */
  deliver: (message: OutboxMessage) => Promise<Delivery>;
  /** Whether the session is parked right now as the transcript sees it, an optimisation rather than the authority. */
  parked: () => boolean;
  /** Called whenever the queue or the blocked flag changes, so a view can re-render. */
  changed: (state: OutboxState) => void;
}

export class Outbox {
  private messages: OutboxMessage[] = [];
  private held: OutboxHold = null;
  private handing: string | null = null;
  // One attempt at a time, since two overlapping pumps would deliver the same head twice.
  private pumping = false;
  // The conversation these messages were typed into. Empty until one exists.
  private session = "";

  constructor(private readonly ports: OutboxPorts) {}

  state(): OutboxState {
    return { messages: [...this.messages], hold: this.held, delivering: this.handing };
  }

  /** Point the queue at a conversation, keeping the messages when the session is only now coming into being. */
  retarget(session: string): void {
    if (session === this.session) return;
    const leavingRealConversation = this.session !== "";
    this.session = session;
    if (leavingRealConversation) this.clear();
  }

  /** A person typed something. */
  add(message: OutboxMessage): void {
    this.messages = [...this.messages, message];
    this.announce();
    // Not while a decision is outstanding, since the answer is known and asking again only refuses twice.
    if (this.held !== "decision") void this.pump();
  }

  /** The decision that was blocking delivery has been answered, which is one of the two retry triggers. */
  released(): void {
    if (this.held !== "decision") return;
    this.held = null;
    this.announce();
    void this.pump();
  }

  /** Try again after a failure to reach the session. The other person-caused trigger. */
  retry(): void {
    if (this.held !== "unreachable") return;
    this.held = null;
    this.announce();
    void this.pump();
  }

  /** A person removed a queued message before it went. */
  remove(id: string): void {
    const remaining = this.messages.filter((message) => message.id !== id);
    if (remaining.length === this.messages.length) return;
    this.messages = remaining;
    this.announce();
  }

  /** Switching to another session: this queue belonged to the one being left. */
  clear(): void {
    if (this.messages.length === 0 && this.held === null) return;
    this.messages = [];
    this.held = null;
    this.handing = null;
    this.announce();
  }

  private announce(): void {
    this.ports.changed(this.state());
  }

  private async pump(): Promise<void> {
    if (this.pumping) return;
    this.pumping = true;
    try {
      while (this.messages.length > 0) {
        // Asked before each message, since answering one decision can leave another open.
        if (this.ports.parked()) {
          this.hold("decision");
          return;
        }
        const head = this.messages[0];
        // Marked before the await, so a view never draws it as waiting, and announced in the same tick.
        this.handing = head.id;
        this.announce();
        let outcome: Delivery;
        try {
          outcome = await this.ports.deliver(head);
        } catch {
          outcome = "failed";
        }
        this.handing = null;
        if (outcome === "refused") {
          this.hold("decision");
          return;
        }
        if (outcome === "failed") {
          // It never reached the session, so it keeps its place and says so.
          this.hold("unreachable");
          return;
        }
        // Delivered: it leaves the queue only now, on the session's own word.
        this.messages = this.messages.filter((message) => message.id !== head.id);
        this.held = null;
        this.announce();
      }
    } finally {
      this.pumping = false;
    }
  }

  private hold(reason: OutboxHold): void {
    // Always announced, even when the reason is unchanged, because the state around it is not.
    this.held = reason;
    this.announce();
  }
}
