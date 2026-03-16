"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/contexts/AuthContext";

export default function LoginPage() {
  const router = useRouter();
  const { login, isLoggedIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  if (isLoggedIn) {
    router.replace("/");
    return null;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(email, password);
      router.replace("/");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-bis-blue-dark">
      <div className="w-full max-w-md">
        <div className="rounded-2xl bg-bis-blue/80 backdrop-blur-md border border-bis-blue/60 shadow-xl p-8">
          <div className="text-center mb-6">
            <h1 className="text-2xl font-bold text-white">BIS Assistant</h1>
            <p className="text-gray-400 text-sm mt-1">Sign in to continue</p>
          </div>
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <div className="px-3 py-2 rounded-lg bg-bis-red/20 border border-bis-red/40 text-bis-red-light text-sm">
                {error}
              </div>
            )}
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-gray-300 mb-1">
                Email
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoComplete="email"
                className="w-full px-4 py-2.5 rounded-xl bg-bis-blue-dark/80 border border-bis-blue/60 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-bis-red/50"
                placeholder="you@example.com"
              />
            </div>
            <div>
              <label htmlFor="password" className="block text-sm font-medium text-gray-300 mb-1">
                Password
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
                className="w-full px-4 py-2.5 rounded-xl bg-bis-blue-dark/80 border border-bis-blue/60 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-bis-red/50"
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="w-full py-3 rounded-xl bg-bis-red hover:bg-bis-red-light disabled:opacity-50 text-white font-medium transition-colors"
            >
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
          <p className="mt-4 text-center text-sm text-gray-400">
            Don&apos;t have an account?{" "}
            <Link href="/register" className="text-bis-red-light hover:underline">
              Register
            </Link>
          </p>
        </div>
        <p className="mt-4 text-center">
          <Link href="/" className="text-gray-500 hover:text-gray-400 text-sm">
            ← Continue without login
          </Link>
        </p>
      </div>
    </div>
  );
}
