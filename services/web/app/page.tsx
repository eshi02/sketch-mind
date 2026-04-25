"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import { useAuth } from "./auth-context";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SubtopicState {
  subtopic_title: string;
  index: number;
  stage: string;
  message?: string;
  video_url: string | null;
  error?: string | null;
}

interface WsState {
  stage: string;
  subtopics?: SubtopicState[];
  videos?: SubtopicState[];
  error?: string;
}

interface VideoEntry {
  subtopic_title: string;
  video_url: string;
  subtopic_index: number;
  index?: number;
}

interface HistoryEntry {
  id: string;
  topic: string;
  session_id: string | null;
  status: string;
  created_at: string;
  videos: VideoEntry[];
}

interface BgGeneration {
  topic: string;
  status: string;
  subtopics: SubtopicState[];
  error: string | null;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const FREE_LIMIT = 3;

const STAGE_LABELS: Record<string, string> = {
  pending: "Waiting...",
  starting: "Starting...",
  researching: "Researching topic...",
  scripting: "Writing script...",
  coding: "Generating animation code...",
  rendering: "Rendering video...",
  fixing: "Rendering video...",
  generating: "Generating videos...",
  completed: "Done!",
  failed: "Failed",
};

const FUN_FACTS = [
  "Honey never spoils. Archaeologists found 3,000-year-old honey in Egyptian tombs that was still edible.",
  "Octopuses have three hearts and blue blood. Two pump blood to the gills, one to the rest of the body.",
  "A day on Venus is longer than a year on Venus. It takes 243 Earth days to rotate but only 225 to orbit the Sun.",
  "Bananas are berries, but strawberries aren't. Botanically, berries come from a single ovary.",
  "The inventor of the Pringles can is buried in one. Fredric Baur's ashes were placed in a Pringles can.",
  "There are more possible chess games than atoms in the observable universe. The Shannon number is about 10^120.",
  "Cleopatra lived closer in time to the Moon landing than to the construction of the Great Pyramid.",
  'A group of flamingos is called a "flamboyance." They\'re pink because of the shrimp they eat.',
  "Light from the Sun takes about 8 minutes and 20 seconds to reach Earth.",
  "The total weight of all ants on Earth roughly equals the total weight of all humans.",
  'Water can boil and freeze at the same time. It\'s called the "triple point."',
  "An average cumulus cloud weighs about 1.1 million pounds.",
  "The shortest war in history lasted 38 minutes - between Britain and Zanzibar in 1896.",
  "Your brain uses about 20% of your body's total energy, despite being only 2% of your mass.",
  "There are more stars in the universe than grains of sand on all of Earth's beaches.",
];

const PIPELINE_STEPS = [
  { key: "researching", label: "Researching", icon: "1" },
  { key: "scripting", label: "Scripting & Coding", icon: "2" },
  { key: "rendering", label: "Rendering", icon: "3" },
];

const EXAMPLE_TOPICS = [
  "Explain the concept of recursion in programming",
  "How binary search algorithm works step by step",
  "The Pythagorean theorem explained visually",
  "How integration works in calculus",
  "Trigonometry: sine, cosine and unit circle",
  "How sorting algorithms compare: bubble vs quick sort",
  "Explain Big O notation with examples",
];

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

function getPipelineStepIndex(stage: string): number {
  if (["researching", "starting", "pending", "Submitting..."].includes(stage)) return 0;
  if (["scripting", "coding", "fixing"].includes(stage)) return 1;
  if (["rendering", "generating"].includes(stage)) return 2;
  return 0;
}

function getAnonCount(): number {
  if (typeof window === "undefined") return 0;
  return parseInt(localStorage.getItem("sketchmind_anon_count") || "0", 10);
}

function bumpAnonCount(): void {
  localStorage.setItem("sketchmind_anon_count", String(getAnonCount() + 1));
}

// ---------------------------------------------------------------------------
// Shared components
// ---------------------------------------------------------------------------

function Spinner({ size = 28 }: { size?: number }) {
  const dotSize = Math.max(4, size / 5);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: dotSize * 0.8 }}>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          style={{
            width: dotSize,
            height: dotSize,
            borderRadius: "50%",
            background: "#818cf8",
            animation: `dotBounce 1.2s ease-in-out ${i * 0.15}s infinite`,
          }}
        />
      ))}
    </div>
  );
}

function FactCard() {
  const [factIndex, setFactIndex] = useState(() =>
    Math.floor(Math.random() * FUN_FACTS.length),
  );
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const interval = setInterval(() => {
      setVisible(false);
      setTimeout(() => {
        setFactIndex((prev) => (prev + 1) % FUN_FACTS.length);
        setVisible(true);
      }, 400);
    }, 6000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div
      style={{
        background: "rgba(79,70,229,0.08)",
        border: "1px solid rgba(79,70,229,0.15)",
        borderRadius: 14,
        padding: "1.25rem 1.5rem",
        maxWidth: 500,
        margin: "0 auto",
        minHeight: 80,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        transition: "opacity 0.4s ease",
        opacity: visible ? 1 : 0,
      }}
    >
      <div
        style={{
          fontSize: "0.7rem",
          fontWeight: 700,
          color: "#a78bfa",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          marginBottom: "0.5rem",
        }}
      >
        Did you know?
      </div>
      <p style={{ color: "#c4b5fd", fontSize: "0.9rem", lineHeight: 1.5, margin: 0 }}>
        {FUN_FACTS[factIndex]}
      </p>
    </div>
  );
}

function LoadingExperience({ stage }: { stage: string }) {
  const activeStep = getPipelineStepIndex(stage);

  return (
    <div style={{ textAlign: "center", padding: "2rem 0" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: "0.5rem",
          marginBottom: "2rem",
        }}
      >
        {PIPELINE_STEPS.map((step, i) => {
          const isDone = i < activeStep;
          const isActive = i === activeStep;
          return (
            <div key={step.key} style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  padding: "0.5rem 1rem",
                  borderRadius: 10,
                  background: isActive
                    ? "rgba(79,70,229,0.15)"
                    : isDone
                      ? "rgba(34,197,94,0.1)"
                      : "rgba(255,255,255,0.03)",
                  border: `1px solid ${
                    isActive
                      ? "rgba(79,70,229,0.3)"
                      : isDone
                        ? "rgba(34,197,94,0.2)"
                        : "rgba(255,255,255,0.06)"
                  }`,
                  transition: "all 0.4s ease",
                  ...(isActive ? { boxShadow: "0 0 20px rgba(79,70,229,0.15)" } : {}),
                }}
              >
                <div
                  style={{
                    width: 22,
                    height: 22,
                    borderRadius: "50%",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: "0.7rem",
                    fontWeight: 700,
                    background: isDone
                      ? "rgba(34,197,94,0.3)"
                      : isActive
                        ? "rgba(79,70,229,0.3)"
                        : "rgba(255,255,255,0.06)",
                    color: isDone ? "#4ade80" : isActive ? "#a5b4fc" : "#555",
                    transition: "all 0.4s ease",
                  }}
                >
                  {isDone ? "\u2713" : step.icon}
                </div>
                <span
                  style={{
                    fontSize: "0.8rem",
                    fontWeight: isActive ? 600 : 400,
                    color: isDone ? "#4ade80" : isActive ? "#a5b4fc" : "#555",
                    transition: "color 0.4s ease",
                  }}
                >
                  {step.label}
                </span>
                {isActive && (
                  <div
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      background: "#818cf8",
                      animation: "pulse 1.5s ease-in-out infinite",
                    }}
                  />
                )}
              </div>
              {i < PIPELINE_STEPS.length - 1 && (
                <div
                  style={{
                    width: 24,
                    height: 2,
                    background: isDone ? "rgba(34,197,94,0.3)" : "rgba(255,255,255,0.08)",
                    borderRadius: 1,
                    transition: "background 0.4s ease",
                  }}
                />
              )}
            </div>
          );
        })}
      </div>

      <p style={{ color: "#a5b4fc", fontSize: "0.9rem", marginBottom: "1.5rem" }}>
        {STAGE_LABELS[stage] || stage}
      </p>

      <FactCard />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------

export default function Home() {
  const { user, token, loading: authLoading, logout } = useAuth();

  const [topic, setTopic] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [subtopics, setSubtopics] = useState<SubtopicState[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [viewingHistory, setViewingHistory] = useState(false);
  const [anonCount, setAnonCount] = useState(0);

  const viewingHistoryRef = useRef(false);
  const bgGen = useRef<BgGeneration | null>(null);

  useEffect(() => {
    viewingHistoryRef.current = viewingHistory;
  }, [viewingHistory]);

  useEffect(() => {
    if (!user) setAnonCount(getAnonCount());
  }, [user]);

  const anonLimitReached = !user && anonCount >= FREE_LIMIT;

  // --- Data fetching ---

  const fetchHistory = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch(`${API_URL}/api/history`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) setHistory(await res.json());
    } catch (err) {
      console.error("Failed to fetch history:", err);
    }
  }, [token]);

  useEffect(() => {
    if (user) {
      fetchHistory();
    } else {
      setSubtopics([]);
      setStatus(null);
      setError(null);
      setTopic("");
      setHistory([]);
      setShowHistory(false);
      setLoading(false);
      setAnonCount(getAnonCount());
    }
  }, [user, fetchHistory]);

  // --- History actions ---

  async function deleteHistoryEntry(id: string) {
    if (!token) return;
    try {
      const res = await fetch(`${API_URL}/api/history/${id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) setHistory((prev) => prev.filter((h) => h.id !== id));
    } catch (err) {
      console.error("Failed to delete history entry:", err);
    }
  }

  async function clearAllHistory() {
    if (!token) return;
    try {
      const res = await fetch(`${API_URL}/api/history`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) setHistory([]);
    } catch (err) {
      console.error("Failed to clear history:", err);
    }
  }

  // --- Generation pipeline ---

  async function handleGenerate() {
    if (!topic.trim() || anonLimitReached || loading) return;

    if (!user) {
      bumpAnonCount();
      setAnonCount(getAnonCount());
    }

    setViewingHistory(false);
    setLoading(true);
    setStatus("Submitting...");
    setSubtopics([]);
    setError(null);
    bgGen.current = { topic, status: "Submitting...", subtopics: [], error: null };

    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;

      const res = await fetch(`${API_URL}/api/generate`, {
        method: "POST",
        headers,
        body: JSON.stringify({ topic }),
      });
      const data = await res.json();

      if (data.status === "cached") {
        setSubtopics(
          (data.videos || []).map((v: VideoEntry, i: number) => ({
            ...v,
            index: v.subtopic_index ?? v.index ?? i,
            stage: "completed",
          })),
        );
        setStatus("Found cached videos!");
        setLoading(false);
        bgGen.current = null;
        fetchHistory();
        return;
      }

      if (data.session_id) {
        pollStatus(data.session_id);
      } else {
        setError("Unexpected response");
        setLoading(false);
        bgGen.current = null;
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Generation failed");
      setLoading(false);
      bgGen.current = null;
    }
  }

  function pollStatus(sessionId: string) {
    const wsUrl = API_URL.replace(/^http/, "ws") + `/ws/status/${sessionId}`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
      const state: WsState = JSON.parse(event.data);

      if (bgGen.current) {
        bgGen.current.status = state.stage;
        if (state.subtopics?.length) bgGen.current.subtopics = [...state.subtopics];
        if (state.error) bgGen.current.error = state.error;
      }

      if (!viewingHistoryRef.current) {
        setStatus(state.stage);
        if (state.subtopics?.length) setSubtopics([...state.subtopics]);
      }

      if (state.stage === "completed") {
        setLoading(false);
        ws.close();
        fetchHistory();
        if (viewingHistoryRef.current) restoreGeneration();
        bgGen.current = null;
      } else if (state.stage === "failed") {
        if (!viewingHistoryRef.current) setError(state.error || "Generation failed");
        setLoading(false);
        ws.close();
        bgGen.current = null;
        fetchHistory();
      }
    };

    ws.onerror = () => {
      if (!viewingHistoryRef.current) setError("WebSocket connection failed");
      setLoading(false);
      bgGen.current = null;
    };
  }

  // --- Navigation helpers ---

  function restoreGeneration() {
    if (bgGen.current) {
      setTopic(bgGen.current.topic);
      setStatus(bgGen.current.status);
      setSubtopics([...bgGen.current.subtopics]);
      setError(bgGen.current.error);
    }
    setViewingHistory(false);
  }

  function loadFromHistory(entry: HistoryEntry) {
    if (loading && bgGen.current) setViewingHistory(true);
    setTopic(entry.topic);
    if (entry.videos.length > 0) {
      setSubtopics(
        entry.videos.map((v, i) => ({
          subtopic_title: v.subtopic_title,
          index: v.subtopic_index ?? i,
          stage: "completed",
          video_url: v.video_url,
        })),
      );
      setStatus("completed");
      setError(null);
    }
    setShowHistory(false);
  }

  // --- Derived state ---

  const succeededVideos = subtopics.filter((s) => s.video_url);
  const failedCount = subtopics.filter((s) => s.stage === "failed").length;
  const isRendering = loading && subtopics.some((s) => s.stage !== "completed" && s.stage !== "failed");

  // --- Auth loading screen ---

  if (authLoading) {
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#050510",
        }}
      >
        <Spinner size={32} />
      </div>
    );
  }

  // --- Main render ---

  return (
    <div style={{ minHeight: "100vh", background: "linear-gradient(180deg, #050510 0%, #0a0a2e 100%)" }}>
      {/* Background orbs */}
      <div
        style={{
          position: "fixed",
          top: "5%",
          right: "20%",
          width: 500,
          height: 500,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(79,70,229,0.08) 0%, transparent 70%)",
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
          background: "radial-gradient(circle, rgba(168,85,247,0.06) 0%, transparent 70%)",
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
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
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
          <span style={{ fontSize: "1.15rem", fontWeight: 700, letterSpacing: "-0.02em" }}>
            SketchMind
          </span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          {user ? (
            <>
              <a
                href="/paths"
                style={{
                  padding: "0.45rem 0.9rem",
                  borderRadius: 8,
                  border: "1px solid rgba(255,255,255,0.1)",
                  background: "transparent",
                  color: "#ccc",
                  fontSize: "0.8rem",
                  textDecoration: "none",
                  transition: "all 0.2s",
                }}
              >
                Paths
              </a>
              <button
                onClick={() => setShowHistory(!showHistory)}
                style={{
                  padding: "0.45rem 0.9rem",
                  borderRadius: 8,
                  border: "1px solid rgba(255,255,255,0.1)",
                  background: showHistory ? "rgba(79,70,229,0.2)" : "transparent",
                  color: "#ccc",
                  fontSize: "0.8rem",
                  cursor: "pointer",
                  transition: "all 0.2s",
                }}
              >
                History
              </button>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  padding: "0.35rem 0.75rem",
                  borderRadius: 8,
                  background: "rgba(255,255,255,0.05)",
                }}
              >
                {user.avatar_url ? (
                  <img
                    src={user.avatar_url}
                    alt={user.name}
                    style={{ width: 28, height: 28, borderRadius: "50%" }}
                    referrerPolicy="no-referrer"
                  />
                ) : (
                  <div
                    style={{
                      width: 28,
                      height: 28,
                      borderRadius: "50%",
                      background: "linear-gradient(135deg, #4f46e5, #7c3aed)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: "0.75rem",
                      fontWeight: 600,
                    }}
                  >
                    {user.name.charAt(0).toUpperCase()}
                  </div>
                )}
                <span style={{ fontSize: "0.8rem", color: "#ccc" }}>{user.name}</span>
              </div>
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
            </>
          ) : (
            <a
              href="/login"
              style={{
                padding: "0.5rem 1.2rem",
                borderRadius: 10,
                border: "none",
                background: "linear-gradient(135deg, #4f46e5, #7c3aed)",
                color: "#fff",
                fontSize: "0.85rem",
                fontWeight: 600,
                textDecoration: "none",
                boxShadow: "0 2px 12px rgba(79,70,229,0.3)",
              }}
            >
              Sign In
            </a>
          )}
        </div>
      </nav>

      <div style={{ display: "flex", position: "relative" }}>
        {/* History sidebar */}
        {showHistory && user && (
          <aside
            style={{
              width: 320,
              minHeight: "calc(100vh - 60px)",
              borderRight: "1px solid rgba(255,255,255,0.06)",
              background: "rgba(10,10,30,0.6)",
              backdropFilter: "blur(12px)",
              padding: "1.5rem 1rem",
              overflowY: "auto",
              flexShrink: 0,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: "1rem",
              }}
            >
              <h3
                style={{
                  fontSize: "0.9rem",
                  color: "#aaa",
                  margin: 0,
                  fontWeight: 600,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                }}
              >
                History
              </h3>
              {history.length > 0 && (
                <button
                  onClick={clearAllHistory}
                  style={{
                    padding: "0.3rem 0.6rem",
                    borderRadius: 6,
                    border: "1px solid rgba(239,68,68,0.2)",
                    background: "transparent",
                    color: "#f87171",
                    fontSize: "0.65rem",
                    cursor: "pointer",
                    transition: "all 0.2s",
                  }}
                >
                  Clear All
                </button>
              )}
            </div>

            {history.length === 0 ? (
              <p style={{ color: "#666", fontSize: "0.85rem" }}>
                No searches yet. Try generating a video!
              </p>
            ) : (
              history.map((h) => (
                <div key={h.id} style={{ position: "relative", marginBottom: "0.5rem" }}>
                  <button
                    onClick={() => loadFromHistory(h)}
                    style={{
                      display: "block",
                      width: "100%",
                      textAlign: "left",
                      padding: "0.75rem 0.85rem",
                      paddingRight: "2rem",
                      borderRadius: 10,
                      border: "1px solid rgba(255,255,255,0.06)",
                      background: "rgba(255,255,255,0.03)",
                      color: "#ddd",
                      cursor: "pointer",
                      transition: "all 0.2s",
                    }}
                  >
                    <div style={{ fontSize: "0.85rem", fontWeight: 500, marginBottom: 4 }}>
                      {h.topic}
                    </div>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "0.5rem",
                        fontSize: "0.7rem",
                        color: "#888",
                      }}
                    >
                      <span
                        style={{
                          display: "inline-block",
                          width: 6,
                          height: 6,
                          borderRadius: "50%",
                          background:
                            h.status === "completed"
                              ? "#22c55e"
                              : h.status === "failed"
                                ? "#ef4444"
                                : "#f59e0b",
                        }}
                      />
                      <span>
                        {new Date(h.created_at).toLocaleDateString(undefined, {
                          month: "short",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </span>
                    </div>
                    {h.videos.length > 0 && (
                      <div style={{ fontSize: "0.7rem", color: "#666", marginTop: 4 }}>
                        {h.videos.length} video{h.videos.length !== 1 ? "s" : ""}
                      </div>
                    )}
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteHistoryEntry(h.id);
                    }}
                    title="Delete"
                    style={{
                      position: "absolute",
                      top: 8,
                      right: 6,
                      width: 22,
                      height: 22,
                      borderRadius: 6,
                      border: "none",
                      background: "transparent",
                      color: "#666",
                      fontSize: "0.75rem",
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      transition: "all 0.2s",
                    }}
                  >
                    x
                  </button>
                </div>
              ))
            )}
          </aside>
        )}

        {/* Main content */}
        <main style={{ flex: 1, maxWidth: 900, margin: "0 auto", padding: "3rem 1.5rem" }}>
          {/* Hero */}
          <div style={{ textAlign: "center", marginBottom: "2.5rem" }}>
            <h1
              style={{
                fontSize: "2.8rem",
                fontWeight: 800,
                marginBottom: "0.5rem",
                background: "linear-gradient(135deg, #fff 0%, #a5b4fc 50%, #c084fc 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
                letterSpacing: "-0.03em",
              }}
            >
              Learn anything visually
            </h1>
            <p style={{ color: "#888", fontSize: "1.05rem", maxWidth: 500, margin: "0 auto" }}>
              Enter any topic and get AI-generated animated educational videos in minutes.
            </p>
          </div>

          {/* Search input */}
          <div
            style={{
              display: "flex",
              gap: "0.5rem",
              maxWidth: 650,
              margin: "0 auto 2rem",
            }}
          >
            <div style={{ flex: 1, position: "relative" }}>
              <input
                type="text"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleGenerate()}
                placeholder="e.g. Pythagorean theorem, photosynthesis, supply & demand"
                style={{
                  width: "100%",
                  padding: "0.85rem 1.1rem",
                  borderRadius: 14,
                  border: "1px solid rgba(255,255,255,0.1)",
                  background: "rgba(255,255,255,0.05)",
                  backdropFilter: "blur(8px)",
                  color: "#ededed",
                  fontSize: "0.95rem",
                  outline: "none",
                  transition: "border-color 0.2s, box-shadow 0.2s",
                  boxSizing: "border-box",
                }}
              />
            </div>
            {anonLimitReached ? (
              <a
                href="/login"
                style={{
                  padding: "0.85rem 1.75rem",
                  borderRadius: 14,
                  border: "none",
                  background: "linear-gradient(135deg, #4f46e5, #7c3aed)",
                  color: "#fff",
                  fontSize: "0.95rem",
                  fontWeight: 600,
                  textDecoration: "none",
                  boxShadow: "0 4px 20px rgba(79,70,229,0.3)",
                  whiteSpace: "nowrap",
                  display: "flex",
                  alignItems: "center",
                }}
              >
                Sign in to continue
              </a>
            ) : (
              <button
                onClick={handleGenerate}
                disabled={loading}
                style={{
                  padding: "0.85rem 1.75rem",
                  borderRadius: 14,
                  border: "none",
                  background: loading
                    ? "rgba(79,70,229,0.3)"
                    : "linear-gradient(135deg, #4f46e5, #7c3aed)",
                  color: "#fff",
                  fontSize: "0.95rem",
                  fontWeight: 600,
                  cursor: loading ? "not-allowed" : "pointer",
                  transition: "all 0.2s",
                  boxShadow: loading ? "none" : "0 4px 20px rgba(79,70,229,0.3)",
                  whiteSpace: "nowrap",
                }}
              >
                {loading ? "Generating..." : "Generate"}
              </button>
            )}
          </div>

          {/* Example topic suggestions */}
          {!loading && !subtopics.length && (
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                justifyContent: "center",
                gap: "0.5rem",
                maxWidth: 650,
                margin: "0 auto 2rem",
              }}
            >
              {EXAMPLE_TOPICS.map((ex) => (
                <button
                  key={ex}
                  onClick={() => setTopic(ex)}
                  style={{
                    padding: "0.45rem 0.9rem",
                    borderRadius: 20,
                    border: "1px solid rgba(255,255,255,0.08)",
                    background: "rgba(255,255,255,0.04)",
                    color: "#a5b4fc",
                    fontSize: "0.8rem",
                    cursor: "pointer",
                    transition: "all 0.2s",
                    whiteSpace: "nowrap",
                  }}
                >
                  {ex}
                </button>
              ))}
            </div>
          )}

          {/* Background generation banner */}
          {viewingHistory && loading && bgGen.current && (
            <button
              onClick={restoreGeneration}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.75rem",
                width: "100%",
                maxWidth: 650,
                margin: "0 auto 1.5rem",
                padding: "0.75rem 1rem",
                borderRadius: 12,
                border: "1px solid rgba(79,70,229,0.3)",
                background: "rgba(79,70,229,0.1)",
                color: "#a5b4fc",
                fontSize: "0.85rem",
                cursor: "pointer",
                transition: "all 0.2s",
                textAlign: "left",
              }}
            >
              <Spinner size={18} />
              <span style={{ flex: 1 }}>
                Generating <strong>{bgGen.current.topic}</strong> in background...
              </span>
              <span style={{ fontSize: "0.75rem", color: "#818cf8", fontWeight: 600 }}>View</span>
            </button>
          )}

          {/* Loading experience */}
          {loading && !subtopics.length && status && <LoadingExperience stage={status} />}

          {/* Error message */}
          {error && !subtopics.length && (
            <p
              style={{
                color: "#f87171",
                textAlign: "center",
                background: "rgba(248,113,113,0.08)",
                borderRadius: 12,
                padding: "0.75rem",
                border: "1px solid rgba(248,113,113,0.15)",
              }}
            >
              {error}
            </p>
          )}

          {/* Partial success notice */}
          {failedCount > 0 && succeededVideos.length > 0 && (
            <p style={{ color: "#fbbf24", textAlign: "center", marginBottom: "1rem", fontSize: "0.9rem" }}>
              {succeededVideos.length} of {subtopics.length} videos generated successfully.
            </p>
          )}

          {/* Subtopic cards grid */}
          {subtopics.length > 0 && (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: subtopics.length === 1 ? "1fr" : "1fr 1fr",
                gap: "1.25rem",
                marginTop: "1.5rem",
                textAlign: "left",
              }}
            >
              {[...subtopics]
                .sort((a, b) => a.index - b.index)
                .map((s) => (
                  <div
                    key={s.index}
                    style={{
                      background: "rgba(15,15,35,0.6)",
                      backdropFilter: "blur(8px)",
                      borderRadius: 16,
                      overflow: "hidden",
                      border: `1px solid ${
                        s.stage === "completed"
                          ? "rgba(34,197,94,0.2)"
                          : s.stage === "failed"
                            ? "rgba(239,68,68,0.2)"
                            : "rgba(255,255,255,0.06)"
                      }`,
                      transition: "border-color 0.3s",
                    }}
                  >
                    {s.video_url ? (
                      <video src={s.video_url} controls style={{ width: "100%", display: "block" }} />
                    ) : (
                      <div
                        style={{
                          aspectRatio: "16/9",
                          display: "flex",
                          flexDirection: "column",
                          alignItems: "center",
                          justifyContent: "center",
                          background: "rgba(5,5,16,0.5)",
                          gap: "0.75rem",
                        }}
                      >
                        {s.stage === "failed" ? (
                          <span
                            style={{
                              color: "#f87171",
                              fontSize: "0.85rem",
                              padding: "0 1rem",
                              textAlign: "center",
                            }}
                          >
                            {s.error || "Failed to generate"}
                          </span>
                        ) : (
                          <>
                            <Spinner size={28} />
                            <span style={{ color: "#a5b4fc", fontSize: "0.8rem" }}>
                              {s.message || STAGE_LABELS[s.stage] || s.stage}
                            </span>
                          </>
                        )}
                      </div>
                    )}

                    <div style={{ padding: "0.75rem 1rem" }}>
                      <h3 style={{ fontSize: "0.95rem", margin: 0, marginBottom: "0.25rem", fontWeight: 600 }}>
                        {s.subtopic_title}
                      </h3>
                      {s.video_url && (
                        <a
                          href={s.video_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          style={{ color: "#818cf8", fontSize: "0.8rem", textDecoration: "none" }}
                        >
                          Open in new tab
                        </a>
                      )}
                    </div>
                  </div>
                ))}
            </div>
          )}

          {/* Fun facts while rendering */}
          {isRendering && subtopics.length > 0 && (
            <div style={{ marginTop: "1.5rem" }}>
              <FactCard />
            </div>
          )}

          {/* Sign-in prompt for anonymous users */}
          {!user && !subtopics.length && !loading && (
            <div
              style={{
                textAlign: "center",
                marginTop: "3rem",
                padding: "2rem",
                borderRadius: 16,
                border: "1px dashed rgba(255,255,255,0.08)",
                background: "rgba(255,255,255,0.02)",
              }}
            >
              <p style={{ color: "#888", fontSize: "0.9rem", margin: 0 }}>
                {anonLimitReached ? (
                  <>
                    You&apos;ve used all {FREE_LIMIT} free generations.{" "}
                    <a href="/login" style={{ color: "#818cf8", textDecoration: "none", fontWeight: 600 }}>
                      Sign in
                    </a>{" "}
                    for unlimited access.
                  </>
                ) : (
                  <>
                    <a href="/login" style={{ color: "#818cf8", textDecoration: "none", fontWeight: 600 }}>
                      Sign in
                    </a>{" "}
                    to save your search history and get unlimited generations.
                    <span style={{ display: "block", marginTop: "0.5rem", fontSize: "0.8rem", color: "#666" }}>
                      {FREE_LIMIT - anonCount} free generation{FREE_LIMIT - anonCount !== 1 ? "s" : ""} remaining
                    </span>
                  </>
                )}
              </p>
            </div>
          )}
        </main>
      </div>

      <style>{`
        @keyframes dotBounce {
          0%, 80%, 100% { opacity: 0.3; transform: scale(0.6); }
          40% { opacity: 1; transform: scale(1); }
        }
        @keyframes pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.4; transform: scale(1.5); }
        }
        input:focus {
          border-color: rgba(79,70,229,0.5) !important;
          box-shadow: 0 0 0 3px rgba(79,70,229,0.1) !important;
        }
        button:hover:not(:disabled) { filter: brightness(1.1); }
        aside button:hover {
          background: rgba(79,70,229,0.12) !important;
          border-color: rgba(79,70,229,0.2) !important;
        }
      `}</style>
    </div>
  );
}
