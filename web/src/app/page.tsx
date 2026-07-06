"use client";

// The root route no longer hosts a launcher — connection setup now lives inside the
// chat page (the connection gate's full-screen fallback when nothing is reachable,
// and the "Connection settings…" dialog on the composer's connection switcher). This
// route just forwards into `/chat`, where the gate takes over.

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function RootPage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/chat");
  }, [router]);
  return null;
}
