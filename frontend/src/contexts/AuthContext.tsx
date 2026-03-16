"use client";

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import {
  clearToken,
  isLoggedIn,
  setToken as persistToken,
  type TokenResponse,
} from "@/lib/api";

type AuthState = {
  email: string | null;
  userId: string | null;
  isLoggedIn: boolean;
  isLoading: boolean;
};

type AuthContextValue = AuthState & {
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => void;
  setAuthFromToken: (data: TokenResponse) => void;
};

const defaultState: AuthState = {
  email: null,
  userId: null,
  isLoggedIn: false,
  isLoading: true,
};

const AuthContext = createContext<AuthContextValue | null>(null);

const AUTH_STORAGE_KEY = "bis_auth_user";

function getStoredUser(): { email: string; userId: string } | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as { email: string; userId: string };
  } catch {
    return null;
  }
}

function setStoredUser(data: { email: string; userId: string } | null) {
  if (typeof window === "undefined") return;
  if (data) localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(data));
  else localStorage.removeItem(AUTH_STORAGE_KEY);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>(defaultState);

  const setAuthFromToken = useCallback((data: TokenResponse) => {
    persistToken(data.access_token);
    const user = { email: data.email, userId: data.user_id };
    setStoredUser(user);
    setState({
      email: data.email,
      userId: data.user_id,
      isLoggedIn: true,
      isLoading: false,
    });
  }, []);

  useEffect(() => {
    const loggedIn = isLoggedIn();
    const user = getStoredUser();
    setState((s) => ({
      ...s,
      isLoggedIn: loggedIn && !!user,
      email: user?.email ?? null,
      userId: user?.userId ?? null,
      isLoading: false,
    }));
  }, []);

  const login = useCallback(
    async (email: string, password: string) => {
      const { login: apiLogin } = await import("@/lib/api");
      const data = await apiLogin(email, password);
      setAuthFromToken(data);
    },
    [setAuthFromToken]
  );

  const register = useCallback(
    async (email: string, password: string) => {
      const { register: apiRegister } = await import("@/lib/api");
      const data = await apiRegister(email, password);
      setAuthFromToken(data);
    },
    [setAuthFromToken]
  );

  const logout = useCallback(() => {
    clearToken();
    setStoredUser(null);
    setState({
      email: null,
      userId: null,
      isLoggedIn: false,
      isLoading: false,
    });
  }, []);

  const value: AuthContextValue = {
    ...state,
    login,
    register,
    logout,
    setAuthFromToken,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
