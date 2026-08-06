import { router } from "expo-router";

/** Go back, or go home if there is no back. */
export function goBack(): void {
  if (router.canGoBack()) {
    router.back();
    return;
  }
  router.replace("/");
}
