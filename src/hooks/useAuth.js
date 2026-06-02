import { useState, useEffect, useCallback } from "react";

export function useAuth() {
  const [authenticated, setAuthenticated] = useState(null);
  const [user, setUser] = useState(null);
  const [oidcConfigured, setOidcConfigured] = useState(false);

  const checkAuth = useCallback(async () => {
    try {
      const r = await fetch("/auth/me");
      const d = await r.json();
      setAuthenticated(d.authenticated);
      setUser(d.user || null);
      setOidcConfigured(d.oidc_configured);
    } catch {
      setAuthenticated(false);
    }
  }, []);

  useEffect(() => { checkAuth(); }, [checkAuth]);

  const login = () => { window.location.href = "/auth/login"; };
  const logout = () => { window.location.href = "/auth/logout"; };

  return { authenticated, user, oidcConfigured, checkAuth, login, logout };
}