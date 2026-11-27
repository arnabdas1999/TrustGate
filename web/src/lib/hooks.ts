"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api } from "./api";

/** Calls `load` now and every `intervalMs` until unmounted. Keeps the last good value. */
export function usePolling<T>(load: () => Promise<T>, intervalMs = 1000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);

  useEffect(() => {
    loadRef.current = load;
  }, [load]);

  const refresh = useCallback(async () => {
    try {
      const value = await loadRef.current();
      setData(value);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (!cancelled) await refresh();
    };
    void tick();
    const timer = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [refresh, intervalMs]);

  return { data, error, refresh };
}

const ACTIVE_WINDOW_MS = 20_000; // an input within the last 20 s counts as active
const FLUSH_EVERY_MS = 5_000;

/**
 * Human-attention logger for the benchmark: time counts only while the page is visible
 * and the person has scrolled, clicked or typed within the last 20 seconds.
 */
export function useActiveTime(investigationId: string | undefined) {
  useEffect(() => {
    if (!investigationId) return;
    let lastInput = Date.now();
    let lastTick = Date.now();
    let pendingMs = 0;

    const onInput = () => {
      lastInput = Date.now();
    };
    const events = ["mousemove", "mousedown", "keydown", "scroll", "touchstart"] as const;
    events.forEach((e) => window.addEventListener(e, onInput, { passive: true }));

    const tick = () => {
      const now = Date.now();
      const visible = document.visibilityState === "visible";
      if (visible && now - lastInput <= ACTIVE_WINDOW_MS) pendingMs += now - lastTick;
      lastTick = now;
      if (pendingMs > 0) {
        const ms = pendingMs;
        pendingMs = 0;
        api.activity(investigationId, ms, visible).catch(() => {
          pendingMs += ms; // retry on the next tick
        });
      }
    };
    const timer = setInterval(tick, FLUSH_EVERY_MS);
    return () => {
      clearInterval(timer);
      tick();
      events.forEach((e) => window.removeEventListener(e, onInput));
    };
  }, [investigationId]);
}

const TOKEN_KEY = "trustgate.approver";
const sessionListeners = new Set<() => void>();

function subscribeSession(callback: () => void) {
  sessionListeners.add(callback);
  window.addEventListener("storage", callback);
  return () => {
    sessionListeners.delete(callback);
    window.removeEventListener("storage", callback);
  };
}

function notifySession() {
  sessionListeners.forEach((callback) => callback());
}

type ApproverSession = { token: string; approver: string };

function parseSession(raw: string | null): ApproverSession | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as ApproverSession;
  } catch {
    return null;
  }
}

/** Approver token kept in sessionStorage (cleared when the tab closes). */
export function useApproverSession() {
  const raw = useSyncExternalStore(
    subscribeSession,
    () => sessionStorage.getItem(TOKEN_KEY),
    () => null,
  );
  const session = useMemo(() => parseSession(raw), [raw]);

  const login = useCallback(async (password: string) => {
    const response = await api.login(password);
    const value: ApproverSession = { token: response.token, approver: response.approver };
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify(value));
    notifySession();
  }, []);

  const logout = useCallback(() => {
    sessionStorage.removeItem(TOKEN_KEY);
    notifySession();
  }, []);

  return { session, login, logout };
}

function subscribeReducedMotion(onChange: () => void) {
  const query = window.matchMedia("(prefers-reduced-motion: reduce)");
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/** True when the visitor asked for reduced motion (false during server rendering). */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    () => false,
  );
}

function subscribeClock(onChange: () => void) {
  const timer = setInterval(onChange, 1000);
  return () => clearInterval(timer);
}

/** The current time, ticking once a second, for countdowns. */
export function useNow(): number {
  return useSyncExternalStore(subscribeClock, () => Math.floor(Date.now() / 1000) * 1000, () => 0);
}

/** True when the API serves fixture data (MOCK=1): every number on screen is demo data then. */
export function useDemoMode(): boolean {
  const { data } = usePolling(api.health, 30000);
  return data?.mock === true;
}
