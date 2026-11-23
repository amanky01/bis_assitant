"use client";

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { getCurrentUser, logout as apiLogout, type AuthUser } from "@/lib/api";

type AuthState = {
  email: string | null;
  userId: string | null;
  isLoggedIn: boolean;
  isLoading: boolean;
};

type AuthContextValue = AuthState & {
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  setAuthUser: (data: AuthUser) => void;
};

const defaultState: AuthState = {
  email: null,
  userId: null,
  isLoggedIn: false,
  isLoading: true,
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>(defaultState);

  const setAuthUser = useCallback((data: AuthUser) => {
    setState({
      email: data.email,
      userId: data.user_id,
      isLoggedIn: true,
      isLoading: false,
    });
  }, []);

  useEffect(() => {
    localStorage.removeItem("bis_token");
    localStorage.removeItem("bis_auth_user");
    let active = true;
    getCurrentUser().then((user) => {
      if (!active) return;
      if (user) setAuthUser(user);
      else setState({ ...defaultState, isLoading: false });
    }).catch(() => {
      if (active) setState({ ...defaultState, isLoading: false });
    });
    return () => { active = false; };
  }, [setAuthUser]);

  const login = useCallback(
    async (email: string, password: string) => {
      const { login: apiLogin } = await import("@/lib/api");
      const data = await apiLogin(email, password);
      setAuthUser(data);
    },
    [setAuthUser]
  );

  const register = useCallback(
    async (email: string, password: string) => {
      const { register: apiRegister } = await import("@/lib/api");
      const data = await apiRegister(email, password);
      setAuthUser(data);
    },
    [setAuthUser]
  );

  const logout = useCallback(async () => {
    await apiLogout();
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
    setAuthUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
