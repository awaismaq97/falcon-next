"use client";

// Last-resort boundary: catches errors thrown in the root layout itself. It
// replaces the whole document, so it renders its own <html>/<body> and uses
// inline styles (the stylesheet may not have loaded if the layout failed).

import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "12px",
          padding: "32px",
          textAlign: "center",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          background: "#0b0b0d",
          color: "#f5f5f5",
        }}
      >
        <div style={{ fontSize: "2rem" }}>🦅</div>
        <div style={{ fontWeight: 600, color: "#ef4444" }}>Falcon failed to load</div>
        <div style={{ maxWidth: "28rem", fontSize: "0.875rem", color: "#a1a1aa" }}>
          {error.message || "A fatal error occurred while starting the app."}
        </div>
        <button
          onClick={reset}
          style={{
            marginTop: "8px",
            borderRadius: "8px",
            border: "1px solid #3a3a42",
            background: "transparent",
            color: "#f5f5f5",
            padding: "8px 16px",
            fontSize: "0.85rem",
            cursor: "pointer",
          }}
        >
          Reload
        </button>
      </body>
    </html>
  );
}
