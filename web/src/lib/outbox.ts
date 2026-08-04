// Messages the session has not taken yet, and the one thing allowed to hand them over.
//
// A session refuses a message for exactly one reason: it is parked on a decision only a person can
// make, and taking a message would mean starting a turn, which would discard the parked one. So a
// message that cannot be delivered is *held*, and the interface shows it as held rather than
// pretending it was sent.
//
// The shape of this file is a reaction to how the previous version failed. Delivery used to be
// attempted from five places — the composer, the end of a locally-driven turn, two branches of the
// read-only attach effect, and a `sessionRunning` edge — and each of them read state that delivery
// itself changed. Sending set `isStreaming`, `isStreaming` was a dependency of the effect that
// triggered sending, and a refused message was put back in the queue. That is a cycle with no
// bottom: refuse, re-queue, re-trigger, refuse. A person who denied a permission and then typed
// something watched their message be retried forever and delivered never.
//
// Two rules keep it from coming back, and they are the whole design:
//
//   1. **One owner.** Nothing else may deliver. There is no second path to keep in step with this
//      one, so there is no pair of paths that can disagree.
//   2. **A trigger this cannot produce.** Delivery is attempted when a person does something: they
//      type a message, or they answer the decision that was blocking it. Delivery changes the
//      queue, the in-flight flag and the session's turn state — and *none of those is a trigger*.
//      A refusal therefore ends the attempt instead of scheduling the next one. Retrying is
//      something a person causes, not something the failure causes.
//
// Nothing here polls, retries on a timer, or listens to the session's own activity. Those are the
// mechanisms that made the loop possible, and their absence is what makes another one impossible.

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

/**
 * Why the queue is not moving, which is a different question from whether it is empty.
 *
 * Named rather than a boolean because the two reasons need different words on screen and have
 * different ways out. "Waiting on a decision" is somebody's turn to act and resolves itself the
 * moment they do. "Could not reach the session" is a fault, and saying "queued" about it is the
 * same lie as saying "sent" about a message nobody took.
 */
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
  /**
   * The message being handed over right now.
   *
   * It is in the list, because it has not been taken yet, but it is not *waiting* for anything —
   * it is going. A view that labels it "queued" tells somebody their message is stuck for the
   * few milliseconds before it lands, which reads as a fault every single time a message is sent
   * normally.
   */
  delivering: string | null;
}

export interface OutboxPorts {
  /** Hand one message to the session. Must not throw; a thrown error is treated as `failed`. */
  deliver: (message: OutboxMessage) => Promise<Delivery>;
  /**
   * Whether the session is parked on a decision right now, as the transcript sees it.
   *
   * An optimisation and not the authority: it saves a round trip that would be refused anyway,
   * and when it is wrong the refusal still governs. The authority is always the session's answer.
   */
  parked: () => boolean;
  /** Called whenever the queue or the blocked flag changes, so a view can re-render. */
  changed: (state: OutboxState) => void;
}

export class Outbox {
  private messages: OutboxMessage[] = [];
  private held: OutboxHold = null;
  private handing: string | null = null;
  // One attempt at a time. Two overlapping pumps would deliver the same head twice, which is the
  // one error a queue must never make.
  private pumping = false;
  // The conversation these messages were typed into. Empty until one exists.
  private session = "";

  constructor(private readonly ports: OutboxPorts) {}

  state(): OutboxState {
    return { messages: [...this.messages], hold: this.held, delivering: this.handing };
  }

  /**
   * Point the queue at a conversation.
   *
   * Two different events look identical to an effect keyed on the session id, and only one of
   * them may throw messages away. A session that did not exist yet *becoming* known is what
   * happens the moment a first message creates one — and those messages are the reason it
   * exists, so dropping them there deletes exactly the thing the person was doing. Moving
   * between two conversations that both already exist is the other event, and there the queue
   * really does belong to the one being left.
   *
   * This lived in a React effect's cleanup, which sees only "the id changed" and cleared for
   * both. Sending a first message into a new conversation and immediately typing a second one
   * destroyed the second, silently, every time.
   */
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
    // Not while a decision is outstanding: the answer is known, and asking again would produce a
    // second refusal and a second thing to tell them about. It goes when the decision is made.
    // An unreachable hold is different — typing again is a perfectly good moment to find out
    // whether the session can be reached now.
    if (this.held !== "decision") void this.pump();
  }

  /**
   * The decision that was blocking delivery has been answered.
   *
   * One of the two retry triggers, and both are caused by a person. Neither can be caused by a
   * delivery, which is what stops a refusal from feeding itself.
   */
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
        // Asked before each message rather than once: answering one decision can leave another
        // open, and the second message must not be thrown at a session that is parked again.
        if (this.ports.parked()) {
          this.hold("decision");
          return;
        }
        const head = this.messages[0];
        // Marked before the await, so a view never draws it as waiting. Announced in the same
        // tick as the `add` that started this, so the two settle into one render rather than a
        // frame of "Queued" flashing past on a message that was about to be sent anyway.
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
          // It never reached the session, so it keeps its place *and says so*. Leaving it
          // labelled "queued" would be the same lie as the one this class exists to stop: a
          // message that looks handled and is not. No timer either — a retry is something a
          // person asks for, here as everywhere else.
          this.hold("unreachable");
          return;
        }
        // Delivered. It leaves the queue only now, on the session's own word — never on a guess
        // about what probably happened to it.
        this.messages = this.messages.filter((message) => message.id !== head.id);
        this.held = null;
        this.announce();
      }
    } finally {
      this.pumping = false;
    }
  }

  private hold(reason: OutboxHold): void {
    // Always announced, even when the reason is the one already showing. This used to return
    // early on an unchanged reason, which was true of the reason and false of the state: the
    // message that had been in flight was no longer in flight, and skipping the announcement
    // left a view saying it was. Two failures in a row and the composer showed a message being
    // delivered forever. A state with more than one field cannot be announced conditionally on
    // one of them.
    this.held = reason;
    this.announce();
  }
}
