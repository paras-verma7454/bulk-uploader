/// <reference types="react" />
import React, { FormEvent, useState } from "react";
import { AuthMode, AuthResponse } from "../types/index";
import { readJson } from "../utils/helpers";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

interface AuthProps {
  onAuthSuccess: (auth: AuthResponse) => void;
  isBusy: boolean;
  setIsBusy: (busy: boolean) => void;
  message: string;
  setMessage: (msg: string) => void;
}

export const Auth: React.FC<AuthProps> = ({ 
  onAuthSuccess, 
  isBusy, 
  setIsBusy, 
  message, 
  setMessage 
}) => {
  const [authMode, setAuthMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  async function handleAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsBusy(true);
    setMessage("");

    try {
      const auth = await readJson<AuthResponse>(
        await fetch(`${API_BASE}/api/v1/auth/${authMode}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password })
        })
      );
      onAuthSuccess(auth);
      setPassword("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Authentication failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <div className="auth-shell" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
      <section className="auth-card" style={{ padding: "2rem", maxWidth: "360px", width: "100%" }}>
        <h2 style={{ marginBottom: "1.5rem", textAlign: "center" }}>{authMode === "login" ? "Login" : "Sign Up"}</h2>
        <form onSubmit={handleAuth} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            Email
            <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" required style={{ padding: "0.5rem", border: "1px solid #ccc", borderRadius: "4px" }} />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            Password
            <input
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              type="password"
              minLength={8}
              required
              style={{ padding: "0.5rem", border: "1px solid #ccc", borderRadius: "4px" }}
            />
          </label>
          <button className="primary-button" disabled={isBusy} type="submit" style={{ padding: "0.75rem", marginTop: "0.5rem" }}>
            {isBusy ? "Working..." : authMode === "login" ? "Login" : "Create account"}
          </button>
        </form>
        <p style={{ textAlign: "center", marginTop: "1rem", fontSize: "0.875rem" }}>
          {authMode === "login" ? "Don't have an account?" : "Already have an account?"}{" "}
          <button type="button" onClick={() => setAuthMode(authMode === "login" ? "signup" : "login")} style={{ background: "none", border: "none", color: "blue", cursor: "pointer", textDecoration: "underline" }}>
            {authMode === "login" ? "Sign up" : "Login"}
          </button>
        </p>
        {message && <p className="form-message" style={{ color: "red", textAlign: "center", marginTop: "1rem" }}>{message}</p>}
      </section>
    </div>
  );
};
