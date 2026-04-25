"use client";
import { useEffect, useState, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../../auth-context";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

interface VideoEntry {
  subtopic_title: string;
  video_url: string;
  subtopic_index: number;
}

interface PathTopic {
  topic: string;
  session_id: string | null;
  completed: boolean;
  videos: VideoEntry[];
}

interface LearningPath {
  id: string;
  title: string;
  topics: PathTopic[];
  current_index: number;
}

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
  error?: string;
}

const STAGE_LABELS: Record<string, string> = {
  pending: "Waiting...",
  starting: "Starting...",
  researching: "Researching topic...",
  scripting: "Writing script...",
  coding: "Generating animation code...",
  rendering: "Rendering video...",
  fixing: "Rendering video...",
  generating: "Generating videos...",
};

function Spinner({ size = 20 }: { size?: number }) {
  const dotSize = Math.max(4, size / 5);
  return (
    <div
      style={{ display: "inline-flex", alignItems: "center", gap: dotSize * 0.8 }}
    >
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

export default function PathDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const router = useRouter();
  const { user, token, loading: authLoading, logout } = useAuth();
  const [path, setPath] = useState<LearningPath | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [liveSubtopics, setLiveSubtopics] = useState<SubtopicState[]>([]);
  const [liveStage, setLiveStage] = useState<string | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveTopicIndex, setLiveTopicIndex] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const [addingTopic, setAddingTopic] = useState(false);
  const [newTopicText, setNewTopicText] = useState("");
  const [addTopicError, setAddTopicError] = useState<string | null>(null);
  const [appending, setAppending] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const fetchPath = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch(`${API_URL}/api/paths/${params.id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 404) {
        router.push("/paths");
        return;
      }
      if (res.ok) setPath(await res.json());
    } finally {
      setLoading(false);
    }
  }, [token, params.id, router]);

  useEffect(() => {
    if (!authLoading && !user) {
      router.push("/login");
      return;
    }
    if (user) fetchPath();
  }, [user, authLoading, router, fetchPath]);

  useEffect(() => {
    return () => {
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  function pollStatus(sessionId: string) {
    if (wsRef.current) wsRef.current.close();
    const wsUrl = API_URL.replace(/^http/, "ws") + `/ws/status/${sessionId}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      const state: WsState = JSON.parse(event.data);
      // "unknown" = in-memory session gone (server restart or pre-fetch
      // long since finished). Videos may be in DB — refetch and stop polling.
      if (state.stage === "unknown") {
        ws.close();
        wsRef.current = null;
        setLiveStage(null);
        setLiveTopicIndex(null);
        fetchPath();
        return;
      }
      setLiveStage(state.stage);
      if (state.subtopics?.length) setLiveSubtopics([...state.subtopics]);
      if (state.error) setLiveError(state.error);
      if (state.stage === "completed" || state.stage === "failed") {
        ws.close();
        wsRef.current = null;
        fetchPath();
      }
    };
    ws.onerror = () => setLiveError("Connection failed");
  }

  async function handleStart(index: number) {
    if (!token || !path) return;

    // Pre-fetched + completed: videos already in DB, just open the card.
    const topic = path.topics[index];
    if (topic?.videos && topic.videos.length > 0) {
      setActiveIndex(index);
      setLiveStage(null);
      setLiveSubtopics([]);
      setLiveTopicIndex(null);
      setLiveError(null);
      return;
    }

    setStarting(true);
    setLiveError(null);
    setLiveSubtopics([]);
    setLiveStage("starting");
    setLiveTopicIndex(index);
    setActiveIndex(index);
    try {
      const res = await fetch(
        `${API_URL}/api/paths/${path.id}/start/${index}`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to start");
      }
      const data = await res.json();
      if (data.status === "cached") {
        setLiveStage("completed");
        await fetchPath();
      } else if (data.session_id) {
        pollStatus(data.session_id);
      }
    } catch (err) {
      setLiveError(err instanceof Error ? err.message : "Failed to start");
      setLiveStage("failed");
    } finally {
      setStarting(false);
    }
  }

  async function handleAddTopic() {
    if (!token || !path) return;
    const topic = newTopicText.trim();
    if (!topic) {
      setAddTopicError("Type a topic name.");
      return;
    }
    setAppending(true);
    setAddTopicError(null);
    try {
      const res = await fetch(`${API_URL}/api/paths/${path.id}/topics`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ topic }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to add topic");
      }
      await fetchPath();
      setNewTopicText("");
      setAddingTopic(false);
    } catch (err) {
      setAddTopicError(
        err instanceof Error ? err.message : "Failed to add topic",
      );
    } finally {
      setAppending(false);
    }
  }

  async function handleComplete(index: number) {
    if (!token || !path) return;
    try {
      const res = await fetch(
        `${API_URL}/api/paths/${path.id}/complete/${index}`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (res.ok) {
        await fetchPath();
        setActiveIndex(null);
        setLiveStage(null);
        setLiveSubtopics([]);
        setLiveTopicIndex(null);
      }
    } catch {
      /* ignore */
    }
  }

  if (authLoading || !user || loading) {
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

  if (!path) {
    return (
      <div
        style={{
          minHeight: "100vh",
          background: "#050510",
          color: "#888",
          padding: "3rem",
        }}
      >
        Path not found.
      </div>
    );
  }

  const total = path.topics.length;
  const doneCount = path.topics.filter((t) => t.completed).length;
  const pct = total === 0 ? 0 : Math.round((doneCount / total) * 100);
  const activeTopic = activeIndex !== null ? path.topics[activeIndex] : null;
  const liveVideos = liveSubtopics.filter((s) => s.video_url);

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
          <span style={{ fontSize: "1.15rem", fontWeight: 700 }}>SketchMind</span>
        </a>

        <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
          <a
            href="/paths"
            style={{
              padding: "0.45rem 0.9rem",
              borderRadius: 8,
              border: "1px solid rgba(255,255,255,0.1)",
              color: "#ccc",
              fontSize: "0.8rem",
              textDecoration: "none",
            }}
          >
            All Paths
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
          maxWidth: 880,
          margin: "0 auto",
          padding: "3rem 1.5rem",
          position: "relative",
        }}
      >
        {/* Header */}
        <div style={{ marginBottom: "2rem" }}>
          <a
            href="/paths"
            style={{
              color: "#a5b4fc",
              fontSize: "0.85rem",
              textDecoration: "none",
            }}
          >
            &larr; Back to paths
          </a>
          <h1
            style={{
              fontSize: "2.2rem",
              fontWeight: 800,
              margin: "0.75rem 0 0.4rem",
              background:
                "linear-gradient(135deg, #fff 0%, #a5b4fc 50%, #c084fc 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              letterSpacing: "-0.03em",
            }}
          >
            {path.title}
          </h1>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.75rem",
              color: "#888",
              fontSize: "0.85rem",
            }}
          >
            <span>
              {doneCount}/{total} completed
            </span>
            <div
              style={{
                flex: 1,
                maxWidth: 260,
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
            <span style={{ color: "#a5b4fc", fontWeight: 600 }}>{pct}%</span>
          </div>
        </div>

        {/* Roadmap */}
        <div style={{ position: "relative", paddingLeft: 36 }}>
          {/* Connecting vertical line */}
          <div
            style={{
              position: "absolute",
              left: 15,
              top: 16,
              bottom: 16,
              width: 2,
              background:
                "linear-gradient(180deg, rgba(79,70,229,0.4) 0%, rgba(255,255,255,0.05) 100%)",
            }}
          />
          {path.topics.map((t, i) => {
            const isCompleted = t.completed;
            const isCurrent = i === path.current_index && !isCompleted;
            const isLocked = i > path.current_index;
            const isActiveCard = activeIndex === i;
            const cardBorder = isCompleted
              ? "rgba(34,197,94,0.3)"
              : isCurrent
                ? "rgba(79,70,229,0.4)"
                : "rgba(255,255,255,0.06)";

            return (
              <div
                key={i}
                style={{ position: "relative", marginBottom: "1rem" }}
              >
                {/* Status circle */}
                <div
                  style={{
                    position: "absolute",
                    left: -36,
                    top: 16,
                    width: 32,
                    height: 32,
                    borderRadius: "50%",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: "0.85rem",
                    fontWeight: 700,
                    background: isCompleted
                      ? "rgba(34,197,94,0.2)"
                      : isCurrent
                        ? "linear-gradient(135deg, #4f46e5, #7c3aed)"
                        : "rgba(255,255,255,0.04)",
                    border: `2px solid ${
                      isCompleted
                        ? "rgba(34,197,94,0.5)"
                        : isCurrent
                          ? "rgba(167,139,250,0.6)"
                          : "rgba(255,255,255,0.1)"
                    }`,
                    color: isCompleted
                      ? "#4ade80"
                      : isCurrent
                        ? "#fff"
                        : "#666",
                    boxShadow: isCurrent
                      ? "0 0 20px rgba(79,70,229,0.4)"
                      : "none",
                    zIndex: 1,
                  }}
                >
                  {isCompleted ? "✓" : isLocked ? "🔒" : i + 1}
                </div>

                {/* Card */}
                <div
                  style={{
                    background: isActiveCard
                      ? "rgba(79,70,229,0.08)"
                      : "rgba(15,15,35,0.6)",
                    backdropFilter: "blur(8px)",
                    border: `1px solid ${cardBorder}`,
                    borderRadius: 14,
                    padding: "1.1rem 1.25rem",
                    transition: "all 0.3s",
                    opacity: isLocked ? 0.55 : 1,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: "1rem",
                      flexWrap: "wrap",
                    }}
                  >
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div
                        style={{
                          fontSize: "0.7rem",
                          fontWeight: 700,
                          color: isCompleted
                            ? "#4ade80"
                            : isCurrent
                              ? "#a5b4fc"
                              : "#666",
                          textTransform: "uppercase",
                          letterSpacing: "0.08em",
                          marginBottom: "0.3rem",
                        }}
                      >
                        {isCompleted
                          ? "Completed"
                          : isCurrent
                            ? "Up next"
                            : "Locked"}
                      </div>
                      <h3
                        style={{
                          margin: 0,
                          fontSize: "1.05rem",
                          fontWeight: 600,
                          color: isLocked ? "#666" : "#ededed",
                        }}
                      >
                        {t.topic}
                      </h3>
                    </div>

                    {/* Action button */}
                    {isCompleted ? (
                      <button
                        onClick={() =>
                          setActiveIndex(activeIndex === i ? null : i)
                        }
                        style={{
                          padding: "0.55rem 1rem",
                          borderRadius: 10,
                          border: "1px solid rgba(34,197,94,0.3)",
                          background: "rgba(34,197,94,0.1)",
                          color: "#4ade80",
                          fontSize: "0.8rem",
                          fontWeight: 600,
                          cursor: "pointer",
                        }}
                      >
                        {activeIndex === i ? "Hide videos" : "Review"}
                      </button>
                    ) : isCurrent ? (
                      <button
                        onClick={() => handleStart(i)}
                        disabled={starting && activeIndex === i}
                        style={{
                          padding: "0.55rem 1.25rem",
                          borderRadius: 10,
                          border: "none",
                          background:
                            "linear-gradient(135deg, #4f46e5, #7c3aed)",
                          color: "#fff",
                          fontSize: "0.85rem",
                          fontWeight: 600,
                          cursor:
                            starting && activeIndex === i
                              ? "not-allowed"
                              : "pointer",
                          boxShadow: "0 4px 14px rgba(79,70,229,0.35)",
                        }}
                      >
                        {starting && activeIndex === i
                          ? "Starting..."
                          : t.session_id
                            ? "Continue"
                            : "Start"}
                      </button>
                    ) : (
                      <span
                        style={{
                          fontSize: "0.75rem",
                          color: "#666",
                          fontStyle: "italic",
                        }}
                      >
                        Complete previous topic to unlock
                      </span>
                    )}
                  </div>

                  {/* Active topic content (live generation or completed videos) */}
                  {isActiveCard && (
                    <div style={{ marginTop: "1.25rem" }}>
                      {/* Live spinner: only on the actively-generating card
                          when no DB videos exist yet. */}
                      {liveTopicIndex === i &&
                        !isCompleted &&
                        liveStage &&
                        liveStage !== "completed" &&
                        liveStage !== "failed" &&
                        liveStage !== "unknown" &&
                        liveVideos.length === 0 &&
                        (!activeTopic?.videos ||
                          activeTopic.videos.length === 0) && (
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: "0.75rem",
                              padding: "0.85rem 1rem",
                              borderRadius: 10,
                              background: "rgba(79,70,229,0.08)",
                              border: "1px solid rgba(79,70,229,0.2)",
                              color: "#a5b4fc",
                              fontSize: "0.85rem",
                            }}
                          >
                            <Spinner size={20} />
                            <span>{STAGE_LABELS[liveStage] || liveStage}</span>
                          </div>
                        )}

                      {liveTopicIndex === i && liveError && (
                        <p
                          style={{
                            color: "#f87171",
                            fontSize: "0.85rem",
                            background: "rgba(248,113,113,0.08)",
                            border: "1px solid rgba(248,113,113,0.15)",
                            borderRadius: 10,
                            padding: "0.75rem",
                            margin: 0,
                          }}
                        >
                          {liveError}
                        </p>
                      )}

                      {/* Prefer DB-backed videos; fall back to live stream. */}
                      {(() => {
                        const videosToShow =
                          activeTopic?.videos && activeTopic.videos.length > 0
                            ? activeTopic.videos.map((v) => ({
                                title: v.subtopic_title,
                                url: v.video_url,
                              }))
                            : liveTopicIndex === i
                              ? liveVideos.map((v) => ({
                                  title: v.subtopic_title,
                                  url: v.video_url || "",
                                }))
                              : [];
                        if (videosToShow.length === 0) return null;
                        return (
                          <div
                            style={{
                              display: "grid",
                              gridTemplateColumns:
                                videosToShow.length === 1
                                  ? "1fr"
                                  : "repeat(auto-fit, minmax(260px, 1fr))",
                              gap: "1rem",
                              marginTop: "1rem",
                            }}
                          >
                            {videosToShow.map((v, idx) => (
                              <div
                                key={idx}
                                style={{
                                  background: "rgba(5,5,16,0.6)",
                                  borderRadius: 12,
                                  overflow: "hidden",
                                  border: "1px solid rgba(255,255,255,0.06)",
                                }}
                              >
                                <video
                                  src={v.url}
                                  controls
                                  style={{ width: "100%", display: "block" }}
                                />
                                <div
                                  style={{
                                    padding: "0.6rem 0.85rem",
                                    fontSize: "0.85rem",
                                    fontWeight: 500,
                                  }}
                                >
                                  {v.title}
                                </div>
                              </div>
                            ))}
                          </div>
                        );
                      })()}

                      {/* Mark-complete: only when this topic has a video. */}
                      {!isCompleted &&
                        ((activeTopic?.videos && activeTopic.videos.length > 0) ||
                          (liveTopicIndex === i && liveStage === "completed")) && (
                          <button
                            onClick={() => handleComplete(i)}
                            style={{
                              marginTop: "1rem",
                              padding: "0.65rem 1.25rem",
                              borderRadius: 10,
                              border: "none",
                              background:
                                "linear-gradient(135deg, #16a34a, #22c55e)",
                              color: "#fff",
                              fontSize: "0.85rem",
                              fontWeight: 600,
                              cursor: "pointer",
                              boxShadow: "0 4px 14px rgba(34,197,94,0.3)",
                            }}
                          >
                            ✓ Mark as Complete &amp; Unlock Next
                          </button>
                        )}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* Add topic */}
        {path.topics.length < 20 && (
          <div style={{ marginTop: "1.25rem", paddingLeft: 36 }}>
            {!addingTopic ? (
              <button
                onClick={() => setAddingTopic(true)}
                style={{
                  width: "100%",
                  padding: "0.85rem 1rem",
                  borderRadius: 12,
                  border: "1px dashed rgba(79,70,229,0.4)",
                  background: "rgba(79,70,229,0.04)",
                  color: "#a5b4fc",
                  fontSize: "0.85rem",
                  fontWeight: 600,
                  cursor: "pointer",
                  transition: "all 0.2s",
                }}
              >
                + Add another topic
              </button>
            ) : (
              <div
                style={{
                  background: "rgba(15,15,35,0.6)",
                  border: "1px solid rgba(79,70,229,0.3)",
                  borderRadius: 12,
                  padding: "1rem",
                }}
              >
                <input
                  type="text"
                  value={newTopicText}
                  onChange={(e) => setNewTopicText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !appending) handleAddTopic();
                    if (e.key === "Escape") {
                      setAddingTopic(false);
                      setNewTopicText("");
                      setAddTopicError(null);
                    }
                  }}
                  autoFocus
                  placeholder="e.g. Hash tables and collisions"
                  style={{
                    width: "100%",
                    padding: "0.65rem 0.9rem",
                    borderRadius: 10,
                    border: "1px solid rgba(255,255,255,0.1)",
                    background: "rgba(255,255,255,0.05)",
                    color: "#ededed",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                    marginBottom: "0.6rem",
                  }}
                />
                {addTopicError && (
                  <p
                    style={{
                      color: "#f87171",
                      fontSize: "0.8rem",
                      margin: "0 0 0.6rem",
                    }}
                  >
                    {addTopicError}
                  </p>
                )}
                <div style={{ display: "flex", gap: "0.5rem" }}>
                  <button
                    onClick={handleAddTopic}
                    disabled={appending}
                    style={{
                      flex: 1,
                      padding: "0.55rem",
                      borderRadius: 8,
                      border: "none",
                      background: appending
                        ? "rgba(79,70,229,0.3)"
                        : "linear-gradient(135deg, #4f46e5, #7c3aed)",
                      color: "#fff",
                      fontSize: "0.85rem",
                      fontWeight: 600,
                      cursor: appending ? "not-allowed" : "pointer",
                    }}
                  >
                    {appending ? "Adding..." : "Add topic"}
                  </button>
                  <button
                    onClick={() => {
                      setAddingTopic(false);
                      setNewTopicText("");
                      setAddTopicError(null);
                    }}
                    style={{
                      padding: "0.55rem 0.9rem",
                      borderRadius: 8,
                      border: "1px solid rgba(255,255,255,0.1)",
                      background: "transparent",
                      color: "#888",
                      fontSize: "0.85rem",
                      cursor: "pointer",
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Done banner */}
        {doneCount === total && total > 0 && (
          <div
            style={{
              marginTop: "2rem",
              padding: "1.5rem",
              borderRadius: 16,
              background:
                "linear-gradient(135deg, rgba(34,197,94,0.15) 0%, rgba(79,70,229,0.15) 100%)",
              border: "1px solid rgba(34,197,94,0.3)",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: "2rem", marginBottom: "0.5rem" }}>🎓</div>
            <h3
              style={{
                margin: "0 0 0.5rem",
                color: "#4ade80",
                fontSize: "1.1rem",
              }}
            >
              Path complete!
            </h3>
            <p style={{ color: "#a5b4fc", fontSize: "0.9rem", margin: 0 }}>
              You&apos;ve finished all topics in {path.title}.
            </p>
          </div>
        )}
      </main>

      <style>{`
        @keyframes dotBounce {
          0%, 80%, 100% { opacity: 0.3; transform: scale(0.6); }
          40% { opacity: 1; transform: scale(1); }
        }
        button:hover:not(:disabled) { filter: brightness(1.1); }
      `}</style>
    </div>
  );
}
