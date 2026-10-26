import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalTurnInput,
  createSubmissionState,
  createTurnSubmission,
  draftForInput,
  markSubmissionFailed,
  markSubmissionRecovered,
  recoverOriginal,
  submissionForInput,
} from "./turn-submission.ts";

test("same content retry keeps the original submission token", () => {
  let next = 0;
  const tokenFactory = () => `token-${++next}`;
  const first = createTurnSubmission("  What changed?  ", tokenFactory);
  const retry = submissionForInput(first, "What changed?", tokenFactory);

  assert.equal(retry.token, first.token);
  assert.equal(next, 1);
});

test("edited content gets a different submission token", () => {
  let next = 0;
  const tokenFactory = () => `token-${++next}`;
  const first = createTurnSubmission("Question A", tokenFactory);
  const edited = submissionForInput(first, "Question B", tokenFactory);

  assert.notEqual(edited.token, first.token);
  assert.equal(edited.input, "Question B");
});

test("canonical input differs only by surrounding whitespace", () => {
  assert.equal(canonicalTurnInput("  A \n"), "A");
});

test("editing after an unknown outcome preserves the original recovery token", () => {
  let next = 0;
  const tokenFactory = () => `token-${++next}`;
  const failedA = markSubmissionFailed(
    createSubmissionState(),
    createTurnSubmission("Question A", tokenFactory),
  );
  const editedB = draftForInput(failedA, "Question B", tokenFactory);

  assert.equal(recoverOriginal(editedB)?.input, "Question A");
  assert.equal(recoverOriginal(editedB)?.token, "token-1");
  assert.equal(editedB.draft?.token, "token-2");
  assert.equal(draftForInput(editedB, "Question A", tokenFactory).draft?.token, "token-3");
});

test("recovering B clears only B and preserves A's uncertain token", () => {
  const state = {
    draft: { token: "token-2", input: "Question B" },
    recovery: { token: "token-1", input: "Question A" },
  };

  const recovered = markSubmissionRecovered(state, state.draft);

  assert.equal(recovered.draft, null);
  assert.deepEqual(recovered.recovery, state.recovery);

  const original = markSubmissionRecovered(recovered, state.recovery);

  assert.equal(original.draft, null);
  assert.equal(original.recovery, null);
});
