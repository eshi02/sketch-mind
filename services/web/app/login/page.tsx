"use client";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "../auth-context";

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: Record<string, unknown>) => void;
          renderButton: (element: HTMLElement, config: Record<string, unknown>) => void;
        };
      };
    };
  }
}

export default function LoginPage() {
  const { loginWithGoogle, googleClientId, user } = useAuth();
  const buttonRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Redirect if already logged in
  useEffect(() => {
    if (user) window.location.href = "/";
  }, [user]);

  useEffect(() => {
    if (!googleClientId) return;

    // Load the Google Identity Services script
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.onload = () => {
      window.google?.accounts.id.initialize({
        client_id: googleClientId,
        callback: handleGoogleResponse,
      });
      if (buttonRef.current) {
        window.google?.accounts.id.renderButton(buttonRef.current, {
          theme: "filled_black",
          size: "large",
          shape: "pill",
          width: 360,
          text: "signin_with",
        });
      }
    };
    document.head.appendChild(script);
    return () => {
      document.head.removeChild(script);
    };
  }, [googleClientId]);

  async function handleGoogleResponse(response: { credential: string }) {
    setError(null);
    setLoading(true);
    try {
      await loginWithGoogle(response.credential);
      window.location.href = "/";
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "linear-gradient(135deg, #050510 0%, #0a0a2e 50%, #1a0a2e 100%)",
        padding: "1rem",
      }}
    >
      {/* Decorative background orbs */}
      <div
        style={{
          position: "fixed",
          top: "10%",
          left: "15%",
          width: 300,
          height: 300,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(79,70,229,0.15) 0%, transparent 70%)",
          filter: "blur(60px)",
          pointerEvents: "none",
        }}
      />
      <div
        style={{
          position: "fixed",
          bottom: "15%",
          right: "10%",
          width: 400,
          height: 400,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(168,85,247,0.12) 0%, transparent 70%)",
          filter: "blur(80px)",
          pointerEvents: "none",
        }}
      />

      <div
        style={{
          width: "100%",
          maxWidth: 420,
          background: "rgba(15,15,35,0.8)",
          backdropFilter: "blur(20px)",
          borderRadius: 20,
          border: "1px solid rgba(255,255,255,0.08)",
          padding: "2.5rem 2rem",
          boxShadow: "0 25px 60px rgba(0,0,0,0.5)",
        }}
      >
        {/* Logo */}
        <div style={{ textAlign: "center", marginBottom: "2rem" }}>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "0.5rem",
              marginBottom: "0.75rem",
            }}
          >
            <div
              style={{
                width: 44,
                height: 44,
                borderRadius: 14,
                background: "linear-gradient(135deg, #4f46e5, #a855f7)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: "1.4rem",
                fontWeight: 700,
              }}
            >
              S
            </div>
            <span style={{ fontSize: "1.6rem", fontWeight: 700, letterSpacing: "-0.02em" }}>
              SketchMind
            </span>
          </div>
          <p style={{ color: "#888", fontSize: "0.95rem", margin: 0, lineHeight: 1.5 }}>
            Sign in to save your search history
            <br />
            and revisit past videos.
          </p>
        </div>

        {/* Divider */}
        <div
          style={{
            height: 1,
            background: "rgba(255,255,255,0.06)",
            margin: "1.5rem 0",
          }}
        />

        {/* Google Sign-In button */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: "1rem",
          }}
        >
          {loading && (
            <p style={{ color: "#a5b4fc", fontSize: "0.85rem", margin: 0 }}>
              Signing you in...
            </p>
          )}

          {/* Google renders its button here */}
          <div ref={buttonRef} />

          {!googleClientId && (
            <p style={{ color: "#f59e0b", fontSize: "0.8rem", textAlign: "center", margin: 0 }}>
              Google Client ID not configured.
              <br />
              Set NEXT_PUBLIC_GOOGLE_CLIENT_ID in your environment.
            </p>
          )}
        </div>

        {error && (
          <p
            style={{
              color: "#f87171",
              fontSize: "0.85rem",
              background: "rgba(248,113,113,0.1)",
              borderRadius: 8,
              padding: "0.5rem 0.75rem",
              marginTop: "1rem",
              textAlign: "center",
            }}
          >
            {error}
          </p>
        )}

        {/* Back link */}
        <div style={{ textAlign: "center", marginTop: "1.5rem" }}>
          <a
            href="/"
            style={{
              color: "#818cf8",
              fontSize: "0.85rem",
              textDecoration: "none",
            }}
          >
            Back to home
          </a>
        </div>
      </div>
    </div>
  );
}
