"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, toApiError, type AuthInput } from "@/lib/api/client";
import { useFrontendSession } from "@/components/providers/session-provider";

/**
 * Async discipline for every workspace-scoped resource.
 *
 * A response is only applied when its (sessionId, resourceKey, request
 * generation) triple still matches the live one. That covers workspace
 * switches, selector switches, and A → B → A sequences where an older
 * request for the same resource would otherwise overwrite newer state.
 *
 * An API failure is kept as an error — it is never flattened into an
 * empty list, so pages can distinguish "failed" from "genuinely empty".
 */
export type WorkspaceQuery<T> = {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  /** True only after a successful load for the current session + key. */
  loaded: boolean;
  reload: () => void;
};

export function useWorkspaceData<T>(
  load: (auth: AuthInput) => Promise<T>,
  /** Resource identity: changing it invalidates in-flight responses. */
  resourceKey: string,
  options?: { enabled?: boolean },
): WorkspaceQuery<T> {
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const enabled = options?.enabled ?? true;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  const loadRef = useRef(load);
  loadRef.current = load;

  // Live identity of the request the component still cares about.
  const activeRef = useRef({ sessionId, resourceKey, generation: 0 });

  useEffect(() => {
    activeRef.current = { sessionId, resourceKey, generation: activeRef.current.generation + 1 };
    setData(null);
    setError(null);
    setLoaded(false);
    setLoading(false);
  }, [sessionId, resourceKey]);

  useEffect(() => {
    if (!connected || !enabled || !resourceKey) return;
    const generation = (activeRef.current.generation += 1);
    const requestSessionId = sessionId;
    const requestKey = resourceKey;
    const isCurrent = () =>
      activeRef.current.sessionId === requestSessionId &&
      activeRef.current.resourceKey === requestKey &&
      activeRef.current.generation === generation;

    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const result = await loadRef.current({ workspaceId, accessToken });
        if (!isCurrent()) return;
        setData(result);
        setLoaded(true);
      } catch (caught) {
        if (!isCurrent()) return;
        setError(toApiError(caught, ""));
        setData(null);
        setLoaded(false);
      } finally {
        if (isCurrent()) setLoading(false);
      }
    })();
  }, [connected, enabled, resourceKey, sessionId, workspaceId, accessToken, reloadToken]);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  return { data, error, loading, loaded, reload };
}

/**
 * Mutation guard with the same identity rules. The callback's result is
 * only applied while the session and resource are still the live ones,
 * so a slow mutation cannot write back after a workspace switch.
 */
export function useWorkspaceMutation(resourceKey: string) {
  const { workspaceId, accessToken, sessionId } = useFrontendSession();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const activeRef = useRef({ sessionId, resourceKey, generation: 0 });
  activeRef.current.sessionId = sessionId;
  activeRef.current.resourceKey = resourceKey;

  useEffect(() => {
    activeRef.current = { sessionId, resourceKey, generation: activeRef.current.generation + 1 };
    setPending(false);
    setError(null);
  }, [sessionId, resourceKey]);

  const run = useCallback(
    async <T,>(
      mutate: (auth: AuthInput) => Promise<T>,
      onSuccess?: (result: T) => void,
    ): Promise<T | null> => {
      const generation = (activeRef.current.generation += 1);
      const requestSessionId = sessionId;
      const requestKey = resourceKey;
      const isCurrent = () =>
        activeRef.current.sessionId === requestSessionId &&
        activeRef.current.resourceKey === requestKey &&
        activeRef.current.generation === generation;

      setPending(true);
      setError(null);
      try {
        const result = await mutate({ workspaceId, accessToken });
        if (!isCurrent()) return null;
        onSuccess?.(result);
        return result;
      } catch (caught) {
        if (isCurrent()) setError(toApiError(caught, ""));
        return null;
      } finally {
        if (isCurrent()) setPending(false);
      }
    },
    [workspaceId, accessToken, sessionId, resourceKey],
  );

  return { run, pending, error, clearError: useCallback(() => setError(null), []) };
}
