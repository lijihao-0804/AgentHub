/** Client-side identity for a conversation submission.

This module is deliberately separate from React so the retry rule can be
tested directly. A failed network write may have reached the backend. Reusing
the same content therefore resumes that logical submission, while changed
content is a new request and receives a new identity.
*/

export type TurnSubmission = {
  token: string;
  input: string;
};

export type TurnSubmissionState = {
  draft: TurnSubmission | null;
  recovery: TurnSubmission | null;
};

export function canonicalTurnInput(value: string): string {
  return value.trim();
}

export function createTurnSubmission(
  input: string,
  tokenFactory: () => string,
): TurnSubmission {
  return {
    token: tokenFactory(),
    input: canonicalTurnInput(input),
  };
}

export function submissionForInput(
  current: TurnSubmission | null,
  input: string,
  tokenFactory: () => string,
): TurnSubmission {
  const canonical = canonicalTurnInput(input);
  if (current && current.input === canonical && current.token.length > 0) {
    return current;
  }
  return createTurnSubmission(canonical, tokenFactory);
}

export function createSubmissionState(): TurnSubmissionState {
  return { draft: null, recovery: null };
}

/**
 * Change the identity used by the next submit while retaining an earlier
 * uncertain submission. Editing A to B must never erase A's recovery token.
 */
export function draftForInput(
  state: TurnSubmissionState,
  input: string,
  tokenFactory: () => string,
): TurnSubmissionState {
  if (
    state.draft &&
    canonicalTurnInput(input) !== state.draft.input &&
    state.draft.token.length > 0
  ) {
    return {
      draft: createTurnSubmission(input, tokenFactory),
      recovery: state.recovery ?? state.draft,
    };
  }
  return {
    draft: submissionForInput(state.draft, input, tokenFactory),
    recovery: state.recovery,
  };
}

export function markSubmissionFailed(
  state: TurnSubmissionState,
  submission: TurnSubmission,
): TurnSubmissionState {
  return { draft: submission, recovery: state.recovery ?? submission };
}

export function markSubmissionRecovered(
  state: TurnSubmissionState,
  recovered: TurnSubmission,
): TurnSubmissionState {
  const draftMatches = state.draft?.token === recovered.token;
  const recoveryMatches = state.recovery?.token === recovered.token;
  if (!draftMatches && !recoveryMatches) {
    return state;
  }
  return {
    draft: draftMatches ? null : state.draft,
    recovery: recoveryMatches ? null : state.recovery,
  };
}

export function recoverOriginal(state: TurnSubmissionState): TurnSubmission | null {
  return state.recovery;
}
