import { router } from "expo-router";

/** Go back, or go home when there is no back, so a close button is never inert. */
export function goBack(): void {
  if (router.canGoBack()) {
    router.back();
    return;
  }
  router.replace("/");
}
