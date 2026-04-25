import type { Metadata } from "next";
import { AuthProvider } from "./auth-context";

export const metadata: Metadata = {
  title: "SketchMind - AI Educational Videos",
  description: "Generate animated educational videos with AI",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body style={{ margin: 0, fontFamily: "'Inter', system-ui, -apple-system, sans-serif", background: "#050510", color: "#ededed" }}>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
