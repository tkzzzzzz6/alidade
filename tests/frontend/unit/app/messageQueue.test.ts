import { describe, expect, it } from "vitest";

import {
  enqueue,
  nextQueueAction,
  removeQueued,
  updateQueued,
} from "../../../../src/app/messageQueue";

describe("follow-up message queue", () => {
  it("enqueues trimmed text and ignores blanks", () => {
    let queue = enqueue([], "  first  ");
    queue = enqueue(queue, "   ");
    queue = enqueue(queue, "second");
    expect(queue.map(m => m.text)).toEqual(["first", "second"]);
    expect(new Set(queue.map(m => m.id)).size).toBe(2);
  });

  it("edits in place and drops a message emptied by an edit", () => {
    const queue = enqueue(enqueue([], "a"), "b");
    const edited = updateQueued(queue, queue[0].id, " A2 ");
    expect(edited.map(m => m.text)).toEqual(["A2", "b"]);
    expect(edited[0].id).toBe(queue[0].id);
    expect(updateQueued(queue, queue[1].id, "  ").map(m => m.text)).toEqual(["a"]);
  });

  it("removes by id", () => {
    const queue = enqueue(enqueue([], "a"), "b");
    expect(removeQueued(queue, queue[0].id).map(m => m.text)).toEqual(["b"]);
  });

  it("sends the next message only after a completed run", () => {
    const queue = enqueue(enqueue([], "a"), "b");
    const action = nextQueueAction(queue, "completed", false, true);
    expect(action.kind).toBe("send");
    if (action.kind === "send") {
      expect(action.message.text).toBe("a");
      expect(action.rest.map(m => m.text)).toEqual(["b"]);
    }
    expect(nextQueueAction(queue, "running", false, true).kind).toBe("idle");
  });

  it("waits for the user's answer when the run paused on a question", () => {
    const queue = enqueue([], "a");
    expect(nextQueueAction(queue, "paused", false, true).kind).toBe("wait-answer");
  });

  it("holds after an error, a stop, an explicit hold, or when no run can start", () => {
    const queue = enqueue([], "a");
    expect(nextQueueAction(queue, "error", false, true).kind).toBe("hold");
    expect(nextQueueAction(queue, "cancelled", false, true).kind).toBe("hold");
    expect(nextQueueAction(queue, "completed", true, true).kind).toBe("hold");
    expect(nextQueueAction(queue, "completed", false, false).kind).toBe("hold");
  });

  it("does nothing with an empty queue", () => {
    expect(nextQueueAction([], "completed", false, true).kind).toBe("idle");
    expect(nextQueueAction([], "error", true, false).kind).toBe("idle");
  });
});
