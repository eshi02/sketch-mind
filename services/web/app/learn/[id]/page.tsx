"use client";
import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

interface Video {
  id: string;
  topic: string;
  video_url: string;
  status: string;
  created_at: string;
}

export default function LearnPage({ params }: { params: { id: string } }) {
  const [video, setVideo] = useState<Video | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API_URL}/api/videos`)
      .then((res) => res.json())
      .then((videos: Video[]) => {
        const found = videos.find((v) => v.id === params.id);
        setVideo(found || null);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [params.id]);

  if (loading) return <p style={{ textAlign: "center", padding: "4rem" }}>Loading...</p>;
  if (!video) return <p style={{ textAlign: "center", padding: "4rem" }}>Video not found.</p>;

  return (
    <main style={{ maxWidth: 800, margin: "0 auto", padding: "3rem 1.5rem" }}>
      <a href="/" style={{ color: "#4f46e5", textDecoration: "none" }}>&larr; Back</a>
      <h1 style={{ fontSize: "1.8rem", margin: "1rem 0 0.5rem" }}>{video.topic}</h1>
      <video
        src={video.video_url}
        controls
        autoPlay
        style={{ width: "100%", borderRadius: 12, background: "#000" }}
      />
    </main>
  );
}
