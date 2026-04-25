"use client";
import { createContext, useContext, useState, useEffect, useCallback, ReactNode } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL;
const GOOGLE_CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || "";

interface User {
  id: string;
  name: string;
  email: string;
  avatar_url?: string;
}

interface AuthContextType {
  user: User | null;
  token: string | null;
  loading: boolean;
  loginWithGoogle: (googleIdToken: string) => Promise<void>;
  logout: () => void;
  googleClientId: string;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const saved = localStorage.getItem("sketchmind_token");
    if (saved) {
      setToken(saved);
      fetch(`${API_URL}/api/auth/me`, {
        headers: { Authorization: `Bearer ${saved}` },
      })
        .then((r) => {
          if (!r.ok) throw new Error();
          return r.json();
        })
        .then((u) => setUser(u))
        .catch(() => {
          localStorage.removeItem("sketchmind_token");
          setToken(null);
        })
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, []);

  const loginWithGoogle = useCallback(async (googleIdToken: string) => {
    const res = await fetch(`${API_URL}/api/auth/google`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id_token: googleIdToken }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Google login failed");
    }
    const data = await res.json();
    localStorage.setItem("sketchmind_token", data.token);
    setToken(data.token);
    setUser(data.user);
  }, []);

  function logout() {
    localStorage.removeItem("sketchmind_token");
    setToken(null);
    setUser(null);
  }

  return (
    <AuthContext.Provider
      value={{ user, token, loading, loginWithGoogle, logout, googleClientId: GOOGLE_CLIENT_ID }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
