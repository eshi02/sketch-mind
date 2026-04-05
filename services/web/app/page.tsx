"use client";
import { useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

interface VideoResult {
  subtopic_title: string;
  video_url: string | null;
  index: number;
  error?: string;
}

export default function Home() {
  const [topic, setTopic] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [videos, setVideos] = useState<VideoResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleGenerate() {
    if (!topic.trim()) return;
    setLoading(true);
    setStatus("Submitting...");
    setVideos(null);
    setError(null);

    try {
      const res = await fetch(`${API_URL}/api/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic }),
      });
      const data = await res.json();

      if (data.status === "cached") {
        setVideos(data.videos);
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
      const state = JSON.parse(event.data);
      setStatus(state.stage);

      if (state.stage === "completed") {
        setVideos(state.videos || []);
        setLoading(false);
        ws.close();
      } else if (state.stage === "failed") {
        setError(state.error || "Generation failed");
        if (state.videos?.length) {
          setVideos(state.videos);
        }
        setLoading(false);
        ws.close();
      }
    };

    ws.onerror = () => {
      setError("WebSocket connection failed");
      setLoading(false);
    };
  }

  const succeededVideos = videos?.filter((v) => v.video_url) || [];
  const failedCount = videos ? videos.length - succeededVideos.length : 0;

  return (
    <main style={{ maxWidth: 900, margin: "0 auto", padding: "4rem 1.5rem", textAlign: "center" }}>
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
            flex: 1, padding: "0.75rem 1rem", borderRadius: 8,
            border: "1px solid #333", background: "#1a1a1a", color: "#ededed",
            fontSize: "1rem",
          }}
        />
        <button
          onClick={handleGenerate}
          disabled={loading}
          style={{
            padding: "0.75rem 1.5rem", borderRadius: 8, border: "none",
            background: loading ? "#333" : "#4f46e5", color: "#fff",
            fontSize: "1rem", cursor: loading ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "Generating..." : "Generate"}
        </button>
      </div>

      {status && (
        <p style={{ color: "#aaa", marginBottom: "1rem" }}>
          Status: <strong>{status}</strong>
        </p>
      )}

      {error && (
        <p style={{ color: "#ef4444" }}>Error: {error}</p>
      )}

      {failedCount > 0 && succeededVideos.length > 0 && (
        <p style={{ color: "#f59e0b", marginBottom: "1rem" }}>
          {succeededVideos.length} of {videos!.length} videos generated successfully.
        </p>
      )}

      {succeededVideos.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: succeededVideos.length === 1 ? "1fr" : "1fr 1fr",
            gap: "1.5rem",
            marginTop: "1.5rem",
            textAlign: "left",
          }}
        >
          {succeededVideos
            .sort((a, b) => a.index - b.index)
            .map((v) => (
              <div
                key={v.index}
                style={{
                  background: "#111",
                  borderRadius: 12,
                  overflow: "hidden",
                  border: "1px solid #222",
                }}
              >
                <video
                  src={v.video_url!}
                  controls
                  style={{ width: "100%", display: "block" }}
                />
                <div style={{ padding: "0.75rem 1rem" }}>
                  <h3 style={{ fontSize: "1rem", margin: 0, marginBottom: "0.25rem" }}>
                    {v.subtopic_title}
                  </h3>
                  <a
                    href={v.video_url!}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ color: "#4f46e5", fontSize: "0.85rem" }}
                  >
                    Open in new tab
                  </a>
                </div>
              </div>
            ))}
        </div>
      )}
    </main>
  );
}
