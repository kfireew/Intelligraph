import { useState, useEffect, useCallback } from "react";

export function useAuth() {
  const [authenticated, setAuthenticated] = useState(null);
  const [user, setUser] = useState(null);
  const [ssoConfigured, setSsoConfigured] = useState(false);

  const checkAuth = useCallback(async () => {
    try {
      const r = await fetch("/auth/me");
      const d = await r.json();
      setAuthenticated(d.authenticated);
      setUser(d.user || null);
      setSsoConfigured(d.sso_configured);
    } catch {
      setAuthenticated(false);
    }
  }, []);

  useEffect(() => { checkAuth(); }, [checkAuth]);

  const login = () => { window.location.href = "/auth/login"; };
  const logout = () => { window.location.href = "/auth/logout"; };

  return { authenticated, user, ssoConfigured, checkAuth, login, logout };
}