import { describe, expect, it } from "vitest";

import { alidadeReducer, dfActions } from "../../../../src/app/dfSlice";

const plan = [
  { id: "step-1", title: "Sales by region", status: "done" as const },
  { id: "step-2", title: "Trend over time", status: "in_progress" as const },
];

describe("analyst run state (LangGraph engine)", () => {
  it("tracks the live plan on the running draft", () => {
    let state = alidadeReducer(
      undefined,
      dfActions.createDraftNode({
        id: "draft-1",
        displayId: "draft-1",
        parentNodeId: "orders",
        parentTableId: "orders",
        source: ["orders"],
        interaction: [],
      })
    );
    state = alidadeReducer(state, dfActions.updateDraftAgentPlan({ draftId: "draft-1", plan }));
    expect(state.draftNodes[0].derive.agentPlan).toEqual(plan);
  });

  it("keeps run-id resume tokens and drops legacy trajectory tokens on load", () => {
    const state = alidadeReducer(
      undefined,
      dfActions.loadState({
        textTurns: [
          {
            kind: "text", id: "t-new", displayId: "t-new", textKind: "clarify",
            content: "Which metric?", parentNodeId: "orders", createdAt: 1,
            resume: { runId: "run-1", completedStepCount: 0 },
            agentPlan: plan,
          },
          {
            kind: "text", id: "t-old", displayId: "t-old", textKind: "clarify",
            content: "Which metric?", parentNodeId: "orders", createdAt: 2,
            resume: { trajectory: [{ role: "system", content: "..." }], completedStepCount: 1 },
          },
        ],
      })
    );
    const byId = Object.fromEntries(state.textTurns.map((turn) => [turn.id, turn]));
    expect(byId["t-new"].resume).toEqual({ runId: "run-1", completedStepCount: 0 });
    expect(byId["t-new"].agentPlan).toEqual(plan);
    expect(byId["t-old"]).not.toHaveProperty("resume");
    expect(byId["t-old"].content).toBe("Which metric?");
  });
});
