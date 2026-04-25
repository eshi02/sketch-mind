"use client";
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

interface PathTopic {
  topic: string;
  session_id: string | null;
  completed: boolean;
}

interface LearningPath {
  id: string;
  title: string;
  topics: PathTopic[];
  current_index: number;
}

export default function PathsListPage() {
  const router = useRouter();
  const { user, token, loading: authLoading, logout } = useAuth();
  const [paths, setPaths] = useState<LearningPath[]>([]);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPaths = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch(`${API_URL}/api/paths`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) setPaths(await res.json());
    } catch {
      /* ignore */
    }
  }, [token]);

  useEffect(() => {
    if (!authLoading && !user) {
      router.push("/login");
      return;
    }
    if (user) fetchPaths();
  }, [user, authLoading, router, fetchPaths]);

  async function handleCreate() {
    if (!token) return;
    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      setError("Add a path title.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/paths`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ title: trimmedTitle, topics: [] }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to create path");
      }
      const created = await res.json();
      router.push(`/paths/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create path");
      setSubmitting(false);
    }
  }

  async function handleDelete(id: string) {
    if (!token) return;
    if (!confirm("Delete this learning path?")) return;
    try {
      const res = await fetch(`${API_URL}/api/paths/${id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) setPaths((prev) => prev.filter((p) => p.id !== id));
    } catch {
      /* ignore */
    }
  }

  if (authLoading || !user) {
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#050510",
          color: "#888",
        }}
      >
        Loading...
      </div>
    );
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "linear-gradient(180deg, #050510 0%, #0a0a2e 100%)",
        color: "#ededed",
      }}
    >
      {/* Background orbs */}
      <div
        style={{
          position: "fixed",
          top: "5%",
          right: "20%",
          width: 500,
          height: 500,
          borderRadius: "50%",
          background:
            "radial-gradient(circle, rgba(79,70,229,0.08) 0%, transparent 70%)",
          filter: "blur(80px)",
          pointerEvents: "none",
        }}
      />
      <div
        style={{
          position: "fixed",
          bottom: "10%",
          left: "10%",
          width: 400,
          height: 400,
          borderRadius: "50%",
          background:
            "radial-gradient(circle, rgba(168,85,247,0.06) 0%, transparent 70%)",
          filter: "blur(60px)",
          pointerEvents: "none",
        }}
      />

      {/* Nav bar */}
      <nav
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "1rem 2rem",
          borderBottom: "1px solid rgba(255,255,255,0.06)",
          backdropFilter: "blur(12px)",
          position: "sticky",
          top: 0,
          zIndex: 100,
          background: "rgba(5,5,16,0.8)",
        }}
      >
        <a
          href="/"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
            textDecoration: "none",
            color: "inherit",
          }}
        >
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: 10,
              background: "linear-gradient(135deg, #4f46e5, #a855f7)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: "0.9rem",
              fontWeight: 700,
            }}
          >
            S
          </div>
          <span
            style={{
              fontSize: "1.15rem",
              fontWeight: 700,
              letterSpacing: "-0.02em",
            }}
          >
            SketchMind
          </span>
        </a>

        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <a
            href="/"
            style={{
              padding: "0.45rem 0.9rem",
              borderRadius: 8,
              border: "1px solid rgba(255,255,255,0.1)",
              color: "#ccc",
              fontSize: "0.8rem",
              textDecoration: "none",
              transition: "all 0.2s",
            }}
          >
            Home
          </a>
          <button
            onClick={logout}
            style={{
              padding: "0.4rem 0.75rem",
              borderRadius: 8,
              border: "1px solid rgba(255,255,255,0.08)",
              background: "transparent",
              color: "#888",
              fontSize: "0.75rem",
              cursor: "pointer",
            }}
          >
            Logout
          </button>
        </div>
      </nav>

      <main
        style={{
          maxWidth: 900,
          margin: "0 auto",
          padding: "3rem 1.5rem",
          position: "relative",
        }}
      >
        <div style={{ marginBottom: "2.5rem" }}>
          <h1
            style={{
              fontSize: "2.4rem",
              fontWeight: 800,
              marginBottom: "0.5rem",
              background:
                "linear-gradient(135deg, #fff 0%, #a5b4fc 50%, #c084fc 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              letterSpacing: "-0.03em",
            }}
          >
            Learning Paths
          </h1>
          <p style={{ color: "#888", fontSize: "1rem", margin: 0 }}>
            Build a syllabus and progress through it one video at a time.
          </p>
        </div>

        {/* Create form */}
        <div
          style={{
            background: "rgba(15,15,35,0.6)",
            backdropFilter: "blur(8px)",
            border: "1px solid rgba(255,255,255,0.06)",
            borderRadius: 16,
            padding: "1.5rem",
            marginBottom: "2.5rem",
          }}
        >
          {!creating ? (
            <button
              onClick={() => setCreating(true)}
              style={{
                width: "100%",
                padding: "1rem",
                borderRadius: 12,
                border: "1px dashed rgba(79,70,229,0.4)",
                background: "rgba(79,70,229,0.05)",
                color: "#a5b4fc",
                fontSize: "0.95rem",
                fontWeight: 600,
                cursor: "pointer",
                transition: "all 0.2s",
              }}
            >
              + Create New Learning Path
            </button>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
              <div>
                <label
                  style={{
                    display: "block",
                    color: "#a5b4fc",
                    fontSize: "0.75rem",
                    fontWeight: 600,
                    textTransform: "uppercase",
                    letterSpacing: "0.05em",
                    marginBottom: "0.4rem",
                  }}
                >
                  Path title
                </label>
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !submitting) handleCreate();
                  }}
                  placeholder="e.g. Calculus Fundamentals, Data Structures"
                  autoFocus
                  style={{
                    width: "100%",
                    padding: "0.75rem 1rem",
                    borderRadius: 10,
                    border: "1px solid rgba(255,255,255,0.1)",
                    background: "rgba(255,255,255,0.05)",
                    color: "#ededed",
                    fontSize: "0.95rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
                <p
                  style={{
                    color: "#666",
                    fontSize: "0.75rem",
                    margin: "0.5rem 0 0",
                  }}
                >
                  ✨ AI will design the syllabus from your title. You can add
                  more topics later.
                </p>
              </div>
              {error && (
                <p style={{ color: "#f87171", fontSize: "0.85rem", margin: 0 }}>
                  {error}
                </p>
              )}
              <div style={{ display: "flex", gap: "0.75rem" }}>
                <button
                  onClick={handleCreate}
                  disabled={submitting}
                  style={{
                    flex: 1,
                    padding: "0.75rem",
                    borderRadius: 10,
                    border: "none",
                    background: submitting
                      ? "rgba(79,70,229,0.3)"
                      : "linear-gradient(135deg, #4f46e5, #7c3aed)",
                    color: "#fff",
                    fontSize: "0.9rem",
                    fontWeight: 600,
                    cursor: submitting ? "not-allowed" : "pointer",
                    boxShadow: submitting
                      ? "none"
                      : "0 4px 20px rgba(79,70,229,0.3)",
                  }}
                >
                  {submitting ? "AI is designing your path..." : "✨ Build Path with AI"}
                </button>
                <button
                  onClick={() => {
                    setCreating(false);
                    setTitle("");
                    setError(null);
                  }}
                  style={{
                    padding: "0.75rem 1.25rem",
                    borderRadius: 10,
                    border: "1px solid rgba(255,255,255,0.1)",
                    background: "transparent",
                    color: "#888",
                    fontSize: "0.9rem",
                    cursor: "pointer",
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Paths list */}
        {paths.length === 0 ? (
          <div
            style={{
              textAlign: "center",
              padding: "3rem 2rem",
              borderRadius: 16,
              border: "1px dashed rgba(255,255,255,0.08)",
              color: "#666",
            }}
          >
            <p style={{ margin: 0, fontSize: "0.95rem" }}>
              No learning paths yet. Create one above to get started.
            </p>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
            {paths.map((p) => {
              const total = p.topics.length;
              const done = p.topics.filter((t) => t.completed).length;
              const pct = total === 0 ? 0 : Math.round((done / total) * 100);
              return (
                <div
                  key={p.id}
                  style={{
                    background: "rgba(15,15,35,0.6)",
                    backdropFilter: "blur(8px)",
                    border: "1px solid rgba(255,255,255,0.06)",
                    borderRadius: 14,
                    padding: "1.25rem",
                    transition: "all 0.2s",
                    cursor: "pointer",
                  }}
                  onClick={() => router.push(`/paths/${p.id}`)}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "flex-start",
                      marginBottom: "0.75rem",
                    }}
                  >
                    <div>
                      <h3
                        style={{
                          margin: 0,
                          fontSize: "1.1rem",
                          fontWeight: 700,
                          marginBottom: "0.3rem",
                        }}
                      >
                        {p.title}
                      </h3>
                      <div style={{ fontSize: "0.75rem", color: "#666" }}>
                        {total} topic{total !== 1 ? "s" : ""}{" "}
                        &middot; {done}/{total} completed
                      </div>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDelete(p.id);
                      }}
                      style={{
                        padding: "0.3rem 0.6rem",
                        borderRadius: 6,
                        border: "1px solid rgba(239,68,68,0.2)",
                        background: "transparent",
                        color: "#f87171",
                        fontSize: "0.7rem",
                        cursor: "pointer",
                      }}
                    >
                      Delete
                    </button>
                  </div>
                  {/* Progress bar */}
                  <div
                    style={{
                      height: 6,
                      borderRadius: 3,
                      background: "rgba(255,255,255,0.05)",
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        height: "100%",
                        width: `${pct}%`,
                        background: "linear-gradient(90deg, #4f46e5, #a855f7)",
                        transition: "width 0.4s ease",
                      }}
                    />
                  </div>
                  <div
                    style={{
                      marginTop: "0.5rem",
                      fontSize: "0.7rem",
                      color: "#a5b4fc",
                      fontWeight: 600,
                    }}
                  >
                    {pct}% complete
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>

      <style>{`
        input:focus, textarea:focus {
          border-color: rgba(79,70,229,0.5) !important;
          box-shadow: 0 0 0 3px rgba(79,70,229,0.1) !important;
        }
        button:hover:not(:disabled) { filter: brightness(1.1); }
      `}</style>
    </div>
  );
}
