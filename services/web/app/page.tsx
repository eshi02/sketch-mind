"use client";
import { useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

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

const STAGE_LABELS: Record<string, string> = {
  pending: "Waiting...",
  starting: "Starting...",
  scripting: "Writing script...",
  coding: "Generating animation code...",
  rendering: "Rendering video...",
  fixing: "Fixing code, retrying...",
  completed: "Done!",
  failed: "Failed",
};

export default function Home() {
  const [topic, setTopic] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [subtopics, setSubtopics] = useState<SubtopicState[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleGenerate() {
    if (!topic.trim()) return;
    setLoading(true);
    setStatus("Submitting...");
    setSubtopics([]);
    setError(null);

    try {
      const res = await fetch(`${API_URL}/api/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic }),
      });
      const data = await res.json();

      if (data.status === "cached") {
        setSubtopics(
          (data.videos || []).map((v: any, i: number) => ({
            ...v,
            index: v.subtopic_index ?? v.index ?? i,
            stage: "completed",
          }))
        );
        setStatus("Found cached videos!");
        setLoading(false);
        return;
      }

      if (data.session_id) {
        pollStatus(data.session_id);
      } else {
        setError("Unexpected response");
        setLoading(false);
      }
    } catch (e: any) {
      setError(e.message);
      setLoading(false);
    }
  }

  function pollStatus(sessionId: string) {
    const wsUrl = API_URL.replace(/^http/, "ws") + `/ws/status/${sessionId}`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
      const state: WsState = JSON.parse(event.data);
      setStatus(state.stage);

      // Update subtopics from WebSocket state
      if (state.subtopics?.length) {
        setSubtopics([...state.subtopics]);
      }

      if (state.stage === "completed") {
        setLoading(false);
        ws.close();
      } else if (state.stage === "failed") {
        setError(state.error || "Generation failed");
        setLoading(false);
        ws.close();
      }
    };

    ws.onerror = () => {
      setError("WebSocket connection failed");
      setLoading(false);
    };
  }

  const succeededVideos = subtopics.filter((s: SubtopicState) => s.video_url);
  const failedCount = subtopics.filter((s: SubtopicState) => s.stage === "failed").length;

  return (
    <main
      style={{
        maxWidth: 900,
        margin: "0 auto",
        padding: "4rem 1.5rem",
        textAlign: "center",
      }}
    >
      <h1 style={{ fontSize: "2.5rem", marginBottom: "0.5rem" }}>SketchMind</h1>
      <p style={{ color: "#888", marginBottom: "2rem" }}>
        Enter any topic and get AI-generated animated educational videos.
      </p>

      <div style={{ display: "flex", gap: "0.5rem", marginBottom: "2rem" }}>
        <input
          type="text"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleGenerate()}
          placeholder="e.g. Pythagorean theorem, photosynthesis, supply & demand"
          style={{
            flex: 1,
            padding: "0.75rem 1rem",
            borderRadius: 8,
            border: "1px solid #333",
            background: "#1a1a1a",
            color: "#ededed",
            fontSize: "1rem",
          }}
        />
        <button
          onClick={handleGenerate}
          disabled={loading}
          style={{
            padding: "0.75rem 1.5rem",
            borderRadius: 8,
            border: "none",
            background: loading ? "#333" : "#4f46e5",
            color: "#fff",
            fontSize: "1rem",
            cursor: loading ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "Generating..." : "Generate"}
        </button>
      </div>

      {/* Global status */}
      {status && !subtopics.length && (
        <p style={{ color: "#aaa", marginBottom: "1rem" }}>
          Status: <strong>{STAGE_LABELS[status] || status}</strong>
        </p>
      )}

      {error && !subtopics.length && (
        <p style={{ color: "#ef4444" }}>Error: {error}</p>
      )}

      {failedCount > 0 && succeededVideos.length > 0 && (
        <p style={{ color: "#f59e0b", marginBottom: "1rem" }}>
          {succeededVideos.length} of {subtopics.length} videos generated
          successfully.
        </p>
      )}

      {/* Subtopic cards grid */}
      {subtopics.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: subtopics.length === 1 ? "1fr" : "1fr 1fr",
            gap: "1.5rem",
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
                  background: "#111",
                  borderRadius: 12,
                  overflow: "hidden",
                  border: `1px solid ${
                    s.stage === "completed"
                      ? "#22c55e33"
                      : s.stage === "failed"
                      ? "#ef444433"
                      : "#333"
                  }`,
                }}
              >
                {/* Video player — shown immediately when video_url is available */}
                {s.video_url ? (
                  <video
                    src={s.video_url}
                    controls
                    style={{ width: "100%", display: "block" }}
                  />
                ) : (
                  /* Progress placeholder */
                  <div
                    style={{
                      aspectRatio: "16/9",
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      justifyContent: "center",
                      background: "#0a0a0a",
                      gap: "0.75rem",
                    }}
                  >
                    {s.stage === "failed" ? (
                      <span style={{ color: "#ef4444", fontSize: "0.9rem" }}>
                        {s.error || "Failed to generate"}
                      </span>
                    ) : (
                      <>
                        <Spinner />
                        <span style={{ color: "#aaa", fontSize: "0.85rem" }}>
                          {s.message || STAGE_LABELS[s.stage] || s.stage}
                        </span>
                      </>
                    )}
                  </div>
                )}

                <div style={{ padding: "0.75rem 1rem" }}>
                  <h3
                    style={{
                      fontSize: "1rem",
                      margin: 0,
                      marginBottom: "0.25rem",
                    }}
                  >
                    {s.subtopic_title}
                  </h3>
                  {s.video_url && (
                    <a
                      href={s.video_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: "#4f46e5", fontSize: "0.85rem" }}
                    >
                      Open in new tab
                    </a>
                  )}
                </div>
              </div>
            ))}
        </div>
      )}
    </main>
  );
}

/* Simple CSS spinner */
function Spinner() {
  return (
    <div
      style={{
        width: 28,
        height: 28,
        border: "3px solid #333",
        borderTop: "3px solid #4f46e5",
        borderRadius: "50%",
        animation: "spin 0.8s linear infinite",
      }}
    >
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
